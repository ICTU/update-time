"""jsDelivr CDN (limited to the npm packages referenced from a Sphinx config at the moment).

Like the pypi and oci sources, this honours Update-time's cooldown: a version published within the cooldown window
is held back, so a freshly published (and possibly compromised) npm release isn't adopted immediately. The version
list and per-file Subresource Integrity hashes come from jsDelivr's package API (data.jsdelivr.com); the publication
date used for the cooldown comes from the npm registry (via the `npmjs` source), because jsDelivr doesn't expose it.
"""

from dataclasses import replace
from typing import TYPE_CHECKING

from packaging.version import Version

from update_time.domain.cooldown import within_cooldown
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.version import DependencyName, DependencyVersion, VersionString, first_eligible, is_valid
from update_time.io.fetch import fetch
from update_time.io.log import get_logger
from update_time.sources.npmjs import get_publication_datetime, newest_publication_date

if TYPE_CHECKING:
    from datetime import datetime

LOG = get_logger("jsdelivr")
HEADERS = {"User-Agent": "Update-time dependency update tool (https://github.com/ICTU/update-time)"}
JSDELIVR_PACKAGE_API = "https://data.jsdelivr.com/v1/packages/npm"


def get_latest_version(
    dependency: DependencyName, current_version_string: VersionString, filename: str
) -> DependencyVersion:
    """Return the latest jsDelivr version published outside the cooldown, with the referenced file's integrity hash.

    Mirrors the pypi and oci sources: walk the available versions newest-first and pick the first one published
    outside the cooldown window. Pre-releases, invalid versions, and versions the npm registry has no publication
    date for (e.g. present on jsDelivr but not yet mirrored) are skipped. Returns the current version unchanged when
    it is invalid, already the newest eligible version, or the referenced file's integrity hash can't be resolved
    (updating the version without a matching hash would break the Subresource Integrity check).
    """
    if not is_valid(current_version_string):
        return DependencyVersion(version=current_version_string)
    current_version = Version(current_version_string)
    candidates = _candidate_versions(dependency, current_version)
    latest = first_eligible(
        candidates,
        lambda version: _eligible_version(dependency, version, filename, current_version_string),
        current_version_string,
    )
    # Attach the newest npm publication date for the staleness check. Unlike the other sources this can cost an
    # extra npm request (when the reference is already at the newest version, so no candidate was resolved), so it
    # is skipped when the check is disabled; `is_stale` remains the single gate on whether a warning is emitted.
    newest_published = newest_publication_date(dependency) if STALE_AFTER.get() > 0 else None
    return replace(latest, newest_published=newest_published)


def _eligible_version(
    dependency: str, version: Version, filename: str, current_version_string: VersionString
) -> DependencyVersion | None:
    """Return the version with its integrity hash when it's past the cooldown, or None when it's still too fresh.

    A version that is past the cooldown but whose referenced file has no integrity hash ends the walk: it returns
    the current version unchanged rather than skipping to an older one, since bumping without a matching hash would
    break the Subresource Integrity check.
    """
    published = _publication_datetime(dependency, version)
    if published is None or within_cooldown(published):
        return None
    if integrity := _get_integrity_hash(dependency, version, filename):
        return DependencyVersion(str(version), sha=integrity, published=published)
    LOG.no_integrity_hash(dependency, str(version), filename)
    return DependencyVersion(version=current_version_string)


def _candidate_versions(dependency: str, current_version: Version) -> list[Version]:
    """Return the dependency's stable versions newer than the current one (`first_eligible` orders them)."""
    response = fetch(f"{JSDELIVR_PACKAGE_API}/{dependency}", LOG, headers=HEADERS)
    if response is None:
        return []
    versions = [
        Version(entry["version"]) for entry in response.json().get("versions", []) if is_valid(entry["version"])
    ]
    return [version for version in versions if version > current_version and not version.is_prerelease]


def _publication_datetime(dependency: str, version: Version) -> datetime | None:
    """Return the version's npm publication date, or None when the npm registry doesn't list it yet.

    A version can be on jsDelivr before the npm registry reports its release date; treat that as unknown (and so, in
    the caller, as too fresh to adopt) rather than crashing.
    """
    try:
        return get_publication_datetime(dependency, str(version))
    except KeyError:
        return None


def _get_integrity_hash(dependency: str, version: Version, filename: str) -> str:
    """Return the Subresource Integrity hash of the referenced file at the version, or "" if it isn't listed.

    The hash must match the file referenced in the URL, not the package's default entry point (which jsDelivr does
    not always list as a hashable file). An empty string signals the caller to leave the reference unchanged.
    """
    url = f"{JSDELIVR_PACKAGE_API}/{dependency}@{version}?structure=flat"
    response = fetch(url, LOG, headers=HEADERS)
    if response is None:
        return ""
    hashes = {entry["name"]: entry["hash"] for entry in response.json()["files"]}
    return f"sha256-{hashes[filename]}" if filename in hashes else ""
