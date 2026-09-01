"""Python Package Index."""

import re
import string
from dataclasses import replace
from functools import cache, partial
from typing import TYPE_CHECKING, NotRequired, TypedDict

from packaging.utils import parse_sdist_filename, parse_wheel_filename
from packaging.version import Version

from update_time.domain.archival import archival_reporting
from update_time.domain.changelog import get_version_changes_from_changelog
from update_time.domain.cooldown import within_cooldown
from update_time.domain.dependency import (
    Archival,
    DependencyName,
    DependencyVersion,
    Project,
    Release,
    VersionString,
    Yank,
    first_eligible,
    is_valid,
)
from update_time.domain.publication import publication_date_reporting
from update_time.domain.vulnerability import vulnerability_reporting
from update_time.domain.yank import with_yank_state, yank_reporting
from update_time.io.fetch import fetch
from update_time.io.log import get_logger
from update_time.primitives.timestamp import newest_timestamp, parse_timestamp
from update_time.sources.github import (
    changes_from_changelog_file,
    changes_from_release,
    github_owner_and_repository,
    github_to_raw,
)

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
# The characters PyPI treats as one and the same separator within a distribution name (see `normalized_name`).
_NAME_SEPARATORS = re.compile(r"[-_.]+")
# GitHub serves its sponsorship pages under this path, which it reserves, so no owner can go by this name.
_GITHUB_SPONSORS_PATH = "sponsors"
# Matches a GitHub repository URL wherever it sits in prose, such as the description a project posts to PyPI.
_GITHUB_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+")


def normalized_name(name: DependencyName) -> DependencyName:
    """Return the name as PyPI spells it, so a pin is matched however the manifest spells it.

    PyPI names a distribution in lower case with each run of `-`, `_`, and `.` collapsed to a single `-`, as
    https://peps.python.org/pep-0503/#normalized-names prescribes, and uv reports a package by that name. So a
    `typing_extensions` pin and the `typing-extensions` uv reports for it are the same dependency.
    """
    return _NAME_SEPARATORS.sub("-", name).lower()


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
    # PyPI answers null for a package that declares no project URLs.
    project_urls: dict[str, str] | None
    yanked: NotRequired[bool]


class ReleaseMetadata(TypedDict):
    """PyPI release metadata."""

    info: _Info
    urls: list[_Distribution]


@cache
def release_metadata(package: str, version: str) -> ReleaseMetadata | None:
    """Get the release metadata from PyPI, or None if it can't be fetched."""
    response = fetch(f"{_PYPI}/pypi/{package}/{version}/json", _LOG)
    return response.json() if response is not None else None


def _project_metadata(package: str) -> dict:
    """Get the package's metadata from PyPI's Index API, or an empty dict if it can't be fetched.

    The name is normalized first, so every spelling of one package shares a single request; the index redirects
    the other spellings to that one anyway.
    """
    return _index_metadata(normalized_name(package))


@cache
def _index_metadata(package: str) -> dict:
    """Fetch the Index API response for the normalized package name, or an empty dict if it can't be fetched.

    Uses the Index (Simple) API rather than the project JSON API's `releases` key, which is deprecated. See
    https://docs.pypi.org/api/json/ and https://docs.pypi.org/api/index-api/. The response carries both the
    available `versions` (PEP 700) and each distribution file's `upload-time` (PEP 700), so `_project_versions`
    and `_newest_release` share this single request.
    """
    headers = {"Accept": "application/vnd.pypi.simple.v1+json"}
    response = fetch(f"{_PYPI}/simple/{package}/", _LOG, headers=headers)
    return response.json() if response is not None else {}


def _project_versions(package: str) -> list[str]:
    """Get all version strings of a package from PyPI's Index API, or an empty list if they can't be fetched."""
    return _project_metadata(package).get("versions", [])


def _versions(package: str) -> list[Version]:
    """Return the package's releases the index lists under a version `packaging` can read."""
    return [Version(release) for release in _project_versions(package) if is_valid(release)]


def _stable_versions(package: str) -> list[Version]:
    """Return the package's releases that are candidates to pin: the valid versions that are neither pre nor dev."""
    return [version for version in _versions(package) if not version.is_prerelease and not version.is_devrelease]


def _newest_release(package: DependencyName) -> Release | None:
    """Return the release the package published most recently, or None when the index dates none it can name.

    The release is read off the distribution files, pre-releases included. So a project that has just shipped a
    pre-release or a back-ported patch is dated by that upload. A file with no upload time is passed over, and so
    is one whose name holds no version, such as the `.egg` and `.exe` files PyPI stopped accepting.
    """
    return Release.newest(
        Release(version=str(version), published=published)
        for file in _project_metadata(package).get("files", [])
        if (published := parse_timestamp(file.get("upload-time"))) is not None
        and (version := _distribution_version(file.get("filename", ""))) is not None
    )


# The project status PEP 792 defines for a project that expects no further release.
_ARCHIVED_STATUS = "archived"


