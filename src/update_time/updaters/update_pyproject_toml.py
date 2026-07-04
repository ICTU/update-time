"""Work-around for the missing `uv update` command, see https://github.com/astral-sh/uv/issues/6794.

Note: This script only considers matching versions ("==") for upgrading. Version specs with other version clauses
("<=", "~=", etc.) are ignored. This means that a version can be prevented from being updated, by using
"package<=max version" as version spec.
"""

import os
import re
import sys
import tomllib
from pathlib import Path, PurePosixPath

import tomlkit

from update_time.domain.cooldown import cooldown_days
from update_time.domain.version import DependencyVersion
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.io.process import run
from update_time.sources.pypi import get_changes, get_publication_datetime

LOG = get_logger("pyproject.toml")
# Signals that a pyproject.toml is managed by a tool other than uv. Running uv on such a project would mishandle it
# (e.g. write a stray uv.lock alongside the real lockfile), so those files are skipped for now.
NON_UV_TOOL_SECTIONS = ("poetry", "pdm")  # `[tool.poetry]` / `[tool.pdm]`
NON_UV_LOCKFILES = {"poetry.lock": "poetry", "pdm.lock": "pdm"}

# Update-time writes its cooldown into `[tool.uv] exclude-newer` with this comment. The comment both explains the
# line and marks it as Update-time's own: a value carrying the marker is kept in sync with `--cooldown`, one without
# it is the user's and left untouched. Detection is on the lower-cased `update-time` token, so the prose can change.
EXCLUDE_NEWER_COMMENT = "managed by Update-time — remove this comment to prevent Update-time from changing it"
EXCLUDE_NEWER_MARKER = "update-time"


def python_manager(pyproject_toml: Path) -> str:
    """Return the project's Python dependency manager (uv, poetry, pdm), defaulting to uv.

    A `[tool.poetry]`/`[tool.pdm]` section is authoritative; otherwise a sibling lockfile is used as a fallback.
    """
    tool = tomllib.loads(pyproject_toml.read_text()).get("tool", {})
    for section in NON_UV_TOOL_SECTIONS:
        if section in tool:
            return section
    for lockfile, manager in NON_UV_LOCKFILES.items():
        if (pyproject_toml.parent / lockfile).exists():
            return manager
    return "uv"


def configure_cooldown(pyproject_tomls: list[Path]) -> None:
    """Persist Update-time's cooldown into the `[tool.uv] exclude-newer` of each involved workspace root.

    uv's `exclude-newer` is a publish-date cutoff that works as a cooldown (it accepts a relative duration such as
    `7 days`). Passing it only on the command line makes uv bake it into the lockfile and then treat a later `uv
    sync --locked` (without the flag) as stale, so Update-time writes it to config instead: uv reads the same cutoff
    on every command and the lockfile stays reproducible. The `UV_EXCLUDE_NEWER` environment variable is the user's
    own global override, so when it is set Update-time adds nothing. uv reads `exclude-newer` from the workspace
    root, so the setting is written once per root, not into each member manifest.
    """
    if os.environ.get("UV_EXCLUDE_NEWER"):
        return
    roots: list[Path] = []
    for pyproject_toml in pyproject_tomls:
        if (root := _workspace_root(pyproject_toml)) not in roots:
            roots.append(root)
    for root in roots:
        _persist_exclude_newer(root)


def _persist_exclude_newer(pyproject_toml: Path) -> None:
    """Write `exclude-newer = "<N> days"` (tagged as Update-time's) to the pyproject.toml, unless a user set it.

    A value carrying Update-time's marker comment is kept in sync with the cooldown; a value without the marker is
    the user's own and is left untouched, as is an already-current marked value (which is not rewritten).
    """
    document = tomlkit.parse(pyproject_toml.read_text())
    existing = document.get("tool", {}).get("uv", {}).get("exclude-newer")
    cooldown = f"{cooldown_days()} days"
    if existing is not None:
        if EXCLUDE_NEWER_MARKER not in existing.trivia.comment.lower():
            return  # A user-set exclude-newer: leave it alone.
        if str(existing) == cooldown:
            return  # Already Update-time's and already current.
    tool = document.setdefault("tool", tomlkit.table(is_super_table=True))
    if "uv" not in tool:
        tool["uv"] = tomlkit.table()
    item = tomlkit.item(cooldown)
    item.comment(EXCLUDE_NEWER_COMMENT)
    tool["uv"]["exclude-newer"] = item
    pyproject_toml.write_text(tomlkit.dumps(document))
    LOG.configured_uv_cooldown(pyproject_toml, cooldown)


