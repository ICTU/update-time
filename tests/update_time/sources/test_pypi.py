"""Unit tests for the PyPI module."""

import logging
from datetime import UTC, datetime
from http import HTTPStatus
from unittest.mock import Mock, patch

from update_time.sources.pypi import (
    CHANGELOG_URL_KEYS,
    REPOSITORY_URL_KEYS,
    get_changes,
    get_latest_version,
    get_publication_datetime,
)

from tests.update_time.helpers import CacheClearingTestCase, mock_response, release_json


@patch("requests.get")
class GetChangesTest(CacheClearingTestCase):
    """Unit tests for getting the changes."""

    @classmethod
    def setUpClass(cls) -> None:
        """Override to disable logging."""
        logging.disable(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls) -> None:
        """Override to enable logging."""
        logging.disable(logging.NOTSET)

    def create_mock_response(
        self, mock_get: Mock, *json: dict | list, text: str = "", status_code: int = HTTPStatus.OK
    ) -> None:
        """Create a mock response for the mock requests.get method with the JSON result."""
        response = Mock()
        response.json.side_effect = list(json)
        response.text = text
        response.status_code = status_code
        response.ok = status_code < HTTPStatus.BAD_REQUEST
        response.headers = {"Content-Type": "text/text"}
        mock_get.return_value = response

    def test_no_url_found(self, mock_get: Mock):
        """Test that the changes are empty if no changelog URL is returned by PyPI."""
        self.create_mock_response(mock_get, {"info": {"description": "Package-foo description"}})
        self.assertEqual("", get_changes("package-1", "1.0"))

    def test_changelog_url_found(self, mock_get: Mock):
        """Test that the changes are returned if a changelog URL is returned by PyPI."""
        changelog = "Changelog\n## 1.1\n- Fixed foo\n"
        for key in CHANGELOG_URL_KEYS:
            self.create_mock_response(mock_get, {"info": {"project_urls": {key: "https://changes"}}}, text=changelog)
            self.assertEqual("## 1.1\n- Fixed foo", get_changes(f"package-2-{key}", "1.1"))

    def test_changelog_url_gives_error(self, mock_get: Mock):
        """Test that changelog URLs are skipped if they result in an HTTP error."""
        for status_code in (HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND):
            for key in CHANGELOG_URL_KEYS:
                self.create_mock_response(
                    mock_get,
                    {"info": {"description": "Package-foo description", "project_urls": {key: "https://changes"}}},
                    status_code=status_code,
                )
                self.assertEqual("", get_changes(f"package-3-{status_code}-{key}", "1.1"))

    def test_repository_url_found(self, mock_get: Mock):
        """Test that the changes are returned if a repository URL is returned by PyPI."""
        changelog = "Changelog\n## 1.1\n- Fixed foo\n"
        repo = "https://github.com/org/repo"
        docs = "https://docs"
        for key in REPOSITORY_URL_KEYS:
            self.create_mock_response(
                mock_get,
                {"info": {"project_urls": {"docs": docs, key: repo}}},
                [release_json("1.1", body=changelog)],
                {"sha": "sha"},
            )
            self.assertEqual(changelog, get_changes(f"package-4-{key}", "1.1"))

    def test_changelog_in_description(self, mock_get: Mock):
        """Test that the changelog from the description is returned."""
        changelog = "1.1\n- Fixed ...\n- Added ..."
        self.create_mock_response(mock_get, {"info": {"description": f"Package description\n{changelog}\n"}})
        self.assertEqual(changelog, get_changes("package-5", "1.1"))

    def test_github_url_in_description_that_has_a_changelog(self, mock_get: Mock):
        """Test that the GitHub URL in the description is used to get the changelog."""
        github_url = "https://github.com/org/bar"
        changelog = "1.1\n- Fixed ...\n- Added ..."
        self.create_mock_response(
            mock_get,
            {"info": {"description": f"Package description\n{github_url}\n"}},
            [release_json("1.1", body=changelog)],
            {"sha": "sha"},
        )
        self.assertEqual(changelog, get_changes("package-6", "1.1"))

    def test_github_url_in_description_that_has_no_changelog(self, mock_get: Mock):
        """Test that the GitHub URL in the description is used to get the changelog."""
        github_url = "https://github.com/org/baz"
        self.create_mock_response(
            mock_get,
            {"info": {"description": f"Package description\n{github_url}\n"}},
            [release_json("1.1")],
            {"sha": "sha"},
        )
        self.assertEqual("", get_changes("package-7", "1.1"))


