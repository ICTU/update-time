"""npmjs unit tests."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock, patch

from update_time.sources.npmjs import get_changes, get_publication_datetime, newest_publication_date, newest_release

from tests.update_time.helpers import CacheClearingTestCase, mock_response

if TYPE_CHECKING:
    from update_time.domain.version import DependencyVersion


class NpmjsPublicationDatetimeTest(CacheClearingTestCase):
    """Unit tests for the npmjs publication datetime fetcher."""

    @patch("requests.get", Mock(return_value=mock_response({"time": {"1.0": "20260530T10:14:40.567Z"}})))
    def test_get_publication_datetime(self):
        """Test that the publication datetime can be fetched."""
        publication_datetime = datetime(2026, 5, 30, 10, 14, 40, 567000, tzinfo=UTC)
        self.assertEqual(publication_datetime, get_publication_datetime("package", "1.0"))

    @patch("logging.Logger.warning", Mock())
    @patch("requests.get", Mock(return_value=mock_response(ok=False)))
    def test_get_publication_datetime_when_unreachable(self):
        """Test that an unreachable registry yields no publication date instead of crashing."""
        self.assertIsNone(get_publication_datetime("package", "1.0"))

    @patch("requests.get", Mock(return_value=mock_response({"time": {}})))
    def test_get_publication_datetime_for_unlisted_version(self):
        """Test that a version the registry doesn't list raises KeyError.

        Callers that may ask for a version outside the registry's `time` map (e.g. the jsdelivr updater) rely on
        this and catch it, so the contract is pinned here at the source that owns it.
        """
        with self.assertRaises(KeyError):
            get_publication_datetime("package", "9.9")

    @patch("logging.Logger.warning", Mock())
    @patch("requests.get", Mock(return_value=mock_response(ok=False)))
    def test_get_changes_when_unreachable(self):
        """Test that an unreachable registry yields no changelog instead of crashing."""
        self.assertEqual("", get_changes("package", "1.0"))


class NpmjsNewestPublicationDateTest(CacheClearingTestCase):
    """Unit tests for the newest publication date across a package's versions."""

    @patch("requests.get")
    def test_newest_ignores_bookkeeping_entries(self, mock_get: Mock):
        """Test that the newest version's date is returned, ignoring the `created`/`modified` entries."""
        time_map = {
            "created": "2019-01-01T00:00:00Z",
            "modified": "2030-01-01T00:00:00Z",  # later than any version, but bookkeeping — must be ignored
            "1.0": "2020-01-01T00:00:00Z",
            "1.1": "2024-06-01T00:00:00Z",
        }
        mock_get.return_value = mock_response({"time": time_map})
        self.assertEqual(datetime(2024, 6, 1, tzinfo=UTC), newest_publication_date("package"))

    @patch("logging.Logger.warning", Mock())
    @patch("requests.get", Mock(return_value=mock_response(ok=False)))
    def test_unreachable(self):
        """Test that an unreachable registry yields no date instead of crashing."""
        self.assertIsNone(newest_publication_date("package"))


class NpmjsNewestReleaseTest(CacheClearingTestCase):
    """Unit tests for the newest release (version + publication date) fetcher."""

    @patch("requests.get")
    def test_newest_release(self, mock_get: Mock):
        """Test that the `latest` dist-tag and its publication date are returned as a DependencyVersion."""
        mock_get.return_value = mock_response(
            {"dist-tags": {"latest": "2.0"}, "time": {"1.0": "2020-01-01T00:00:00Z", "2.0": "2024-06-01T00:00:00Z"}}
        )
        release = cast("DependencyVersion", newest_release("package"))
        self.assertEqual("2.0", release.version)
        self.assertEqual(datetime(2024, 6, 1, tzinfo=UTC), release.newest_published)

    @patch("logging.Logger.warning", Mock())
    @patch("requests.get", Mock(return_value=mock_response(ok=False)))
    def test_no_latest_tag(self):
        """Test that a package with no `latest` dist-tag (e.g. unreachable) yields None."""
        self.assertIsNone(newest_release("package"))


class GetChangesRepositoryTest(CacheClearingTestCase):
    """Unit tests for locating the changelog from npm's varied `repository` metadata."""

    @patch("requests.get")
    def test_missing_repository(self, mock_get: Mock):
        """Test that a package with no repository metadata yields no changelog instead of crashing."""
        mock_get.return_value = mock_response({"name": "package"})  # no `repository` field
        self.assertEqual("", get_changes("no_repo", "1.0"))

    @patch("requests.get")
    def test_string_repository_shorthand(self, mock_get: Mock):
        """Test that a repository given as an npm shorthand string doesn't crash the changelog lookup."""
        mock_get.return_value = mock_response({"repository": "github:org/package"})
        self.assertEqual("", get_changes("string_repo", "1.0"))

    @patch("requests.get")
    def test_repository_object_without_url(self, mock_get: Mock):
        """Test that a repository object without a url yields no changelog instead of crashing."""
        mock_get.return_value = mock_response({"repository": {"type": "git"}})
        self.assertEqual("", get_changes("no_url", "1.0"))

    @patch("update_time.sources.npmjs.changes_from_release", Mock(return_value="Changelog"))
    @patch("requests.get")
    def test_repository_object_url_is_used(self, mock_get: Mock):
        """Test that a repository object's url is parsed and used to find the changelog."""
        mock_get.return_value = mock_response({"repository": {"url": "git+https://github.com/org/package.git"}})
        self.assertEqual("Changelog", get_changes("with_repo", "1.0"))