def _workspace_root(pyproject_toml: Path) -> Path:
    """Return the pyproject.toml of the uv workspace root governing the project, or the project itself.

    uv reads `exclude-newer` from the workspace root, not from a member manifest's own `[tool.uv]`, so walk up from
    the project's directory: the root is the nearest ancestor whose pyproject.toml declares a `[tool.uv.workspace]`
    that includes this project. A project that is part of no workspace (the common case) is its own root.
    """
    project_dir = pyproject_toml.parent
    for ancestor in (project_dir, *project_dir.parents):
        candidate = ancestor / "pyproject.toml"
        workspace = _workspace_table(candidate)
        if workspace is not None and _workspace_includes(ancestor, workspace, project_dir):
            return candidate
    return pyproject_toml


def _workspace_table(pyproject_toml: Path) -> dict | None:
    """Return the `[tool.uv.workspace]` table of the pyproject.toml, or None if it has none or can't be read."""
    try:
        text = pyproject_toml.read_text()
    except OSError:
        return None  # No pyproject.toml at this ancestor (or it can't be read): keep walking up.
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None  # An unrelated, malformed pyproject.toml up the tree shouldn't break resolution.
    return config.get("tool", {}).get("uv", {}).get("workspace")


def _workspace_includes(root_dir: Path, workspace: dict, project_dir: Path) -> bool:
    """Return whether the workspace rooted at `root_dir` includes the project in `project_dir`.

    uv treats the root project as a member of its own workspace, and every other project matched by a `members`
    glob and not excluded by an `exclude` glob (both relative to the root).
    """
    if project_dir == root_dir:
        return True
    # `root_dir` is always an ancestor of `project_dir` here (see `_workspace_root`), so this never raises.
    relative = PurePosixPath(project_dir.relative_to(root_dir).as_posix())
    if any(relative.full_match(pattern) for pattern in workspace.get("exclude", [])):
        return False
    return any(relative.full_match(pattern) for pattern in workspace.get("members", []))


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


def update_pyproject_toml(pyproject_toml: Path) -> bool:
    """Update the pyproject.toml with the latest dependency versions; return whether `uv tree` succeeded.

    When `uv tree` fails (e.g. the registry is unreachable) nothing can be determined to update, and re-locking
    would fail the same way, so the caller skips the lockfile update for this file rather than trying it in vain.
    """
    LOG.path(pyproject_toml)
    # The cooldown lives in `[tool.uv] exclude-newer` (see `configure_cooldown`), which uv reads for `tree` as well.
    # `--frozen` is omitted because `uv tree --outdated` only honors that cooldown when it is free to re-resolve.
    uv_tree = [
        "uv",
        "tree",
        "--directory",
        str(pyproject_toml.parent),
        "--quiet",
        "--depth=1",
        "--all-groups",
        "--outdated",
    ]
    outdated = run(uv_tree)
    if not outdated.ok:
        return False
    lines_with_updates = [line for line in outdated.stdout.splitlines() if " (latest: " in line]
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
    return True


def update_uv_lock(pyproject_toml: Path) -> None:
    """Update the uv.lock file for the pyproject.toml."""
    LOG.path(pyproject_toml.parent / "uv.lock")
    uv_lock = ["uv", "lock", "--directory", str(pyproject_toml.parent), "--upgrade", "--quiet", "--no-progress"]
    run(uv_lock)


def update_pyproject_tomls() -> int:
    """Find all uv-managed pyproject.toml files, update them, and then update the uv.lock files."""
    files = []
    for pyproject_toml in glob("pyproject.toml"):
        if (manager := python_manager(pyproject_toml)) == "uv":
            files.append(pyproject_toml)
        else:
            LOG.unsupported_package_manager(pyproject_toml, manager, "uv")
    # Persist the cooldown into config before running uv, so `uv tree`/`uv lock` read it and the lockfile they
    # produce stays reproducible with a plain `uv sync --locked` (see `configure_cooldown`).
    configure_cooldown(files)
    # Update every pyproject.toml first, then lock (a uv workspace shares one lockfile, so all member manifests
    # must be bumped before locking). Skip the lockfile update for files whose `uv tree` failed: it would fail too.
    updated = [pyproject_toml for pyproject_toml in files if update_pyproject_toml(pyproject_toml)]
    for pyproject_toml in updated:
        update_uv_lock(pyproject_toml)
    return 0


def main() -> int:  # pragma: no cover
    """Update the dependencies in the repository's pyproject.toml files."""
    return update_pyproject_tomls()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
