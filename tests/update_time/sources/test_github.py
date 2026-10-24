"""Unit tests for the GitHub source."""

import unittest
from datetime import UTC, datetime
from logging import ERROR, WARNING
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock, patch

import requests

from update_time.domain import cooldown, dependency
from update_time.domain.bound import NO_BOUND, Verb
from update_time.domain.changelog import is_markdown
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import Archival, ArchivedSubject, PinnedDependency, Release, Unserved
from update_time.io.log import Logger
from update_time.primitives.timestamp import utc_date_and_time
from update_time.sources import github
from update_time.sources.github import (
    GitHubRepository,
    TaggedVersion,
    _get_release,
    _newest_release,
    archival,
    changes_from_changelog_file,
    changes_from_release,
    github_repository,
    github_to_raw,
    release_tags,
)

from tests.helpers import mock_response, patch_environ, patch_get, requested_urls
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import COMMIT_SHA, COMMIT_SHA1, COMMIT_SHA2, FRESH_DATE, PAST_COOLDOWN_DATE
from tests.update_time.helpers import CacheClearingTestCase, LoggingTestCase, bound, days_ago, message_of
from tests.update_time.sources.helpers import (
    GITHUB_RATE_LIMITED_REASON,
    contents_json,
    contents_url,
    file_url,
    github_commits_json,
    github_rate_limited,
    github_release_json,
    github_requests,
    github_tag_json,
    markdown_changelog,
    markdown_changes,
    page_url,
    patch_github,
    repository_url,
    respond_per_url,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from update_time.domain.bound import VersionBound
    from update_time.domain.dependency import DependencyVersion, VersionString


def get_latest_version(
    action: str, current_version: str, version_bound: VersionBound = NO_BOUND, cooldown_days: int = COOLDOWN.default
) -> DependencyVersion:
    """Return what the source resolves for the action and version, as a test writes them."""
    pinned = PinnedDependency(action, current_version)
    return github.get_latest_version(pinned, version_bound, cooldown_days, check_archival=True)


class GitHubURLtoRawTest(unittest.TestCase):
    """Unit tests for the GitHub URL to raw URL function."""

    def test_non_github_url(self):
        """Test that non-GitHub URLs are unchanged."""
        non_github_url = "https://notgithub.com/blob/example.md"
        self.assertEqual(github_to_raw(non_github_url), non_github_url)

    def test_github_url_with_blob(self):
        """Test that a GitHub blob URL is rewritten to its raw URL, dropping the `/blob` segment."""
        github_url = "https://github.com/user/repo/blob/example.md"
        self.assertEqual(github_to_raw(github_url), "https://raw.githubusercontent.com/user/repo/example.md")

    def test_github_url_without_blob(self):
        """Test that a GitHub URL without a `/blob` segment is rewritten to its raw URL."""
        github_url = "https://github.com/user/repo/example.md"
        self.assertEqual(github_to_raw(github_url), "https://raw.githubusercontent.com/user/repo/example.md")


class GitHubRepositoryTest(unittest.TestCase):
    """Unit tests for reading the GitHub repository a URL names."""

    def assert_repository(self, expected: GitHubRepository | None, *urls: str) -> None:
        """Assert that each URL names the expected repository."""
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(github_repository(url), expected)

    def test_non_github_url(self):
        """Test that a non-GitHub URL does not name a repository, scp-like ssh URLs included."""
        self.assert_repository(None, "https://example.org", "git@gitlab.com:ICTU/update-time.git")

    def test_github_url(self):
        """Test that a GitHub URL names a repository."""
        self.assert_repository(GitHubRepository("ICTU", "quality-time"), "https://github.com/ICTU/quality-time")

    @kills(
        Mutation(
            github.github_repository,
            "if len(path_parts) > 1 and path_parts[0] != _GITHUB_SPONSORS_PATH:",
            "if len(path_parts) > 1:",
            "a GitHub sponsors page is read as the repository `sponsors/<owner>`, which GitHub is then asked about",
        )
    )
    def test_github_sponsors_url(self):
        """Test that a GitHub sponsors URL does not name a repository."""
        self.assert_repository(None, "https://github.com/sponsors/ICTU")

    def test_github_url_without_repo(self):
        """Test that a GitHub URL naming an owner alone does not name a repository, with or without a trailing slash."""
        self.assert_repository(None, "https://github.com/ICTU", "https://github.com/ICTU/")

    def test_npm_git_url(self):
        """Test that an npm-style git+https URL with a .git suffix is parsed."""
        self.assert_repository(GitHubRepository("ICTU", "update-time"), "git+https://github.com/ICTU/update-time.git")

    def test_npm_ssh_url(self):
        """Test that an npm-style git+ssh URL, whose user information precedes the host, is parsed."""
        self.assert_repository(GitHubRepository("ICTU", "update-time"), "git+ssh://git@github.com/ICTU/update-time.git")

    def test_scp_like_ssh_url(self):
        """Test that the scp-like ssh form is parsed, with and without the `.git` suffix."""
        self.assert_repository(
            GitHubRepository("ICTU", "update-time"),
            "git@github.com:ICTU/update-time.git",
            "git@github.com:ICTU/update-time",
        )

    def test_url_with_fragment(self):
        """Test that a URL with a trailing fragment (as npm uses) is parsed."""
        self.assert_repository(GitHubRepository("ICTU", "update-time"), "https://github.com/ICTU/update-time#readme")


class GetLatestVersionTest(LoggingTestCase):
    """Unit tests for getting the latest version for a GitHub repository."""

    def assert_version(self, latest: DependencyVersion, version: VersionString, changes: str, sha: str) -> None:
        """Assert that the resolved version has the given version, changelog, and commit SHA."""
        self.assertEqual(version, latest.version)
        self.assertEqual(changes, latest.changes)
        self.assertEqual(sha, latest.sha)  # The commit SHA is what the updater pins to.

    def assert_tag_skipped(self, dependency: str, reason: str) -> None:
        """Assert that the dependency's v1.1 tag was skipped for the reason, leaving it on version 1.0."""
        latest = get_latest_version(dependency, "1.0")
        self.assert_version(latest, "1.0", "", "")
        self.assert_error_logged(
            message_of(Logger.undated_commit), dependency=dependency, candidate="v1.1", current="1.0", reason=reason
        )

    @patch_github(releases=[github_release_json("1.0", body="changelog")], tags=[], commit=github_commits_json())
    def test_unchanged(self):
        """Test that a release matching the current version resolves to that version and its commit SHA."""
        latest = get_latest_version("owner/repository", "1.0")
        self.assert_version(latest, "1.0", "changelog", COMMIT_SHA)

    @patch_github(releases=[github_release_json("1.1", body="changelog")], tags=[], commit=github_commits_json())
    def test_newer(self):
        """Test that a newer release is resolved, with its changelog, its commit SHA, and the markup GitHub uses."""
        latest = get_latest_version("owner/repository", "1.0")
        self.assert_version(latest, "1.1", "changelog", COMMIT_SHA)
        self.assertTrue(is_markdown(latest.changes))

    @patch_github(
        releases=[github_release_json("1.1", published_at=PAST_COOLDOWN_DATE)], tags=[], commit=github_commits_json()
    )
    def test_publication_date(self):
        """Test that the resolved release's publication date is captured."""
        latest = get_latest_version("owner/repository", "1.0")
        self.assertEqual(latest.publication.value, PAST_COOLDOWN_DATE)

    @patch_github(
        releases=[github_release_json("1.1"), github_release_json("2.0")], tags=[], commit=github_commits_json()
    )
    def test_bound_narrows_candidates(self):
        """Test that a version bound drops out-of-bound releases so a bounded release wins over a higher one."""
        version_bound = bound(Verb.ALLOW, "update<2")
        latest = get_latest_version("owner/bounded", "1.0", version_bound)
        self.assert_version(latest, "1.1", "", COMMIT_SHA)

    @patch_github(releases=[github_release_json("0.9")], tags=[])
    def test_older_release_kept(self):
        """Test that the current version is kept, without an error, when every release is older than it."""
        latest = get_latest_version("owner/older", "1.0")
        self.assert_version(latest, "1.0", "", "")

    @patch_github()
    def test_no_error_when_github_cannot_be_reached(self):
        """Test that an unreachable repo logs only the fetch warnings, not a redundant 'no valid version' error."""
        latest = get_latest_version("owner/unreachable", "1.0")
        self.assertEqual(latest.version, "1.0")
        self.assertEqual(len(self.records(WARNING)), 2)  # One could-not-fetch warning per endpoint

    def test_a_repository_offering_no_valid_version_keeps_the_current_one(self):
        """Test which repositories leave the reference on its current version, saying they do not tag a release.

        Each case names a repository of its own, since the releases and tags are cached per repository.
        """
        cases = {
            "no releases and no tags": ([], []),
            "only a draft release": ([github_release_json("1.1", draft=True)], []),
            "only a prerelease": ([github_release_json("1.1", prerelease=True)], []),
            "a release tagged invalid-1.1": ([github_release_json("invalid-1.1")], []),
            "only a prerelease tag": ([], [github_tag_json("v2.0.0-alpha.8")]),
        }
        for case, (releases, tags) in cases.items():
            with self.subTest(case=case), patch_github(releases=releases, tags=tags):
                action = f"owner/{case}"
                latest = get_latest_version(action, "1.0")
                self.assertEqual((latest.version, latest.unserved), ("1.0", Unserved.RELEASES))

    @patch_github(releases=[github_release_json("1.1")], tags=[])
    def test_no_commit_sha(self):
        """Test that the current version is kept when the commit SHA can't be fetched for the eligible release."""
        latest = get_latest_version("owner/no sha", "1.0")
        self.assert_version(latest, "1.0", "", "")
        url = "https://github.com/owner/no sha/releases/tag/1.1"
        message = message_of(Logger.no_commit_sha)
        self.assert_error_logged(message, dependency="owner/no sha", version="1.1", reason="HTTP 404", url=url)

    @patch_github(
        releases=[
            github_release_json("2.0", published_at=FRESH_DATE),
            github_release_json("1.1", published_at=PAST_COOLDOWN_DATE),
        ],
        tags=[],
        commit=github_commits_json(),
    )
    def test_skip_releases_within_cooldown(self):
        """Test that a release published within the cooldown is skipped in favor of an older, eligible release."""
        latest = get_latest_version("owner/with cooldown", "1.0")
        self.assert_version(latest, "1.1", "", COMMIT_SHA)
        self.assertEqual(PAST_COOLDOWN_DATE, latest.publication.value)
        newest = latest.project.newest
        self.assertEqual(Release("2.0", FRESH_DATE), newest)  # The release the cooldown held back dates it

    @patch_github(
        releases=[github_release_json("1.1", published_at=days_ago(10))], tags=[], commit=github_commits_json()
    )
    def test_cooldown_decides_eligibility(self):
        """Test that a version is held back or adopted according to the cooldown the getter is passed.

        The two cooldowns are resolved for the same action, so the result cached for the first would answer the
        second if the cache did not key on the cooldown.
        """
        for cooldown_days, expected in ((30, "1.0"), (5, "1.1")):
            with self.subTest(cooldown_days=cooldown_days):
                latest = get_latest_version("owner/cooldown", "1.0", cooldown_days=cooldown_days)
                self.assertEqual(latest.version, expected)

    @patch_github(releases=[], tags=[github_tag_json("v1.1")], commit=github_commits_json(date=PAST_COOLDOWN_DATE))
    def test_tag_without_release(self):
        """Test that a version that is tagged but not released is resolved, with its SHA from the tags list."""
        latest = get_latest_version("owner/tags only", "1.0")
        self.assert_version(latest, "1.1", "", COMMIT_SHA)
        self.assertEqual(latest.publication.value, PAST_COOLDOWN_DATE)  # The tagged commit's committer date
        newest = Release("1.1", PAST_COOLDOWN_DATE)
        self.assertEqual(latest.project.newest, newest)  # Also feeds the staleness check

    @patch_github(
        releases=[github_release_json("v1.1", body="changelog", published_at=PAST_COOLDOWN_DATE)],
        tags=[github_tag_json("v1.1", sha=COMMIT_SHA)],
    )
    def test_tag_with_release(self):
        """Test that a tag with a release keeps the release's metadata, and its SHA from the tags list."""
        latest = get_latest_version("owner/tag with release", "1.0")
        self.assert_version(latest, "1.1", "changelog", COMMIT_SHA)
        self.assertEqual(latest.publication.value, PAST_COOLDOWN_DATE)

    @patch_github(
        releases=[github_release_json("v1.0", published_at=PAST_COOLDOWN_DATE)],
        tags=[github_tag_json("v2.0", sha=COMMIT_SHA2), github_tag_json("v1.0", sha=COMMIT_SHA1)],
        commit=github_commits_json(date=PAST_COOLDOWN_DATE),
    )
    def test_tags_running_ahead_of_releases(self):
        """Test that a repo whose releases (v1.0) fell behind its tags (v2.0) is updated to the newest tag."""
        latest = get_latest_version("owner/mixed", "1.0")
        self.assert_version(latest, "2.0", "", COMMIT_SHA2)

    @patch_github(
        releases=[github_release_json("v0.9", published_at=PAST_COOLDOWN_DATE)],
        tags=[github_tag_json("v1.1", sha=COMMIT_SHA2), github_tag_json("v0.9", sha=COMMIT_SHA1)],
        commit=github_commits_json(date=FRESH_DATE),
    )
    def test_skip_tags_within_cooldown(self):
        """Test that a tag whose commit falls within the cooldown is skipped, beside a release of another commit."""
        latest = get_latest_version("owner/fresh tag", "1.0")
        self.assert_version(latest, "1.0", "", "")

    @patch_github(
        releases=[github_release_json("v8.3.0", published_at=FRESH_DATE), github_release_json("v8", draft=True)],
        tags=[github_tag_json("v8.3.0"), github_tag_json("v8"), github_tag_json("v8.3")],
        commit=github_commits_json(date=PAST_COOLDOWN_DATE),
    )
    def test_skip_tag_on_the_commit_of_a_release_within_cooldown(self):
        """Test that a tag without a release is skipped while a release of the same commit falls within the cooldown.

        A draft release of that commit, listed after the published one, does not date the tag.
        """
        latest = get_latest_version("owner/tag beside fresh release", "8.2.2")
        self.assert_version(latest, "8.2.2", "", "")

    @patch_github(
        releases=[github_release_json("v2", published_at=PAST_COOLDOWN_DATE, created_at=FRESH_DATE.isoformat())],
        tags=[github_tag_json("v2.14.0"), github_tag_json("v2")],
        commit=github_commits_json(date=FRESH_DATE),
    )
    def test_skip_versions_on_the_commit_a_release_tag_was_moved_onto(self):
        """Test that a release published before its commit, and a tag beside it, wait for that commit's cooldown."""
        latest = get_latest_version("owner/moved release", "1.0")
        self.assert_version(latest, "1.0", "", "")

    def test_tag_takes_the_earliest_publication_date_of_the_releases_of_its_commit(self):
        """Test that a tag without a release takes the earliest date of its commit's releases, whatever the tag order.

        Each case names a repository of its own, since the releases and tags are cached per repository.
        """
        releases = [
            github_release_json("v8.3", published_at=PAST_COOLDOWN_DATE),
            github_release_json("v8", published_at=FRESH_DATE),
        ]
        cases = {
            "the fresh release listed last": ["v8.3", "v8", "v8.3.0"],
            "the fresh release listed first": ["v8", "v8.3", "v8.3.0"],
        }
        for case, tag_names in cases.items():
            tags = [github_tag_json(tag_name) for tag_name in tag_names]
            with self.subTest(case=case), patch_github(releases=releases, tags=tags):
                latest = get_latest_version(f"owner/{case}", "8.2.2")
                self.assert_version(latest, "8.3.0", "", COMMIT_SHA)
                self.assertEqual(latest.publication.value, PAST_COOLDOWN_DATE)

    @patch_github(releases=[], tags=[github_tag_json("v1.1")])
    def test_skip_tag_whose_commit_cannot_be_fetched(self):
        """Test that a tag is skipped, and the skip logged with the reason, when its commit can't be fetched."""
        self.assert_tag_skipped("owner/no date", "HTTP 404")

    @patch_github(releases=[], tags=[github_tag_json("v1.1")], commit=github_commits_json())
    def test_skip_tag_whose_commit_has_no_date(self):
        """Test that a tag is skipped, and the skip logged with the reason, when its commit has no committer date."""
        self.assert_tag_skipped("owner/no date", "the commit has no committer date")

    @kills(
        Mutation(
            github._committer_date,
            "not committer or ",
            "",
            "a commit whose committer GitHub reports as null ends the run with a traceback",
            raises="AttributeError: 'NoneType' object has no attribute 'get'",
        ),
    )
    @patch_github(
        releases=[], tags=[github_tag_json("v1.1")], commit={"sha": COMMIT_SHA, "commit": {"committer": None}}
    )
    def test_skip_tag_whose_commit_has_no_committer(self):
        """Test that a tag is skipped, and the skip logged with the reason, when GitHub reports a null committer."""
        self.assert_tag_skipped("owner/no date", "the commit has no committer date")

    @kills(
        Mutation(
            github.get_latest_version,
            "lambda version: version.version == current,",
            "lambda version: False,",
            "a rate-limited commit leaves the reference unpinned, holding back its own version as well",
        ),
        Mutation(
            cooldown.CooldownWalk.past,
            "if publication.value is not None:",
            "if True:",
            "a rate-limited commit is remembered as held back without a date, which the held-back line cannot render",
        ),
    )
    @patch_github(
        releases=[],
        tags=[
            github_tag_json("v1.2", sha=COMMIT_SHA2),
            github_tag_json("v1.1", sha=COMMIT_SHA1),
            github_tag_json("v1.0", sha=COMMIT_SHA),
        ],
        commit=github_rate_limited(),
    )
    def test_rate_limited_commit_holds_back_the_tags_in_between(self):
        """Test that a tag whose commit is rate limited holds back the tags in between, but not the current one.

        The rate-limited tag itself is not reported as held back by the cooldown.
        """
        dependency = "owner/rate-limited"
        latest = get_latest_version(dependency, "1.0")
        self.assert_version(latest, "1.0", "", COMMIT_SHA)
        self.assertIsNone(latest.fresh_release)
        commits = f"https://api.github.com/repos/{dependency}/commits/{COMMIT_SHA2}"
        self.assertEqual(github_requests("commits"), [commits])
        reason = GITHUB_RATE_LIMITED_REASON
        self.assert_error_logged(
            message_of(Logger.undated_commit), dependency=dependency, candidate="v1.2", current="1.0", reason=reason
        )

    @patch_github(releases=[], tags=[github_tag_json("v1.1")], commit=requests.exceptions.Timeout())
    def test_skip_tag_when_the_commit_request_fails(self):
        """Test that a commits request that fails at the transport level is logged as the reason for the skip."""
        self.assert_tag_skipped("owner/timeout", "the request failed")

    @patch_github(
        releases=[github_release_json("v4.3", body="changelog", published_at=PAST_COOLDOWN_DATE)],
        tags=[github_tag_json("v4.3"), github_tag_json("v4.3.0")],
        commit=github_commits_json(date=PAST_COOLDOWN_DATE),
    )
    def test_most_precise_tag_wins_with_the_release_notes_of_the_same_version(self):
        """Test that of two tags naming the same version, the more precise wins, with the other's release notes."""
        latest = get_latest_version("owner/precise tag", "4.3")
        self.assert_version(latest, "4.3.0", "changelog", COMMIT_SHA)


class NewestReleaseTest(LoggingTestCase):
    """Unit tests for the newest release of a GitHub repo."""

    @kills(
        Mutation(
            dependency.Release.__lt__,
            "return (self.published, self._sortable_version) < (other.published, other._sortable_version)",
            "return (self._sortable_version, self.published) < (other._sortable_version, other.published)",
            "the release named is the highest version rather than the one whose date was measured",
        )
    )
    @patch_github(
        releases=[
            github_release_json("2.0", published_at=PAST_COOLDOWN_DATE),
            github_release_json("1.2b1", prerelease=True, published_at=FRESH_DATE),
            github_release_json("1.0"),  # A draft-like release with no publication date is ignored.
        ],
        tags=[],
    )
    def test_newest_across_releases(self):
        """Test that the most recently published release wins, whatever version it names.

        The 1.2b1 pre-release was published after 2.0, so the highest version is not the one measured.
        """
        self.assertEqual(_newest_release(GitHubRepository("owner", "active")), Release("1.2b1", FRESH_DATE))

    @patch_github(releases=[], tags=[])
    def test_no_releases(self):
        """Test that a repo with no releases has no newest release."""
        self.assertIsNone(_newest_release(GitHubRepository("owner", "no releases")))

    @patch_github()
    def test_fetch_failure(self):
        """Test that no release is returned when neither the releases nor the tags can be fetched."""
        self.assertIsNone(_newest_release(GitHubRepository("owner", "unreachable")))
        self.assertEqual(len(self.records(WARNING)), 2)  # One could-not-fetch warning per endpoint

    @patch_github(releases=[], tags=[github_tag_json("v1.0")], commit=github_commits_json(date=FRESH_DATE))
    def test_tag_without_release(self):
        """Test that a repo that tags without releasing takes its newest release from the tagged commit."""
        self.assertEqual(_newest_release(GitHubRepository("owner", "tags only")), Release("1.0", FRESH_DATE))

    @patch_github(
        releases=[github_release_json("1.0", published_at=PAST_COOLDOWN_DATE)],
        tags=[github_tag_json("v2.0")],
        commit=github_commits_json(date=FRESH_DATE),
    )
    def test_tag_running_ahead_of_releases(self):
        """Test that a repo whose releases fell behind its tags takes its newest release from the newest tag."""
        self.assertEqual(_newest_release(GitHubRepository("owner", "mixed")), Release("2.0", FRESH_DATE))

    @kills(
        Mutation(
            github.TaggedVersion.version_string,
            "return str(self.version) if self.has_valid_version else self.tag_name",
            "return str(self.version)",
            "a release tagged with something that is no version ends the run with a traceback",
            raises="packaging.version.InvalidVersion: Invalid version: 'nightly'",
        ),
    )
    @patch_github(releases=[github_release_json("nightly", published_at=FRESH_DATE)], tags=[])
    def test_release_tagged_with_no_version(self):
        """Test that a repo whose newest release is tagged with no version is reported by that tag."""
        self.assertEqual(_newest_release(GitHubRepository("owner", "unversioned")), Release("nightly", FRESH_DATE))

    @patch_github(
        releases=[github_release_json("2.0", published_at=PAST_COOLDOWN_DATE)], tags=[github_tag_json("v1.0")]
    )
    def test_tag_behind_releases_needs_no_commit(self):
        """Test that a repo whose releases cover its newest version needs no commits fetch for the newest release."""
        self.assertEqual(_newest_release(GitHubRepository("owner", "released")), Release("2.0", PAST_COOLDOWN_DATE))
        self.assert_no_warnings_logged()


class ArchivalTest(LoggingTestCase):
    """Unit tests for the archival state GitHub declares for a repository."""

    @kills(
        Mutation(
            github._repository_metadata,
            'response = _fetch_github(f"{_GITHUB_API}/{repository}")',
            'response = _fetch_github(f"{_GITHUB_API}/{repository}/")',
            "the repository is asked for at a URL GitHub answers 404, so no repository ever reads as archived",
        )
    )
    @patch("requests.Session.get")
    def test_archived_flag(self, mock_get: Mock):
        """Test that a repository reads as archived exactly when GitHub's repository endpoint flags it archived.

        A repository whose endpoint answers with an error reads as active. Each case names a repository of its
        own, since the response is cached per repository.
        """
        archived = Archival(archived=True, subject=ArchivedSubject.REPOSITORY)
        cases: dict[str, tuple[Mock, Archival, bool]] = {  # The response, the archival read, and whether it warns
            "archived": (mock_response({"archived": True}), archived, False),
            "active": (mock_response({"archived": False}), Archival(), False),
            "without-the-flag": (mock_response({}), Archival(), False),
            "unreachable": (mock_response(None, ok=False), Archival(), True),
        }
        for repository, (response, expected, warns) in cases.items():
            with self.subTest(repository=repository):
                mock_get.return_value = response
                self.assertEqual(archival(GitHubRepository("owner", repository), check_archival=True), expected)
                if warns:
                    self.assert_could_not_fetch_logged()
                else:
                    self.assert_no_warnings_logged()
        requested = requested_urls(mock_get)
        self.assertEqual(requested, [repository_url(f"owner/{name}") for name in cases])

    @kills(
        Mutation(
            github.archival,
            "if check_archival and",
            "_repository_metadata(repository)\n    if check_archival and",
            "the source is told not to check for archival but fetches the repository anyway, so the run pays for a "
            "check it does not make",
        )
    )
    @patch("requests.Session.get")
    def test_a_source_told_not_to_check_asks_github_nothing(self, mock_get: Mock):
        """Test that a source told not to check asks GitHub nothing and reads the repository as active."""
        mock_get.return_value = mock_response({"archived": True})
        self.assertEqual(archival(GitHubRepository("owner", "not checked"), check_archival=False), Archival())
        mock_get.assert_not_called()


class GetReleaseTest(LoggingTestCase):
    """Unit tests for getting a release matching a specific package and version."""

    def assert_release(self, release: TaggedVersion | None, tag_name: str, body: str = "") -> None:
        """Assert that the release found is the one the tag names, carrying the changes it was published with."""
        self.assertEqual(cast("TaggedVersion", release).tag_name, tag_name)
        self.assertEqual(cast("TaggedVersion", release).body, body)

    def assert_releases(self, repository: str, cases: list[tuple[str, str, str]]) -> None:
        """Assert that each case's package and version resolve, in the repository, to the release its tag names."""
        for package, version, tag in cases:
            with self.subTest(tag=tag):
                self.assert_release(
                    _get_release(GitHubRepository("owner", repository), release_tags(package, version)),
                    tag,
                    "Changelog",
                )

    @patch_get(
        [
            github_release_json("puppeteer-core-v25.0.4", body="Changelog"),
            github_release_json("selenium-4.47.0", body="Changelog"),
            github_release_json("astro@7.1.4", body="Changelog"),
            github_release_json("pyproject-fmt/2.28.2", body="Changelog"),
        ]
    )
    def test_monorepo_tag_match(self):
        """Test finding a release in a monorepo, however its tags join the package name and the version."""
        cases = [
            ("puppeteer-core", "25.0.4", "puppeteer-core-v25.0.4"),
            ("selenium", "4.47.0", "selenium-4.47.0"),
            ("astro", "7.1.4", "astro@7.1.4"),
            ("pyproject-fmt", "2.28.2", "pyproject-fmt/2.28.2"),
        ]
        self.assert_releases("monorepo tagging per package", cases)

    @kills(
        Mutation(
            github._package_names,
            "[package] if unscoped == package else [package, unscoped]",
            "[package]",
            "a scoped npm package tagged without its scope has no changelog reported",
            raises="AttributeError: 'NoneType' object has no attribute 'tag_name'",
        ),
        Mutation(
            github.release_tags,
            'for name in names for joiner in ("-v", "-", "@", "/")',
            'for name in names for joiner in ("-v", "-", "@", "/") if name == package or joiner == "@"',
            "a monorepo prefixing the unscoped name with a dash has no changelog reported",
            raises="AttributeError: 'NoneType' object has no attribute 'tag_name'",
        ),
        Mutation(
            github.release_tags,
            'for name in names for joiner in ("-v", "-", "@", "/")',
            'for name in names for joiner in ("-v", "-", "@", "/") if name == package or joiner != "/"',
            "a monorepo joining the unscoped name and the version with a slash has no changelog reported",
            raises="AttributeError: 'NoneType' object has no attribute 'tag_name'",
        ),
    )
    @patch_get(
        [
            github_release_json("plugin-rsc@0.5.34"),
            github_release_json("plugin-react@6.1.1", body="Changelog"),
            github_release_json("widget-v1.2.3", body="Changelog"),
            github_release_json("gadget-4.5.6", body="Changelog"),
            github_release_json("gizmo/7.0.0", body="Changelog"),
        ]
    )
    def test_scoped_tag_without_its_scope_match(self):
        """Test finding a scoped npm package's release, however the monorepo joins its unscoped name and version."""
        cases = [
            ("@vitejs/plugin-react", "6.1.1", "plugin-react@6.1.1"),
            ("@scope/widget", "1.2.3", "widget-v1.2.3"),
            ("@scope/gadget", "4.5.6", "gadget-4.5.6"),
            ("@scope/gizmo", "7.0.0", "gizmo/7.0.0"),
        ]
        self.assert_releases("monorepo tagging without the scope", cases)

    @kills(
        Mutation(
            github._package_names,
            "[package] if unscoped == package else [package, unscoped]",
            "[package] if unscoped == package else [unscoped, package]",
            "a repository tagging one version both with and without the scope has the wrong release reported for it",
        ),
    )
    @patch_get(
        [
            github_release_json("react@11.14.0", body="Unscoped"),
            github_release_json("@emotion/react@11.14.0", body="Scoped"),
        ]
    )
    def test_scoped_tag_takes_precedence(self):
        """Test that the tag carrying the scope wins over the one spelling the same version without it."""
        release = _get_release(GitHubRepository("emotion-js", "emotion"), release_tags("@emotion/react", "11.14.0"))
        self.assert_release(release, "@emotion/react@11.14.0", "Scoped")

    @kills(
        Mutation(
            github.release_tags,
            'return [*package_tags, f"v{version}", version]',
            'return [*package_tags[:1], f"v{version}", version, *package_tags[1:]]',
            "a package whose repository also tags the version alone has the wrong release reported for it",
        ),
    )
    @patch_get(
        [
            github_release_json("v6.1.1", body="Version only"),
            github_release_json("plugin-react@6.1.1", body="Changelog"),
            github_release_json("v2.28.2", body="Version only"),
            github_release_json("pyproject-fmt/2.28.2", body="Changelog"),
        ]
    )
    def test_package_tag_takes_precedence(self):
        """Test that a tag naming the package wins over the tag naming the same version alone."""
        cases = [
            ("@vitejs/plugin-react", "6.1.1", "plugin-react@6.1.1"),
            ("pyproject-fmt", "2.28.2", "pyproject-fmt/2.28.2"),
        ]
        self.assert_releases("monorepo tagging the version too", cases)

    @kills(
        Mutation(
            github.release_tags,
            '("-v", "-", "@", "/")',
            '("-", "-v", "@", "/")',
            "a repository spelling one version's tag both ways has the wrong release reported for it",
        ),
    )
    @patch_get(
        [
            github_release_json("25.0.4"),
            github_release_json("v25.0.4"),
            github_release_json("puppeteer-core-25.0.4"),
            github_release_json("puppeteer-core-v25.0.4"),
        ]
    )
    def test_monorepo_tag_takes_precedence(self):
        """Test that the package-prefixed tag with a v wins over the other spellings of the same version."""
        self.assert_release(
            _get_release(GitHubRepository("puppeteer", "monorepo"), release_tags("puppeteer-core", "25.0.4")),
            "puppeteer-core-v25.0.4",
        )

    @patch_get([github_release_json("v1.2.3", body="Changelog"), github_release_json("4.5.6", body="Changelog")])
    def test_version_tag_match(self):
        """Test finding a release whose tag names the version alone, with or without a `v` prefix."""
        cases = [("any", "1.2.3", "v1.2.3"), ("any", "4.5.6", "4.5.6")]
        self.assert_releases("repo tagging the version alone", cases)

    @patch_get([github_release_json("v1.0")])
    def test_no_matching_tag(self):
        """Test that None is returned when no tag matches the requested version."""
        self.assertIsNone(
            _get_release(GitHubRepository("owner", "repo with non matching tag"), release_tags("any", "1.1"))
        )

    @patch("requests.Session.get")
    def test_repo_without_releases(self, mock_get: Mock):
        """Test that a non-OK response yields no release, and is reported as a failed fetch."""
        mock_get.return_value = mock_response([], ok=False)
        self.assertIsNone(
            _get_release(GitHubRepository("owner", "repo without releases for get_release"), release_tags("any", "1.0"))
        )
        self.assert_could_not_fetch_logged(mock_get().url, status=mock_get().status_code)

    @patch("requests.Session.get")
    def test_timeout(self, mock_get: Mock):
        """Test that a timed-out request yields no release, and is reported as a timeout."""
        mock_get.side_effect = requests.exceptions.Timeout
        self.assertIsNone(
            _get_release(GitHubRepository("owner", "repo without releases for get_release"), release_tags("any", "1.0"))
        )
        url = "https://api.github.com/repos/owner/repo without releases for get_release/releases?per_page=100"
        self.assert_logged(message_of(Logger.timeout), url=url)


class ChangesFromReleaseTest(CacheClearingTestCase):
    """Unit tests for getting the changelog from a GitHub release."""

    RELEASE_PAGE = "https://github.com/owner/repo/releases/tag/1.1"

    @patch("requests.Session.get")
    def test_no_repository(self, mock_get: Mock):
        """Test that the changes are empty, without querying GitHub, when there is no repository."""
        self.assertEqual(changes_from_release(None, "any", "1.0"), "")
        mock_get.assert_not_called()

    @kills(
        Mutation(
            github.TaggedVersion.from_release,
            '"", markdown=True, url=',
            '"", markdown=False, url=',
            "a release body is read as text, so the Markdown GitHub renders it in is shown raw",
        ),
    )
    @patch_get([github_release_json("1.1", body="Changelog", html_url=RELEASE_PAGE)])
    def test_the_body_is_returned_as_markdown_linking_the_release_page(self):
        """Test that the body of the matching release is returned as Markdown, linking the release's page."""
        changes = changes_from_release(GitHubRepository("owner", "repo with changes"), "any", "1.1")
        self.assertEqual(changes, "Changelog")
        self.assertTrue(is_markdown(changes))
        self.assertEqual(changes.url, self.RELEASE_PAGE)

    @patch_get([github_release_json("9.9")])
    def test_no_matching_release(self):
        """Test that the changes are empty when no release matches the version."""
        self.assertEqual(
            changes_from_release(GitHubRepository("owner", "repo without matching release"), "any", "1.1"), ""
        )

    @kills(
        Mutation(
            github.TaggedVersion.from_release,
            'release.get("body")',
            'release["body"]',
            "a release GitHub answers without a body ends the run with a traceback",
            raises="KeyError: 'body'",
        ),
    )
    @patch_get(
        [
            {
                "tag_name": "1.1",
                "html_url": RELEASE_PAGE,
                "draft": False,
                "prerelease": False,
                "published_at": None,
            }
        ]
    )
    def test_release_without_body(self):
        """Test that the changes are empty when the release GitHub answers carries no body."""
        self.assertEqual(
            changes_from_release(GitHubRepository("owner", "repo without a release body"), "any", "1.1"), ""
        )


class ChangesFromChangelogFileTest(LoggingTestCase):
    """Unit tests for getting the changelog from a changelog file in a repository."""

    MONOREPO = GitHubRepository("org", "monorepo")
    CHANGELOG = markdown_changelog("1.1")
    CHANGES = markdown_changes("1.1")

    def create_responses(self, mock_get: Mock, listings: dict[str, tuple[str, ...]]) -> None:
        """Point the mock Session.get at a listing per directory, each giving the files that directory holds.

        A file named like a changelog answers the changelog naming version 1.1.
        """
        responses = {
            contents_url(self.MONOREPO, directory): mock_response(contents_json(*names))
            for directory, names in listings.items()
        }
        responses[file_url("CHANGELOG.md")] = mock_response(text=self.CHANGELOG)
        respond_per_url(mock_get, responses)

    @kills(
        Mutation(
            github.changes_from_changelog_file,
            "if directory:",
            "_list_contents(repository)\n    if directory:",
            "a package whose own directory holds its changelog costs a listing of the repository's root as well",
        ),
    )
    @patch("requests.Session.get")
    def test_changelog_file_in_a_directory(self, mock_get: Mock):
        """Test that a changelog file in the package's directory supplies the changes, leaving the root unread."""
        self.create_responses(mock_get, {"packages/package": ("CHANGELOG.md",), "": ("README.md",)})
        changes = changes_from_changelog_file(self.MONOREPO, "1.1", "packages/package")
        self.assertEqual(changes, self.CHANGES)
        self.assertNotIn(contents_url(self.MONOREPO), requested_urls(mock_get))

    @patch("requests.Session.get")
    def test_directory_without_a_changelog_file(self, mock_get: Mock):
        """Test that a directory naming no changelog file leaves the repository's root to supply the changes."""
        self.create_responses(mock_get, {"packages/package": ("README.md",), "": ("CHANGELOG.md",)})
        changes = changes_from_changelog_file(self.MONOREPO, "1.1", "packages/package")
        self.assertEqual(changes, self.CHANGES)
        self.assertIn(contents_url(self.MONOREPO, "packages/package"), requested_urls(mock_get))

    @kills(
        Mutation(
            github._changes_from_files,
            'markdown=is_markdown_file(entry["name"])',
            "markdown=False",
            "a Markdown changelog in a repository's root is read as text, so its markup is shown raw",
        ),
    )
    @patch("requests.Session.get")
    def test_the_changes_link_the_files_page_in_the_markup_its_extension_names(self, mock_get: Mock):
        """Test that a changelog file's changes link its page, and that its extension says whether they're Markdown."""
        for name, markdown in {"CHANGELOG.md": True, "CHANGELOG.rst": False, "CHANGELOG": False}.items():
            with self.subTest(changelog=name):
                responses = {
                    contents_url(self.MONOREPO): mock_response(contents_json(name)),
                    file_url(name): mock_response(text=self.CHANGELOG),
                }
                respond_per_url(mock_get, responses)
                changes = changes_from_changelog_file(self.MONOREPO, "1.1")
                self.assertEqual(changes, self.CHANGES)
                self.assertEqual(is_markdown(changes), markdown)
                self.assertEqual(changes.url, page_url(name))

    @kills(
        Mutation(
            github,
            "@cache\ndef _changelog_file(",
            "def _changelog_file(",
            "a changelog file is downloaded again for every package that falls back to it",
        ),
    )
    @patch("requests.Session.get")
    def test_changelog_file_is_fetched_once(self, mock_get: Mock):
        """Test that two packages falling back to one changelog file cost a single fetch of it between them."""
        self.create_responses(mock_get, {"packages/one": (), "packages/two": (), "": ("CHANGELOG.md",)})
        self.assertEqual(changes_from_changelog_file(self.MONOREPO, "1.1", "packages/one"), self.CHANGES)
        self.assertEqual(changes_from_changelog_file(self.MONOREPO, "1.0", "packages/two"), "")
        self.assertEqual(requested_urls(mock_get).count(file_url("CHANGELOG.md")), 1)

    @kills(
        Mutation(
            github._list_contents,
            'return _list(repository, f"contents/{directory}", require_ok=not directory)',
            'return _list(repository, f"contents/{directory}")',
            "a package whose registry metadata names a directory that moved warns on every run",
        ),
    )
    @patch("requests.Session.get")
    def test_directory_the_repository_does_not_serve(self, mock_get: Mock):
        """Test that a directory the repository doesn't serve leaves the root to supply the changes, without warning."""
        not_found = mock_response(ok=False, status_code=404, reason="Not Found", url="https://not/found")
        respond_per_url(
            mock_get,
            {
                contents_url(self.MONOREPO, "packages/gone"): not_found,
                contents_url(self.MONOREPO): mock_response(contents_json("CHANGELOG.md")),
                file_url("CHANGELOG.md"): mock_response(text=self.CHANGELOG),
            },
        )
        changes = changes_from_changelog_file(self.MONOREPO, "1.1", "packages/gone")
        self.assertEqual(changes, self.CHANGES)
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            github._list,
            "listing = response.json()\n    return tuple(listing) if isinstance(listing, list) else ()",
            "return tuple(response.json())",
            "a directory the contents endpoint answers with a file ends the run with a traceback",
            raises="TypeError: string indices must be integers, not 'str'",
        ),
    )
    @patch("requests.Session.get")
    def test_directory_answered_as_a_file(self, mock_get: Mock):
        """Test that a directory GitHub answers with a file leaves the repository's root to supply the changes."""
        file_json = {"name": "package", "download_url": file_url("package"), "git_url": "https://tree/package"}
        respond_per_url(
            mock_get,
            {
                contents_url(self.MONOREPO, "packages/package"): mock_response(file_json),
                contents_url(self.MONOREPO): mock_response(contents_json("CHANGELOG.md")),
                file_url("CHANGELOG.md"): mock_response(text=self.CHANGELOG),
            },
        )
        changes = changes_from_changelog_file(self.MONOREPO, "1.1", "packages/package")
        self.assertEqual(changes, self.CHANGES)


