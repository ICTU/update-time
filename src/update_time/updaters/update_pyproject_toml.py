"""Work-around for the missing `uv update` command, see https://github.com/astral-sh/uv/issues/6794.

Note: This script only considers matching versions ("==") for upgrading. Version specs with other version clauses
("<=", "~=", etc.) are ignored. This means that a version can be prevented from being updated, by using
"package<=max version" as version spec.
"""

import os
import re
import sys
import tomllib
from typing import TYPE_CHECKING

from update_time.domain.cooldown import cooldown_days
from update_time.domain.version import DependencyVersion
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.io.process import run
from update_time.sources.pypi import get_changes, get_publication_datetime

if TYPE_CHECKING:
    from pathlib import Path

LOG = get_logger("pyproject.toml")


def exclude_newer_options(pyproject_toml: Path) -> list[str]:
    """Return the uv option that applies Update-time's cooldown, or nothing if the project configures its own.

    uv's `exclude-newer` is a publish-date cutoff that works as a cooldown (it accepts a relative duration such as
    `7 days`). uv reads its own cutoff from the `UV_EXCLUDE_NEWER` environment variable or `[tool.uv]` in the
    pyproject.toml; when either is set, that wins and Update-time adds nothing. The option is passed to both `uv
    tree --outdated` and `uv lock --upgrade` so the reported and the locked versions agree.
    """
    if os.environ.get("UV_EXCLUDE_NEWER"):
        return []
    config = tomllib.loads(pyproject_toml.read_text())
    if "exclude-newer" in config.get("tool", {}).get("uv", {}):
        return []
    return ["--exclude-newer", f"{cooldown_days()} days"]


class Versions:
    """Mapping of package names to versions."""

    def __init__(self, package_versions: dict[str, str]) -> None:
        """Keep track of the versions, a mapping of package names to latest versions."""
        self.package_versions = package_versions

    def get_package_spec(self, match: re.Match) -> str:
        """Return a package spec for the package name with the latest version if available or the old version if not."""
        name = match.group("name")
        version = self.package_versions.get(name.lower(), match.group("version"))
        return f'"{name}=={version}"'


def parse_line_with_update(line: str) -> tuple[str, str]:
    """Parse the package name and latest version from a `uv tree --outdated` line, e.g. '| package (latest: v1.1)'."""
    fields = line.split()
    return fields[1], fields[-1].lstrip("v").rstrip(")")


def update_pyproject_toml(pyproject_toml: Path) -> None:
    """Update the pyproject.toml file with latest version of dependencies."""
    LOG.path(pyproject_toml)
    # `uv tree --outdated` only honors the cooldown (exclude-newer) when not run with `--frozen`, so it is omitted.
    uv_tree = [
        "uv",
        "tree",
        "--directory",
        str(pyproject_toml.parent),
        "--quiet",
        "--depth=1",
        "--all-groups",
        "--outdated",
        *exclude_newer_options(pyproject_toml),
    ]
    outdated = run(uv_tree)
    lines_with_updates = [line for line in outdated.splitlines() if " (latest: " in line]
    for line in lines_with_updates:
        package, version = parse_line_with_update(line)
        changes = get_changes(package, version)
        published = get_publication_datetime(package, version)
        dependency_version = DependencyVersion(version, changes, published=published)
        LOG.new_version(package, dependency_version, pyproject_toml)
    latest_versions = Versions(dict(parse_line_with_update(line) for line in lines_with_updates))
    package_spec = re.compile(r'"(?P<name>[A-Za-z0-9_.\-]+)==(?P<version>[A-Za-z0-9_.\-]+)"')
    current_pyproject_toml = pyproject_toml.read_text()
    updated_pyproject_toml = package_spec.sub(latest_versions.get_package_spec, current_pyproject_toml)
    if updated_pyproject_toml != current_pyproject_toml:
        pyproject_toml.write_text(updated_pyproject_toml)


def update_uv_lock(pyproject_toml: Path) -> None:
    """Update the uv.lock file for the pyproject.toml."""
    LOG.path(pyproject_toml.parent / "uv.lock")
    uv_lock = ["uv", "lock", "--directory", str(pyproject_toml.parent), "--upgrade", "--quiet", "--no-progress"]
    run([*uv_lock, *exclude_newer_options(pyproject_toml)])


def update_pyproject_tomls() -> int:
    """Find all pyproject.toml files, update them, and then update the uv.lock files."""
    files = list(glob("pyproject.toml"))
    for pyproject_toml in files:
        update_pyproject_toml(pyproject_toml)
    for pyproject_toml in files:
        update_uv_lock(pyproject_toml)
    return 0


def main() -> int:  # pragma: no cover
    """Update the dependencies in the repository's pyproject.toml files."""
    return update_pyproject_tomls()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
