"""GitHub unit tests."""

import unittest
from datetime import UTC, datetime, timedelta
from logging import WARNING
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock, patch

import requests

from update_time.domain import dependency
from update_time.domain.bound import NO_BOUND, Verb
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import Archival, ArchivedSubject, Release
from update_time.io.log import Logger
from update_time.sources import github
from update_time.sources.github import (
    TaggedVersion,
    _archival,
    _get_release,
    _newest_release,
    changes_from_changelog_file,
    changes_from_release,
    get_latest_version,
    github_owner_and_repository,
    github_to_raw,
)

from tests.helpers import mock_response, patch_get
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import COMMIT_SHA, COMMIT_SHA1, COMMIT_SHA2
from tests.update_time.helpers import (
    CacheClearingTestCase,
    LoggingTestCase,
    bound,
    github_commits_json,
    github_release_json,
    github_tag_json,
    patch_github,
)
from tests.update_time.sources.helpers import (
    contents_json,
    contents_url,
    file_url,
    markdown_changelog,
    markdown_changes,
    requested_urls,
    respond_per_url,
)

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyVersion, VersionString

_OLD_DATE = datetime.now(UTC) - timedelta(days=10)  # A publication date outside the cooldown window
_OLD_ISO = _OLD_DATE.isoformat()
_RECENT_DATE = datetime.now(UTC) - timedelta(days=1)  # A publication date within the cooldown window
_RECENT_ISO = _RECENT_DATE.isoformat()


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


