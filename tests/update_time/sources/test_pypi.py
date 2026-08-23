"""Unit tests for the PyPI module."""

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from update_time.domain.bound import NO_BOUND, Verb
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import Release, Yank
from update_time.sources import github, pypi
from update_time.sources.pypi import (
    _changelog_from_url,
    get_changes,
    get_latest_version,
    get_publication_datetime,
    newest_release,
)

from tests.helpers import mock_response, patch_get
from tests.mutation import Mutation, kills
from tests.update_time.helpers import (
    PYPI_OLD_UPLOAD,
    CacheClearingTestCase,
    LoggingTestCase,
    bound,
    github_release_json,
    pypi_index,
    pypi_release,
    yanked_file,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

# The mutations of how a null the PyPI metadata reports for the project URLs is read. The tests of the updater that
# rewrites the pins kill them too, so they are named here rather than spelled out in each registration.
NULL_PROJECT_URLS_READ_AS_A_DICT = Mutation(
    pypi,
    'urls = info.get("project_urls") or {}',
    'urls = info.get("project_urls", {})',
    "the project URLs PyPI reports as null are read as a dictionary, which ends the run with a traceback",
    raises="AttributeError: 'NoneType' object has no attribute 'items'",
)
A_RELEASE_WITHOUT_PROJECT_URLS_SKIPPED = Mutation(
    pypi,
    "    if metadata is None:\n        return None",
    '    if metadata is None or metadata["info"].get("project_urls", {}) is None:\n        return None',
    "a release whose project URLs PyPI reports as null is skipped rather than adopted",
)


@patch("requests.get")
class GetChangesTest(LoggingTestCase):
    """Unit tests for getting the changes."""

    def create_mock_response(
        self,
        mock_get: Mock,
        *json: dict | list,
        text: str = "",
        status_code: int = HTTPStatus.OK,
        content_type: str | None = "text/text",
    ) -> None:
        """Point the mock requests.get at one response whose successive `.json()` calls return the given payloads.

        The changelog heuristics make several requests off one release (the PyPI metadata, then a changelog URL or
        GitHub releases); the shared response returns the next JSON payload on each `.json()` and the same text and
        status for all of them.
        """
        ok = status_code < HTTPStatus.BAD_REQUEST
        headers = {} if content_type is None else {"Content-Type": content_type}
        response = mock_response(text=text, status_code=status_code, ok=ok, headers=headers)
        response.json.side_effect = list(json)
        mock_get.return_value = response

    def create_description_responses(self, mock_get: Mock, description: str, body: str | None = None) -> None:
        """Point the mock requests.get at the PyPI metadata carrying that description, and at one GitHub release.

        The release stands for what a repository linked in the description answers for version 1.1, so its body
        is the changelog such a link supplies, or the null GitHub reports for a release published without notes.
        """
        self.create_mock_response(
            mock_get, {"info": {"description": description}}, [github_release_json("1.1", body=body)]
        )

    def create_mock_response_per_url(self, mock_get: Mock, metadata: dict, unreachable_url: str) -> None:
        """Point the mock requests.get at the release metadata, answering the unreachable URL with an HTTP error."""
        unreachable = mock_response(status_code=HTTPStatus.NOT_FOUND, ok=False)
        reachable = mock_response(metadata, status_code=HTTPStatus.OK, ok=True)
        mock_get.side_effect = lambda url, **_kwargs: unreachable if url == unreachable_url else reachable

    @staticmethod
    def metadata_url(package: str, version: str = "1.1") -> str:
        """Return the URL PyPI serves the release metadata of the package and version at."""
        return f"https://pypi.org/pypi/{package}/{version}/json"

    @staticmethod
    def releases_url(repository: str) -> str:
        """Return the URL GitHub serves the repository's releases at."""
        return f"https://api.github.com/repos/{repository}/releases?per_page=100"

    @staticmethod
    def contents_url(repository: str) -> str:
        """Return the URL GitHub serves the repository's root listing at."""
        return f"https://api.github.com/repos/{repository}/contents/"

    @staticmethod
    def file_url(name: str) -> str:
        """Return the URL the repository serves the file of that name at."""
        return f"https://raw/{name}"

    @classmethod
    def root_entry(cls, name: str, text: str | None) -> dict[str, object]:
        """Return the root listing entry for the file, one with no file to fetch when its text is None."""
        return {
            "name": name,
            "type": "dir" if text is None else "file",
            "download_url": None if text is None else cls.file_url(name),
        }

    @staticmethod
    def requested_urls(mock_get: Mock) -> list[str]:
        """Return the URLs the mock requests.get was asked for, in the order they were asked for."""
        return [call.args[0] for call in mock_get.call_args_list]

    def assert_releases_requested(self, mock_get: Mock, *repositories: str) -> None:
        """Assert that GitHub was asked for the releases of exactly these repositories, in this order."""
        requested = [url for url in self.requested_urls(mock_get) if "/releases" in url]
        expected = [self.releases_url(repository) for repository in repositories]
        self.assertEqual(requested, expected)

    def create_discovery_responses(
        self,
        mock_get: Mock,
        *packages: str,
        files: Mapping[str, str | None] | None = None,
        repository: str,
        description: str = "Package description",
        extra: Mapping[str | None, Mock] | None = None,
    ) -> None:
        """Point the mock requests.get at the responses for packages whose metadata names one GitHub repository.

        The repository's root lists an entry per name in `files`, each answering its text, and the entry of a name
        mapped to None is one with no file to fetch, as a directory is. The repository publishes no releases.
        `extra` adds a response, or replaces one, under the URL it is keyed by. A URL neither holds raises a
        KeyError, so a request the test did not expect fails loudly rather than being answered.
        """
        info = {"description": description, "project_urls": {"Source": f"https://github.com/{repository}"}}
        listing = [self.root_entry(name, text) for name, text in (files or {}).items()]
        responses: dict[str | None, Mock] = {
            **{self.metadata_url(package): mock_response({"info": info}) for package in packages},
            self.releases_url(repository): mock_response([]),
            self.contents_url(repository): mock_response(listing),
            **{self.file_url(name): mock_response(text=text) for name, text in (files or {}).items() if text},
            **(extra or {}),
        }
        mock_get.side_effect = lambda url, **_kwargs: responses[url]

    def assert_root_listed(self, mock_get: Mock, *repositories: str) -> None:
        """Assert that GitHub was asked for the root listing of exactly these repositories, in this order."""
        requested = [url for url in self.requested_urls(mock_get) if url.endswith("/contents/")]
        expected = [self.contents_url(repository) for repository in repositories]
        self.assertEqual(requested, expected)

    def test_no_url_found(self, mock_get: Mock):
        """Test that the changes are empty if no changelog URL is returned by PyPI."""
        self.create_mock_response(mock_get, {"info": {"description": "Package-foo description"}})
        self.assertEqual(get_changes("pycparser", "1.0"), "")

    def test_changelog_url_found(self, mock_get: Mock):
        """Test that the changes are returned if PyPI returns a changelog URL, under any label read as one."""
        changelog = "Changelog\n## 1.1\n- Fixed foo\n"
        for key in ("changelog", "changes", "whatsnew", "history", "What's New"):
            with self.subTest(key=key):
                self.create_mock_response(
                    mock_get,
                    {"info": {"description": "Package-foo description", "project_urls": {key: "https://changes"}}},
                    text=changelog,
                )
                self.assertEqual(get_changes(f"package-2-{key}", "1.1"), "## 1.1\n- Fixed foo")

    @kills(
        Mutation(
            pypi,
            'changelog_response.headers.get("Content-Type", "")',
            'changelog_response.headers["Content-Type"]',
            "a changelog URL answered without a content type ends the run with a traceback",
            raises="KeyError: 'Content-Type'",
        ),
    )
    def test_changelog_url_without_content_type(self, mock_get: Mock):
        """Test that the changes are returned when the changelog URL's server sends no Content-Type header."""
        self.create_mock_response(
            mock_get,
            {"info": {"description": "Package-foo description", "project_urls": {"changelog": "https://changes"}}},
            text="Changelog\n## 1.1\n- Fixed foo\n",
            content_type=None,
        )
        self.assertEqual(get_changes("humanize", "1.1"), "## 1.1\n- Fixed foo")

    def test_changelog_url_gives_error(self, mock_get: Mock):
        """Test that a changelog URL that gives an HTTP error doesn't stop the later heuristics."""
        changelog = "1.1\n- Fixed ...\n- Added ..."
        project_urls = {"changelog": "https://changes"}
        metadata = {"info": {"description": f"Package description\n{changelog}\n", "project_urls": project_urls}}
        self.create_mock_response_per_url(mock_get, metadata, unreachable_url="https://changes")
        self.assertEqual(get_changes("certifi", "1.1"), changelog)

    def test_repository_url_found(self, mock_get: Mock):
        """Test that the changes are returned if PyPI returns a repository URL, under any label read as one."""
        changelog = "Changelog\n## 1.1\n- Fixed foo\n"
        repo = "https://github.com/org/repo"
        docs = "https://docs"
        for key in ("repository", "source", "homepage", "Source Code", "GitHub"):
            with self.subTest(key=key):
                self.create_mock_response(
                    mock_get,
                    {"info": {"description": "Package-foo description", "project_urls": {"docs": docs, key: repo}}},
                    [github_release_json("1.1", body=changelog)],
                )
                self.assertEqual(get_changes(f"package-4-{key}", "1.1"), changelog)

    def test_github_project_url_under_another_label(self, mock_get: Mock):
        """Test that a GitHub project URL is read as the repository, whatever label it carries."""
        changelog = "Changelog\n## 1.1\n- Fixed foo\n"
        project_urls = {"Documentation": "https://idna.readthedocs.io", "Bug Tracker": "https://github.com/kjd/idna"}
        self.create_mock_response(
            mock_get,
            {"info": {"description": "Package-foo description", "project_urls": project_urls}},
            [github_release_json("1.1", body=changelog)],
        )
        self.assertEqual(get_changes("idna", "1.1"), changelog)

    def test_labelled_repository_url_is_read_first(self, mock_get: Mock):
        """Test that a project URL labelled as the repository is read before a GitHub URL under another label."""
        changelog = "Changelog\n## 1.1\n- Fixed foo\n"
        project_urls = {"Funding": "https://github.com/aio-libs/.github", "Source": "https://github.com/aio-libs/yarl"}
        self.create_mock_response(
            mock_get,
            {"info": {"description": "Package-foo description", "project_urls": project_urls}},
            [github_release_json("1.1", body=changelog)],
        )
        self.assertEqual(get_changes("yarl", "1.1"), changelog)
        self.assert_releases_requested(mock_get, "aio-libs/yarl")

    def test_source_url_is_read_before_the_homepage(self, mock_get: Mock):
        """Test that a project URL labelled as the source is read before one labelled as the homepage."""
        changelog = "Changelog\n## 1.1\n- Fixed foo\n"
        project_urls = {
            "Homepage": "https://github.com/python/typing",
            "Source": "https://github.com/python/typing_extensions",
        }
        self.create_mock_response(
            mock_get,
            {"info": {"description": "Package-foo description", "project_urls": project_urls}},
            [github_release_json("1.1", body=changelog)],
        )
        self.assertEqual(get_changes("typing-extensions", "1.1"), changelog)
        self.assert_releases_requested(mock_get, "python/typing_extensions")

    def test_sponsors_project_url_is_not_a_repository(self, mock_get: Mock):
        """Test that a GitHub sponsors URL is not asked for releases, and that the later heuristics still run."""
        changelog = "1.1\n- Fixed ...\n- Added ..."
        project_urls = {"Funding": "https://github.com/sponsors/webknjaz"}
        self.create_mock_response(
            mock_get,
            {"info": {"description": f"Package description\n{changelog}\n", "project_urls": project_urls}},
            [],
        )
        self.assertEqual(get_changes("frozenlist", "1.1"), changelog)
        self.assert_releases_requested(mock_get)

    @kills(NULL_PROJECT_URLS_READ_AS_A_DICT)
    def test_changelog_in_description(self, mock_get: Mock):
        """Test that the description's changelog is returned when PyPI omits the project URLs or reports them null."""
        changelog = "1.1\n- Fixed ...\n- Added ..."
        for case, project_urls in (("missing", {}), ("null", {"project_urls": None})):
            with self.subTest(project_urls=case):
                info = {"description": f"Package description\n{changelog}\n"} | project_urls
                self.create_mock_response(mock_get, {"info": info})
                self.assertEqual(get_changes(f"package-5-{case}", "1.1"), changelog)

    def test_github_url_in_description_that_has_no_changelog(self, mock_get: Mock):
        """Test that a GitHub release without a body yields no changelog."""
        github_url = "https://github.com/pytest-dev/pluggy"
        self.create_description_responses(mock_get, f"Package description\n{github_url}\n")
        self.assertEqual(get_changes("pluggy", "1.1"), "")

    _FIRST_URL_ONLY = Mutation(
        pypi,
        "    for match in _GITHUB_URL_RE.finditer(description):",
        "    for match in list(_GITHUB_URL_RE.finditer(description))[:1]:",
        "a package whose description links another project above its own repository reports no changes",
    )

    @kills(_FIRST_URL_ONLY)
    def test_another_projects_url_in_description_is_passed_over(self, mock_get: Mock):
        """Test that a description linking another project before the package's own reports the package's changes."""
        changelog = "1.1\n- Fixed ...\n- Added ..."
        description = "Badge https://github.com/readme/featured\nSource https://github.com/python-attrs/attrs\n"
        self.create_description_responses(mock_get, description, changelog)
        self.assertEqual(get_changes("attrs", "1.1"), changelog)
        self.assert_releases_requested(mock_get, "python-attrs/attrs")

    _NAMES_COMPARED_AS_SPELLED = Mutation(
        pypi,
        "return normalized_name(repository) == normalized_name(package)",
        "return repository == package",
        "a package whose repository spells its name with another separator, or in another case, reports no changes",
    )

    _NAME_MATCHED_AS_A_SUBSTRING = Mutation(
        pypi,
        "return normalized_name(repository) == normalized_name(package)",
        "return normalized_name(package) in normalized_name(repository)",
        "a repository whose name merely contains the package's is read as the package's own",
    )

    @kills(_NAMES_COMPARED_AS_SPELLED, _NAME_MATCHED_AS_A_SUBSTRING)
    def test_which_repository_names_match_the_package_name(self, mock_get: Mock):
        """Test which repository names count as the package's, compared as PyPI normalizes a distribution name."""
        changelog = "1.1\n- Fixed ...\n- Added ..."
        cases = (
            ("Ousret/charset_normalizer", "charset-normalizer", True),
            ("python-pillow/Pillow", "pillow", True),
            ("zopefoundation/zope.interface", "zope-interface", True),
            ("chrisjsewell/markdown-it-pyrs", "markdown-it-py", False),
        )
        for repository, package, matches in cases:
            with self.subTest(repository=repository):
                mock_get.reset_mock()
                description = f"Package description\nhttps://github.com/{repository}\n"
                self.create_description_responses(mock_get, description, changelog)
                asked = [repository] if matches else []
                self.assertEqual(get_changes(package, "1.1"), changelog if matches else "")
                self.assert_releases_requested(mock_get, *asked)

    _SPONSORS_URL_IS_A_REPOSITORY = Mutation(
        pypi,
        "    _owner, repository = _github_repository(url)",
        "    _owner, repository = github_owner_and_repository(url)",
        "a sponsors page carrying the package's name is read as its repository, so the package reports no changes",
    )

    @kills(_SPONSORS_URL_IS_A_REPOSITORY)
    def test_sponsors_url_in_description_is_not_a_repository(self, mock_get: Mock):
        """Test that a sponsors URL naming the package is passed over for the repository linked below it."""
        changelog = "1.1\n- Fixed ...\n- Added ..."
        description = "Sponsor https://github.com/sponsors/tqdm\nSource https://github.com/tqdm/tqdm\n"
        self.create_description_responses(mock_get, description, changelog)
        self.assertEqual(get_changes("tqdm", "1.1"), changelog)
        self.assert_releases_requested(mock_get, "tqdm/tqdm")

    _NO_DISCOVERY = Mutation(
        pypi,
        "_changelog_from_repository_root(url, version)",
        '""',
        "a package keeping its changelog in a file in its repository reports no changes at all",
    )

    @kills(_NO_DISCOVERY)
    def test_changelog_file_in_the_repository_root(self, mock_get: Mock):
        """Test that a changelog file in the repository root supplies the changes when no earlier heuristic does."""
        changelog = "Changelog\n=========\n\n1.1\n===\n\n- Fixed foo\n\n1.0\n===\n\n- Fixed bar\n"
        files = {"README.md": "Not a changelog", "CHANGES.rst": changelog}
        self.create_discovery_responses(mock_get, "gevent", files=files, repository="gevent/gevent")
        self.assertEqual(get_changes("gevent", "1.1"), "1.1\n===\n\n- Fixed foo")

    _UNGUARDED_URL = Mutation(
        github,
        '        if _is_changelog_file(entry["name"]) and (url := entry["download_url"]):\n'
        "            response = fetch(url, _LOG)",
        '        if _is_changelog_file(entry["name"]):\n            response = fetch(entry["download_url"], _LOG)',
        "a directory named like a changelog, such as pip's `news`, costs a request for the file it has none of",
    )

    @kills(_UNGUARDED_URL)
    def test_changelog_named_entry_that_is_no_file(self, mock_get: Mock):
        """Test that an entry named like a changelog with no file to fetch is passed over without a request."""
        changelog = "Changelog\n=========\n\n1.1\n===\n\n- Fixed foo\n"
        # The null download URL of the `news` directory answers a text naming no version, rather than raising.
        self.create_discovery_responses(
            mock_get,
            "pip",
            files={"news": None, "NEWS.rst": changelog},
            repository="pypa/pip",
            extra={None: mock_response(text="Nothing to report")},
        )
        self.assertEqual(get_changes("pip", "1.1"), "1.1\n===\n\n- Fixed foo")
        self.assertNotIn(None, self.requested_urls(mock_get))

    def test_release_is_read_before_the_repository_root(self, mock_get: Mock):
        """Test that a package whose release answers costs no root listing, and reports the release's changes."""
        release = github_release_json("1.1", body="## 1.1\n- Fixed in the release")
        self.create_discovery_responses(
            mock_get,
            "urllib3",
            files={"CHANGES.rst": "1.1\n===\n\n- Fixed in the file\n"},
            repository="urllib3/urllib3",
            extra={self.releases_url("urllib3/urllib3"): mock_response([release])},
        )
        self.assertEqual(get_changes("urllib3", "1.1"), "## 1.1\n- Fixed in the release")
        self.assert_root_listed(mock_get)

    _ROOT_FIRST = Mutation(
        pypi,
        '    if changelog := _changelog_from_description(info["description"], package, version):',
        "    for url in repository_urls:\n"
        "        if changelog := _changelog_from_repository_root(url, version):\n"
        "            return changelog\n"
        '    if changelog := _changelog_from_description(info["description"], package, version):',
        "a package whose description holds the changes reports the repository file's instead, at a request extra",
    )

    @kills(_ROOT_FIRST)
    def test_description_is_read_before_the_repository_root(self, mock_get: Mock):
        """Test that a package whose description holds the changes costs no root listing, and reports those."""
        description = "Package description\n1.1\n- Fixed in the description\n"
        self.create_discovery_responses(
            mock_get,
            "six",
            files={"CHANGES.rst": "1.1\n===\n\n- Fixed in the file\n"},
            repository="benjaminp/six",
            description=description,
        )
        self.assertEqual(get_changes("six", "1.1"), "1.1\n- Fixed in the description")
        self.assert_root_listed(mock_get)

    _URL_ENDS_THE_SEARCH = Mutation(
        pypi,
        '    if changelog := _changelog_from_description(info["description"], package, version):',
        '    changelog = _changelog_from_description(info["description"], package, version)\n'
        '    if changelog or _GITHUB_URL_RE.search(info["description"]):',
        "a package whose description links another project reports no changes, though its repository's root "
        "holds a changelog file",
    )

    @kills(_URL_ENDS_THE_SEARCH)
    def test_repository_root_is_read_when_the_description_names_another_project(self, mock_get: Mock):
        """Test that a description linking only another project leaves the repository root to supply the changes."""
        changelog = "# Changelog\n\n## 1.1\n\n- Fixed foo\n\n## 1.0\n\n- Fixed bar\n"
        description = "Package description\nBuilt with https://github.com/sloria/environs\n"
        self.create_discovery_responses(
            mock_get,
            "python-dotenv",
            files={"CHANGELOG.md": changelog},
            repository="theskumar/python-dotenv",
            description=description,
        )
        self.assertEqual(get_changes("python-dotenv", "1.1"), "## 1.1\n\n- Fixed foo")
        self.assert_releases_requested(mock_get, "theskumar/python-dotenv")
        self.assert_root_listed(mock_get, "theskumar/python-dotenv")

    _FEWER_NAMES = Mutation(
        github,
        '_CHANGELOG_FILE_NAMES = frozenset({"changes", "changelog", "history", "news", "releases"})',
        '_CHANGELOG_FILE_NAMES = frozenset({"changes", "changelog"})',
        "a repository naming its changelog file `history`, `news`, or `releases` reports no changes",
    )

    @kills(_FEWER_NAMES)
    def test_which_root_entries_count_as_the_changelog(self, mock_get: Mock):
        """Test which names a root entry is read as the changelog under, compared without regard for case."""
        changelog = "1.1\n===\n\n- Fixed foo\n"
        cases = (
            ("CHANGES.rst", True),
            ("CHANGELOG.md", True),
            ("history.txt", True),
            ("News", True),
            ("RELEASES.RST", True),
            ("README.md", False),
            ("CHANGELOG.html", False),
            ("changelog.d", False),
        )
        for index, (name, found) in enumerate(cases):
            with self.subTest(name=name):
                mock_get.reset_mock()
                package, repository = f"package-19-{index}", f"org/repo-{index}"
                self.create_discovery_responses(mock_get, package, files={name: changelog}, repository=repository)
                self.assertEqual(get_changes(package, "1.1"), "1.1\n===\n\n- Fixed foo" if found else "")
                self.assert_root_listed(mock_get, repository)

    _UNGUARDED_LISTING = Mutation(
        github,
        "    response = fetch(contents_url, _LOG, headers=_github_headers())\n"
        "    return tuple(response.json()) if response is not None else None",
        "    response = fetch(contents_url, _LOG, headers=_github_headers())\n    return tuple(response.json())",
        "a repository whose root listing cannot be fetched ends the run with a traceback",
        raises="AttributeError: 'NoneType' object has no attribute 'json'",
    )

    @kills(_UNGUARDED_LISTING)
    def test_root_listing_unreachable(self, mock_get: Mock):
        """Test that a root listing that can't be fetched yields no changes, and is reported as unreachable."""
        contents_url = self.contents_url("pypa/packaging")
        unreachable = mock_response(status_code=HTTPStatus.NOT_FOUND, ok=False, url=contents_url)
        self.create_discovery_responses(
            mock_get, "packaging", repository="pypa/packaging", extra={contents_url: unreachable}
        )
        self.assertEqual(get_changes("packaging", "1.1"), "")
        self.assert_root_listed(mock_get, "pypa/packaging")
        self.assert_could_not_fetch_logged(url=contents_url)

    _UNCACHED = Mutation(
        github,
        "@cache\ndef _list_root(",
        "def _list_root(",
        "every package sharing a repository costs a root listing of its own",
    )

    @kills(_UNCACHED)
    def test_root_listing_is_fetched_once_per_repository(self, mock_get: Mock):
        """Test that two packages sharing a repository cost one root listing between them."""
        self.create_discovery_responses(
            mock_get,
            "google-cloud-storage",
            "google-cloud-bigquery",
            files={"CHANGES.rst": "1.1\n===\n\n- Fixed foo\n"},
            repository="googleapis/google-cloud-python",
        )
        for package in ("google-cloud-storage", "google-cloud-bigquery"):
            with self.subTest(package=package):
                self.assertEqual(get_changes(package, "1.1"), "1.1\n===\n\n- Fixed foo")
        self.assert_root_listed(mock_get, "googleapis/google-cloud-python")

    _FIRST_FILE_ONLY = Mutation(
        github,
        "(changes := get_version_changes_from_changelog(response.text, version)):",
        "(changes := get_version_changes_from_changelog(response.text, version)) is not None:",
        "a root whose first changelog file names no version reports no changes, though another file names it",
    )

    @kills(_FIRST_FILE_ONLY)
    def test_changelog_file_naming_no_version_is_passed_over(self, mock_get: Mock):
        """Test that a root holding a changelog file naming no version is searched on for one that names it."""
        files = {
            "CHANGELOG.md": "Changelog\n\n## 0.9\n\n- Fixed bar\n",
            "CHANGES.rst": "1.1\n===\n\n- Fixed foo\n",
        }
        self.create_discovery_responses(mock_get, "cryptography", files=files, repository="pyca/cryptography")
        self.assertEqual(get_changes("cryptography", "1.1"), "1.1\n===\n\n- Fixed foo")

    def test_release_metadata_unreachable(self, mock_get: Mock):
        """Test that the changes are empty, and no source is consulted, when PyPI doesn't serve the metadata."""
        self.create_mock_response(mock_get, status_code=HTTPStatus.NOT_FOUND)
        self.assertEqual(get_changes("setuptools", "1.1"), "")
        self.assert_releases_requested(mock_get)

    def test_changelog_url_unreachable(self, mock_get: Mock):
        """Test that an unreachable changelog URL yields an empty changelog instead of crashing."""
        self.create_mock_response(mock_get, status_code=HTTPStatus.NOT_FOUND)
        self.assertEqual(_changelog_from_url("https://changes", "1.0"), "")


class GetPublicationDateTimeTest(CacheClearingTestCase):
    """Unit tests for getting the publication date time of releases."""

    @patch_get({"urls": [{"upload_time_iso_8601": "2026-05-30T12:00:03.678901Z"}]})
    def test_publication_datetime(self):
        """Test that the publication datetime is returned."""
        published = datetime(2026, 5, 30, 12, 0, 3, 678901, tzinfo=UTC)
        self.assertEqual(published, get_publication_datetime("package", "1.0"))

    @patch_get(
        {
            "urls": [
                {"upload_time_iso_8601": "2026-05-30T12:00:03.678901Z"},
                {"upload_time_iso_8601": "2026-05-30T12:00:03.543210Z"},
            ]
        }
    )
    def test_max_publication_datetime(self):
        """Test that the latest publication datetime is returned if there are multiple distributions."""
        published = datetime(2026, 5, 30, 12, 0, 3, 678901, tzinfo=UTC)
        self.assertEqual(published, get_publication_datetime("package", "1.0"))


class NewestReleaseTest(LoggingTestCase):
    """Unit tests for the release a package published most recently."""

    @patch_get({"versions": ["1.0"]})
    def test_no_files(self):
        """Test that no release is returned when the Index API lists no distribution files."""
        self.assertIsNone(newest_release("no_files"))

    @patch_get(
        {
            "versions": ["1.0.1", "2.0"],
            "files": [
                {"filename": "example-2.0.tar.gz", "upload-time": "2020-01-01T00:00:00Z"},
                {"filename": "example-1.0.1.tar.gz", "upload-time": "2020-06-01T00:00:00Z"},
                {"filename": "no-upload-time.whl"},
                {"filename": "unreadable-name.txt", "upload-time": "2021-01-01T00:00:00Z"},
            ],
        }
    )
    def test_newest_across_files(self):
        """Test that the newest release is the one whose upload is the most recent.

        The backport 1.0.1 was uploaded after 2.0, so the highest version is not the one measured. The upload
        after it names no version, and the file without an upload time names no date.
        """
        self.assertEqual(Release("1.0.1", datetime(2020, 6, 1, tzinfo=UTC)), newest_release("files"))

    @patch_get(ok=False)
    def test_fetch_failure(self):
        """Test that no release is returned when the Index API can't be fetched."""
        self.assertIsNone(newest_release("error"))
        self.assert_could_not_fetch_logged()


@patch("requests.get")
class GetLatestVersionTest(LoggingTestCase):
    """Unit tests for getting the latest version from PyPI.

    `get_latest_version` makes its requests in a fixed order, so each test's `mock_get.side_effect` mirrors it:
    first the Index API (the version list), then — newest-first — one per-version metadata request per candidate,
    stopping at the first eligible release. A test supplies only as many responses as reaching its expected version
    requires. The current version's yank state is read from that same Index API response.
    """

    def test_invalid_current_version(self, mock_get: Mock):
        """Test that an invalid current version is returned unchanged without querying PyPI."""
        self.assertEqual(
            get_latest_version("package", "not a version", NO_BOUND, COOLDOWN.default).version, "not a version"
        )
        mock_get.assert_not_called()

    def test_no_newer_version(self, mock_get: Mock):
        """Test that the current version is returned when it is already the latest."""
        mock_get.side_effect = [pypi_index("1.0")]
        self.assertEqual(get_latest_version("no_newer", "1.0", NO_BOUND, COOLDOWN.default).version, "1.0")

    def test_new_version(self, mock_get: Mock):
        """Test that the latest version is returned, with its publication date."""
        mock_get.side_effect = [pypi_index("1.0", "1.1"), pypi_release()]
        latest = get_latest_version("new_version", "1.0", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "1.1")
        self.assertEqual(datetime(2020, 1, 1, tzinfo=UTC), latest.published)

    @kills(A_RELEASE_WITHOUT_PROJECT_URLS_SKIPPED)
    def test_new_version_of_a_release_without_project_urls(self, mock_get: Mock):
        """Test that a release whose metadata reports the project URLs as null is adopted like any other."""
        mock_get.side_effect = [pypi_index("1.0", "1.1"), pypi_release(project_urls=None)]
        self.assertEqual(get_latest_version("null_urls", "1.0", NO_BOUND, COOLDOWN.default).version, "1.1")

    def test_highest_version(self, mock_get: Mock):
        """Test that the highest of multiple newer versions is returned."""
        mock_get.side_effect = [pypi_index("1.0", "1.2", "1.1"), pypi_release()]
        self.assertEqual(get_latest_version("highest", "1.0", NO_BOUND, COOLDOWN.default).version, "1.2")

    @kills(
        Mutation(
            pypi,
            "    return replace(latest, newest=newest_release(package))",
            "    _n = newest_release(package)\n"
            "    return replace(latest, newest=None if _n is None else type(_n)(latest.version, _n.published))",
            "the release attached names the version the run leaves the pin on, not the package's newest",
        )
    )
    def test_newest_release_attached(self, mock_get: Mock):
        """Test that the newest release is attached, and not the version the run leaves the pin on.

        The cooldown holds 2.0 back, so the pin stays on 1.0 while 2.0 is the release that dates the package.
        """
        fresh = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        files = [{"filename": "package-1.0.tar.gz", "upload-time": PYPI_OLD_UPLOAD}]
        files += [{"filename": "package-2.0.tar.gz", "upload-time": fresh}]
        mock_get.side_effect = [pypi_index("1.0", "2.0", files=files), pypi_release(fresh)]
        latest = get_latest_version("stale", "1.0", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "1.0")
        self.assertEqual(Release("2.0", datetime.fromisoformat(fresh)), latest.newest)

    def test_prerelease_ignored(self, mock_get: Mock):
        """Test that pre-releases are ignored without fetching their metadata."""
        mock_get.side_effect = [pypi_index("1.0", "2.0b1")]
        self.assertEqual(get_latest_version("prerelease", "1.0", NO_BOUND, COOLDOWN.default).version, "1.0")

    def test_bound_narrows_candidates(self, mock_get: Mock):
        """Test that a version bound drops out-of-bound candidates so a bounded version wins over a higher one."""
        mock_get.side_effect = [pypi_index("1.0", "1.9", "2.0"), pypi_release()]
        version_bound = bound(Verb.ALLOW, "update<2")
        self.assertEqual(get_latest_version("bounded", "1.0", version_bound, COOLDOWN.default).version, "1.9")

    def test_yanked_release_ignored(self, mock_get: Mock):
        """Test that yanked releases are ignored."""
        mock_get.side_effect = [pypi_index("1.0", "1.1"), pypi_release(yanked=True)]
        self.assertEqual(get_latest_version("yanked", "1.0", NO_BOUND, COOLDOWN.default).version, "1.0")

    def test_yanked_current_version_attached(self, mock_get: Mock):
        """Test that when the pin stays put on a yanked version, its yank reason is attached from the Index API."""
        mock_get.side_effect = [
            pypi_index("1.0", files=[yanked_file("yanked_pin-1.0.tar.gz", reason="broke Python 3.10")])
        ]
        latest = get_latest_version("yanked_pin", "1.0", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "1.0")
        self.assertEqual(latest.yank, Yank(yanked=True, reason="broke Python 3.10"))

    def test_yanked_current_version_without_reason(self, mock_get: Mock):
        """Test that a yanked pin with no maintainer reason is flagged as yanked with an empty reason."""
        mock_get.side_effect = [pypi_index("1.0", files=[yanked_file("yanked_pin-1.0-py3-none-any.whl")])]
        self.assertEqual(get_latest_version("yanked_pin", "1.0", NO_BOUND, COOLDOWN.default).yank, Yank(yanked=True))

    def test_no_yank_when_the_update_moves_away(self, mock_get: Mock):
        """Test that a run updating away from a yanked pin reports no yank, since the pin no longer sits on one."""
        files = [yanked_file("moved-1.0.tar.gz", reason="broke Python 3.10")]
        mock_get.side_effect = [pypi_index("1.0", "1.1", files=files), pypi_release()]
        latest = get_latest_version("moved", "1.0", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "1.1")
        self.assertEqual(latest.yank, Yank())

    def test_yanked_file_of_another_version_ignored(self, mock_get: Mock):
        """Test that a yanked file of another version, or an unparsable filename, leaves the pin unyanked."""
        files = [yanked_file("pin-0.9.tar.gz", reason="old"), yanked_file("not-a-distribution")]
        mock_get.side_effect = [pypi_index("1.0", files=files)]
        self.assertFalse(get_latest_version("pin", "1.0", NO_BOUND, COOLDOWN.default).yank.yanked)

    def test_release_without_files_ignored(self, mock_get: Mock):
        """Test that releases without distribution files are ignored."""
        mock_get.side_effect = [pypi_index("1.0", "1.1"), pypi_release(upload_time="")]
        self.assertEqual(get_latest_version("no_files", "1.0", NO_BOUND, COOLDOWN.default).version, "1.0")

    def test_release_metadata_unavailable_ignored(self, mock_get: Mock):
        """Test that a candidate whose metadata can't be fetched is skipped instead of crashing the run."""
        mock_get.side_effect = [pypi_index("1.0", "1.1"), mock_response(ok=False)]
        self.assertEqual(get_latest_version("metadata_error", "1.0", NO_BOUND, COOLDOWN.default).version, "1.0")

    def test_invalid_release_ignored(self, mock_get: Mock):
        """Test that releases with an invalid version are ignored without fetching their metadata."""
        mock_get.side_effect = [pypi_index("1.0", "not-a-version")]
        self.assertEqual(get_latest_version("invalid_release", "1.0", NO_BOUND, COOLDOWN.default).version, "1.0")

    def test_cooldown_decides_eligibility(self, mock_get: Mock):
        """Test that a release is held back or adopted according to the cooldown the getter is passed."""
        published = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        for cooldown_days, expected in ((30, "1.0"), (5, "1.1")):
            with self.subTest(cooldown_days=cooldown_days):
                mock_get.side_effect = [pypi_index("1.0", "1.1"), pypi_release(published)]
                package = f"cooldown_argument_{cooldown_days}"  # A fresh name per case, as the fetches are cached.
                self.assertEqual(get_latest_version(package, "1.0", NO_BOUND, cooldown_days).version, expected)
