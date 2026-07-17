"""Unit tests for the PyPI module."""

from datetime import UTC, datetime
from http import HTTPStatus
from unittest.mock import Mock, patch

from update_time.domain.version import NO_BOUND, Verb
from update_time.sources.pypi import (
    CHANGELOG_URL_KEYS,
    REPOSITORY_URL_KEYS,
    changelog_from_url,
    get_changes,
    get_latest_version,
    get_publication_datetime,
    newest_publication_date,
)

from tests.update_time.helpers import (
    PYPI_OLD_UPLOAD,
    CacheClearingTestCase,
    LoggingTestCase,
    bound,
    github_commits_json,
    github_release_json,
    mock_response,
    patch_get,
    pypi_index,
    pypi_release,
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

    def test_no_url_found(self, mock_get: Mock):
        """Test that the changes are empty if no changelog URL is returned by PyPI."""
        self.create_mock_response(mock_get, {"info": {"description": "Package-foo description"}})
        self.assertEqual(get_changes("package-1", "1.0"), "")

    def test_changelog_url_found(self, mock_get: Mock):
        """Test that the changes are returned if a changelog URL is returned by PyPI."""
        changelog = "Changelog\n## 1.1\n- Fixed foo\n"
        for key in CHANGELOG_URL_KEYS:
            self.create_mock_response(mock_get, {"info": {"project_urls": {key: "https://changes"}}}, text=changelog)
            self.assertEqual(get_changes(f"package-2-{key}", "1.1"), "## 1.1\n- Fixed foo")

    def test_changelog_url_gives_error(self, mock_get: Mock):
        """Test that changelog URLs are skipped if they result in an HTTP error."""
        for status_code in (HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND):
            for key in CHANGELOG_URL_KEYS:
                self.create_mock_response(
                    mock_get,
                    {"info": {"description": "Package-foo description", "project_urls": {key: "https://changes"}}},
                    status_code=status_code,
                )
                self.assertEqual(get_changes(f"package-3-{status_code}-{key}", "1.1"), "")

    def test_repository_url_found(self, mock_get: Mock):
        """Test that the changes are returned if a repository URL is returned by PyPI."""
        changelog = "Changelog\n## 1.1\n- Fixed foo\n"
        repo = "https://github.com/org/repo"
        docs = "https://docs"
        for key in REPOSITORY_URL_KEYS:
            self.create_mock_response(
                mock_get,
                {"info": {"project_urls": {"docs": docs, key: repo}}},
                [github_release_json("1.1", body=changelog)],
                github_commits_json(),
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
            [github_release_json("1.1", body=changelog)],
            github_commits_json(),
        )
        self.assertEqual(changelog, get_changes("package-6", "1.1"))

    def test_github_url_in_description_that_has_no_changelog(self, mock_get: Mock):
        """Test that the GitHub URL in the description is used to get the changelog."""
        github_url = "https://github.com/org/baz"
        self.create_mock_response(
            mock_get,
            {"info": {"description": f"Package description\n{github_url}\n"}},
            [github_release_json("1.1")],
            github_commits_json(),
        )
        self.assertEqual(get_changes("package-7", "1.1"), "")

    def test_changelog_url_unreachable(self, mock_get: Mock):
        """Test that an unreachable changelog URL yields an empty changelog instead of crashing."""
        self.create_mock_response(mock_get, status_code=HTTPStatus.NOT_FOUND)
        self.assertEqual(changelog_from_url("https://changes", "1.0"), "")


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
    requires.
    """

    def test_invalid_current_version(self, mock_get: Mock):
        """Test that an invalid current version is returned unchanged without querying PyPI."""
        self.assertEqual(get_latest_version("package", "not a version", NO_BOUND).version, "not a version")
        mock_get.assert_not_called()

    def test_no_newer_version(self, mock_get: Mock):
        """Test that the current version is returned when it is already the latest."""
        mock_get.side_effect = [pypi_index("1.0")]
        self.assertEqual(get_latest_version("no_newer", "1.0", NO_BOUND).version, "1.0")

    def test_new_version(self, mock_get: Mock):
        """Test that the latest version is returned, with its publication date."""
        mock_get.side_effect = [pypi_index("1.0", "1.1"), pypi_release()]
        latest = get_latest_version("new_version", "1.0", NO_BOUND)
        self.assertEqual(latest.version, "1.1")
        self.assertEqual(datetime(2020, 1, 1, tzinfo=UTC), latest.published)

    def test_highest_version(self, mock_get: Mock):
        """Test that the highest of multiple newer versions is returned."""
        mock_get.side_effect = [pypi_index("1.0", "1.2", "1.1"), pypi_release()]
        self.assertEqual(get_latest_version("highest", "1.0", NO_BOUND).version, "1.2")

    def test_newest_published_attached(self, mock_get: Mock):
        """Test that the newest release date is attached, so an up-to-date pin can still be flagged as stale."""
        mock_get.side_effect = [pypi_index("1.0", files=[{"upload-time": PYPI_OLD_UPLOAD}])]
        latest = get_latest_version("stale", "1.0", NO_BOUND)
        self.assertEqual(latest.version, "1.0")
        self.assertEqual(datetime(2020, 1, 1, tzinfo=UTC), latest.newest_published)

    def test_prerelease_ignored(self, mock_get: Mock):
        """Test that pre-releases are ignored without fetching their metadata."""
        mock_get.side_effect = [pypi_index("1.0", "2.0b1")]
        self.assertEqual(get_latest_version("prerelease", "1.0", NO_BOUND).version, "1.0")

    def test_version_filter_bounds_candidates(self, mock_get: Mock):
        """Test that a version filter drops out-of-bound candidates so a bounded version wins over a higher one."""
        mock_get.side_effect = [pypi_index("1.0", "1.9", "2.0"), pypi_release()]
        version_filter = bound(Verb.ALLOW, "update<2")
        self.assertEqual(get_latest_version("bounded", "1.0", version_filter).version, "1.9")

    def test_yanked_release_ignored(self, mock_get: Mock):
        """Test that yanked releases are ignored."""
        mock_get.side_effect = [pypi_index("1.0", "1.1"), pypi_release(yanked=True)]
        self.assertEqual(get_latest_version("yanked", "1.0", NO_BOUND).version, "1.0")

    def test_release_without_files_ignored(self, mock_get: Mock):
        """Test that releases without distribution files are ignored."""
        mock_get.side_effect = [pypi_index("1.0", "1.1"), pypi_release(upload_time="")]
        self.assertEqual(get_latest_version("no_files", "1.0", NO_BOUND).version, "1.0")

    def test_release_metadata_unavailable_ignored(self, mock_get: Mock):
        """Test that a candidate whose metadata can't be fetched is skipped instead of crashing the run."""
        mock_get.side_effect = [pypi_index("1.0", "1.1"), mock_response(ok=False)]
        self.assertEqual(get_latest_version("metadata_error", "1.0", NO_BOUND).version, "1.0")

    def test_invalid_release_ignored(self, mock_get: Mock):
        """Test that releases with an invalid version are ignored without fetching their metadata."""
        mock_get.side_effect = [pypi_index("1.0", "not-a-version")]
        self.assertEqual(get_latest_version("invalid_release", "1.0", NO_BOUND).version, "1.0")

    def test_release_within_cooldown_ignored(self, mock_get: Mock):
        """Test that a release published within the cooldown period is held back."""
        recent = datetime.now(UTC).isoformat()
        mock_get.side_effect = [pypi_index("1.0", "1.1"), pypi_release(recent)]
        self.assertEqual(get_latest_version("cooldown", "1.0", NO_BOUND).version, "1.0")