class GitHubOwnerAndRepositoryTest(unittest.TestCase):
    """Unit tests for the GitHub owner and repository parse function."""

    def assert_owner_and_repository(self, expected: tuple[str, str], *urls: str) -> None:
        """Assert that each URL parses to the expected owner and repository."""
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(github_owner_and_repository(url), expected)

    def test_non_github_url(self):
        """Test that a non-GitHub URL returns an empty owner and repository, scp-like ssh URLs included."""
        self.assert_owner_and_repository(("", ""), "https://example.org", "git@gitlab.com:ICTU/update-time.git")

    def test_github_url(self):
        """Test that a GitHub URL returns an owner and repository."""
        self.assert_owner_and_repository(("ICTU", "quality-time"), "https://github.com/ICTU/quality-time")

    def test_github_url_without_repo(self):
        """Test that a GitHub URL returns an empty owner and repository if the repository is missing."""
        self.assert_owner_and_repository(("", ""), "https://github.com/ICTU")

    def test_npm_git_url(self):
        """Test that an npm-style git+https URL with a .git suffix is parsed."""
        self.assert_owner_and_repository(("ICTU", "update-time"), "git+https://github.com/ICTU/update-time.git")

    def test_npm_ssh_url(self):
        """Test that an npm-style git+ssh URL, whose user information precedes the host, is parsed."""
        self.assert_owner_and_repository(("ICTU", "update-time"), "git+ssh://git@github.com/ICTU/update-time.git")

    def test_scp_like_ssh_url(self):
        """Test that the scp-like ssh form is parsed, with and without the `.git` suffix."""
        self.assert_owner_and_repository(
            ("ICTU", "update-time"), "git@github.com:ICTU/update-time.git", "git@github.com:ICTU/update-time"
        )

    def test_url_with_fragment(self):
        """Test that a URL with a trailing fragment (as npm uses) is parsed."""
        self.assert_owner_and_repository(("ICTU", "update-time"), "https://github.com/ICTU/update-time#readme")


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
        self.assertEqual(
            get_latest_version("owner/repository", "not a version", NO_BOUND, COOLDOWN.default).version, "not a version"
        )
        mock_get.assert_not_called()

    @patch_github(releases=[github_release_json("1.0", body="changelog")], tags=[], commit=github_commits_json())
    def test_unchanged(self):
        """Test that a release matching the current version resolves to that version and its commit SHA."""
        self.assert_version(
            get_latest_version("owner/repository", "1.0", NO_BOUND, COOLDOWN.default), "1.0", "changelog", COMMIT_SHA
        )

    @patch_github(releases=[github_release_json("1.1", body="changelog")], tags=[], commit=github_commits_json())
    def test_newer(self):
        """Test that a newer release is resolved, with its changelog and commit SHA."""
        self.assert_version(
            get_latest_version("owner/repository", "1.0", NO_BOUND, COOLDOWN.default), "1.1", "changelog", COMMIT_SHA
        )

    @patch_github(releases=[github_release_json("1.1", published_at=_OLD_ISO)], tags=[], commit=github_commits_json())
    def test_publication_date(self):
        """Test that the resolved release's publication date is captured."""
        self.assertEqual(get_latest_version("owner/repository", "1.0", NO_BOUND, COOLDOWN.default).published, _OLD_DATE)

    @patch_github(
        releases=[github_release_json("1.1"), github_release_json("2.0")], tags=[], commit=github_commits_json()
    )
    def test_bound_narrows_candidates(self):
        """Test that a version bound drops out-of-bound releases so a bounded release wins over a higher one."""
        version_bound = bound(Verb.ALLOW, "update<2")
        self.assert_version(
            get_latest_version("owner/bounded", "1.0", version_bound, COOLDOWN.default), "1.1", "", COMMIT_SHA
        )

    @patch_github(releases=[github_release_json("0.9")], tags=[])
    def test_older_release_kept(self):
        """Test that the current version is kept, without an error, when every release is older than it."""
        self.assert_version(get_latest_version("owner/older", "1.0", NO_BOUND, COOLDOWN.default), "1.0", "", "")

    @patch_github()
    def test_no_error_when_github_cannot_be_reached(self):
        """Test that an unreachable repo logs only the fetch warnings, not a redundant 'no valid version' error."""
        self.assertEqual(get_latest_version("owner/unreachable", "1.0", NO_BOUND, COOLDOWN.default).version, "1.0")
        self.assertEqual(len(self.records(WARNING)), 2)  # One could-not-fetch warning per endpoint

    @patch_github(releases=[], tags=[])
    def test_no_version_error_when_repo_has_no_versions(self):
        """Test that a repo without releases and tags keeps the current version, logging a 'no valid version' error."""
        self.assertEqual(get_latest_version("owner/no versions", "1.0", NO_BOUND, COOLDOWN.default).version, "1.0")
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, dependency="owner/no versions")

    @patch_github(releases=[github_release_json("1.1", draft=True)], tags=[])
    def test_skip_draft_releases(self):
        """Test that draft releases are not candidates, logging a 'no valid version' error for the reachable repo."""
        self.assertEqual(get_latest_version("owner/only a draft", "1.0", NO_BOUND, COOLDOWN.default).version, "1.0")
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, dependency="owner/only a draft")

    @patch_github(releases=[github_release_json("1.1", prerelease=True)], tags=[])
    def test_skip_prerelease_releases(self):
        """Test that prerelease releases are not candidates, logging a 'no valid version' error for the repo."""
        self.assertEqual(
            get_latest_version("owner/only a prerelease", "1.0", NO_BOUND, COOLDOWN.default).version, "1.0"
        )
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, dependency="owner/only a prerelease")

    @patch_github(releases=[github_release_json("invalid-1.1")], tags=[])
    def test_invalid_versions(self):
        """Test that invalid versions are not candidates, logging a 'no valid version' error for the reachable repo."""
        self.assertEqual(get_latest_version("owner/invalid version", "1.0", NO_BOUND, COOLDOWN.default).version, "1.0")
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, dependency="owner/invalid version")

    @patch_github(releases=[github_release_json("1.1")], tags=[])
    def test_no_commit_sha(self):
        """Test that the current version is kept when the commit SHA can't be fetched for the eligible release."""
        self.assert_version(get_latest_version("owner/no sha", "1.0", NO_BOUND, COOLDOWN.default), "1.0", "", "")
        url = "https://github.com/owner/no sha/releases/tag/1.1"
        self.assert_error_logged(
            Logger._MESSAGE_NO_COMMIT_SHA, dependency="owner/no sha", version="1.1", reason="HTTP 404", url=url
        )

    @kills(
        Mutation(
            github,
            "    return replace(latest, project=repository_project)",
            "    return replace(latest, project=Project(newest=replace(repository_project.newest, "
            "version=latest.version) if repository_project.newest else None))",
            "the release attached names the version the run leaves the reference on, not the repository's newest",
        )
    )
    @patch_github(
        releases=[
            github_release_json("2.0", published_at=_RECENT_ISO),
            github_release_json("1.1", published_at=_OLD_ISO),
        ],
        tags=[],
        commit=github_commits_json(),
    )
    def test_skip_releases_within_cooldown(self):
        """Test that a release published within the cooldown is skipped in favor of an older, eligible release."""
        latest = get_latest_version("owner/with cooldown", "1.0", NO_BOUND, COOLDOWN.default)
        self.assert_version(latest, "1.1", "", COMMIT_SHA)
        self.assertEqual(_OLD_DATE, latest.published)
        newest = latest.project.newest
        self.assertEqual(Release("2.0", _RECENT_DATE), newest)  # The release the cooldown held back dates it

    @patch_github(releases=[github_release_json("1.1", published_at=_OLD_ISO)], tags=[], commit=github_commits_json())
    def test_cooldown_decides_eligibility(self):
        """Test that a version is held back or adopted according to the cooldown the getter is passed.

        The two cooldowns are resolved for the same action, so the result cached for the first would answer the
        second if the cache did not key on the cooldown.
        """
        for cooldown_days, expected in ((30, "1.0"), (5, "1.1")):
            with self.subTest(cooldown_days=cooldown_days):
                latest = get_latest_version("owner/cooldown argument", "1.0", NO_BOUND, cooldown_days)
                self.assertEqual(latest.version, expected)

    @patch_github(releases=[], tags=[github_tag_json("v1.1")], commit=github_commits_json(date=_OLD_ISO))
    def test_tag_without_release(self):
        """Test that a version that is tagged but not released is resolved, with its SHA from the tags list."""
        latest = get_latest_version("owner/tags only", "1.0", NO_BOUND, COOLDOWN.default)
        self.assert_version(latest, "1.1", "", COMMIT_SHA)
        self.assertEqual(latest.published, _OLD_DATE)  # The tagged commit's committer date
        newest = Release("1.1", _OLD_DATE)
        self.assertEqual(latest.project.newest, newest)  # Also feeds the staleness check

    @patch_github(
        releases=[github_release_json("v1.1", body="changelog", published_at=_OLD_ISO)],
        tags=[github_tag_json("v1.1", sha=COMMIT_SHA)],
    )
    def test_tag_with_release(self):
        """Test that a tag with a release keeps the release's metadata, and its SHA from the tags list."""
        latest = get_latest_version("owner/tag with release", "1.0", NO_BOUND, COOLDOWN.default)
        self.assert_version(latest, "1.1", "changelog", COMMIT_SHA)
        self.assertEqual(latest.published, _OLD_DATE)

    @patch_github(
        releases=[github_release_json("v1.0", published_at=_OLD_ISO)],
        tags=[github_tag_json("v2.0", sha=COMMIT_SHA2), github_tag_json("v1.0", sha=COMMIT_SHA1)],
        commit=github_commits_json(date=_OLD_ISO),
    )
    def test_tags_running_ahead_of_releases(self):
        """Test that a repo whose releases (v1.0) fell behind its tags (v2.0) is updated to the newest tag."""
        self.assert_version(
            get_latest_version("owner/mixed", "1.0", NO_BOUND, COOLDOWN.default), "2.0", "", COMMIT_SHA2
        )

    @patch_github(releases=[], tags=[github_tag_json("v2.0.0-alpha.8")])
    def test_skip_prerelease_tags(self):
        """Test that a tag without a release is recognised as a pre-release by its version and is not a candidate."""
        self.assertEqual(
            get_latest_version("owner/only a prerelease tag", "1.0", NO_BOUND, COOLDOWN.default).version, "1.0"
        )
        self.assert_error_logged(Logger._MESSAGE_NO_VERSION, dependency="owner/only a prerelease tag")

    @patch_github(releases=[], tags=[github_tag_json("v1.1")], commit=github_commits_json(date=_RECENT_ISO))
    def test_skip_tags_within_cooldown(self):
        """Test that a tag whose commit falls within the cooldown is skipped."""
        self.assert_version(get_latest_version("owner/fresh tag", "1.0", NO_BOUND, COOLDOWN.default), "1.0", "", "")

    @patch_github(releases=[], tags=[github_tag_json("v1.1")])
    def test_skip_tag_whose_commit_cannot_be_fetched(self):
        """Test that a tag is skipped, and the skip logged with the reason, when its commit can't be fetched."""
        self.assert_version(get_latest_version("owner/no date", "1.0", NO_BOUND, COOLDOWN.default), "1.0", "", "")
        self.assert_error_logged(Logger._MESSAGE_NO_TAG_DATE, dependency="owner/no date", tag="v1.1", reason="HTTP 404")

    @patch_github(releases=[], tags=[github_tag_json("v1.1")], commit=github_commits_json())
    def test_skip_tag_whose_commit_has_no_date(self):
        """Test that a tag is skipped, and the skip logged with the reason, when its commit has no committer date."""
        self.assert_version(get_latest_version("owner/no date", "1.0", NO_BOUND, COOLDOWN.default), "1.0", "", "")
        self.assert_error_logged(
            Logger._MESSAGE_NO_TAG_DATE,
            dependency="owner/no date",
            tag="v1.1",
            reason="the commit has no committer date",
        )

    @kills(
        Mutation(
            github,
            'return parse_timestamp(committer.get("date")) if committer else None',
            'return parse_timestamp(committer.get("date"))',
            "a commit whose committer GitHub reports as null ends the run with a traceback",
            raises="AttributeError: 'NoneType' object has no attribute 'get'",
        ),
    )
    @patch_github(
        releases=[], tags=[github_tag_json("v1.1")], commit={"sha": COMMIT_SHA, "commit": {"committer": None}}
    )
    def test_skip_tag_whose_commit_has_no_committer(self):
        """Test that a tag is skipped, and the skip logged with the reason, when GitHub reports a null committer."""
        self.assert_version(get_latest_version("owner/no date", "1.0", NO_BOUND, COOLDOWN.default), "1.0", "", "")
        self.assert_error_logged(
            Logger._MESSAGE_NO_TAG_DATE,
            dependency="owner/no date",
            tag="v1.1",
            reason="the commit has no committer date",
        )

    @patch_github(
        releases=[],
        tags=[github_tag_json("v1.1")],
        commit=mock_response({"message": "API rate limit exceeded"}, ok=False, status_code=403),
    )
    def test_skip_tag_when_rate_limited(self):
        """Test that the GitHub-supplied reason for a failed commit fetch, such as rate limiting, is logged."""
        self.assert_version(get_latest_version("owner/rate limited", "1.0", NO_BOUND, COOLDOWN.default), "1.0", "", "")
        self.assert_error_logged(
            Logger._MESSAGE_NO_TAG_DATE,
            dependency="owner/rate limited",
            tag="v1.1",
            reason="HTTP 403, API rate limit exceeded",
        )

    @patch_github(releases=[], tags=[github_tag_json("v1.1")], commit=requests.exceptions.Timeout())
    def test_skip_tag_when_the_commit_request_fails(self):
        """Test that a commits request that fails at the transport level is logged as the reason for the skip."""
        self.assert_version(get_latest_version("owner/timeout", "1.0", NO_BOUND, COOLDOWN.default), "1.0", "", "")
        self.assert_error_logged(
            Logger._MESSAGE_NO_TAG_DATE, dependency="owner/timeout", tag="v1.1", reason="the request failed"
        )

    @patch_github(
        releases=[github_release_json("v5.0.0", body="changelog", published_at=_OLD_ISO)],
        tags=[github_tag_json("v5"), github_tag_json("v5.0.0", sha=COMMIT_SHA)],
    )
    def test_release_wins_over_bare_tag_of_the_same_version(self):
        """Test that a moving major tag (v5) does not shadow the release of the same version (v5.0.0)."""
        latest = get_latest_version("owner/moving tag", "5.0.0", NO_BOUND, COOLDOWN.default)
        self.assert_version(latest, "5.0.0", "changelog", COMMIT_SHA)


