"""Updater script for jsDelivr CDN URLs (limited to npm packages in the Sphinx config at the moment)."""

import re
from pathlib import Path

from update_time.domain.location import Location
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.references.rewrite import rewrite_match
from update_time.sources.jsdelivr import get_latest_version

LOG = get_logger("jsdelivr")
# Match a jsDelivr npm URL together with the Subresource Integrity hash that follows it, so both stay in sync. The
# file path after the version is captured so its (instead of the package default's) integrity hash is what gets updated.
JSDELIVR_RE = re.compile(
    r"https://cdn\.jsdelivr\.net/npm/(?P<dependency>[\w-]+)@(?P<version>[\d.]+)(?P<filename>/[^\"]*)"
    r".*?\"integrity\": \"(?P<sha>sha\d+-[A-Za-z0-9+/=]+)\"",
    re.DOTALL,
)


def update_jsdelivr(content: str, path: Path) -> str:
    """Update the version and integrity hash of all jsDelivr URLs in the content."""

    def replace(match: re.Match[str]) -> str:
        dependency, version, filename = match.group("dependency"), match.group("version"), match.group("filename")
        latest_version = get_latest_version(dependency, version, filename)
        # A whole-file substitution, not a line-based rewrite, so the URL's line isn't tracked: report file-only.
        location = Location(path)
        LOG.warn_if_stale(dependency, latest_version, location)
        LOG.warn_if_yanked(dependency, latest_version, location)
        if latest_version.version == version:
            return match.group(0)
        LOG.new_version(dependency, latest_version, location)
        return rewrite_match(match, {"version": latest_version.version, "sha": latest_version.sha})

    return JSDELIVR_RE.sub(replace, content)


def update_jsdelivrs() -> None:
    """Find the Sphinx config files under docs/ and update the jsDelivr URLs in them."""
    for sphinx_config_py in glob("conf.py", start=Path.cwd() / "docs"):
        LOG.path(sphinx_config_py)
        old_content = sphinx_config_py.read_text()
        new_content = update_jsdelivr(old_content, sphinx_config_py)
        if new_content != old_content:
            sphinx_config_py.write_text(new_content)


def main() -> None:  # pragma: no cover
    """Update the jsDelivr URLs in the repository's Sphinx configuration."""
    update_jsdelivrs()


if __name__ == "__main__":  # pragma: no cover
    main()
