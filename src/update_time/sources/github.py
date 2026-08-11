"""GitHub functions."""

import os
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import cache, cached_property, total_ordering
from typing import TYPE_CHECKING, NotRequired, TypedDict
from urllib.parse import urlparse

from packaging.version import Version

from update_time.domain.cooldown import within_cooldown
from update_time.domain.version import (
    DependencyName,
    DependencyVersion,
    VersionString,
    first_eligible,
    is_valid,
)
from update_time.io.fetch import fetch
from update_time.io.log import get_logger
from update_time.primitives.timestamp import newest_timestamp, parse_timestamp

if TYPE_CHECKING:
    from datetime import datetime

    from update_time.domain.bound import VersionBound

_LOG = get_logger("github")

# The GitHub REST API's per-repository base URL, shared by the releases, tags, and commits endpoints.
_GITHUB_API = "https://api.github.com/repos"
# GitHub's maximum page size, for the releases and tags endpoints. Only the first page of each is fetched, so at
# most this many of the most recent releases and tags are considered.
_PER_PAGE = 100


class _ReleaseJSON(TypedDict):
    """A release from the GitHub releases endpoint."""

    tag_name: str
    body: str | None
    draft: bool
    prerelease: bool
    published_at: str | None  # None for a draft, which hasn't been published


class _TaggedCommit(TypedDict):
    """The commit a tag points to, in a GitHub tags endpoint result."""

    sha: str


class _TagJSON(TypedDict):
    """A tag from the GitHub tags endpoint."""

    name: str
    commit: _TaggedCommit


class _Committer(TypedDict):
    """The committer of the git commit in a GitHub commits endpoint result."""

    date: NotRequired[str]


class _GitCommit(TypedDict):
    """The git commit inside a GitHub commits endpoint result."""

    committer: _Committer | None


class _CommitJSON(TypedDict):
    """A commit from the GitHub commits endpoint."""

    sha: str
    commit: NotRequired[_GitCommit]


@total_ordering
@dataclass(frozen=True)
class TaggedVersion:
    """A version of a GitHub repository, known from its tag, its release, or both.

    Every published release is a tagged commit, so a version can always be pinned to the commit its tag points at;
    only a draft release has no tag yet (GitHub creates it on publish), and drafts are never update candidates.
    """

    owner: str
    repository: str
    tag_name: str
    body: str = ""
    draft: bool = False
    prerelease: bool = False
    published_at: datetime | None = None
    sha: str = ""  # The tagged commit's SHA, when the version came from the tags endpoint (which lists it)
    has_release: bool = True  # False for a version that was tagged but not published as a GitHub release

    @classmethod
    def from_release(cls, owner: str, repository: str, release: _ReleaseJSON) -> TaggedVersion:
        """Create a TaggedVersion from a GitHub releases endpoint result."""
        return cls(
            owner=owner,
            repository=repository,
            tag_name=release["tag_name"],
            body=release["body"] or "",
            draft=release["draft"],
            prerelease=release["prerelease"],
            published_at=parse_timestamp(release["published_at"]),
        )

    @classmethod
    def from_tag(cls, owner: str, repository: str, tag: _TagJSON, release: _ReleaseJSON | None) -> TaggedVersion:
        """Create a TaggedVersion from a GitHub tags endpoint result, enriched with the tag's release when there is one.

        The tags endpoint lists each tag's commit SHA, so a version that came in as a tag never needs the commits
        endpoint to be pinned. A tag without a release carries no pre-release flag; its version tells instead
        (`v4.0.0-alpha.8`).
        """
        sha = tag["commit"]["sha"]
        if release is not None:
            return replace(cls.from_release(owner, repository, release), sha=sha)
        tag_name = tag["name"]
        prerelease = is_valid(tag_name) and Version(tag_name).is_prerelease
        return cls(
            owner=owner, repository=repository, tag_name=tag_name, prerelease=prerelease, sha=sha, has_release=False
        )

    @property
    def has_valid_version(self) -> bool:
        """Return whether the tag is a valid version."""
        return is_valid(self.tag_name)

    @property
    def is_candidate(self) -> bool:
        """Return whether this version could be an update: a valid, non-draft, non-prerelease version.

        These are the name-only checks (no cooldown, no commits-endpoint fetch) that narrow the versions before each
        candidate's metadata is resolved, mirroring `oci.Tag.is_candidate_for`.
        """
        return not self.draft and not self.prerelease and self.has_valid_version

    @property
    def commit_ref(self) -> str:
        """Return the ref that identifies the tagged commit at the commits endpoint.

        The commit's SHA when the version came from the tags endpoint; the tag name for a version built from a
        release only, whose commit the tags endpoint didn't list.
        """
        return self.sha or self.tag_name

    @cached_property
    def commit_sha(self) -> str | None:
        """Return the commit SHA for this version's tag — as listed by the tags endpoint, or fetched — or None."""
        if self.sha:
            return self.sha
        dependency = self.dependency
        commit, reason = _get_commit(self.owner, self.repository, self.tag_name)
        if commit is None:
            _LOG.no_commit_sha(
                dependency, self.tag_name, reason, f"https://github.com/{dependency}/releases/tag/{self.tag_name}"
            )
            return None
        return commit["sha"]

    @property
    def dependency(self) -> str:
        """Return the dependency as <owner>/<repository>."""
        return f"{self.owner}/{self.repository}"

    @cached_property
    def publication_date(self) -> datetime | None:
        """Return the release's publication date, or the tagged commit's committer date for a tag without a release.

        The tags endpoint lists no dates, so a tag without a release resolves its date from the commit it tags. That
        date can understate the version's age — a tag can be created long after its commit — but never overstates it.
        None when the commit can't be fetched.
        """
        if self.has_release:
            return self.published_at
        return _commit_datetime(self.owner, self.repository, self.commit_ref)

    @property
    def missing_date_reason(self) -> str | None:
        """Return why the publication date couldn't be resolved, or None when it could (or when none is needed).

        Only a tag without a release needs a date to be an update candidate: its date takes a separate commits
        request, and if that request failing (say, due to rate limiting) made the tag eligible anyway, the cooldown
        could be bypassed by API flakiness. A release never has a missing-date reason: its date arrives with the
        releases list itself, so there is no separate fetch to fail, and the only undated releases are drafts,
        which are never candidates.
        """
        if self.has_release or self.publication_date is not None:
            return None
        _commit, reason = _get_commit(self.owner, self.repository, self.commit_ref)
        return reason or "the commit has no committer date"

    @property
    def version(self) -> Version:
        """Return the tag's version. Version accepts (and normalizes away) the common `v` prefix."""
        return Version(self.tag_name)

    def __lt__(self, other: TaggedVersion) -> bool:
        """Order versions — a released version above a bare tag of the same version — so candidates sort newest-first.

        The tie-breaker matters for a moving major tag (`v5`) pointing at the same version as a release (`v5.0.0`):
        preferring the release keeps its release notes and its exact version in the pin comment.
        """
        return (self.version, self.has_release) < (other.version, other.has_release)


