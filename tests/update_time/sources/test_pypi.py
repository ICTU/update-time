"""Unit tests for the PyPI module."""

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from unittest.mock import Mock, patch

from update_time.domain.bound import NO_BOUND, Verb
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import Yank
from update_time.sources.pypi import (
    _changelog_from_url,
    get_changes,
    get_latest_version,
    get_publication_datetime,
    newest_publication_date,
)

from tests.helpers import mock_response, patch_get
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


@patch("requests.get")
class GetChangesTest(LoggingTestCase):
    """Unit tests for getting the changes."""

    def create_mock_response(
        self, mock_get: Mock, *json: dict | list, text: str = "", status_code: int = HTTPStatus.OK
    ) -> None:
        """Point the mock requests.get at one response whose successive `.json()` calls return the given payloads.

        The changelog heuristics make several requests off one release (the PyPI metadata, then a changelog URL or
        GitHub releases); the shared response returns the next JSON payload on each `.json()` and the same text and
        status for all of them.
        """
        ok = status_code < HTTPStatus.BAD_REQUEST
        response = mock_response(text=text, status_code=status_code, ok=ok, headers={"Content-Type": "text/text"})
        response.json.side_effect = list(json)
        mock_get.return_value = response

    def create_mock_response_per_url(self, mock_get: Mock, metadata: dict, unreachable_url: str) -> None:
        """Point the mock requests.get at the release metadata, answering the unreachable URL with an HTTP error."""
        unreachable = mock_response(status_code=HTTPStatus.NOT_FOUND, ok=False)
        reachable = mock_response(metadata, status_code=HTTPStatus.OK, ok=True)
        mock_get.side_effect = lambda url, **_kwargs: unreachable if url == unreachable_url else reachable

    def assert_releases_requested(self, mock_get: Mock, *repositories: str) -> None:
        """Assert that GitHub was asked for the releases of exactly these repositories, in this order."""
        requested = [call.args[0] for call in mock_get.call_args_list if "/releases" in call.args[0]]
        expected = [f"https://api.github.com/repos/{repository}/releases?per_page=100" for repository in repositories]
        self.assertEqual(requested, expected)

    def test_no_url_found(self, mock_get: Mock):
        """Test that the changes are empty if no changelog URL is returned by PyPI."""
        self.create_mock_response(mock_get, {"info": {"description": "Package-foo description"}})
        self.assertEqual(get_changes("package-1", "1.0"), "")

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

    def test_changelog_url_gives_error(self, mock_get: Mock):
        """Test that a changelog URL that gives an HTTP error doesn't stop the later heuristics."""
        changelog = "1.1\n- Fixed ...\n- Added ..."
        project_urls = {"changelog": "https://changes"}
        metadata = {"info": {"description": f"Package description\n{changelog}\n", "project_urls": project_urls}}
        self.create_mock_response_per_url(mock_get, metadata, unreachable_url="https://changes")
        self.assertEqual(get_changes("package-3", "1.1"), changelog)

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
        project_urls = {"Documentation": "https://docs", "Bug Tracker": "https://github.com/org/repo"}
        self.create_mock_response(
            mock_get,
            {"info": {"description": "Package-foo description", "project_urls": project_urls}},
            [github_release_json("1.1", body=changelog)],
        )
        self.assertEqual(get_changes("package-8", "1.1"), changelog)

    def test_labelled_repository_url_is_read_first(self, mock_get: Mock):
        """Test that a project URL labelled as the repository is read before a GitHub URL under another label."""
        changelog = "Changelog\n## 1.1\n- Fixed foo\n"
        project_urls = {"Funding": "https://github.com/org/funding", "Source": "https://github.com/org/repo"}
        self.create_mock_response(
            mock_get,
            {"info": {"description": "Package-foo description", "project_urls": project_urls}},
            [github_release_json("1.1", body=changelog)],
        )
        self.assertEqual(get_changes("package-11", "1.1"), changelog)
        self.assert_releases_requested(mock_get, "org/repo")

    def test_source_url_is_read_before_the_homepage(self, mock_get: Mock):
        """Test that a project URL labelled as the source is read before one labelled as the homepage."""
        changelog = "Changelog\n## 1.1\n- Fixed foo\n"
        project_urls = {"Homepage": "https://github.com/org/home", "Source": "https://github.com/org/repo"}
        self.create_mock_response(
            mock_get,
            {"info": {"description": "Package-foo description", "project_urls": project_urls}},
            [github_release_json("1.1", body=changelog)],
        )
        self.assertEqual(get_changes("package-12", "1.1"), changelog)
        self.assert_releases_requested(mock_get, "org/repo")

    def test_sponsors_project_url_is_not_a_repository(self, mock_get: Mock):
        """Test that a GitHub sponsors URL is not asked for releases, and that the later heuristics still run."""
        changelog = "1.1\n- Fixed ...\n- Added ..."
        project_urls = {"Funding": "https://github.com/sponsors/owner"}
        self.create_mock_response(
            mock_get,
            {"info": {"description": f"Package description\n{changelog}\n", "project_urls": project_urls}},
            [],
        )
        self.assertEqual(get_changes("package-9", "1.1"), changelog)
        self.assert_releases_requested(mock_get)

    def test_changelog_in_description(self, mock_get: Mock):
        """Test that the changelog from the description is returned."""
        changelog = "1.1\n- Fixed ...\n- Added ..."
        self.create_mock_response(mock_get, {"info": {"description": f"Package description\n{changelog}\n"}})
        self.assertEqual(get_changes("package-5", "1.1"), changelog)

    def test_github_url_in_description_that_has_a_changelog(self, mock_get: Mock):
        """Test that the GitHub URL in the description is used to get the changelog."""
        github_url = "https://github.com/org/bar"
        changelog = "1.1\n- Fixed ...\n- Added ..."
        self.create_mock_response(
            mock_get,
            {"info": {"description": f"Package description\n{github_url}\n"}},
            [github_release_json("1.1", body=changelog)],
        )
        self.assertEqual(get_changes("package-6", "1.1"), changelog)

    def test_github_url_in_description_that_has_no_changelog(self, mock_get: Mock):
        """Test that a GitHub release without a body yields no changelog."""
        github_url = "https://github.com/org/baz"
        self.create_mock_response(
            mock_get,
            {"info": {"description": f"Package description\n{github_url}\n"}},
            [github_release_json("1.1")],
        )
        self.assertEqual(get_changes("package-7", "1.1"), "")

    def test_sponsors_url_in_description_is_not_a_repository(self, mock_get: Mock):
        """Test that a GitHub sponsors URL in the description is not asked for releases."""
        sponsors_url = "https://github.com/sponsors/owner"
        self.create_mock_response(mock_get, {"info": {"description": f"Package description\n{sponsors_url}\n"}}, [])
        self.assertEqual(get_changes("package-10", "1.1"), "")
        self.assert_releases_requested(mock_get)

    def test_release_metadata_unreachable(self, mock_get: Mock):
        """Test that the changes are empty, and no source is consulted, when PyPI doesn't serve the metadata."""
        self.create_mock_response(mock_get, status_code=HTTPStatus.NOT_FOUND)
        self.assertEqual(get_changes("package-13", "1.1"), "")
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


