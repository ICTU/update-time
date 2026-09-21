"""npmjs unit tests."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from update_time.domain.dependency import Release, Yank
from update_time.sources import npmjs
from update_time.sources.npmjs import (
    deprecation,
    get_changes,
    get_publication_datetime,
    newest_release,
)

from tests.helpers import mock_response, patch_get
from tests.mutation import Mutation, kills
from tests.update_time.helpers import LoggingTestCase, github_release_json
from tests.update_time.sources.helpers import (
    contents_json,
    contents_url,
    file_url,
    markdown_changelog,
    markdown_changes,
    releases_url,
    requested_urls,
    respond_per_url,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

# The repository the packages in these tests name, as owner and repository.
_REPOSITORY = "org/package"


def _version_url(package: str, version: str = "1.0") -> str:
    """Return the URL the npm registry serves the package version's document at."""
    return f"https://registry.npmjs.org/{package}/{version}"


class NpmjsPublicationDatetimeTest(LoggingTestCase):
    """Unit tests for the npmjs publication datetime fetcher."""

    @patch_get({"time": {"1.0": "20260530T10:14:40.567Z"}})
    def test_get_publication_datetime(self):
        """Test that the publication datetime can be fetched."""
        publication_datetime = datetime(2026, 5, 30, 10, 14, 40, 567000, tzinfo=UTC)
        self.assertEqual(publication_datetime, get_publication_datetime("package", "1.0"))

    @patch_get(ok=False)
    def test_get_publication_datetime_when_unreachable(self):
        """Test that an unreachable registry yields no publication date instead of crashing."""
        self.assertIsNone(get_publication_datetime("package", "1.0"))

    @kills(
        Mutation(
            npmjs,
            'return parse_timestamp(_package_metadata(package).get("time", {}).get(version))',
            'return parse_timestamp(_package_metadata(package).get("time", {})[version])',
            "a version the registry dates nowhere in its time map ends the run with a traceback",
            raises="KeyError: '9.9'",
        ),
    )
    @patch_get({"time": {}})
    def test_get_publication_datetime_for_unlisted_version(self):
        """Test that a version the registry doesn't list yields no publication date instead of raising."""
        self.assertIsNone(get_publication_datetime("package", "9.9"))

    @patch_get(ok=False)
    def test_get_changes_when_unreachable(self):
        """Test that an unreachable registry yields no changelog instead of crashing."""
        self.assertEqual(get_changes("package", "1.0"), "")


class NpmjsNewestReleaseTest(LoggingTestCase):
    """Unit tests for the newest release (version + publication date) fetcher."""

    @patch_get({"time": {"2.0": "2020-01-01T00:00:00Z", "1.0.1": "2024-06-01T00:00:00Z"}})
    def test_newest_release(self):
        """Test that the version published most recently is returned, and not the highest one.

        The backport 1.0.1 was published after 2.0, which the dist-tag still names.
        """
        self.assertEqual(Release("1.0.1", datetime(2024, 6, 1, tzinfo=UTC)), newest_release("package"))

    @patch_get(
        {
            "time": {
                "created": "2019-01-01T00:00:00Z",
                "modified": "2030-01-01T00:00:00Z",  # later than any version, but bookkeeping — must be ignored
                "1.0": "2020-01-01T00:00:00Z",
                "1.1": "2024-06-01T00:00:00Z",
            }
        }
    )
    def test_bookkeeping_entries_are_ignored(self):
        """Test that the `created` and `modified` entries are passed over, whatever they are dated."""
        self.assertEqual(Release("1.1", datetime(2024, 6, 1, tzinfo=UTC)), newest_release("package"))

    @patch_get(ok=False)
    def test_unreachable(self):
        """Test that an unreachable registry yields no release instead of crashing."""
        self.assertIsNone(newest_release("package"))


class NpmjsDeprecationTest(LoggingTestCase):
    """Unit tests for reading a version's npm deprecation state."""

    @patch_get({"versions": {"1.0": {"deprecated": "use 2.0 instead"}}})
    def test_deprecated_version(self):
        """Test that a deprecated version's flag and message are returned."""
        self.assertEqual(deprecation("package", "1.0"), Yank(yanked=True, reason="use 2.0 instead"))

    @patch_get({"versions": {"1.0": {}}})
    def test_undeprecated_version(self):
        """Test that a version without a deprecation message is not flagged."""
        self.assertEqual(deprecation("package", "1.0"), Yank())

    @patch_get({"versions": {}})
    def test_unlisted_version(self):
        """Test that a version the registry doesn't list is not flagged."""
        self.assertEqual(deprecation("package", "9.9"), Yank())


