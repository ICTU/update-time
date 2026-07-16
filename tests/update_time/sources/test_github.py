"""GitHub unit tests."""

import unittest
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import ANY, Mock, patch

import requests
from packaging.specifiers import SpecifierSet

from update_time.domain.version import NO_BOUND, VersionFilter
from update_time.io.log import Logger
from update_time.sources.github import (
    TaggedVersion,
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
    github_commits_json,
    github_release_json,
    github_tag_json,
    mock_response,
    patch_get,
    patch_github,
)

if TYPE_CHECKING:
    from update_time.domain.version import DependencyVersion, VersionString

OLD_DATE = datetime.now(UTC) - timedelta(days=10)  # A publication date outside the cooldown window
OLD_ISO = OLD_DATE.isoformat()
RECENT_DATE = datetime.now(UTC) - timedelta(days=1)  # A publication date within the cooldown window
RECENT_ISO = RECENT_DATE.isoformat()


class GitHubURLtoRawTest(unittest.TestCase):
    """Unit tests for the GitHub URL to raw URL function."""

    def test_non_github_url(self):
        """Test that non-GitHub URLs are unchanged."""
        non_github_url = "https://notgithub.com/blob/example.md"
        self.assertEqual(non_github_url, github_to_raw(non_github_url))

    def test_github_url_with_blob(self):
        """Test that a GitHub blob URL is rewritten to its raw URL, dropping the `/blob` segment."""
        github_url = "https://github.com/user/repo/blob/example.md"
        self.assertEqual(github_to_raw(github_url), "https://raw.githubusercontent.com/user/repo/example.md")

    def test_github_url_without_blob(self):
        """Test that a GitHub URL without a `/blob` segment is rewritten to its raw URL."""
        github_url = "https://github.com/user/repo/example.md"
        self.assertEqual(github_to_raw(github_url), "https://raw.githubusercontent.com/user/repo/example.md")


class GitHubOwnerAndRepositoryTest(unittest.TestCase):
    """Unit tests for the GitHub owner and repository parse function."""

    def test_non_github_url(self):
        """Test that non-GitHub URLs return an empty owner and repository."""
        self.assertEqual(github_owner_and_repository("https://example.org"), ("", ""))

    def test_github_url(self):
        """Test that a GitHub URL returns an owner and repository."""
        self.assertEqual(github_owner_and_repository("https://github.com/ICTU/quality-time"), ("ICTU", "quality-time"))

    def test_github_url_without_repo(self):
        """Test that a GitHub URL returns an empty owner and repository if the repository is missing."""
        self.assertEqual(github_owner_and_repository("https://github.com/ICTU"), ("", ""))

    def test_npm_git_url(self):
        """Test that an npm-style git+https URL with a .git suffix is parsed."""
        self.assertEqual(
            github_owner_and_repository("git+https://github.com/ICTU/update-time.git"), ("ICTU", "update-time")
        )

    def test_url_with_fragment(self):
        """Test that a URL with a trailing fragment (as npm uses) is parsed."""
        self.assertEqual(
            github_owner_and_repository("https://github.com/ICTU/update-time#readme"), ("ICTU", "update-time")
        )