class GitHubHeadersTest(CacheClearingTestCase):
    """Unit tests for the headers the GitHub API requests carry."""

    def assert_request_headers(self, mock_get: Mock, expected: dict[str, str]) -> None:
        """Assert that a GitHub request carries the expected headers."""
        mock_get.return_value = mock_response({})
        archival(GitHubRepository("owner", "repository"), check_archival=True)
        self.assertEqual(mock_get.call_args.kwargs["headers"], expected)

    @kills(
        Mutation(
            github._github_headers,
            '{"Authorization": f"Bearer {github_token}"}',
            "{}",
            "every GitHub API request goes out unauthenticated, so a run spends an anonymous caller's rate limit",
        )
    )
    @patch_environ({"GITHUB_TOKEN": "pat123"})  # nosec
    @patch("requests.Session.get")
    def test_authorization_header_when_a_token_is_set(self, mock_get: Mock):
        """Test that a request carries a bearer authorization header when GITHUB_TOKEN is set."""
        self.assert_request_headers(mock_get, {"Authorization": "Bearer pat123"})

    @kills(
        Mutation(
            github._github_headers,
            "(TOKEN_VARIABLE)) else {}",
            '(TOKEN_VARIABLE)) else {"Authorization": "Bearer"}',
            "a run given no token sends an empty bearer header instead of asking anonymously",
        )
    )
    @patch_environ()
    @patch("requests.Session.get")
    def test_no_authorization_header_without_a_token(self, mock_get: Mock):
        """Test that a request carries no authorization header when GITHUB_TOKEN is not set."""
        self.assert_request_headers(mock_get, {})