class NewestPublicationDateTest(LoggingTestCase):
    """Unit tests for the newest publication date across all of a package's releases."""

    @patch_get({"versions": ["1.0"]})
    def test_no_files(self):
        """Test that no date is returned when the Index API lists no distribution files."""
        self.assertIsNone(newest_publication_date("no_files"))

    @patch_get(
        {
            "files": [
                {"upload-time": "2020-01-01T00:00:00Z"},
                {"upload-time": "2020-06-01T00:00:00Z"},
                {"filename": "no-upload-time.whl"},
            ]
        }
    )
    def test_newest_across_files(self):
        """Test that the most recent upload time across all files is returned, ignoring files without one."""
        self.assertEqual(datetime(2020, 6, 1, tzinfo=UTC), newest_publication_date("files"))

    @patch_get(ok=False)
    def test_fetch_failure(self):
        """Test that no date is returned when the Index API can't be fetched."""
        self.assertIsNone(newest_publication_date("error"))
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

    def test_highest_version(self, mock_get: Mock):
        """Test that the highest of multiple newer versions is returned."""
        mock_get.side_effect = [pypi_index("1.0", "1.2", "1.1"), pypi_release()]
        self.assertEqual(get_latest_version("highest", "1.0", NO_BOUND, COOLDOWN.default).version, "1.2")

    def test_newest_published_attached(self, mock_get: Mock):
        """Test that the newest release date is attached, so an up-to-date pin can still be flagged as stale."""
        mock_get.side_effect = [pypi_index("1.0", files=[{"upload-time": PYPI_OLD_UPLOAD}])]
        latest = get_latest_version("stale", "1.0", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "1.0")
        self.assertEqual(datetime(2020, 1, 1, tzinfo=UTC), latest.newest_published)

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
