"""jsDelivr CDN (limited to the npm packages referenced from a Sphinx config at the moment).

The version list and per-file Subresource Integrity hashes come from jsDelivr's package API at data.jsdelivr.com.
The publication date used for the cooldown comes from the npm registry, through the `npmjs` source, because
jsDelivr doesn't expose it.
"""

from dataclasses import replace
from typing import TYPE_CHECKING

from packaging.version import Version

from update_time.domain.cooldown import within_cooldown
from update_time.domain.publication import publication_date_reporting
from update_time.domain.version import DependencyName, DependencyVersion, VersionString, first_eligible, is_valid
from update_time.domain.vulnerability import vulnerability_reporting
from update_time.domain.yank import yank_reporting
from update_time.io.fetch import fetch
from update_time.io.log import get_logger
from update_time.sources.npmjs import deprecation, get_publication_datetime, newest_publication_date

if TYPE_CHECKING:
    from datetime import datetime

    from update_time.domain.bound import NewVersionGetter, VersionBound

_LOG = get_logger("jsdelivr")
_HEADERS = {"User-Agent": "Update-time dependency update tool (https://github.com/ICTU/update-time)"}
_JSDELIVR_PACKAGE_API = "https://data.jsdelivr.com/v1/packages/npm"


def version_getter(filename: str) -> NewVersionGetter:
    """Return a new-version getter for the file a jsDelivr URL references.

    The file is part of the reference rather than of the dependency, and the `NewVersionGetter` contract leaves no
    room for it, so each reference closes over its own. The integrity hash resolved has to be the hash of the file
    the URL points at, not of the package's default entry point. Every getter is registered as yank-reporting, since
    the versions it resolves can carry npm's per-version deprecation as their yank, and as vulnerability-reporting,
    since OSV holds advisories for the npm releases they name.
    """

    def get_latest_version(
        dependency: DependencyName,
        current_version_string: VersionString,
        version_bound: VersionBound,
        cooldown_days: int,
    ) -> DependencyVersion:
        """Return the latest jsDelivr version published outside the cooldown, with the file's integrity hash.

        Mirrors the pypi and oci sources: walk the available versions newest-first and pick the first one published
        outside the cooldown window. Pre-releases, invalid versions, versions the bound rules out, and versions the
        npm registry has no publication date for (e.g. present on jsDelivr but not yet mirrored) are skipped.
        Returns the current version unchanged when it is invalid, already the newest eligible version, or the
        referenced file's integrity hash can't be resolved (updating the version without a matching hash would break
        the Subresource Integrity check).
        """
        if not is_valid(current_version_string):
            return DependencyVersion(version=current_version_string)
        candidates = _candidate_versions(dependency, current_version_string, version_bound)
        latest = first_eligible(
            candidates,
            lambda version: _eligible_version(dependency, version, filename, current_version_string, cooldown_days),
            current_version_string,
        )
        # When the run leaves the reference on its current version, attach that version's deprecation as a yank so a
        # pin left on a deprecated release can be warned about; whether to warn is decided by `warn_if_yanked`.
        if latest.version == current_version_string:
            latest = replace(latest, yank=deprecation(dependency, current_version_string))
        # Attach the newest npm publication date for the staleness check.
        return replace(latest, newest_published=newest_publication_date(dependency))

    return publication_date_reporting(vulnerability_reporting(yank_reporting(get_latest_version)))


def _eligible_version(
    dependency: str, version: Version, filename: str, current_version_string: VersionString, cooldown_days: int
) -> DependencyVersion | None:
    """Return the version with its integrity hash when it's eligible, or None when it's too fresh or deprecated.

    A version that is eligible but whose referenced file has no integrity hash ends the walk: it returns the current
    version unchanged rather than skipping to an older one, since bumping without a matching hash would break the
    Subresource Integrity check.
    """
    published = _publication_datetime(dependency, version)
    if published is None or within_cooldown(published, cooldown_days):
        return None
    version_string = str(version)
    if deprecation(dependency, version_string).yanked:
        return None
    if integrity := integrity_hash(dependency, version_string, filename):
        return DependencyVersion(version_string, sha=integrity, published=published)
    _LOG.no_integrity_hash(dependency, version_string, filename)
    return DependencyVersion(version=current_version_string)


def _candidate_versions(
    dependency: str, current_version_string: VersionString, version_bound: VersionBound
) -> list[Version]:
    """Return the stable versions newer than the current one that the bound admits (`first_eligible` orders them)."""
    response = fetch(f"{_JSDELIVR_PACKAGE_API}/{dependency}", _LOG, headers=_HEADERS)
    if response is None:
        return []
    current_version = Version(current_version_string)
    versions = [
        Version(entry["version"]) for entry in response.json().get("versions", []) if is_valid(entry["version"])
    ]
    return [
        version
        for version in versions
        if version > current_version
        and not version.is_prerelease
        and version_bound.keeps(version, current_version_string)
    ]


def _publication_datetime(dependency: str, version: Version) -> datetime | None:
    """Return the version's npm publication date, or None when the npm registry doesn't list it yet.

    A version can be on jsDelivr before the npm registry reports its release date; treat that as unknown (and so, in
    the caller, as too fresh to adopt) rather than crashing.
    """
    try:
        return get_publication_datetime(dependency, str(version))
    except KeyError:
        return None


def integrity_hash(dependency: DependencyName, version: VersionString, filename: str) -> str:
    """Return the Subresource Integrity hash of the referenced file at the version, or "" if it isn't listed.

    The hash must match the file referenced in the URL, not the package's default entry point (which jsDelivr does
    not always list as a hashable file). An empty string signals the caller to leave the reference unchanged.
    """
    url = f"{_JSDELIVR_PACKAGE_API}/{dependency}@{version}?structure=flat"
    response = fetch(url, _LOG, headers=_HEADERS)
    if response is None:
        return ""
    hashes = {entry["name"]: entry["hash"] for entry in response.json()["files"]}
    return f"sha256-{hashes[filename]}" if filename in hashes else ""
