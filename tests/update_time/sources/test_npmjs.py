"""npmjs unit tests."""

from datetime import UTC, datetime
from unittest.mock import Mock, patch

from update_time.domain.dependency import Release, Yank
from update_time.sources import npmjs
from update_time.sources.npmjs import (
    deprecation,
    get_changes,
    get_publication_datetime,
    newest_release,
)

from tests.helpers import patch_get
from tests.mutation import Mutation, kills
from tests.update_time.helpers import CacheClearingTestCase, LoggingTestCase


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


class GetChangesRepositoryTest(CacheClearingTestCase):
    """Unit tests for locating the changelog from npm's varied `repository` metadata."""

    def test_unreadable_repository(self):
        """Test that repository metadata that names no GitHub URL yields no changelog, without raising."""
        for package, metadata in (
            ("no_repo", {"name": "package"}),  # no `repository` field
            ("no_url", {"repository": {"type": "git"}}),
            ("url_not_a_string", {"repository": {"url": 42}}),
        ):
            with self.subTest(metadata=metadata), patch_get(metadata):
                self.assertEqual(get_changes(package, "1.0"), "")

    @patch("update_time.sources.npmjs.changes_from_release", return_value="Changelog")
    def test_repository_naming_a_github_repository(self, changes_from_release: Mock):
        """Test that every spelling of a GitHub repository npm allows is used to find the changelog."""
        for package, repository in (
            ("host_shorthand", "github:org/package"),
            ("bare_shorthand", "org/package"),
            ("https_repo", {"url": "git+https://github.com/org/package.git"}),
            ("ssh_repo", {"url": "git+ssh://git@github.com/org/package.git"}),
            ("scp_repo", {"url": "git@github.com:org/package.git"}),
        ):
            with self.subTest(repository=repository), patch_get({"repository": repository}):
                self.assertEqual(get_changes(package, "1.0"), "Changelog")
                changes_from_release.assert_called_once_with("org", "package", package, "1.0")
                changes_from_release.reset_mock()