def github_to_raw(url: str) -> str:
    """Convert GitHub URLs to URLs that return raw content."""
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.netloc == "github.com":
        # Build the corresponding raw.githubusercontent.com URL based on the parsed path.
        raw_path = parsed.path.replace("/blob/", "/")
        return f"https://raw.githubusercontent.com{raw_path}"
    return url


def github_owner_and_repository(url: str) -> tuple[str, str]:
    """Parse the GitHub owner and repository from a URL, including npm-style `git+https`, `git+ssh`, and `.git` URLs."""
    parsed = urlparse(url.removeprefix("git+"))
    if parsed.hostname == "github.com":
        path_parts = parsed.path.lstrip("/").split("/")
        if len(path_parts) > 1:
            return path_parts[0], path_parts[1].removesuffix(".git")
    return "", ""


@cache
def _list_releases(owner: str, repository: str) -> tuple[_ReleaseJSON, ...] | None:
    """Fetch the GitHub releases for a repo, or None when they couldn't be fetched.

    An empty tuple means the repo was reached but has no releases; None means the fetch itself failed (already
    logged by `fetch`). Distinguishing the two lets callers avoid reporting a network problem a second time.
    """
    releases_url = f"{_GITHUB_API}/{owner}/{repository}/releases?per_page={_PER_PAGE}"
    response = fetch(releases_url, _LOG, headers=_github_headers())
    return tuple(response.json()) if response is not None else None


@cache
def _list_tags(owner: str, repository: str) -> tuple[_TagJSON, ...] | None:
    """Fetch the GitHub tags for a repo, or None when they couldn't be fetched.

    Mirrors `_list_releases`: an empty tuple means the repo was reached but has no tags; None means the fetch
    itself failed (already logged by `fetch`).
    """
    tags_url = f"{_GITHUB_API}/{owner}/{repository}/tags?per_page={_PER_PAGE}"
    response = fetch(tags_url, _LOG, headers=_github_headers())
    return tuple(response.json()) if response is not None else None