class GitHubRateLimitTest(LoggingTestCase):
    """Unit tests for warning that GitHub's API rate limit is used up."""

    RESET = datetime(2026, 10, 17, 15, 42, tzinfo=UTC)

    def refused_response(self, overrides: Mapping[str, str | None] | None = None) -> Mock:
        """Return the 403 GitHub sends, with headers saying the limit of 60 requests is used up until `RESET`.

        An override replaces a header, or leaves it out where its value is None.
        """
        defaults: dict[str, str | None] = {
            "X-RateLimit-Limit": "60",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(self.RESET.timestamp())),
        }
        merged = {**defaults, **(overrides or {})}
        headers = {name: value for name, value in merged.items() if value is not None}
        return mock_response(
            {"message": "API rate limit exceeded"}, ok=False, status_code=403, reason="Forbidden", headers=headers
        )

    @patch("requests.Session.get")
    def test_a_used_up_rate_limit_is_reported_as_an_error(self, mock_get: Mock):
        """Test that a response saying the rate limit is used up is reported as an error, in the token's form."""
        mock_get.return_value = self.refused_response()
        for case, environment, message in (
            ("anonymous", {}, message_of(Logger.github_rate_limit_used_up)),
            ("with a token", {"GITHUB_TOKEN": "a-token"}, message_of(Logger.github_token_rate_limit_used_up)),  # nosec B105
        ):
            with self.subTest(case), patch_environ(environment, clear=True):
                archival(GitHubRepository("owner", "repository"), check_archival=True)
                self.assert_error_logged(message, limit=60, reset=utc_date_and_time(self.RESET))

    @kills(
        Mutation(
            github,
            "@cache\ndef _log_used_up_rate_limit(",
            "def _log_used_up_rate_limit(",
            "a used-up rate limit is reported once for every request GitHub refuses",
        )
    )
    @patch_environ()
    @patch("requests.Session.get")
    def test_a_used_up_rate_limit_is_reported_once(self, mock_get: Mock):
        """Test that a process reports the used-up rate limit once, however many responses say so."""
        mock_get.return_value = self.refused_response()
        for name in ("first", "second"):
            archival(GitHubRepository("owner", name), check_archival=True)
        self.assertEqual(mock_get.call_count, 2)
        self.assert_error_logged(
            message_of(Logger.github_rate_limit_used_up), limit=60, reset=utc_date_and_time(self.RESET)
        )

    @kills(
        Mutation(
            github._report_used_up_rate_limit,
            'if response.headers.get("X-RateLimit-Remaining") != "0":',
            "if response.ok:",
            "every request GitHub refuses is reported as a used-up rate limit, whatever the reason",
        )
    )
    @patch_environ()
    @patch("requests.Session.get")
    def test_a_rate_limit_that_is_not_used_up_is_not_reported(self, mock_get: Mock):
        """Test that a failed response is not reported as a used-up rate limit while requests remain."""
        mock_get.return_value = self.refused_response({"X-RateLimit-Remaining": "59"})
        archival(GitHubRepository("owner", "repository"), check_archival=True)
        mock_get.assert_called_once()
        self.assertEqual(self.records(ERROR), [])

    @patch_environ()
    @patch("requests.Session.get")
    def test_an_unreadable_rate_limit_header_is_logged_at_debug(self, mock_get: Mock):
        """Test that a rate-limit header that is missing or not a whole number is logged at debug level."""
        for case, overrides, error in (
            ("limit", {"X-RateLimit-Limit": "sixty"}, "invalid literal for int() with base 10: 'sixty'"),
            ("reset", {"X-RateLimit-Reset": "soon"}, "invalid literal for int() with base 10: 'soon'"),
            ("missing limit", {"X-RateLimit-Limit": None}, "'X-RateLimit-Limit'"),
        ):
            with self.subTest(case):
                mock_get.return_value = self.refused_response(overrides)
                archival(GitHubRepository("owner", "repository"), check_archival=True)
                self.assert_logged(message_of(Logger.unreadable_github_rate_limit), error=error)