class GetPublicationDateTimeTest(CacheClearingTestCase):
    """Unit tests for getting the publication date time of releases."""

    @patch("requests.get")
    def test_publication_datetime(self, mock_get: Mock):
        """Test that the publication datetime is returned."""
        mock_get.return_value = mock_response({"urls": [{"upload_time_iso_8601": "2026-05-30T12:00:03.678901Z"}]})
        published = datetime(2026, 5, 30, 12, 0, 3, 678901, tzinfo=UTC)
        self.assertEqual(published, get_publication_datetime("package", "1.0"))

    @patch("requests.get")
    def test_max_publication_datetime(self, mock_get: Mock):
        """Test that the latest publication datetime is returned if there are multiple distributions."""
        mock_get.return_value = mock_response(
            {
                "urls": [
                    {"upload_time_iso_8601": "2026-05-30T12:00:03.678901Z"},
                    {"upload_time_iso_8601": "2026-05-30T12:00:03.543210Z"},
                ]
            }
        )
        published = datetime(2026, 5, 30, 12, 0, 3, 678901, tzinfo=UTC)
        self.assertEqual(published, get_publication_datetime("package", "1.0"))


@patch("requests.get")
class GetLatestVersionTest(CacheClearingTestCase):
    """Unit tests for getting the latest version from PyPI."""

    OLD = "2020-01-01T00:00:00.000000Z"  # Well outside the cooldown window.

    def versions(self, *versions: str) -> Mock:
        """Return a mock Index API response listing the given version strings."""
        return mock_response({"versions": list(versions)})

    def release(self, upload_time: str = OLD, *, yanked: bool = False) -> Mock:
        """Return a mock per-version metadata response (with an empty changelog)."""
        urls = [{"upload_time_iso_8601": upload_time}] if upload_time else []
        return mock_response({"info": {"description": "", "yanked": yanked}, "urls": urls})

    def test_invalid_current_version(self, mock_get: Mock):
        """Test that an invalid current version is returned unchanged without querying PyPI."""
        self.assertEqual("not a version", get_latest_version("package", "not a version").version)
        mock_get.assert_not_called()

    def test_no_newer_version(self, mock_get: Mock):
        """Test that the current version is returned when it is already the latest."""
        mock_get.side_effect = [self.versions("1.0")]
        self.assertEqual("1.0", get_latest_version("no_newer", "1.0").version)

    def test_new_version(self, mock_get: Mock):
        """Test that the latest version is returned, with its publication date."""
        mock_get.side_effect = [self.versions("1.0", "1.1"), self.release()]
        latest = get_latest_version("new_version", "1.0")
        self.assertEqual("1.1", latest.version)
        self.assertEqual(datetime(2020, 1, 1, tzinfo=UTC), latest.published)

    def test_highest_version(self, mock_get: Mock):
        """Test that the highest of multiple newer versions is returned."""
        mock_get.side_effect = [self.versions("1.0", "1.2", "1.1"), self.release()]
        self.assertEqual("1.2", get_latest_version("highest", "1.0").version)

    def test_prerelease_ignored(self, mock_get: Mock):
        """Test that pre-releases are ignored without fetching their metadata."""
        mock_get.side_effect = [self.versions("1.0", "2.0b1")]
        self.assertEqual("1.0", get_latest_version("prerelease", "1.0").version)

    def test_yanked_release_ignored(self, mock_get: Mock):
        """Test that yanked releases are ignored."""
        mock_get.side_effect = [self.versions("1.0", "1.1"), self.release(yanked=True)]
        self.assertEqual("1.0", get_latest_version("yanked", "1.0").version)

    def test_release_without_files_ignored(self, mock_get: Mock):
        """Test that releases without distribution files are ignored."""
        mock_get.side_effect = [self.versions("1.0", "1.1"), self.release(upload_time="")]
        self.assertEqual("1.0", get_latest_version("no_files", "1.0").version)

    def test_invalid_release_ignored(self, mock_get: Mock):
        """Test that releases with an invalid version are ignored without fetching their metadata."""
        mock_get.side_effect = [self.versions("1.0", "not-a-version")]
        self.assertEqual("1.0", get_latest_version("invalid_release", "1.0").version)

    def test_release_within_cooldown_ignored(self, mock_get: Mock):
        """Test that a release published within the cooldown period is held back."""
        recent = datetime.now(UTC).isoformat()
        mock_get.side_effect = [self.versions("1.0", "1.1"), self.release(recent)]
        self.assertEqual("1.0", get_latest_version("cooldown", "1.0").version)