class GetLatestVersionTest(LoggingTestCase):
    """Unit tests for getting the latest version for a GitHub action."""

    def assert_version(self, latest: DependencyVersion, version: VersionString, changes: str, sha: str) -> None:
        """Assert that the resolved version has the given version, changelog, and commit SHA."""
        self.assertEqual(version, latest.version)
        self.assertEqual(changes, latest.changes)
        self.assertEqual(sha, latest.sha)  # The commit SHA is what the updater pins to.

    @patch("requests.get")
    def test_invalid_current_version(self, mock_get: Mock):
        """Test that an unparsable current version is returned unchanged, without querying GitHub."""
        self.assertEqual(get_latest_version("owner/repository", "not a version", NO_BOUND).version, "not a version")
        mock_get.assert_not_called()

    @patch_github(releases=[github_release_json("1.0", body="changelog")], tags=[], commit=github_commits_json())
    def test_unchanged(self):
        """Test that a release matching the current version resolves to that version and its commit SHA."""
        self.assert_version(get_latest_version("owner/repository", "1.0", NO_BOUND), "1.0", "changelog", "sha")

    @patch_github(releases=[github_release_json("1.1", body="changelog")], tags=[], commit=github_commits_json())
    def test_newer(self):
        """Test that a newer release is resolved, with its changelog and commit SHA."""
        self.assert_version(get_latest_version("owner/repository", "1.0", NO_BOUND), "1.1", "changelog", "sha")

    @patch_github(releases=[github_release_json("1.1", published_at=OLD_ISO)], tags=[], commit=github_commits_json())
    def test_publication_date(self):
        """Test that the resolved release's publication date is captured."""
        self.assertEqual(get_latest_version("owner/repository", "1.0", NO_BOUND).published, OLD_DATE)

    @patch_github(
        releases=[github_release_json("1.1"), github_release_json("2.0")], tags=[], commit=github_commits_json()
    )
    def test_version_filter_bounds_candidates(self):
        """Test that a version filter drops out-of-bound releases so a bounded release wins over a higher one."""
        version_filter = VersionFilter(SpecifierSet("<2"), allow=True)
        self.assert_version(get_latest_version("owner/bounded", "1.0", version_filter), "1.1", "", "sha")

    @patch_github(releases=[github_release_json("0.9")], tags=[])
    def test_older_release_kept(self):
        """Test that the current version is kept, without an error, when every release is older than it."""
        self.assert_version(get_latest_version("owner/older", "1.0", NO_BOUND), "1.0", "", "")

    @patch_github()
    def test_no_error_when_github_cannot_be_reached(self):
        """Test that an unreachable repo logs only the fetch warnings, not a redundant 'no valid version' error."""
        self.assertEqual(get_latest_version("owner/unreachable", "1.0", NO_BOUND).version, "1.0")
        self.assertEqual(self.mock_warning.call_count, 2)  # One could-not-fetch warning per endpoint

    @patch_github(releases=[], tags=[])
    def test_no_version_error_when_repo_has_no_versions(self):
        """Test that a repo without releases and tags keeps the current version, logging a 'no valid version' error."""
        self.assertEqual(get_latest_version("owner/no versions", "1.0", NO_BOUND).version, "1.0")
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, "owner/no versions")

    @patch_github(releases=[github_release_json("1.1", draft=True)], tags=[])
    def test_skip_draft_releases(self):
        """Test that draft releases are not candidates, logging a 'no valid version' error for the reachable repo."""
        self.assertEqual(get_latest_version("owner/only a draft", "1.0", NO_BOUND).version, "1.0")
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, "owner/only a draft")

    @patch_github(releases=[github_release_json("1.1", prerelease=True)], tags=[])
    def test_skip_prerelease_releases(self):
        """Test that prerelease releases are not candidates, logging a 'no valid version' error for the repo."""
        self.assertEqual(get_latest_version("owner/only a prerelease", "1.0", NO_BOUND).version, "1.0")
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, "owner/only a prerelease")

    @patch_github(releases=[github_release_json("invalid-1.1")], tags=[])
    def test_invalid_versions(self):
        """Test that invalid versions are not candidates, logging a 'no valid version' error for the reachable repo."""
        self.assertEqual(get_latest_version("owner/invalid version", "1.0", NO_BOUND).version, "1.0")
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, "owner/invalid version")

    @patch_github(releases=[github_release_json("1.1")], tags=[])
    def test_no_commit_sha(self):
        """Test that the current version is kept when the commit SHA can't be fetched for the eligible release."""
        self.assert_version(get_latest_version("owner/no sha", "1.0", NO_BOUND), "1.0", "", "")
        url = "https://github.com/owner/no sha/releases/tag/1.1"
        self.assert_error_logged(Logger._MESSAGE_NO_COMMIT_SHA, "owner/no sha", "1.1", "HTTP 404", url)

    @patch_github(
        releases=[
            github_release_json("2.0", published_at=RECENT_ISO),
            github_release_json("1.1", published_at=OLD_ISO),
        ],
        tags=[],
        commit=github_commits_json(),
    )
    def test_skip_releases_within_cooldown(self):
        """Test that a release published within the cooldown is skipped in favor of an older, eligible release."""
        latest = get_latest_version("owner/with cooldown", "1.0", NO_BOUND)
        self.assert_version(latest, "1.1", "", "sha")
        self.assertEqual(OLD_DATE, latest.published)

    @patch_github(releases=[], tags=[github_tag_json("v1.1")], commit=github_commits_json(date=OLD_ISO))
    def test_tag_without_release(self):
        """Test that a version that is tagged but not released is resolved, with its SHA from the tags list."""
        latest = get_latest_version("owner/tags only", "1.0", NO_BOUND)
        self.assert_version(latest, "1.1", "", "sha")
        self.assertEqual(latest.published, OLD_DATE)  # The tagged commit's committer date
        self.assertEqual(latest.newest_published, OLD_DATE)  # Also feeds the staleness check

    @patch_github(
        releases=[github_release_json("v1.1", body="changelog", published_at=OLD_ISO)],
        tags=[github_tag_json("v1.1", sha="tag sha")],
    )
    def test_tag_with_release(self):
        """Test that a tag with a release keeps the release's metadata, its SHA from the tags list (no commits fetch).

        The commits endpoint is unreachable in this test, so needing it for the SHA or the date would fail the test.
        """
        latest = get_latest_version("owner/tag with release", "1.0", NO_BOUND)
        self.assert_version(latest, "1.1", "changelog", "tag sha")
        self.assertEqual(latest.published, OLD_DATE)

    @patch_github(
        releases=[github_release_json("v1.0", published_at=OLD_ISO)],
        tags=[github_tag_json("v2.0", sha="new sha"), github_tag_json("v1.0", sha="old sha")],
        commit=github_commits_json(date=OLD_ISO),
    )
    def test_tags_running_ahead_of_releases(self):
        """Test that a repo whose releases (v1.0) fell behind its tags (v2.0) is updated to the newest tag."""
        self.assert_version(get_latest_version("owner/mixed", "1.0", NO_BOUND), "2.0", "", "new sha")

    @patch_github(releases=[], tags=[github_tag_json("v2.0.0-alpha.8")])
    def test_skip_prerelease_tags(self):
        """Test that a tag without a release is recognised as a pre-release by its version and is not a candidate."""
        self.assertEqual(get_latest_version("owner/only a prerelease tag", "1.0", NO_BOUND).version, "1.0")
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, "owner/only a prerelease tag")

    @patch_github(releases=[], tags=[github_tag_json("v1.1")], commit=github_commits_json(date=RECENT_ISO))
    def test_skip_tags_within_cooldown(self):
        """Test that a tag whose commit falls within the cooldown is skipped."""
        self.assert_version(get_latest_version("owner/fresh tag", "1.0", NO_BOUND), "1.0", "", "")

    @patch_github(releases=[], tags=[github_tag_json("v1.1")])
    def test_skip_tag_whose_commit_cannot_be_fetched(self):
        """Test that a tag is skipped, and the skip logged with the reason, when its commit can't be fetched."""
        self.assert_version(get_latest_version("owner/no date", "1.0", NO_BOUND), "1.0", "", "")
        self.assert_error_logged(Logger._MESSAGE_NO_TAG_DATE, "owner/no date", "v1.1", "HTTP 404")

    @patch_github(releases=[], tags=[github_tag_json("v1.1")], commit=github_commits_json())
    def test_skip_tag_whose_commit_has_no_date(self):
        """Test that a tag is skipped, and the skip logged with the reason, when its commit has no committer date."""
        self.assert_version(get_latest_version("owner/no date", "1.0", NO_BOUND), "1.0", "", "")
        self.assert_error_logged(
            Logger._MESSAGE_NO_TAG_DATE, "owner/no date", "v1.1", "the commit has no committer date"
        )

    @patch_github(
        releases=[],
        tags=[github_tag_json("v1.1")],
        commit=mock_response({"message": "API rate limit exceeded"}, ok=False, status_code=403),
    )
    def test_skip_tag_when_rate_limited(self):
        """Test that the GitHub-supplied reason for a failed commit fetch, such as rate limiting, is logged."""
        self.assert_version(get_latest_version("owner/rate limited", "1.0", NO_BOUND), "1.0", "", "")
        self.assert_error_logged(
            Logger._MESSAGE_NO_TAG_DATE, "owner/rate limited", "v1.1", "HTTP 403, API rate limit exceeded"
        )

    @patch_github(releases=[], tags=[github_tag_json("v1.1")], commit=requests.exceptions.Timeout())
    def test_skip_tag_when_the_commit_request_fails(self):
        """Test that a commits request that fails at the transport level is logged as the reason for the skip."""
        self.assert_version(get_latest_version("owner/timeout", "1.0", NO_BOUND), "1.0", "", "")
        self.assert_error_logged(Logger._MESSAGE_NO_TAG_DATE, "owner/timeout", "v1.1", "the request failed")

    @patch_github(
        releases=[github_release_json("v5.0.0", body="changelog", published_at=OLD_ISO)],
        tags=[github_tag_json("v5"), github_tag_json("v5.0.0", sha="release sha")],
    )
    def test_release_wins_over_bare_tag_of_the_same_version(self):
        """Test that a moving major tag (v5) does not shadow the release of the same version (v5.0.0)."""
        latest = get_latest_version("owner/moving tag", "5.0.0", NO_BOUND)
        self.assert_version(latest, "5.0.0", "changelog", "release sha")


