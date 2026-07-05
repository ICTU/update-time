"""GitHub unit tests."""

import logging
import unittest
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import ANY, Mock, patch

import requests

from update_time.sources.github import (
    Release,
    changes_from_release,
    get_latest_release,
    get_release,
    github_owner_and_repository,
    github_to_raw,
)

from tests.update_time.helpers import CacheClearingTestCase, commits_json, mock_response, release_json


def assert_fetch_warning(mock_warning: Mock, mock_get: Mock) -> None:
    """Assert that exactly one 'could not fetch' warning was logged for the mocked response's URL and status."""
    mock_warning.assert_called_once_with(
        "Could not fetch %s: %s", mock_get().url, mock_get().status_code, stacklevel=ANY
    )


class GitHubURLtoRawTest(unittest.TestCase):
    """Unit tests for the GitHub URL to raw URL function."""

    def test_non_github_url(self):
        """Test that non-GitHub URLs are unchanged."""
        non_github_url = "https://notgithub.com/blob/example.md"
        self.assertEqual(non_github_url, github_to_raw(non_github_url))

    def test_github_url_with_blob(self):
        """Test that a GitHub blob URL is rewritten to its raw URL, dropping the `/blob` segment."""
        github_url = "https://github.com/user/repo/blob/example.md"
        self.assertEqual("https://raw.githubusercontent.com/user/repo/example.md", github_to_raw(github_url))

    def test_github_url_without_blob(self):
        """Test that a GitHub URL without a `/blob` segment is rewritten to its raw URL."""
        github_url = "https://github.com/user/repo/example.md"
        self.assertEqual("https://raw.githubusercontent.com/user/repo/example.md", github_to_raw(github_url))


class GitHubOwnerAndRepositoryTest(unittest.TestCase):
    """Unit tests for the GitHub owner and repository parse function."""

    def test_non_github_url(self):
        """Test that non-GitHub URLs return an empty owner and repository."""
        self.assertEqual(("", ""), github_owner_and_repository("https://example.org"))

    def test_github_url(self):
        """Test that a GitHub URL returns an owner and repository."""
        self.assertEqual(("ICTU", "quality-time"), github_owner_and_repository("https://github.com/ICTU/quality-time"))

    def test_github_url_without_repo(self):
        """Test that a GitHub URL returns an empty owner and repository if the repository is missing."""
        self.assertEqual(("", ""), github_owner_and_repository("https://github.com/ICTU"))

    def test_npm_git_url(self):
        """Test that an npm-style git+https URL with a .git suffix is parsed."""
        self.assertEqual(
            ("ICTU", "update-time"), github_owner_and_repository("git+https://github.com/ICTU/update-time.git")
        )

    def test_url_with_fragment(self):
        """Test that a URL with a trailing fragment (as npm uses) is parsed."""
        self.assertEqual(
            ("ICTU", "update-time"), github_owner_and_repository("https://github.com/ICTU/update-time#readme")
        )


class GetLatestReleaseTest(CacheClearingTestCase):
    """Unit tests for getting the latest release for a GitHub repo."""

    @classmethod
    def setUpClass(cls) -> None:
        """Silence the 'no valid version' error that the release-filtering tests trigger by design.

        The tests that assert on that error patch the logger explicitly, which keeps working while disabled.
        """
        logging.disable(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls) -> None:
        """Re-enable logging for the rest of the suite."""
        logging.disable(logging.NOTSET)

    @patch("requests.get")
    def test_get_latest_release(self, mock_get: Mock):
        """Test getting the latest release."""
        mock_get.side_effect = [mock_response([release_json("1.0")]), mock_response(commits_json())]
        release = get_latest_release("owner", "repository")
        self.assertEqual(Release(owner="owner", repository="repository", tag_name="1.0"), release)
        self.assertEqual("sha", cast("Release", release).commit_sha)

    @patch("logging.Logger.error")
    @patch("logging.Logger.warning")
    @patch("requests.get")
    def test_no_error_when_releases_cannot_be_fetched(self, mock_get: Mock, mock_warning: Mock, mock_error: Mock):
        """Test that an unreachable repo logs only the fetch warning, not a redundant 'no valid version' error."""
        mock_get.return_value = mock_response([], ok=False)
        self.assertIsNone(get_latest_release("owner", "unreachable repository"))
        assert_fetch_warning(mock_warning, mock_get)
        mock_error.assert_not_called()

    @patch("logging.Logger.error")
    @patch("requests.get", Mock(return_value=mock_response([])))
    def test_no_version_error_when_repo_has_no_releases(self, mock_error: Mock):
        """Test that a reachable repo with no eligible releases logs a 'no valid version' error."""
        self.assertIsNone(get_latest_release("owner", "repository without releases"))
        mock_error.assert_called_once_with(
            "No valid version found for %s", "owner/repository without releases", stacklevel=ANY
        )

    @patch("requests.get", Mock(return_value=mock_response([release_json("1.0", draft=True)])))
    def test_skip_draft_releases(self):
        """Test that draft releases are not included."""
        self.assertIsNone(get_latest_release("owner", "repository with only a draft release"))

    @patch("requests.get", Mock(return_value=mock_response([release_json("1.0", prerelease=True)])))
    def test_skip_prerelease_releases(self):
        """Test that prerelease releases are not included."""
        self.assertIsNone(get_latest_release("owner", "repository with only a prerelease release"))

    @patch("requests.get", Mock(return_value=mock_response([release_json("invalid-1.0")])))
    def test_invalid_versions(self):
        """Test that invalid versions are not included."""
        self.assertIsNone(get_latest_release("owner", "repository with a invalid version"))

    @patch("logging.Logger.error")
    @patch("requests.get")
    def test_http_error_on_commits_endpoint(self, mock_get: Mock, mock_error: Mock):
        """Test that reading commit_sha returns an empty string and logs an error when the commits endpoint fails."""
        mock_get.side_effect = [
            mock_response([release_json("1.0")]),
            mock_response({}, ok=False),
        ]
        release = get_latest_release("owner", "repository 2")
        self.assertIsNotNone(release)
        self.assertIsNone(cast("Release", release).commit_sha)
        mock_error.assert_called_once_with(
            "Could not fetch commit SHA for %s %s: %s",
            "owner/repository 2",
            "1.0",
            "https://github.com/owner/repository 2/releases/tag/1.0",
            stacklevel=ANY,
        )

    @patch("requests.get")
    def test_skip_releases_within_cooldown(self, mock_get: Mock):
        """Test that releases published within the cooldown period are skipped in favor of older releases."""
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        old_iso = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        mock_get.return_value = mock_response(
            [
                release_json("2.0", published_at=recent),
                release_json("1.0", published_at=old_iso),
            ]
        )
        release = get_latest_release("owner", "repository with cooldown")
        self.assertEqual(
            Release(
                owner="owner",
                repository="repository with cooldown",
                tag_name="1.0",
                published_at=datetime.fromisoformat(old_iso),
            ),
            release,
        )