class NewestReleaseTest(LoggingTestCase):
    """Unit tests for the newest release of a GitHub repo."""

    @kills(
        Mutation(
            dependency,
            "        return (self.published, self._sortable_version) < (other.published, other._sortable_version)",
            "        return (self._sortable_version, self.published) < (other._sortable_version, other.published)",
            "the release named is the highest version rather than the one whose date was measured",
        )
    )
    @patch_github(
        releases=[
            github_release_json("2.0", published_at=_OLD_ISO),
            github_release_json("1.2b1", prerelease=True, published_at=_RECENT_ISO),
            github_release_json("1.0"),  # A draft-like release with no publication date is ignored.
        ],
        tags=[],
    )
    def test_newest_across_releases(self):
        """Test that the most recently published release wins, whatever version it names.

        The 1.2b1 pre-release was published after 2.0, so the highest version is not the one measured.
        """
        self.assertEqual(_newest_release("owner", "active"), Release("1.2b1", _RECENT_DATE))

    @patch_github(releases=[], tags=[])
    def test_no_releases(self):
        """Test that a repo with no releases has no newest release."""
        self.assertIsNone(_newest_release("owner", "no releases"))

    @patch_github()
    def test_fetch_failure(self):
        """Test that no release is returned when neither the releases nor the tags can be fetched."""
        self.assertIsNone(_newest_release("owner", "unreachable"))
        self.assertEqual(len(self.records(WARNING)), 2)  # One could-not-fetch warning per endpoint

    @patch_github(releases=[], tags=[github_tag_json("v1.0")], commit=github_commits_json(date=_RECENT_ISO))
    def test_tag_without_release(self):
        """Test that a repo that tags without releasing takes its newest release from the tagged commit."""
        self.assertEqual(_newest_release("owner", "tags only"), Release("1.0", _RECENT_DATE))

    @patch_github(
        releases=[github_release_json("1.0", published_at=_OLD_ISO)],
        tags=[github_tag_json("v2.0")],
        commit=github_commits_json(date=_RECENT_ISO),
    )
    def test_tag_running_ahead_of_releases(self):
        """Test that a repo whose releases fell behind its tags takes its newest release from the newest tag."""
        self.assertEqual(_newest_release("owner", "mixed"), Release("2.0", _RECENT_DATE))

    @kills(
        Mutation(
            github,
            "        return str(self.version) if self.has_valid_version else self.tag_name",
            "        return str(self.version)",
            "a release tagged with something that is no version ends the run with a traceback",
            raises="packaging.version.InvalidVersion: Invalid version: 'nightly'",
        ),
    )
    @patch_github(releases=[github_release_json("nightly", published_at=_RECENT_ISO)], tags=[])
    def test_release_tagged_with_no_version(self):
        """Test that a repo whose newest release is tagged with no version is reported by that tag."""
        self.assertEqual(_newest_release("owner", "unversioned"), Release("nightly", _RECENT_DATE))

    @patch_github(releases=[github_release_json("2.0", published_at=_OLD_ISO)], tags=[github_tag_json("v1.0")])
    def test_tag_behind_releases_needs_no_commit(self):
        """Test that a repo whose releases cover its newest version needs no commits fetch for the newest release."""
        self.assertEqual(_newest_release("owner", "released"), Release("2.0", _OLD_DATE))
        self.assert_no_warnings_logged()