@cache
def _get_commit(owner: str, repository: str, ref: str) -> tuple[_CommitJSON | None, str]:
    """Fetch the commit for the ref (a tag name or commit SHA): the commit and an empty string, or None and why not.

    Shared by the commit-SHA lookup for a release and the committer-date lookup for a tag without a release, so a
    candidate that needs both costs one request. `require_ok=False` keeps `fetch`'s generic warning out of a non-OK
    response, so each caller can report the failure, with the returned reason, in its own terms. A non-OK GitHub
    response explains itself in its body's `message` (e.g. "API rate limit exceeded for …"), so that is included.
    """
    commits_url = f"{_GITHUB_API}/{owner}/{repository}/commits/{ref}"
    response = fetch(commits_url, _LOG, headers=_github_headers(), require_ok=False)
    if response is None:
        return None, "the request failed"
    if not response.ok:
        message = ""
        with suppress(ValueError):  # A body that isn't JSON has no message to include
            message = (response.json() or {}).get("message", "")
        return None, f"HTTP {response.status_code}" + (f", {message}" if message else "")
    return response.json(), ""


def _commit_datetime(owner: str, repository: str, ref: str) -> datetime | None:
    """Return the committer date of the commit the ref points to, or None when it can't be fetched."""
    commit, _reason = _get_commit(owner, repository, ref)
    git_commit = commit.get("commit") if commit else None
    committer = git_commit["committer"] if git_commit else None
    return parse_timestamp(committer.get("date")) if committer else None


def _tagged_versions(owner: str, repository: str) -> list[TaggedVersion] | None:
    """Return the repo's versions: its tags enriched with their releases, plus releases whose tag wasn't listed.

    Tags are the version universe — every release tags the commit it was cut from — so a version that was tagged
    but never released is a candidate too. Where a release exists for a tag, the release's metadata (publication
    date, release notes, pre-release flag) enriches it. Both endpoints return only their first page, so a release
    whose tag falls outside the fetched tags is kept as a release-only candidate, resolving its commit SHA through
    the commits endpoint. None means neither endpoint could be reached (each failure is already logged by `fetch`).
    """
    releases = _list_releases(owner, repository)
    tags = _list_tags(owner, repository)
    if releases is None and tags is None:
        return None
    releases_by_tag = {release["tag_name"]: release for release in releases or ()}
    listed_tags = {tag["name"] for tag in tags or ()}
    tagged_versions = [
        TaggedVersion.from_tag(owner, repository, tag, releases_by_tag.get(tag["name"])) for tag in tags or ()
    ]
    tagged_versions.extend(
        TaggedVersion.from_release(owner, repository, release)
        for tag_name, release in releases_by_tag.items()
        if tag_name not in listed_tags
    )
    return tagged_versions


@cache
def get_latest_version(
    action: DependencyName, current_version: VersionString, version_bound: VersionBound, cooldown_days: int
) -> DependencyVersion:
    """Return the latest eligible version for the GitHub action, or the current version unchanged.

    Mirrors `pypi.get_latest_version` and `oci.get_latest_tag`. The repo's versions are its tags enriched with their
    releases, from `_tagged_versions`. Narrow them to candidates by name: a valid, non-draft, non-prerelease version
    at least as new as the current one. The current version itself is included, so an action referenced by tag only
    can be pinned to its commit SHA without a version bump. Then walk the candidates newest-first with
    `first_eligible`, resolving each candidate's publication date, cooldown, and commit SHA until one is eligible.
    A `version_bound` bound narrows the candidates before the highest is picked. When the versions were fetched
    but none is valid, that's logged as "no valid version"; a fetch failure is left to `fetch`'s own warning, so a
    network problem isn't reported twice. The newest publication date is always attached (for the staleness
    check), even when the version is unchanged.
    """
    if not is_valid(current_version):
        return DependencyVersion(version=current_version)
    owner, repository, *_path = action.split("/")
    newest_published = newest_publication_date(owner, repository)
    unchanged = DependencyVersion(current_version, newest_published=newest_published)
    tagged_versions = _tagged_versions(owner, repository)
    if tagged_versions is None:
        return unchanged  # Couldn't reach GitHub; the fetches already logged a warning.
    valid_versions = [version for version in tagged_versions if version.is_candidate]
    if not valid_versions:
        _LOG.no_version(f"{owner}/{repository}")
        return unchanged
    current = Version(current_version)
    candidates = [
        version
        for version in valid_versions
        if version.version >= current and version_bound.keeps(version.version, current_version)
    ]
    latest = first_eligible(candidates, lambda version: _eligible_version(version, cooldown_days), current_version)
    return replace(latest, newest_published=newest_published)