class GetReleaseTest(CacheClearingTestCase):
    """Unit tests for getting a release matching a specific package and version."""

    @patch("requests.get")
    def test_monorepo_tag_match(self, mock_get: Mock):
        """Test finding a release in a monorepo where tags are prefixed with the package name."""
        mock_get.return_value = mock_response(
            [
                release_json("puppeteer-v25.1.0"),
                release_json("puppeteer-core-v25.0.4", body="Changelog"),
            ]
        )
        release = get_release("puppeteer", "monorepo", "puppeteer-core", "25.0.4")
        self.assertEqual("puppeteer-core-v25.0.4", cast("Release", release).tag_name)
        self.assertEqual("Changelog", cast("Release", release).body)

    @patch("requests.get")
    def test_monorepo_tag_takes_precedence(self, mock_get: Mock):
        """Test that the package-prefixed tag wins over the v-prefixed and bare tags for the same version.

        The competing tags are listed before the package-prefixed one, so matching by list order rather than by
        specificity would pick the wrong release.
        """
        mock_get.return_value = mock_response(
            [
                release_json("25.0.4"),
                release_json("v25.0.4"),
                release_json("puppeteer-core-v25.0.4"),
            ]
        )
        release = get_release("puppeteer", "monorepo", "puppeteer-core", "25.0.4")
        self.assertEqual("puppeteer-core-v25.0.4", cast("Release", release).tag_name)

    @patch("requests.get", Mock(return_value=mock_response([release_json("v1.2.3")])))
    def test_v_prefix_tag_match(self):
        """Test finding a release whose tag is the version prefixed with 'v'."""
        release = get_release("owner", "repo with v prefix", "any", "1.2.3")
        self.assertEqual("v1.2.3", cast("Release", release).tag_name)

    @patch("requests.get", Mock(return_value=mock_response([release_json("1.2.3")])))
    def test_bare_version_tag_match(self):
        """Test finding a release whose tag is the bare version."""
        release = get_release("owner", "repo with bare version", "any", "1.2.3")
        self.assertEqual("1.2.3", cast("Release", release).tag_name)

    @patch("requests.get", Mock(return_value=mock_response([release_json("v1.0")])))
    def test_no_matching_tag(self):
        """Test that None is returned when no tag matches the requested version."""
        self.assertIsNone(get_release("owner", "repo with non matching tag", "any", "1.1"))

    @patch("logging.Logger.warning")
    @patch("requests.get")
    def test_repo_without_releases(self, mock_get: Mock, mock_warning: Mock):
        """Test that None is returned when the repository can't be reached."""
        mock_get.return_value = mock_response([], ok=False)
        self.assertIsNone(get_release("owner", "repo without releases for get_release", "any", "1.0"))
        assert_fetch_warning(mock_warning, mock_get)

    @patch("logging.Logger.warning")
    @patch("requests.get")
    def test_timeout(self, mock_get: Mock, mock_warning: Mock):
        """Test that None is returned when the repository can't be reached."""
        mock_get.side_effect = requests.exceptions.Timeout
        self.assertIsNone(get_release("owner", "repo without releases for get_release", "any", "1.0"))
        mock_warning.assert_called_once_with(
            "Timeout while fetching %s",
            "https://api.github.com/repos/owner/repo without releases for get_release/releases?per_page=100",
            stacklevel=ANY,
        )


class ChangesFromReleaseTest(CacheClearingTestCase):
    """Unit tests for getting the changelog from a GitHub release."""

    @patch("requests.get")
    def test_no_owner_or_repository(self, mock_get: Mock):
        """Test that the changes are empty, without querying GitHub, when the owner or repository is missing."""
        self.assertEqual("", changes_from_release("", "", "any", "1.0"))
        mock_get.assert_not_called()

    @patch("requests.get", Mock(return_value=mock_response([release_json("1.1", body="Changelog")])))
    def test_changelog(self):
        """Test that the body of the matching release is returned."""
        self.assertEqual("Changelog", changes_from_release("owner", "repo with changes", "any", "1.1"))

    @patch("requests.get", Mock(return_value=mock_response([release_json("9.9")])))
    def test_no_matching_release(self):
        """Test that the changes are empty when no release matches the version."""
        self.assertEqual("", changes_from_release("owner", "repo without matching release", "any", "1.1"))
