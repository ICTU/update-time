"""Update uv-managed pyproject.toml dependencies (work-around for the missing `uv update` command).

See https://github.com/astral-sh/uv/issues/6794. Note: only exact-match version specs (`==`) are upgraded. Version
specs with other clauses (`<=`, `~=`, etc.) are ignored, so a `package<=max version` spec opts a dependency out.
"""

import os
from pathlib import Path, PurePosixPath

from update_time.domain.cooldown import cooldown_days
from update_time.domain.version import DependencyVersion
from update_time.file_formats import pyproject_toml as pyproject_toml_format
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


def python_manager(pyproject_toml: Path, config: dict) -> str:
    """Return the project's Python dependency manager (uv, poetry, pdm), defaulting to uv.

    A `[tool.poetry]`/`[tool.pdm]` section is authoritative; otherwise a sibling lockfile is used as a fallback. The
    already-parsed `config` is passed in so the file isn't read again.
    """
    tool = config.get("tool", {})
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
    existing = pyproject_toml_format.tool_key(pyproject_toml, "uv", "exclude-newer")
    cooldown = f"{cooldown_days()} days"
    if existing is not None:
        value, comment = existing
        if EXCLUDE_NEWER_MARKER not in comment.lower():
            return  # A user-set exclude-newer: leave it alone.
        if value == cooldown:
            return  # Already Update-time's and already current.
    pyproject_toml_format.set_tool_key(pyproject_toml, "uv", "exclude-newer", cooldown, comment=EXCLUDE_NEWER_COMMENT)
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
    """Return the `[tool.uv.workspace]` table of the pyproject.toml, or None if it has none or can't be read.

    A missing (probing an ancestor) or malformed pyproject.toml reads back as None, so resolution keeps walking up
    rather than aborting.
    """
    config = pyproject_toml_format.read(pyproject_toml) or {}
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
    latest_versions = dict(parse_line_with_update(line) for line in lines_with_updates)
    pyproject_toml_format.rewrite_pinned_versions(pyproject_toml, latest_versions)
    return True


def update_uv_lock(pyproject_toml: Path) -> None:
    """Update the uv.lock file for the pyproject.toml."""
    LOG.path(pyproject_toml.parent / "uv.lock")
    uv_lock = ["uv", "lock", "--directory", str(pyproject_toml.parent), "--upgrade", "--quiet", "--no-progress"]
    run(uv_lock)
