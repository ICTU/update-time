"""npmjs unit tests."""

from datetime import UTC, datetime
from unittest.mock import Mock, patch

from update_time.sources.npmjs import get_changes, get_publication_datetime

from tests.update_time.helpers import CacheClearingTestCase, mock_response


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

    @patch("logging.Logger.warning", Mock())
    @patch("requests.get", Mock(return_value=mock_response(ok=False)))
    def test_get_changes_when_unreachable(self):
        """Test that an unreachable registry yields no changelog instead of crashing."""
        self.assertEqual("", get_changes("package", "1.0"))


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
