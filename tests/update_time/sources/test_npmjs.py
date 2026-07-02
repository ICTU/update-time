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
