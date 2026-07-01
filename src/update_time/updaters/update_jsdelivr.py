"""Updater script for jsdelivr CDN URLs (limited to NPM packages in the Sphinx config at the moment).

Like the pypi and oci sources, this honours Update-time's cooldown: a version published within the cooldown window
is held back, so a freshly published (and possibly compromised) npm release isn't adopted immediately.
"""

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from packaging.version import Version

from update_time.domain.cooldown import within_cooldown
from update_time.domain.version import DependencyName, DependencyVersion, VersionString, is_valid
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.sources.npmjs import get_publication_datetime

if TYPE_CHECKING:
    from datetime import datetime

LOG = get_logger("jsdelivr")
HEADERS = {"User-Agent": "Update-time dependency update tool (https://github.com/ICTU/update-time)"}
JSDELIVR_PACKAGE_API = "https://data.jsdelivr.com/v1/packages/npm"
# Match a jsDelivr npm URL together with the Subresource Integrity hash that follows it, so both stay in sync. The
# file path after the version is captured so its (instead of the package default's) integrity hash is what gets updated.
JSDELIVR_RE = re.compile(
    r"https://cdn\.jsdelivr\.net/npm/(?P<dependency>[\w-]+)@(?P<version>[\d.]+)(?P<filename>/[^\"]*)"
    r".*?\"integrity\": \"(?P<sha>sha\d+-[A-Za-z0-9+/=]+)\"",
    re.DOTALL,
)


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
    for version in _candidate_versions(dependency, current_version):
        if (published := _publication_datetime(dependency, version)) and not within_cooldown(published):
            if integrity := _get_integrity_hash(dependency, version, filename):
                return DependencyVersion(str(version), sha=integrity, published=published)
            LOG.no_integrity_hash(dependency, str(version), filename)
            return DependencyVersion(version=current_version_string)
    return DependencyVersion(version=current_version_string)


def _candidate_versions(dependency: str, current_version: Version) -> list[Version]:
    """Return the dependency's stable versions newer than the current one, newest first."""
    response = requests.get(f"{JSDELIVR_PACKAGE_API}/{dependency}", headers=HEADERS, timeout=10)
    response.raise_for_status()
    versions = [
        Version(entry["version"]) for entry in response.json().get("versions", []) if is_valid(entry["version"])
    ]
    newer = [version for version in versions if version > current_version and not version.is_prerelease]
    return sorted(newer, reverse=True)


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
    response = requests.get(url, headers=HEADERS, timeout=10)
    response.raise_for_status()
    hashes = {entry["name"]: entry["hash"] for entry in response.json()["files"]}
    return f"sha256-{hashes[filename]}" if filename in hashes else ""


def update_jsdelivr(content: str, path: Path) -> str:
    """Update the version and integrity hash of all jsDelivr URLs in the content."""

    def replace(match: re.Match[str]) -> str:
        dependency, version, filename = match.group("dependency"), match.group("version"), match.group("filename")
        latest_version = get_latest_version(dependency, version, filename)
        if latest_version.version == version:
            return match.group(0)
        LOG.new_version(dependency, latest_version, path)
        return match.group(0).replace(version, latest_version.version).replace(match.group("sha"), latest_version.sha)

    return JSDELIVR_RE.sub(replace, content)


def update_jsdelivrs() -> int:
    """Find the Sphinx config files under docs/ and update the jsDelivr URLs in them."""
    for sphinx_config_py in glob("conf.py", start=Path.cwd() / "docs"):
        LOG.path(sphinx_config_py)
        old_content = sphinx_config_py.read_text()
        new_content = update_jsdelivr(old_content, sphinx_config_py)
        if new_content != old_content:
            sphinx_config_py.write_text(new_content)
    return 0


def main() -> int:  # pragma: no cover
    """Update the jsDelivr URLs in the repository's Sphinx configuration."""
    return update_jsdelivrs()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
