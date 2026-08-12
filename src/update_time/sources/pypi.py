"""Python Package Index."""

import re
import string
from dataclasses import replace
from functools import cache
from typing import TYPE_CHECKING, NotRequired, TypedDict

from packaging.utils import parse_sdist_filename, parse_wheel_filename
from packaging.version import Version

from update_time.domain.changelog import get_version_changes_from_changelog
from update_time.domain.cooldown import within_cooldown
from update_time.domain.version import (
    DependencyName,
    DependencyVersion,
    VersionString,
    Yank,
    first_eligible,
    is_valid,
)
from update_time.domain.vulnerability import vulnerability_reporting
from update_time.domain.yank import yank_reporting
from update_time.io.fetch import fetch
from update_time.io.log import get_logger
from update_time.primitives.timestamp import newest_timestamp
from update_time.sources.github import changes_from_release, github_owner_and_repository, github_to_raw

if TYPE_CHECKING:
    from datetime import datetime

    from update_time.domain.bound import VersionBound

_LOG = get_logger("pypi")

# The PyPI host, serving both the JSON API (`/pypi/…/json`) and the Index API (`/simple/…`).
_PYPI = "https://pypi.org"

# The project-URL labels whose URL is fetched as a changelog file: PEP 753's `changelog` label with its aliases,
# spelled as `_normalized_label` returns them.
_CHANGELOG_URL_LABELS = {"changelog", "changes", "whatsnew", "history"}
# The project-URL labels whose URL is read as the source repository, likeliest label first: PEP 753's `source` label
# with its aliases, then its `homepage` label. All spelled as `_normalized_label` returns them.
_REPOSITORY_URL_LABELS_BY_RANK = ({"source", "repository", "sourcecode", "github"}, {"homepage"})
_LABEL_NORMALIZATION = str.maketrans("", "", string.punctuation + string.whitespace)
# GitHub serves its sponsorship pages under this path, which it reserves, so no owner can go by this name.
_GITHUB_SPONSORS_PATH = "sponsors"


def _normalized_label(label: str) -> str:
    """Return the project-URL label in the normalized form PEP 753 compares labels in.

    Deleting the punctuation and whitespace and lower-casing what remains makes `Source Code`, `source-code`, and
    `sourcecode` one label.
    """
    return label.translate(_LABEL_NORMALIZATION).lower()


def _repository_rank(label: str) -> int:
    """Return the rank of the project-URL label as a pointer to the source repository, the likeliest ranking lowest.

    A label that is none of the repository labels ranks last, behind every label that is one.
    """
    normalized = _normalized_label(label)
    for rank, labels in enumerate(_REPOSITORY_URL_LABELS_BY_RANK):
        if normalized in labels:
            return rank
    return len(_REPOSITORY_URL_LABELS_BY_RANK)


def _repository_urls(project_urls: dict[str, str]) -> list[str]:
    """Return the project URLs to read as the source repository, the likeliest first.

    Projects publish their repository under labels that say something else, from `Bug Tracker` to `Changelog`, so
    every URL is a candidate. Reading them by rank keeps a project that labels its repository properly from being
    read out of a lesser URL, and URLs of equal rank keep the order the project published them in.
    """
    return [url for _, url in sorted(project_urls.items(), key=lambda item: _repository_rank(item[0]))]


class _Distribution(TypedDict):
    """A distribution file uploaded for a PyPI release."""

    upload_time_iso_8601: str
    yanked: NotRequired[bool]


class _Info(TypedDict):
    """PyPI release info."""

    description: str
    project_urls: dict[str, str]
    yanked: NotRequired[bool]


class Release(TypedDict):
    """PyPI release metadata."""

    info: _Info
    urls: list[_Distribution]


@cache
def release_metadata(package: str, version: str) -> Release | None:
    """Get the release metadata from PyPI, or None if it can't be fetched."""
    response = fetch(f"{_PYPI}/pypi/{package}/{version}/json", _LOG)
    return response.json() if response is not None else None


@cache
def _project_metadata(package: str) -> dict:
    """Get the package's metadata from PyPI's Index API, or an empty dict if it can't be fetched.

    Uses the Index (Simple) API rather than the project JSON API's `releases` key, which is deprecated. See
    https://docs.pypi.org/api/json/ and https://docs.pypi.org/api/index-api/. The response carries both the
    available `versions` (PEP 700) and each distribution file's `upload-time` (PEP 700), so `_project_versions`
    and `newest_publication_date` share this single request.
    """
    headers = {"Accept": "application/vnd.pypi.simple.v1+json"}
    response = fetch(f"{_PYPI}/simple/{package}/", _LOG, headers=headers)
    return response.json() if response is not None else {}


def _project_versions(package: str) -> list[str]:
    """Get all version strings of a package from PyPI's Index API, or an empty list if they can't be fetched."""
    return _project_metadata(package).get("versions", [])


def newest_publication_date(package: str) -> datetime | None:
    """Return the most recent distribution-file upload time across all of the package's releases, or None.

    This is the "newest release" date the staleness check compares against: the latest moment the project
    published anything at all. It is taken over every file (any version, including pre-releases), so a project
    that recently shipped a pre-release or a back-ported patch still counts as active and is not flagged as stale.
    """
    files = _project_metadata(package).get("files", [])
    return newest_timestamp(file.get("upload-time") for file in files)


def _release_datetime(urls: list[_Distribution]) -> datetime | None:
    """Return the latest upload datetime of a release's distribution files, or None if there are none."""
    return newest_timestamp(url["upload_time_iso_8601"] for url in urls)


