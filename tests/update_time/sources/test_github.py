"""GitHub unit tests."""

import unittest
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import ANY, Mock, patch

import requests

from update_time.io.log import Logger
from update_time.sources.github import (
    Release,
    changes_from_release,
    get_latest_version,
    get_release,
    github_owner_and_repository,
    github_to_raw,
    newest_publication_date,
)

from tests.update_time.helpers import (
    CacheClearingTestCase,
    LoggingTestCase,
    commits_json,
    mock_response,
    patch_get,
    release_json,
)

if TYPE_CHECKING:
    from update_time.domain.version import DependencyVersion, VersionString


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


class GetLatestVersionTest(LoggingTestCase):
    """Unit tests for getting the latest release version for a GitHub action."""

    def assert_version(self, latest: DependencyVersion, version: VersionString, changes: str, sha: str) -> None:
        """Assert that the resolved version has the given version, changelog, and commit SHA."""
        self.assertEqual(version, latest.version)
        self.assertEqual(changes, latest.changes)
        self.assertEqual(sha, latest.sha)  # The commit SHA is what the updater pins to.

    @patch("requests.get")
    def test_invalid_current_version(self, mock_get: Mock):
        """Test that an unparsable current version is returned unchanged, without querying GitHub."""
        self.assertEqual("not a version", get_latest_version("owner/repository", "not a version").version)
        mock_get.assert_not_called()

    @patch("requests.get")
    def test_unchanged(self, mock_get: Mock):
        """Test that a release matching the current version resolves to that version and its commit SHA."""
        mock_get.side_effect = [mock_response([release_json("1.0", body="changelog")]), mock_response(commits_json())]
        self.assert_version(get_latest_version("owner/repository", "1.0"), "1.0", "changelog", "sha")

    @patch("requests.get")
    def test_newer(self, mock_get: Mock):
        """Test that a newer release is resolved, with its changelog and commit SHA."""
        mock_get.side_effect = [mock_response([release_json("1.1", body="changelog")]), mock_response(commits_json())]
        self.assert_version(get_latest_version("owner/repository", "1.0"), "1.1", "changelog", "sha")

    @patch("requests.get")
    def test_publication_date(self, mock_get: Mock):
        """Test that the resolved release's publication date is captured."""
        published = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        mock_get.side_effect = [
            mock_response([release_json("1.1", published_at=published)]),
            mock_response(commits_json()),
        ]
        self.assertEqual(datetime.fromisoformat(published), get_latest_version("owner/repository", "1.0").published)

    @patch_get([release_json("0.9")])
    def test_older_release_kept(self):
        """Test that the current version is kept, without an error, when every release is older than it."""
        self.assert_version(get_latest_version("owner/older", "1.0"), "1.0", "", "")

    @patch("requests.get")
    def test_no_error_when_releases_cannot_be_fetched(self, mock_get: Mock):
        """Test that an unreachable repo logs only the fetch warning, not a redundant 'no valid version' error."""
        mock_get.return_value = mock_response([], ok=False)
        self.assertEqual("1.0", get_latest_version("owner/unreachable", "1.0").version)
        self.assert_could_not_fetch_logged(mock_get().url, mock_get().status_code)

    @patch_get([])
    def test_no_version_error_when_repo_has_no_releases(self):
        """Test that a reachable repo with no releases keeps the current version and logs a 'no valid version' error."""
        self.assertEqual("1.0", get_latest_version("owner/no releases", "1.0").version)
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, "owner/no releases")

    @patch_get([release_json("1.1", draft=True)])
    def test_skip_draft_releases(self):
        """Test that draft releases are not candidates, logging a 'no valid version' error for the reachable repo."""
        self.assertEqual("1.0", get_latest_version("owner/only a draft", "1.0").version)
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, "owner/only a draft")

    @patch_get([release_json("1.1", prerelease=True)])
    def test_skip_prerelease_releases(self):
        """Test that prerelease releases are not candidates, logging a 'no valid version' error for the repo."""
        self.assertEqual("1.0", get_latest_version("owner/only a prerelease", "1.0").version)
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, "owner/only a prerelease")

    @patch_get([release_json("invalid-1.1")])
    def test_invalid_versions(self):
        """Test that invalid versions are not candidates, logging a 'no valid version' error for the reachable repo."""
        self.assertEqual("1.0", get_latest_version("owner/invalid version", "1.0").version)
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, "owner/invalid version")

    @patch("requests.get")
    def test_no_commit_sha(self, mock_get: Mock):
        """Test that the current version is kept when the commit SHA can't be fetched for the eligible release."""
        mock_get.side_effect = [mock_response([release_json("1.1")]), mock_response({}, ok=False)]
        self.assert_version(get_latest_version("owner/no sha", "1.0"), "1.0", "", "")
        url = "https://github.com/owner/no sha/releases/tag/1.1"
        self.assert_error_logged(Logger._MESSAGE_NO_COMMIT_SHA, "owner/no sha", "1.1", url)

    @patch("requests.get")
    def test_skip_releases_within_cooldown(self, mock_get: Mock):
        """Test that a release published within the cooldown is skipped in favor of an older, eligible release."""
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        old_iso = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        mock_get.side_effect = [
            mock_response([release_json("2.0", published_at=recent), release_json("1.1", published_at=old_iso)]),
            mock_response(commits_json()),
        ]
        latest = get_latest_version("owner/with cooldown", "1.0")
        self.assert_version(latest, "1.1", "", "sha")
        self.assertEqual(datetime.fromisoformat(old_iso), latest.published)