class NewestPublicationDateTest(LoggingTestCase):
    """Unit tests for the newest publication date of a GitHub repo."""

    @patch_github(
        releases=[
            github_release_json("2.0b1", prerelease=True, published_at=RECENT_ISO),
            github_release_json("1.1", published_at=OLD_ISO),
            github_release_json("1.0"),  # A draft-like release with no publication date is ignored.
        ],
        tags=[],
    )
    def test_newest_across_releases(self):
        """Test that the most recent publication date wins, including pre-releases and ignoring undated releases."""
        self.assertEqual(newest_publication_date("owner", "active"), RECENT_DATE)

    @patch_github(releases=[], tags=[])
    def test_no_releases(self):
        """Test that a repo with no releases has no newest publication date."""
        self.assertIsNone(newest_publication_date("owner", "no releases"))

    @patch_github()
    def test_fetch_failure(self):
        """Test that no date is returned when neither the releases nor the tags can be fetched."""
        self.assertIsNone(newest_publication_date("owner", "unreachable"))
        self.assertEqual(self.mock_warning.call_count, 2)  # One could-not-fetch warning per endpoint

    @patch_github(releases=[], tags=[github_tag_json("v1.0")], commit=github_commits_json(date=RECENT_ISO))
    def test_tag_without_release(self):
        """Test that a repo that tags without releasing takes its newest date from the tagged commit."""
        self.assertEqual(newest_publication_date("owner", "tags only"), RECENT_DATE)

    @patch_github(
        releases=[github_release_json("1.0", published_at=OLD_ISO)],
        tags=[github_tag_json("v2.0")],
        commit=github_commits_json(date=RECENT_ISO),
    )
    def test_tag_running_ahead_of_releases(self):
        """Test that a repo whose releases fell behind its tags takes its newest date from the newest tagged commit."""
        self.assertEqual(newest_publication_date("owner", "mixed"), RECENT_DATE)

    @patch_github(releases=[github_release_json("2.0", published_at=OLD_ISO)], tags=[github_tag_json("v1.0")])
    def test_tag_behind_releases_needs_no_commit(self):
        """Test that a repo whose releases cover its newest version needs no commits fetch for the newest date.

        The commits endpoint is unreachable in this test, so fetching the tag's commit date would fail the test.
        """
        self.assertEqual(newest_publication_date("owner", "released"), OLD_DATE)
        self.assert_no_warnings_logged()