def _eligible_version(tagged_version: TaggedVersion, cooldown_days: int) -> DependencyVersion | None:
    """Resolve the candidate's publication date and commit SHA and return it as a DependencyVersion when eligible.

    Eligible means past the cooldown and with a resolvable commit SHA to pin to. Otherwise None, so `first_eligible`
    skips to the next (older) candidate — the same fall-through the OCI and PyPI sources use. A candidate with a
    missing publication date (see `TaggedVersion.missing_date_reason`) is skipped, logged with the reason, rather than
    adopted with the cooldown unchecked.
    """
    if (reason := tagged_version.missing_date_reason) is not None:
        _LOG.no_tag_date(tagged_version.dependency, tagged_version.tag_name, reason)
        return None
    published = tagged_version.publication_date
    if within_cooldown(published, cooldown_days) or (sha := tagged_version.commit_sha) is None:
        return None
    return DependencyVersion(str(tagged_version.version), tagged_version.body, sha, published)


def _newest_tag_beyond_releases(owner: str, repository: str) -> _TagJSON | None:
    """Return the highest-versioned tag when it runs ahead of every dated release, or None.

    This decides whether the repo's newest activity might be a tag rather than a release, so that
    `newest_publication_date` knows to fetch that tag's commit date. Pre-release versions count on both sides,
    mirroring the newest-release date, which also includes pre-releases.
    """
    versioned_tags = [
        (Version(tag["name"]), tag) for tag in _list_tags(owner, repository) or () if is_valid(tag["name"])
    ]
    if not versioned_tags:
        return None
    # The key limits the comparison to the version: at equal versions (a moving `v5` tag next to `v5.0.0`) comparing
    # the whole pair would fail on the tag dicts.
    newest_version, newest_tag = max(versioned_tags, key=lambda versioned_tag: versioned_tag[0])
    release_versions = [
        Version(release["tag_name"])
        for release in _list_releases(owner, repository) or ()
        if release["published_at"] and is_valid(release["tag_name"])
    ]
    if release_versions and newest_version <= max(release_versions):
        return None
    return newest_tag


def newest_publication_date(owner: str, repository: str) -> datetime | None:
    """Return the repo's most recent publication date, or None if it has none.

    Taken over every release (including pre-releases and backports, since the newest date wins regardless of
    version order) and ignoring cooldown eligibility, so a repo that has just published anything counts as active.
    Drafts carry no publication date and are naturally excluded. When the repo's highest version is a tag that
    runs ahead of every dated release, that tag's commit date is folded in, so a repo that tags without releasing
    is not falsely reported as stale. The release dates stay primary because the tags list carries no dates: every
    tag date costs a commits request, so only the one tag the releases can't answer for is fetched, and a tag-only
    backport below that tag's version is knowingly missed.
    """
    releases = _list_releases(owner, repository) or ()
    dates = [newest_timestamp(release["published_at"] for release in releases)]
    if (tag := _newest_tag_beyond_releases(owner, repository)) is not None:
        dates.append(_commit_datetime(owner, repository, tag["commit"]["sha"]))
    return max((date for date in dates if date is not None), default=None)


def get_release(owner: str, repository: str, package: str, version: str) -> TaggedVersion | None:
    """Get the release matching the package and version from the GitHub releases API.

    Tries tag names in order of specificity:
    1. `<package>-v<version>` (monorepo, e.g. `puppeteer-core-v25.0.4`).
    2. `v<version>` (e.g. `v25.0.4`).
    3. `<version>` (e.g. `25.0.4`).
    """
    releases_by_tag = {release["tag_name"]: release for release in (_list_releases(owner, repository) or ())}
    for tag in (f"{package}-v{version}", f"v{version}", version):
        if tag in releases_by_tag:
            return TaggedVersion.from_release(owner, repository, releases_by_tag[tag])
    return None


def changes_from_release(owner: str, repository: str, package: str, version: str) -> str:
    """Return the body of the GitHub release matching the package and version, or empty string if absent."""
    if not (owner and repository):
        return ""
    release = get_release(owner, repository, package, version)
    return release.body if release else ""


def _github_headers() -> dict[str, str]:
    """Return GitHub API request headers, including authorization if GITHUB_TOKEN is set."""
    return {"Authorization": f"Bearer {github_token}"} if (github_token := os.environ.get("GITHUB_TOKEN")) else {}
