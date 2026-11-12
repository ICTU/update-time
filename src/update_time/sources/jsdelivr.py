"""jsDelivr CDN (limited to the npm packages referenced from a Sphinx config at the moment).

The version list and per-file Subresource Integrity hashes come from jsDelivr's package API at data.jsdelivr.com.
The publication date used for the cooldown comes from the npm registry, through the `npmjs` source, because
jsDelivr doesn't expose it.
"""

from functools import partial
from typing import TYPE_CHECKING

from packaging.version import Version

from update_time.domain.cooldown import CooldownWalk, first_eligible
from update_time.domain.dependency import (
    DependencyName,
    DependencyVersion,
    PinnedDependency,
    Unserved,
    VersionString,
    is_valid,
)
from update_time.domain.yank import with_yank_state
from update_time.io.fetch import fetch_json
from update_time.io.log import get_logger
from update_time.primitives.lookup import LookedUp
from update_time.sources.npmjs import deprecation, get_publication_datetime

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter, VersionBound

_LOG = get_logger("jsdelivr")
_HEADERS = {"User-Agent": "Update-time dependency update tool (https://github.com/ICTU/update-time)"}
_JSDELIVR_PACKAGE_API = "https://data.jsdelivr.com/v1/packages/npm"


def version_getter(filename: str) -> NewVersionGetter:
    """Return a new-version getter for the file a jsDelivr URL references.

    The file is part of the reference rather than of the dependency, and the `NewVersionGetter` contract leaves no
    room for it, so each reference closes over its own. The integrity hash resolved has to be the hash of the file
    the URL points at, not of the package's default entry point.
    """

    def get_latest_version(
        pinned: PinnedDependency, version_bound: VersionBound, cooldown_days: int
    ) -> DependencyVersion:
        """Return the latest jsDelivr version published outside the cooldown, with the file's integrity hash.

        Mirrors the pypi and oci sources: walk the available versions newest-first and pick the first one published
        outside the cooldown. Pre-releases, invalid versions, versions the bound rules out, and versions the
        npm registry has no publication date for (e.g. present on jsDelivr but not yet mirrored) are skipped.
        Returns the current version unchanged when it is invalid, already the newest eligible version, or the
        referenced file's integrity hash can't be resolved (updating the version without a matching hash would break
        the Subresource Integrity check). An invalid version comes back as one npm does not serve, so nothing dates it.
        """
        if not is_valid(pinned.version):
            return DependencyVersion(version=pinned.version, unserved=Unserved.VERSION)
        candidates = _candidate_versions(pinned, version_bound)
        latest = first_eligible(
            candidates,
            lambda version, walk: _eligible_version(pinned, version, filename, walk),
            lambda _: False,
            pinned.version,
            cooldown_days,
        )
        return with_yank_state(latest, pinned.version, partial(deprecation, pinned.name))

    return get_latest_version


def _eligible_version(
    pinned: PinnedDependency, version: Version, filename: str, walk: CooldownWalk
) -> DependencyVersion | None:
    """Return the version with its integrity hash when it's eligible, or None when it's too fresh or deprecated.

    A version that is eligible but whose referenced file has no integrity hash ends the walk: it returns the current
    version unchanged rather than skipping to an older one, since bumping without a matching hash would break the
    Subresource Integrity check. A version the npm registry dates nowhere counts as too fresh, since a version it
    has yet to date is one it has just received.
    """
    version_string = str(version)
    if (yank := deprecation(pinned.name, version_string)) is not None and yank.yanked:
        return None
    published = get_publication_datetime(pinned.name, version_string)
    if published is None or walk.holds_back(version_string, published):
        return None
    if integrity := integrity_hash(pinned.name, version_string, filename):
        return DependencyVersion(version_string, sha=integrity, publication=LookedUp(published))
    _LOG.no_integrity_hash(pinned.name, version_string, filename)
    return DependencyVersion(version=pinned.version)


def _candidate_versions(pinned: PinnedDependency, version_bound: VersionBound) -> list[Version]:
    """Return the stable versions newer than the pinned one that the bound admits (`first_eligible` orders them)."""
    if (listing := fetch_json(f"{_JSDELIVR_PACKAGE_API}/{pinned.name}", _LOG, headers=_HEADERS).value) is None:
        return []
    current_version = Version(pinned.version)
    versions = [Version(entry["version"]) for entry in listing.get("versions", []) if is_valid(entry["version"])]
    return [
        version
        for version in versions
        if version > current_version and not version.is_prerelease and version_bound.keeps(version, pinned.version)
    ]


def integrity_hash(dependency: DependencyName, version: VersionString, filename: str) -> str:
    """Return the Subresource Integrity hash of the referenced file at the version, or "" if it can't be read.

    The hash must match the file referenced in the URL, not the package's default entry point (which jsDelivr does
    not always list as a hashable file). An empty string signals the caller to leave the reference unchanged.
    """
    url = f"{_JSDELIVR_PACKAGE_API}/{dependency}@{version}?structure=flat"
    if (listing := fetch_json(url, _LOG, headers=_HEADERS).value) is None:
        return ""
    hashes = {entry["name"]: entry["hash"] for entry in listing["files"]}
    return f"sha256-{hashes[filename]}" if filename in hashes else ""