class GetChangesRepositoryTest(LoggingTestCase):
    """Unit tests for locating the changelog from npm's varied `repository` metadata."""

    def test_unreadable_repository(self):
        """Test that metadata naming no GitHub URL yields no changelog, without raising and without asking GitHub."""
        for package, metadata in (
            ("no_repo", {"name": "package"}),  # no `repository` field
            ("no_url", {"repository": {"type": "git"}}),
            ("url_not_a_string", {"repository": {"url": 42}}),
        ):
            with self.subTest(metadata=metadata), patch_get(metadata) as mock_get:
                self.clear_caches()
                self.assertEqual(get_changes(package, "1.0"), "")
                self.assertEqual(requested_urls(mock_get), [_version_url(package)])

    @patch("requests.get")
    def test_repository_naming_a_github_repository(self, mock_get: Mock):
        """Test that every spelling of a GitHub repository npm allows is used to find the changelog."""
        release = github_release_json("1.0", body="Changelog")
        for package, repository in (
            ("host_shorthand", "github:org/package"),
            ("bare_shorthand", "org/package"),
            ("https_repo", {"url": "git+https://github.com/org/package.git"}),
            ("ssh_repo", {"url": "git+ssh://git@github.com/org/package.git"}),
            ("scp_repo", {"url": "git@github.com:org/package.git"}),
        ):
            with self.subTest(repository=repository):
                self.clear_caches()
                mock_get.reset_mock()
                respond_per_url(
                    mock_get,
                    {
                        _version_url(package): mock_response({"repository": repository}),
                        releases_url(_REPOSITORY): mock_response([release]),
                    },
                )
                self.assertEqual(get_changes(package, "1.0"), "Changelog")
                self.assertEqual(requested_urls(mock_get), [_version_url(package), releases_url(_REPOSITORY)])


class GetChangesFallbackTest(LoggingTestCase):
    """Unit tests for the changelog file read when the repository publishes no matching release."""

    CHANGELOG = markdown_changelog("1.0")
    CHANGES = markdown_changes("1.0")

    def create_responses(
        self, mock_get: Mock, name: str, releases: list, listings: Mapping[str, str], repository: dict | str = ""
    ) -> None:
        """Point the mock requests.get at the package's registry document, its releases, and a listing per directory.

        The repository called `name` serves the releases and the listings, each giving the file that directory
        holds, keyed by directory and by the empty string for its root. A file named like a changelog answers the
        changelog naming version 1.0.
        """
        respond_per_url(
            mock_get,
            {
                _version_url("package"): mock_response({"repository": repository or name}),
                releases_url(name): mock_response(releases),
                **{
                    contents_url(name, directory): mock_response(contents_json(file))
                    for directory, file in listings.items()
                },
                file_url("CHANGELOG.md"): mock_response(text=self.CHANGELOG),
            },
        )

    @kills(
        Mutation(
            npmjs,
            "    return changes_from_release(repository.owner, repository.name, package, version) "
            "or changes_from_changelog_file(\n"
            "        repository.owner, repository.name, version, repository.directory\n    )",
            "    return changes_from_release(repository.owner, repository.name, package, version)",
            "a package whose repository publishes no matching release reports no changelog",
        ),
    )
    @patch("requests.get")
    def test_changelog_file_when_no_release_matches(self, mock_get: Mock):
        """Test that a repository publishing no release for the version has its changelog file read."""
        self.create_responses(mock_get, _REPOSITORY, [], {"": "CHANGELOG.md"})
        self.assertEqual(get_changes("package", "1.0"), self.CHANGES)

    @patch("requests.get")
    def test_no_changelog_file_when_a_release_matches(self, mock_get: Mock):
        """Test that a repository publishing a release for the version has no changelog file read."""
        release = github_release_json("1.0", body="Release notes")
        self.create_responses(mock_get, _REPOSITORY, [release], {"": "CHANGELOG.md"})
        self.assertEqual(get_changes("package", "1.0"), "Release notes")
        self.assertNotIn(contents_url(_REPOSITORY), requested_urls(mock_get))

    @kills(
        Mutation(
            npmjs,
            "        repository.owner, repository.name, version, repository.directory",
            '        repository.owner, repository.name, version, ""',
            "the changelog file of a package a monorepo builds from a directory is looked for in the root",
        ),
    )
    @patch("requests.get")
    def test_changelog_file_in_the_directory_the_registry_names(self, mock_get: Mock):
        """Test that the directory the registry names is where the package's changelog file is looked for."""
        repository = {"url": "https://github.com/org/monorepo", "directory": "packages/package"}
        listings = {"": "README.md", "packages/package": "CHANGELOG.md"}
        self.create_responses(mock_get, "org/monorepo", [], listings, repository)
        self.assertEqual(get_changes("package", "1.0"), self.CHANGES)
