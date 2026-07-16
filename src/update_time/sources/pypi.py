"""Python Package Index."""

import re
from dataclasses import replace
from functools import cache
from typing import TYPE_CHECKING, NotRequired, TypedDict

from packaging.version import Version

from update_time.domain.changelog import get_version_changes_from_changelog
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
from update_time.sources.github import changes_from_release, github_owner_and_repository, github_to_raw

if TYPE_CHECKING:
    from datetime import datetime

LOG = get_logger("pypi")

# The PyPI host, serving both the JSON API (`/pypi/…/json`) and the Index API (`/simple/…`).
PYPI = "https://pypi.org"

CHANGELOG_URL_KEYS = {"changes", "changelog", "release notes"}
REPOSITORY_URL_KEYS = {"repository", "source", "homepage"}


class Distribution(TypedDict):
    """A distribution file uploaded for a PyPI release."""

    upload_time_iso_8601: str
    yanked: NotRequired[bool]


class Info(TypedDict):
    """PyPI release info."""

    description: str
    project_urls: dict[str, str]
    yanked: NotRequired[bool]


class Release(TypedDict):
    """PyPI release metadata."""

    info: Info
    urls: list[Distribution]


@cache
def release_metadata(package: str, version: str) -> Release | None:
    """Get the release metadata from PyPI, or None if it can't be fetched."""
    response = fetch(f"{PYPI}/pypi/{package}/{version}/json", LOG)
    return response.json() if response is not None else None


@cache
def project_metadata(package: str) -> dict:
    """Get the package's metadata from PyPI's Index API, or an empty dict if it can't be fetched.

    Uses the Index (Simple) API rather than the project JSON API's `releases` key, which is deprecated. See
    https://docs.pypi.org/api/json/ and https://docs.pypi.org/api/index-api/. The response carries both the
    available `versions` (PEP 700) and each distribution file's `upload-time` (PEP 700), so `project_versions`
    and `newest_publication_date` share this single request.
    """
    headers = {"Accept": "application/vnd.pypi.simple.v1+json"}
    response = fetch(f"{PYPI}/simple/{package}/", LOG, headers=headers)
    return response.json() if response is not None else {}


def project_versions(package: str) -> list[str]:
    """Get all version strings of a package from PyPI's Index API, or an empty list if they can't be fetched."""
    return project_metadata(package).get("versions", [])


def newest_publication_date(package: str) -> datetime | None:
    """Return the most recent distribution-file upload time across all of the package's releases, or None.

    This is the "newest release" date the staleness check compares against: the latest moment the project
    published anything at all. It is taken over every file (any version, including pre-releases), so a project
    that recently shipped a pre-release or a back-ported patch still counts as active and is not flagged as stale.
    """
    files = project_metadata(package).get("files", [])
    return newest_datetime(file["upload-time"] for file in files if file.get("upload-time"))


def release_datetime(urls: list[Distribution]) -> datetime | None:
    """Return the latest upload datetime of a release's distribution files, or None if there are none."""
    return newest_datetime(url["upload_time_iso_8601"] for url in urls)


def get_latest_version(
    package: DependencyName, current_version: VersionString, version_filter: VersionFilter
) -> DependencyVersion:
    """Return the latest stable release of the package that is available outside the cooldown window.

    Pre-releases, dev-releases, yanked releases, and releases still within the cooldown period are ignored, as is
    any release the `version_filter` bound rules out. Returns the current version unchanged when it is invalid or
    already the latest eligible version.
    """
    if not is_valid(current_version):
        return DependencyVersion(version=current_version)
    current = Version(current_version)
    versions = [Version(release) for release in project_versions(package) if is_valid(release)]
    candidates = [
        version
        for version in versions
        if version > current
        and not version.is_prerelease
        and not version.is_devrelease
        and version_filter.keeps(version)
    ]
    latest = first_eligible(candidates, lambda version: _eligible_release(package, version), current_version)
    # Always attach the newest release date so an already-up-to-date pin can still be flagged as stale. It rides on
    # the Index API response fetched above, so it costs no extra request; whether it counts as stale (and whether the
    # check is enabled at all) is decided by `is_stale` where the warning would be logged.
    return replace(latest, newest_published=newest_publication_date(package))


def _eligible_release(package: str, version: Version) -> DependencyVersion | None:
    """Return the release as a DependencyVersion when it's eligible, or None when it's yanked or too fresh."""
    metadata = release_metadata(package, str(version))
    if metadata is None:
        return None
    published = release_datetime(metadata["urls"])
    if metadata["info"].get("yanked") or published is None or within_cooldown(published):
        return None
    latest = str(version)
    return DependencyVersion(latest, changes=get_changes(package, latest), published=published)


def get_changes(package: str, version: str) -> str:
    """Return the changelog for the PyPI package and version.

    Since there's no standardized way that PyPI packages refer to a changelog, apply several heuristics to find it:
    - Check for changelog URLs in attributes typically used to refer to the changelog
    - Check for GitHub repository URLs in attributes typically used to refer to the source repository
      and use that to find GitHub releases
    - Check for a changelog in the package description
    - Check for a GitHub URL in the package description and use that to find GitHub releases
    """
    metadata = release_metadata(package, version)
    if metadata is None:
        return ""
    info = metadata["info"]
    urls = info.get("project_urls", {})
    for url_key, url in urls.items():
        if url_key.lower() in CHANGELOG_URL_KEYS and (changelog := changelog_from_url(url, version)):
            return changelog
    for url_key, url in urls.items():
        if url_key.lower() in REPOSITORY_URL_KEYS and (
            changelog := changelog_from_github_releases(url, package, version)
        ):
            return changelog
    description = info["description"]
    return changelog_from_description(description, version) or changelog_from_github_url_in_description(
        description, package, version
    )


def get_publication_datetime(package: str, version: str) -> datetime | None:
    """Return the datetime the version was published, or None if it can't be fetched or has no distribution files."""
    metadata = release_metadata(package, version)
    return release_datetime(metadata["urls"]) if metadata is not None else None


def changelog_from_url(url: str, version: str) -> str:
    """Get the changelog from the URL."""
    changelog_response = fetch(github_to_raw(url), LOG)
    if changelog_response is None:
        return ""
    if changelog_response.headers["Content-Type"].startswith("text/html"):
        return ""
    return get_version_changes_from_changelog(changelog_response.text, version)


def changelog_from_description(description: str, version: str) -> str:
    """Get the changelog from the description."""
    return get_version_changes_from_changelog(description, version) if version in description else ""


def changelog_from_github_url_in_description(description: str, package: str, version: str) -> str:
    """Get the changelog from the description posted to PyPI."""
    github_url = r"https://github\.com/([\w.-]+)/([\w.-]+)"
    if not (match := re.search(github_url, description)):
        return ""
    return changelog_from_github_releases(match.group(), package, version)


def changelog_from_github_releases(url: str, package: str, version: str) -> str:
    """Get the changelog from the GitHub releases."""
    owner, repository = github_owner_and_repository(url)
    return changes_from_release(owner, repository, package, version)
