"""GitHub functions."""

import os
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache, cached_property
from urllib.parse import urlparse

from packaging.version import Version

from update_time.domain.cooldown import within_cooldown
from update_time.domain.staleness import newest_datetime
from update_time.domain.version import (
    DependencyName,
    DependencyVersion,
    VersionFilter,
    VersionString,
    first_eligible,
    is_valid,
)
from update_time.io.fetch import fetch
from update_time.io.log import get_logger

LOG = get_logger("github")


@dataclass(frozen=True)
class Release:
    """A release from the GitHub releases endpoint."""

    owner: str
    repository: str
    tag_name: str
    body: str = ""
    draft: bool = False
    prerelease: bool = False
    published_at: datetime | None = None

    @classmethod
    def from_json(cls, owner: str, repository: str, release: dict) -> Release:
        """Create a Release from a GitHub releases endpoint result."""
        published_at = release.get("published_at")
        return cls(
            owner=owner,
            repository=repository,
            tag_name=release.get("tag_name", ""),
            body=release.get("body", ""),
            draft=release.get("draft", False),
            prerelease=release.get("prerelease", False),
            published_at=datetime.fromisoformat(published_at) if published_at else None,
        )

    @property
    def has_valid_version(self) -> bool:
        """Return whether the release tag is a valid version."""
        return is_valid(self.tag_name)

    @property
    def within_cooldown(self) -> bool:
        """Return whether the release was published within the configured cooldown period."""
        return within_cooldown(self.published_at)

    @property
    def is_candidate(self) -> bool:
        """Return whether this release could be an update: a valid, non-draft, non-prerelease version.

        These are the name-only checks (no cooldown, no commit-SHA fetch) that narrow the releases before each
        candidate's metadata is resolved, mirroring `oci.Tag.is_candidate_for`.
        """
        return not self.draft and not self.prerelease and self.has_valid_version

    @cached_property
    def commit_sha(self) -> str | None:
        """Fetch the commit SHA for this release's tag, or None if the commits endpoint can't be reached."""
        dependency = f"{self.owner}/{self.repository}"
        commits_url = f"https://api.github.com/repos/{dependency}/commits/{self.tag_name}"
        response = fetch(commits_url, LOG, headers=_github_headers(), require_ok=False)
        if response is None or not response.ok:
            LOG.no_commit_sha(
                dependency, self.tag_name, f"https://github.com/{dependency}/releases/tag/{self.tag_name}"
            )
            return None
        return response.json()["sha"]

    @property
    def version(self) -> Version:
        """Return the release version."""
        return Version(self.tag_name.lstrip("v"))

    def __lt__(self, other: Release) -> bool:
        """Order releases by version, so candidates sort newest-first without a sort key."""
        return self.version < other.version


def github_to_raw(url: str) -> str:
    """Convert GitHub URLs to URLs that return raw content."""
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.netloc == "github.com":
        # Build the corresponding raw.githubusercontent.com URL based on the parsed path.
        raw_path = parsed.path.replace("/blob/", "/")
        return f"https://raw.githubusercontent.com{raw_path}"
    return url


def github_owner_and_repository(url: str) -> tuple[str, str]:
    """Parse the GitHub owner and repository from a URL, including npm-style `git+https` and `.git` URLs."""
    parsed = urlparse(url.removeprefix("git+"))
    if parsed.netloc == "github.com":
        path_parts = parsed.path.lstrip("/").split("/")
        if len(path_parts) > 1:
            return path_parts[0], path_parts[1].removesuffix(".git")
    return "", ""


@cache
def _list_releases(owner: str, repository: str) -> tuple[dict, ...] | None:
    """Fetch the GitHub releases for a repo, or None when they couldn't be fetched.

    An empty tuple means the repo was reached but has no releases; None means the fetch itself failed (already
    logged by `fetch`). Distinguishing the two lets callers avoid reporting a network problem a second time.
    """
    releases_url = f"https://api.github.com/repos/{owner}/{repository}/releases?per_page=100"
    response = fetch(releases_url, LOG, headers=_github_headers())
    return tuple(response.json()) if response is not None else None


@cache
def get_latest_version(
    action: DependencyName, current_version: VersionString, version_filter: VersionFilter
) -> DependencyVersion:
    """Return the latest eligible release for the GitHub action, or the current version unchanged.

    Mirrors `pypi.get_latest_version` and `oci.get_latest_tag`: narrow the releases to candidates by name (a valid,
    non-draft, non-prerelease version at least as new as the current one — the current version itself included, so
    an action referenced by tag only can be pinned to its commit SHA without a version bump), then walk them
    newest-first with `first_eligible`, resolving each candidate's commit SHA and cooldown until one is eligible. A
    `version_filter` bound narrows the candidates before the highest is picked. When the releases were fetched but
    none carries a valid version, that's logged as "no valid version"; a fetch failure is left to `fetch`'s own
    warning, so a network problem isn't reported twice. The newest release date is always attached (for the
    staleness check), even when the version is unchanged.
    """
    if not is_valid(current_version):
        return DependencyVersion(version=current_version)
    owner, repository, *_path = action.split("/")
    newest_published = newest_publication_date(owner, repository)
    unchanged = DependencyVersion(current_version, newest_published=newest_published)
    releases = _list_releases(owner, repository)
    if releases is None:
        return unchanged  # Couldn't reach GitHub; the fetch already logged a warning.
    valid_releases = [
        release for release in (Release.from_json(owner, repository, raw) for raw in releases) if release.is_candidate
    ]
    if not valid_releases:
        LOG.no_version(f"{owner}/{repository}")
        return unchanged
    current = Version(current_version)
    candidates = [
        release for release in valid_releases if release.version >= current and version_filter.keeps(release.version)
    ]
    latest = first_eligible(candidates, _eligible_release, current_version)
    return replace(latest, newest_published=newest_published)


def _eligible_release(release: Release) -> DependencyVersion | None:
    """Resolve the candidate release's commit SHA and return it as a DependencyVersion when eligible, or None.

    Eligible means past the cooldown and with a resolvable commit SHA to pin to. Otherwise None, so `first_eligible`
    skips to the next (older) candidate — the same fall-through the OCI and PyPI sources use.
    """
    if release.within_cooldown or (sha := release.commit_sha) is None:
        return None
    return DependencyVersion(str(release.version), release.body, sha, release.published_at)


def newest_publication_date(owner: str, repository: str) -> datetime | None:
    """Return the repo's most recent release publication date, or None if it has no dated releases.

    Taken over every release (including pre-releases) and ignoring cooldown eligibility, so a repo that has just
    published anything counts as active. Drafts carry no publication date and are naturally excluded. Reuses the
    cached releases list, so it costs no extra request on top of `get_latest_version`.
    """
    releases = _list_releases(owner, repository) or ()
    return newest_datetime(release["published_at"] for release in releases if release.get("published_at"))


def get_release(owner: str, repository: str, package: str, version: str) -> Release | None:
    """Get the release matching the package and version from the GitHub releases API.

    Tries tag names in order of specificity:
    1. `<package>-v<version>` (monorepo, e.g. `puppeteer-core-v25.0.4`)
    2. `v<version>` (e.g. `v25.0.4`)
    3. `<version>` (e.g. `25.0.4`)
    """
    releases_by_tag = {release.get("tag_name"): release for release in (_list_releases(owner, repository) or ())}
    for tag in (f"{package}-v{version}", f"v{version}", version):
        if tag in releases_by_tag:
            return Release.from_json(owner, repository, releases_by_tag[tag])
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
