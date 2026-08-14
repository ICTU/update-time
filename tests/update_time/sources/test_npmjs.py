"""npmjs unit tests."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock, patch

from update_time.domain.dependency import Yank
from update_time.sources.npmjs import (
    deprecation,
    get_changes,
    get_publication_datetime,
    newest_publication_date,
    newest_release,
)

from tests.helpers import patch_get
from tests.update_time.helpers import CacheClearingTestCase, LoggingTestCase

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyVersion


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

    @patch_get({"time": {}})
    def test_get_publication_datetime_for_unlisted_version(self):
        """Test that a version the registry doesn't list raises KeyError."""
        with self.assertRaises(KeyError):
            get_publication_datetime("package", "9.9")

    @patch_get(ok=False)
    def test_get_changes_when_unreachable(self):
        """Test that an unreachable registry yields no changelog instead of crashing."""
        self.assertEqual(get_changes("package", "1.0"), "")


class NpmjsNewestPublicationDateTest(LoggingTestCase):
    """Unit tests for the newest publication date across a package's versions."""

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
    def test_newest_ignores_bookkeeping_entries(self):
        """Test that the newest version's date is returned, ignoring the `created`/`modified` entries."""
        self.assertEqual(datetime(2024, 6, 1, tzinfo=UTC), newest_publication_date("package"))

    @patch_get(ok=False)
    def test_unreachable(self):
        """Test that an unreachable registry yields no date instead of crashing."""
        self.assertIsNone(newest_publication_date("package"))


class NpmjsNewestReleaseTest(LoggingTestCase):
    """Unit tests for the newest release (version + publication date) fetcher."""

    @patch_get({"dist-tags": {"latest": "2.0"}, "time": {"1.0": "2020-01-01T00:00:00Z", "2.0": "2024-06-01T00:00:00Z"}})
    def test_newest_release(self):
        """Test that the `latest` dist-tag and its publication date are returned as a DependencyVersion."""
        release = cast("DependencyVersion", newest_release("package"))
        self.assertEqual(release.version, "2.0")
        self.assertEqual(datetime(2024, 6, 1, tzinfo=UTC), release.newest_published)

    @patch_get(ok=False)
    def test_no_latest_tag(self):
        """Test that a package with no `latest` dist-tag (e.g. unreachable) yields None."""
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