class ArchivalTest(LoggingTestCase):
    """Unit tests for the archival state GitHub declares for a repository."""

    @kills(
        Mutation(
            github,
            '    response = _fetch_github(f"{_GITHUB_API}/{owner}/{repository}")',
            '    response = _fetch_github(f"{_GITHUB_API}/{owner}/{repository}/")',
            "the repository is asked for at a URL GitHub answers 404, so no repository ever reads as archived",
        )
    )
    @patch("requests.get")
    def test_archived_flag(self, mock_get: Mock):
        """Test that a repository reads as archived exactly when GitHub's repository endpoint flags it archived.

        A repository whose endpoint answers with an error reads as active. Each case names a repository of its
        own, since the response is cached per repository.
        """
        archived = Archival(archived=True, subject=ArchivedSubject.REPOSITORY)
        cases: dict[str, tuple[Mock, Archival]] = {
            "archived": (mock_response({"archived": True}), archived),
            "active": (mock_response({"archived": False}), Archival()),
            "without-the-flag": (mock_response({}), Archival()),
            "unreachable": (mock_response(None, ok=False), Archival()),
        }
        for repository, (response, expected) in cases.items():
            with self.subTest(repository=repository):
                mock_get.return_value = response
                self.assertEqual(_archival("owner", repository), expected)
        requested = [call.args[0] for call in mock_get.call_args_list]
        self.assertEqual(requested, [f"https://api.github.com/repos/owner/{name}" for name in cases])
        self.assert_could_not_fetch_logged()