class GetReleaseTest(LoggingTestCase):
    """Unit tests for getting a release matching a specific package and version."""

    @patch_get(
        [github_release_json("puppeteer-v25.1.0"), github_release_json("puppeteer-core-v25.0.4", body="Changelog")]
    )
    def test_monorepo_tag_match(self):
        """Test finding a release in a monorepo where tags are prefixed with the package name."""
        release = get_release("puppeteer", "monorepo", "puppeteer-core", "25.0.4")
        self.assertEqual(cast("TaggedVersion", release).tag_name, "puppeteer-core-v25.0.4")
        self.assertEqual(cast("TaggedVersion", release).body, "Changelog")

    @patch_get(
        [github_release_json("25.0.4"), github_release_json("v25.0.4"), github_release_json("puppeteer-core-v25.0.4")]
    )
    def test_monorepo_tag_takes_precedence(self):
        """Test that the package-prefixed tag wins over the v-prefixed and bare tags for the same version.

        The competing tags are listed before the package-prefixed one, so matching by list order rather than by
        specificity would pick the wrong release.
        """
        release = get_release("puppeteer", "monorepo", "puppeteer-core", "25.0.4")
        self.assertEqual(cast("TaggedVersion", release).tag_name, "puppeteer-core-v25.0.4")

    @patch_get([github_release_json("v1.2.3")])
    def test_v_prefix_tag_match(self):
        """Test finding a release whose tag is the version prefixed with 'v'."""
        release = get_release("owner", "repo with v prefix", "any", "1.2.3")
        self.assertEqual(cast("TaggedVersion", release).tag_name, "v1.2.3")

    @patch_get([github_release_json("1.2.3")])
    def test_bare_version_tag_match(self):
        """Test finding a release whose tag is the bare version."""
        release = get_release("owner", "repo with bare version", "any", "1.2.3")
        self.assertEqual(cast("TaggedVersion", release).tag_name, "1.2.3")

    @patch_get([github_release_json("v1.0")])
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
        self.assertEqual(changes_from_release("", "", "any", "1.0"), "")
        mock_get.assert_not_called()

    @patch_get([github_release_json("1.1", body="Changelog")])
    def test_changelog(self):
        """Test that the body of the matching release is returned."""
        self.assertEqual(changes_from_release("owner", "repo with changes", "any", "1.1"), "Changelog")

    @patch_get([github_release_json("9.9")])
    def test_no_matching_release(self):
        """Test that the changes are empty when no release matches the version."""
        self.assertEqual(changes_from_release("owner", "repo without matching release", "any", "1.1"), "")