class NewestPublicationDateTest(LoggingTestCase):
    """Unit tests for the newest release publication date of a GitHub repo."""

    @patch("requests.get")
    def test_newest_across_releases(self, mock_get: Mock):
        """Test that the most recent publication date wins, including pre-releases and ignoring undated releases."""
        newest = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        older = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        mock_get.return_value = mock_response(
            [
                release_json("2.0b1", prerelease=True, published_at=newest),
                release_json("1.1", published_at=older),
                release_json("1.0"),  # A draft-like release with no publication date is ignored.
            ]
        )
        self.assertEqual(datetime.fromisoformat(newest), newest_publication_date("owner", "active"))

    @patch_get([])
    def test_no_releases(self):
        """Test that a repo with no releases has no newest publication date."""
        self.assertIsNone(newest_publication_date("owner", "no releases"))

    @patch_get([], ok=False)
    def test_fetch_failure(self):
        """Test that no date is returned when the releases can't be fetched."""
        self.assertIsNone(newest_publication_date("owner", "unreachable"))
        self.assert_could_not_fetch_logged()


class GetReleaseTest(LoggingTestCase):
    """Unit tests for getting a release matching a specific package and version."""

    @patch_get([release_json("puppeteer-v25.1.0"), release_json("puppeteer-core-v25.0.4", body="Changelog")])
    def test_monorepo_tag_match(self):
        """Test finding a release in a monorepo where tags are prefixed with the package name."""
        release = get_release("puppeteer", "monorepo", "puppeteer-core", "25.0.4")
        self.assertEqual("puppeteer-core-v25.0.4", cast("Release", release).tag_name)
        self.assertEqual("Changelog", cast("Release", release).body)

    @patch_get([release_json("25.0.4"), release_json("v25.0.4"), release_json("puppeteer-core-v25.0.4")])
    def test_monorepo_tag_takes_precedence(self):
        """Test that the package-prefixed tag wins over the v-prefixed and bare tags for the same version.

        The competing tags are listed before the package-prefixed one, so matching by list order rather than by
        specificity would pick the wrong release.
        """
        release = get_release("puppeteer", "monorepo", "puppeteer-core", "25.0.4")
        self.assertEqual("puppeteer-core-v25.0.4", cast("Release", release).tag_name)

    @patch_get([release_json("v1.2.3")])
    def test_v_prefix_tag_match(self):
        """Test finding a release whose tag is the version prefixed with 'v'."""
        release = get_release("owner", "repo with v prefix", "any", "1.2.3")
        self.assertEqual("v1.2.3", cast("Release", release).tag_name)

    @patch_get([release_json("1.2.3")])
    def test_bare_version_tag_match(self):
        """Test finding a release whose tag is the bare version."""
        release = get_release("owner", "repo with bare version", "any", "1.2.3")
        self.assertEqual("1.2.3", cast("Release", release).tag_name)

    @patch_get([release_json("v1.0")])
    def test_no_matching_tag(self):
        """Test that None is returned when no tag matches the requested version."""
        self.assertIsNone(get_release("owner", "repo with non matching tag", "any", "1.1"))

    @patch("requests.get")
    def test_repo_without_releases(self, mock_get: Mock):
        """Test that None is returned when the repository can't be reached."""
        mock_get.return_value = mock_response([], ok=False)
        self.assertIsNone(get_release("owner", "repo without releases for get_release", "any", "1.0"))
        self.assert_could_not_fetch_logged(mock_get().url, mock_get().status_code)

    @patch("requests.get")
    def test_timeout(self, mock_get: Mock):
        """Test that None is returned when the repository can't be reached."""
        mock_get.side_effect = requests.exceptions.Timeout
        self.assertIsNone(get_release("owner", "repo without releases for get_release", "any", "1.0"))
        url = "https://api.github.com/repos/owner/repo without releases for get_release/releases?per_page=100"
        self.mock_warning.assert_called_once_with(Logger._MESSAGE_TIMEOUT, url, stacklevel=ANY)


class ChangesFromReleaseTest(CacheClearingTestCase):
    """Unit tests for getting the changelog from a GitHub release."""

    @patch("requests.get")
    def test_no_owner_or_repository(self, mock_get: Mock):
        """Test that the changes are empty, without querying GitHub, when the owner or repository is missing."""
        self.assertEqual("", changes_from_release("", "", "any", "1.0"))
        mock_get.assert_not_called()

    @patch_get([release_json("1.1", body="Changelog")])
    def test_changelog(self):
        """Test that the body of the matching release is returned."""
        self.assertEqual("Changelog", changes_from_release("owner", "repo with changes", "any", "1.1"))

    @patch_get([release_json("9.9")])
    def test_no_matching_release(self):
        """Test that the changes are empty when no release matches the version."""
        self.assertEqual("", changes_from_release("owner", "repo without matching release", "any", "1.1"))