class GetReleaseTest(LoggingTestCase):
    """Unit tests for getting a release matching a specific package and version."""

    def assert_release(self, release: TaggedVersion | None, tag_name: str, body: str = "") -> None:
        """Assert that the release found is the one the tag names, carrying the changes it was published with."""
        self.assertEqual(cast("TaggedVersion", release).tag_name, tag_name)
        self.assertEqual(cast("TaggedVersion", release).body, body)

    @patch_get(
        [github_release_json("puppeteer-v25.1.0"), github_release_json("puppeteer-core-v25.0.4", body="Changelog")]
    )
    def test_monorepo_tag_match(self):
        """Test finding a release in a monorepo where tags are prefixed with the package name."""
        release = _get_release("puppeteer", "monorepo", "puppeteer-core", "25.0.4")
        self.assert_release(release, "puppeteer-core-v25.0.4", "Changelog")

    @kills(
        Mutation(
            github,
            '("-v", "-", "@")',
            '("-v", "@")',
            "a release whose monorepo tag carries no v prefix is reported as having no changelog",
            raises="AttributeError: 'NoneType' object has no attribute 'tag_name'",
        ),
    )
    @patch_get([github_release_json("selenium-4.46.0"), github_release_json("selenium-4.47.0", body="Changelog")])
    def test_monorepo_tag_without_v_match(self):
        """Test finding a release in a monorepo whose package-prefixed tags carry no `v`."""
        release = _get_release("SeleniumHQ", "selenium", "selenium", "4.47.0")
        self.assert_release(release, "selenium-4.47.0", "Changelog")

    @kills(
        Mutation(
            github,
            '("-v", "-", "@")',
            '("-v", "-")',
            "a release whose monorepo tag joins the package and the version with an @ has no changelog reported",
            raises="AttributeError: 'NoneType' object has no attribute 'tag_name'",
        ),
    )
    @patch_get([github_release_json("astro@7.1.4", body="Changelog")])
    def test_monorepo_tag_with_at_sign_match(self):
        """Test finding a release in a monorepo whose tags join the package and the version with an `@`."""
        release = _get_release("owner", "monorepo with at signs", "astro", "7.1.4")
        self.assert_release(release, "astro@7.1.4", "Changelog")

    @kills(
        Mutation(
            github,
            "[package] if unscoped == package else [package, unscoped]",
            "[package]",
            "a scoped npm package tagged without its scope has no changelog reported",
            raises="AttributeError: 'NoneType' object has no attribute 'tag_name'",
        ),
        Mutation(
            github,
            'for name in _package_names(package) for joiner in ("-v", "-", "@")',
            'for name in _package_names(package) for joiner in ("-v", "-", "@") if name == package or joiner == "@"',
            "a monorepo prefixing the unscoped name with a dash has no changelog reported",
            raises="AttributeError: 'NoneType' object has no attribute 'tag_name'",
        ),
    )
    @patch_get(
        [
            github_release_json("plugin-rsc@0.5.34"),
            github_release_json("plugin-react@6.1.1", body="Changelog"),
            github_release_json("widget-v1.2.3", body="Changelog"),
            github_release_json("gadget-4.5.6", body="Changelog"),
        ]
    )
    def test_scoped_tag_without_its_scope_match(self):
        """Test finding a scoped npm package's release, however the monorepo joins its unscoped name and version."""
        cases = [
            ("@vitejs/plugin-react", "6.1.1", "plugin-react@6.1.1"),
            ("@scope/widget", "1.2.3", "widget-v1.2.3"),
            ("@scope/gadget", "4.5.6", "gadget-4.5.6"),
        ]
        for package, version, tag in cases:
            with self.subTest(tag=tag):
                release = _get_release("owner", "monorepo tagging without the scope", package, version)
                self.assert_release(release, tag, "Changelog")

    @kills(
        Mutation(
            github,
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
        release = _get_release("emotion-js", "emotion", "@emotion/react", "11.14.0")
        self.assert_release(release, "@emotion/react@11.14.0", "Scoped")

    @kills(
        Mutation(
            github,
            'for tag in [*package_tags, f"v{version}", version]:',
            'for tag in [*package_tags[:3], f"v{version}", version, *package_tags[3:]]:',
            "a scoped npm package whose repository also tags the version alone has the wrong release reported for it",
        ),
    )
    @patch_get(
        [
            github_release_json("v6.1.1", body="Version only"),
            github_release_json("plugin-react@6.1.1", body="Unscoped"),
        ]
    )
    def test_unscoped_tag_takes_precedence(self):
        """Test that the tag naming the package without its scope wins over the one naming the version alone."""
        release = _get_release("vitejs", "vite-plugin-react", "@vitejs/plugin-react", "6.1.1")
        self.assert_release(release, "plugin-react@6.1.1", "Unscoped")

    @kills(
        Mutation(
            github,
            '("-v", "-", "@")',
            '("-", "-v", "@")',
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
        self.assert_release(_get_release("puppeteer", "monorepo", "puppeteer-core", "25.0.4"), "puppeteer-core-v25.0.4")

    @patch_get([github_release_json("v1.2.3")])
    def test_v_prefix_tag_match(self):
        """Test finding a release whose tag is the version prefixed with 'v'."""
        self.assert_release(_get_release("owner", "repo with v prefix", "any", "1.2.3"), "v1.2.3")

    @patch_get([github_release_json("1.2.3")])
    def test_bare_version_tag_match(self):
        """Test finding a release whose tag is the bare version."""
        self.assert_release(_get_release("owner", "repo with bare version", "any", "1.2.3"), "1.2.3")

    @patch_get([github_release_json("v1.0")])
    def test_no_matching_tag(self):
        """Test that None is returned when no tag matches the requested version."""
        self.assertIsNone(_get_release("owner", "repo with non matching tag", "any", "1.1"))

    @patch("requests.get")
    def test_repo_without_releases(self, mock_get: Mock):
        """Test that a non-OK response yields no release, and is reported as a failed fetch."""
        mock_get.return_value = mock_response([], ok=False)
        self.assertIsNone(_get_release("owner", "repo without releases for get_release", "any", "1.0"))
        self.assert_could_not_fetch_logged(mock_get().url, mock_get().status_code)

    @patch("requests.get")
    def test_timeout(self, mock_get: Mock):
        """Test that a timed-out request yields no release, and is reported as a timeout."""
        mock_get.side_effect = requests.exceptions.Timeout
        self.assertIsNone(_get_release("owner", "repo without releases for get_release", "any", "1.0"))
        url = "https://api.github.com/repos/owner/repo without releases for get_release/releases?per_page=100"
        self.assert_logged(Logger._MESSAGE_TIMEOUT, url=url)


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

    @kills(
        Mutation(
            github,
            'body=release.get("body") or "",',
            'body=release["body"] or "",',
            "a release GitHub answers without a body ends the run with a traceback",
            raises="KeyError: 'body'",
        ),
    )
    @patch_get([{"tag_name": "1.1", "draft": False, "prerelease": False, "published_at": None}])
    def test_release_without_body(self):
        """Test that the changes are empty when the release GitHub answers carries no body."""
        self.assertEqual(changes_from_release("owner", "repo without a release body", "any", "1.1"), "")


class ChangesFromChangelogFileTest(LoggingTestCase):
    """Unit tests for getting the changelog from a changelog file in a repository."""

    MONOREPO = "org/monorepo"
    CHANGELOG = markdown_changelog("1.1")
    CHANGES = markdown_changes("1.1")

    def create_responses(self, mock_get: Mock, listings: dict[str, tuple[str, ...]]) -> None:
        """Point the mock requests.get at a listing per directory, each giving the files that directory holds.

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
            github,
            "    if directory:",
            "    _list_contents(owner, repository)\n    if directory:",
            "a package whose own directory holds its changelog costs a listing of the repository's root as well",
        ),
    )
    @patch("requests.get")
    def test_changelog_file_in_a_directory(self, mock_get: Mock):
        """Test that a changelog file in the package's directory supplies the changes, leaving the root unread."""
        self.create_responses(mock_get, {"packages/package": ("CHANGELOG.md",), "": ("README.md",)})
        changes = changes_from_changelog_file("org", "monorepo", "1.1", "packages/package")
        self.assertEqual(changes, self.CHANGES)
        self.assertNotIn(contents_url(self.MONOREPO), requested_urls(mock_get))

    @patch("requests.get")
    def test_directory_without_a_changelog_file(self, mock_get: Mock):
        """Test that a directory naming no changelog file leaves the repository's root to supply the changes."""
        self.create_responses(mock_get, {"packages/package": ("README.md",), "": ("CHANGELOG.md",)})
        changes = changes_from_changelog_file("org", "monorepo", "1.1", "packages/package")
        self.assertEqual(changes, self.CHANGES)
        self.assertIn(contents_url(self.MONOREPO, "packages/package"), requested_urls(mock_get))

    @kills(
        Mutation(
            github,
            "@cache\ndef _changelog_file(",
            "def _changelog_file(",
            "a changelog file is downloaded again for every package that falls back to it",
        ),
    )
    @patch("requests.get")
    def test_changelog_file_is_fetched_once(self, mock_get: Mock):
        """Test that two packages falling back to one changelog file cost a single fetch of it between them."""
        self.create_responses(mock_get, {"packages/one": (), "packages/two": (), "": ("CHANGELOG.md",)})
        self.assertEqual(changes_from_changelog_file("org", "monorepo", "1.1", "packages/one"), self.CHANGES)
        self.assertEqual(changes_from_changelog_file("org", "monorepo", "1.0", "packages/two"), "")
        self.assertEqual(requested_urls(mock_get).count(file_url("CHANGELOG.md")), 1)

    @kills(
        Mutation(
            github,
            '    return _list(owner, repository, f"contents/{directory}", require_ok=not directory)',
            '    return _list(owner, repository, f"contents/{directory}")',
            "a package whose registry metadata names a directory that moved warns on every run",
        ),
    )
    @patch("requests.get")
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
        changes = changes_from_changelog_file("org", "monorepo", "1.1", "packages/gone")
        self.assertEqual(changes, self.CHANGES)
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            github,
            "    listing = response.json()\n    return tuple(listing) if isinstance(listing, list) else ()",
            "    return tuple(response.json())",
            "a directory the contents endpoint answers with a file ends the run with a traceback",
            raises="TypeError: string indices must be integers, not 'str'",
        ),
    )
    @patch("requests.get")
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
        changes = changes_from_changelog_file("org", "monorepo", "1.1", "packages/package")
        self.assertEqual(changes, self.CHANGES)