@vulnerability_reporting
@yank_reporting
def get_latest_version(
    package: DependencyName, current_version: VersionString, version_bound: VersionBound, cooldown_days: int
) -> DependencyVersion:
    """Return the latest stable release of the package that is available outside the cooldown window.

    Returns the current version unchanged when it is invalid or already the latest eligible version.
    """
    if not is_valid(current_version):
        return DependencyVersion(version=current_version)
    current = Version(current_version)
    versions = [Version(release) for release in _project_versions(package) if is_valid(release)]
    candidates = [
        version
        for version in versions
        if version > current
        and not version.is_prerelease
        and not version.is_devrelease
        and version_bound.keeps(version, current_version)
    ]
    latest = first_eligible(
        candidates, lambda version: _eligible_release(package, version, cooldown_days), current_version
    )
    # Always attach the newest release date so an already-up-to-date pin can still be flagged as stale. It rides on
    # the Index API response fetched above, so it costs no extra request; whether it counts as stale (and whether the
    # check is enabled at all) is decided by `is_stale` where the warning would be logged.
    latest = replace(latest, newest_published=newest_publication_date(package))
    # When the run leaves the reference on its current version, attach that version's yank state so a pin left on a
    # yanked release can be warned about; whether to warn is decided by `warn_if_yanked` where it would be logged.
    if latest.version == current_version:
        latest = replace(latest, yank=yank_state(package, current_version))
    return latest


def yank_state(package: str, version: str) -> Yank:
    """Return the version's yank state (PEP 592).

    The Index API lists each distribution file's yank state (`false`, `true`, or the reason string); a version is
    yanked when one of its files is. Files whose name doesn't parse to a version are skipped. A version that doesn't
    parse is reported unyanked, since there is nothing to match the files against.
    """
    if not is_valid(version):
        return Yank()
    pinned = Version(version)
    for file in _project_metadata(package).get("files", []):
        yanked = file.get("yanked")
        if yanked and _distribution_version(file.get("filename", "")) == pinned:
            return Yank(yanked=True, reason=yanked if isinstance(yanked, str) else "")
    return Yank()


def _distribution_version(filename: str) -> Version | None:
    """Return the version encoded in a distribution filename, or None when it can't be parsed."""
    parse = parse_wheel_filename if filename.endswith(".whl") else parse_sdist_filename
    try:
        return parse(filename)[1]
    except ValueError:  # InvalidWheelFilename, InvalidSdistFilename, and InvalidVersion all subclass ValueError.
        return None


def _eligible_release(package: str, version: Version, cooldown_days: int) -> DependencyVersion | None:
    """Return the release as a DependencyVersion when it's eligible, or None when it's yanked or too fresh."""
    metadata = release_metadata(package, str(version))
    if metadata is None:
        return None
    published = _release_datetime(metadata["urls"])
    if metadata["info"].get("yanked") or published is None or within_cooldown(published, cooldown_days):
        return None
    latest = str(version)
    return DependencyVersion(latest, changes=get_changes(package, latest), published=published)


def get_changes(package: str, version: str) -> str:
    """Return the changelog for the PyPI package and version.

    Since there's no standardized way that PyPI packages refer to a changelog, apply several heuristics to find it:
    - Check the project URLs labelled as the changelog, fetching each as a changelog file.
    - Check the GitHub releases of each project URL that points at GitHub, reading the URLs labelled as the
      source repository before the rest.
    - Check for a changelog in the package description.
    - Check for a GitHub URL in the package description and use that to find GitHub releases.
    """
    metadata = release_metadata(package, version)
    if metadata is None:
        return ""
    info = metadata["info"]
    urls = info.get("project_urls", {})
    for label, url in urls.items():
        if _normalized_label(label) in _CHANGELOG_URL_LABELS and (changelog := _changelog_from_url(url, version)):
            return changelog
    for url in _repository_urls(urls):
        if changelog := _changelog_from_github_releases(url, package, version):
            return changelog
    description = info["description"]
    return _changelog_from_description(description, version) or _changelog_from_github_url_in_description(
        description, package, version
    )


def get_publication_datetime(package: str, version: str) -> datetime | None:
    """Return the datetime the version was published, or None if it can't be fetched or has no distribution files."""
    metadata = release_metadata(package, version)
    return _release_datetime(metadata["urls"]) if metadata is not None else None


def _changelog_from_url(url: str, version: str) -> str:
    """Get the changelog from the URL."""
    changelog_response = fetch(github_to_raw(url), _LOG)
    if changelog_response is None:
        return ""
    if changelog_response.headers["Content-Type"].startswith("text/html"):
        return ""
    return get_version_changes_from_changelog(changelog_response.text, version)


def _changelog_from_description(description: str, version: str) -> str:
    """Get the changelog from the description."""
    return get_version_changes_from_changelog(description, version) if version in description else ""


def _changelog_from_github_url_in_description(description: str, package: str, version: str) -> str:
    """Get the changelog from the description posted to PyPI."""
    github_url = r"https://github\.com/([\w.-]+)/([\w.-]+)"
    if not (match := re.search(github_url, description)):
        return ""
    return _changelog_from_github_releases(match.group(), package, version)


def _changelog_from_github_releases(url: str, package: str, version: str) -> str:
    """Get the changelog from the GitHub releases.

    A `github.com/sponsors/…` URL parses as the repository `sponsors/<owner>`, which does not exist, so it is
    skipped rather than asked for releases.
    """
    owner, repository = github_owner_and_repository(url)
    if owner == _GITHUB_SPONSORS_PATH:
        return ""
    return changes_from_release(owner, repository, package, version)