@archival_reporting
def project(package: DependencyName) -> Project:
    """Return what PyPI reports about the project: its newest release, and whether it declares it archived."""
    return Project(newest=_newest_release(package), archival=_archival(package))


def _archival(package: DependencyName) -> Archival:
    """Return what PyPI declares about the project: whether it is archived, and the reason published beside it."""
    project_status = _project_metadata(package).get("project-status") or {}
    # PEP 792's text spells the status key `state`, where PyPI's Index API documentation shows `status`.
    archived = project_status.get("status") == _ARCHIVED_STATUS
    return Archival(archived=archived, reason=project_status.get("reason") or "")


def _release_datetime(urls: list[_Distribution]) -> datetime | None:
    """Return the latest upload datetime of a release's distribution files, or None if there are none."""
    return newest_timestamp(url["upload_time_iso_8601"] for url in urls)


@archival_reporting
@publication_date_reporting
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
    candidates = [
        version
        for version in _stable_versions(package)
        if version > current and version_bound.keeps(version, current_version)
    ]
    latest = first_eligible(
        candidates, lambda version: _eligible_release(package, version, cooldown_days), current_version
    )
    latest = with_yank_state(latest, current_version, partial(yank_state, package))
    # Always attach what PyPI reports about the project, so a pin that is already up to date can still be reported
    # as stale or archived. It rides on the Index API response fetched above, so it costs no request of its own.
    return replace(latest, project=project(package))


def yank_state(package: str, version: str) -> Yank:
    """Return the version's yank state (PEP 592).

    The Index API lists each distribution file's yank state (`false`, `true`, or the reason string); a version is
    yanked when one of its files is. Files whose name doesn't parse to a version are skipped. The version asked
    about has to parse as one, since it is compared against the version each filename encodes.
    """
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
    - Check for a GitHub URL in the package description whose repository carries the package's name, and
      use that to find GitHub releases.
    - Check the root of each project URL that points at GitHub for a changelog file.
    """
    metadata = release_metadata(package, version)
    if metadata is None:
        return ""
    info = metadata["info"]
    urls = info.get("project_urls") or {}
    for label, url in urls.items():
        if _normalized_label(label) in _CHANGELOG_URL_LABELS and (changelog := _changelog_from_url(url, version)):
            return changelog
    repository_urls = _repository_urls(urls)
    for url in repository_urls:
        if changelog := _changelog_from_github_releases(url, package, version):
            return changelog
    if changelog := _changelog_from_description(info["description"], package, version):
        return changelog
    for url in repository_urls:
        if changelog := _changelog_from_repository_root(url, version):
            return changelog
    return ""


def get_publication_datetime(package: str, version: str) -> datetime | None:
    """Return the datetime the version was published, or None if it can't be fetched or has no distribution files."""
    metadata = release_metadata(package, version)
    return _release_datetime(metadata["urls"]) if metadata is not None else None


def _changelog_from_url(url: str, version: str) -> str:
    """Get the changelog from the URL."""
    changelog_response = fetch(github_to_raw(url), _LOG)
    if changelog_response is None:
        return ""
    if changelog_response.headers.get("Content-Type", "").startswith("text/html"):
        return ""
    return get_version_changes_from_changelog(changelog_response.text, version)


def _changelog_from_description(description: str, package: str, version: str) -> str:
    """Get the changelog from the description posted to PyPI, or from the releases of the repository it links."""
    return get_version_changes_from_changelog(description, version) or _changelog_from_github_url_in_description(
        description, package, version
    )


def _changelog_from_github_url_in_description(description: str, package: str, version: str) -> str:
    """Get the changelog from a GitHub URL in the description whose repository carries the package's name.

    A description links the repositories of other projects as well as the package's own, and another project's
    repository can hold a release tagged with the version asked for, whose notes describe that project's release.
    """
    for match in _GITHUB_URL_RE.finditer(description):
        if _names_the_package(match.group(), package):
            return _changelog_from_github_releases(match.group(), package, version)
    return ""


def _names_the_package(url: str, package: str) -> bool:
    """Return whether the URL points at a repository carrying the package's name."""
    _owner, repository = _github_repository(url)
    return normalized_name(repository) == normalized_name(package)


def _github_repository(url: str) -> tuple[str, str]:
    """Return the owner and repository the URL points at, or empty strings when it points at no repository.

    A `github.com/sponsors/…` URL parses as the repository `sponsors/<owner>`, which does not exist, so it is
    reported as no repository rather than queried.
    """
    owner, repository = github_owner_and_repository(url)
    return ("", "") if owner == _GITHUB_SPONSORS_PATH else (owner, repository)


def _changelog_from_github_releases(url: str, package: str, version: str) -> str:
    """Get the changelog from the GitHub releases."""
    owner, repository = _github_repository(url)
    return changes_from_release(owner, repository, package, version)


def _changelog_from_repository_root(url: str, version: str) -> str:
    """Get the changelog from a changelog file in the root of the repository."""
    owner, repository = _github_repository(url)
    return changes_from_changelog_file(owner, repository, version)
