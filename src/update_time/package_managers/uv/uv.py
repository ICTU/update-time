"""uv's cooldown setting, the layout of a uv workspace, and the sources of the dependencies uv resolves.

uv also recompiles a requirements file it compiled, as `UvPipCompile`.
"""

import os
import re
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, ClassVar, override

from update_time.domain.archival import archival_reporting
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion, normalized_python_name
from update_time.io.filesystem import is_scanned
from update_time.io.log import get_logger
from update_time.io.process import is_installed, run
from update_time.lock_files.uv_lock import UvLock
from update_time.manifests import pyproject_config
from update_time.manifests.pyproject_config import PyprojectConfig
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.markers.reference import SteeredResolvedReference
from update_time.package_managers.requirements_compiler import RequirementsCompiler
from update_time.primitives.command import Command
from update_time.sources.pypi import project

if TYPE_CHECKING:
    from collections.abc import Iterable

    from update_time.domain.dependency import DependencyName
    from update_time.io.log import Logger
    from update_time.manifests.pyproject_config import UvWorkspace
    from update_time.manifests.python_dependency_toml import (
        Declaration,
        PythonDependencyToml,
    )

_LOG = get_logger("pyproject.toml")
# Signals that a pyproject.toml is managed by a tool other than uv. Running uv on such a project would mishandle it
# (e.g. write a stray uv.lock alongside the real lockfile), so those files are skipped for now.
_NON_UV_TOOL_SECTIONS = ("poetry", "pdm")  # `[tool.poetry]` / `[tool.pdm]`
_NON_UV_LOCKFILES = {"poetry.lock": "poetry", "pdm.lock": "pdm"}

# Update-time writes its cooldown into `[tool.uv] exclude-newer` with the managed-by comment. It recognises the comment
# there by the lower-cased `update-time` word, so the prose can change.
_MANAGED_BY_COMMENT = "managed by Update-time — remove this comment to set exclude-newer yourself"
_MANAGED_BY_COMMENT_WORD = "update-time"
EXCLUDE_NEWER_OPTION = "--exclude-newer"
# What `--show-settings` prints about `exclude-newer`: `Some(…)` where the project's config, the user's config, or
# `UV_EXCLUDE_NEWER` sets one, and `None` where none of them does.
_SETTINGS_EXCLUDE_NEWER = re.compile(r"exclude_newer: ExcludeNewer \{\s*global: (?P<value>None|Some)\b")


def other_manager(pyproject_toml: Path, config: PyprojectConfig) -> str | None:
    """Return the Python dependency manager other than uv that the project signals (poetry, pdm), if any.

    A `[tool.poetry]`/`[tool.pdm]` section is authoritative; otherwise a sibling lockfile is used as a fallback.
    """
    for section in _NON_UV_TOOL_SECTIONS:
        if section in config.tools:
            return section
    for lockfile, manager in _NON_UV_LOCKFILES.items():
        if (pyproject_toml.parent / lockfile).exists():
            return manager
    return None


def workspace_root_is_scanned(pyproject_toml: Path) -> bool:
    """Return whether the scan reaches the root of the project's uv workspace, and log why not where it does not."""
    root = _workspace_root(pyproject_toml)
    if is_scanned(root):
        return True
    _LOG.workspace_root_outside_the_scan(pyproject_toml, "uv", root)
    return False


def manages(pyproject_toml: Path, config: PyprojectConfig) -> bool:
    """Return whether uv manages the project, and log why not where it does not.

    uv manages a project whose workspace root has a uv.lock beside it or a `[tool.uv]` table, unless that table sets
    `managed = false`. The project's own `config`, already parsed, serves where the project is its own workspace root.
    """
    root = _workspace_root(pyproject_toml)
    root_config = config if root == pyproject_toml else pyproject_config.read(root) or PyprojectConfig()
    if not root_config.uv.managed:
        _LOG.project_marked_unmanaged(pyproject_toml, root)
        return False
    lock = UvLock.beside(root)
    if lock.path.exists() or "uv" in root_config.tools:
        return True
    _LOG.not_a_uv_project(pyproject_toml, lock.path)
    return False


def configure_cooldown(pyproject_toml: Path) -> None:
    """Write `exclude-newer = "<N> days"`, with the managed-by comment, to the workspace root, unless a user set it.

    uv's `exclude-newer` is a publish-date cutoff that works as a cooldown (it accepts a relative duration such as
    `7 days`). Passing it only on the command line makes uv bake it into the lockfile and then treat a later `uv
    sync --locked` (without the flag) as stale, so Update-time writes it to config instead: uv reads the same cutoff
    on every command and the lockfile stays reproducible. uv reads `exclude-newer` from the workspace root, not from a
    member. The `UV_EXCLUDE_NEWER` environment variable is the user's own global override, so when it is set
    Update-time adds nothing.

    A file whose `tool` or `tool.uv` is not a table gets a warning instead. Without uv on the path, nothing is written,
    and the uv run that follows reports uv missing.

    A value carrying the managed-by comment is kept in sync with the cooldown; a value without the comment is
    the user's own and is left untouched, as is an already-current value carrying it (which is not rewritten).
    """
    if os.environ.get("UV_EXCLUDE_NEWER") or not is_installed("uv"):
        return
    file = PyprojectToml(pyproject_toml)
    existing = file.tool_key("uv", "exclude-newer")
    cooldown = _duration(COOLDOWN.get())
    if existing is None and settings_hold_exclude_newer(Command("uv", "lock"), pyproject_toml, _LOG):
        return  # An exclude-newer set outside the file, such as in a uv.toml: leave it alone.
    if existing is not None:
        value, comment = existing
        if _MANAGED_BY_COMMENT_WORD not in comment.lower():
            return  # A user-set exclude-newer: leave it alone.
        if value == cooldown:
            return  # Already Update-time's and already current.
    if file.set_tool_key("uv", "exclude-newer", cooldown, comment=_MANAGED_BY_COMMENT):
        _LOG.configured_uv_cooldown(pyproject_toml, cooldown)
    else:
        _LOG.uv_cooldown_not_set(pyproject_toml)


def settings_hold_exclude_newer(command: Command, file: Path, log: Logger) -> bool:
    """Return whether uv's settings for the command, run in the file's directory, hold an `exclude-newer`.

    The settings include an `exclude-newer` in the file itself. Without uv on the path, uv is not asked, and its
    settings hold none.
    """
    return is_installed("uv") and _settings_hold_exclude_newer(command, file.parent, file, log)


def _duration(days: int) -> str:
    """Return the number of days as uv reads a relative `exclude-newer`."""
    return f"{days} days"


def _settings_hold_exclude_newer(command: Command, directory: Path, file: Path, log: Logger) -> bool:
    """Return whether uv's settings for the file's command, run in the directory, hold an `exclude-newer`.

    Where Update-time cannot read from uv's settings whether they hold one, it warns and treats them as holding none.
    """
    settings = run(Command(*command, "--show-settings"), cwd=directory)
    if not (match := _SETTINGS_EXCLUDE_NEWER.search(settings.stdout)):
        log.uv_settings_unreadable(file)
        return False
    return match["value"] == "Some"


class UvPipCompile(RequirementsCompiler):
    """uv as the tool that recompiles a requirements file it compiled, with its `exclude-newer` as the cutoff."""

    tool: ClassVar[str] = "uv pip compile"
    setting: ClassVar[str] = "uv exclude-newer"
    cutoff_option: ClassVar[str] = EXCLUDE_NEWER_OPTION
    package_cooldown_option: ClassVar[str | None] = "--exclude-newer-package"

    @override
    def settings_hold_cutoff(self, command: Command) -> bool:
        """Return whether uv's settings for the command hold an `exclude-newer`."""
        return _settings_hold_exclude_newer(command, self.compiled.compile_directory, self.compiled.path, self.log)

    @override
    def duration(self, days: int) -> str:
        """Return the number of days as uv reads a relative `exclude-newer`."""
        return _duration(days)


def workspace_pyproject_tomls(pyproject_toml: Path) -> list[Path]:
    """Return the pyproject.toml of each project in the uv workspace of the project, or the project's own alone."""
    root = _workspace_root(pyproject_toml)
    if (workspace := _workspace_table(root)) is None:
        return [pyproject_toml]
    root_dir = root.parent
    members = {
        member / "pyproject.toml"
        for pattern in workspace.members
        for member in root_dir.glob(pattern)
        if (member / "pyproject.toml").exists() and _workspace_includes(root_dir, workspace, member)
    }
    return [root, *sorted(members - {root})]


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


def _workspace_table(pyproject_toml: Path) -> UvWorkspace | None:
    """Return the `[tool.uv.workspace]` table of the pyproject.toml, or None if it has none or can't be read.

    A missing (probing an ancestor) or malformed pyproject.toml reads back as None, so resolution keeps walking up
    rather than aborting.
    """
    config = pyproject_config.read(pyproject_toml)
    return None if config is None else config.uv.workspace


def _workspace_includes(root_dir: Path, workspace: UvWorkspace, project_dir: Path) -> bool:
    """Return whether the workspace rooted at `root_dir` includes the project in `project_dir`.

    uv treats the root project as a member of its own workspace, and every other project matched by a `members`
    glob and not excluded by an `exclude` glob (both relative to the root).
    """
    if project_dir == root_dir:
        return True
    # `root_dir` is always an ancestor of `project_dir` here (see `_workspace_root`), so this never raises.
    relative = PurePosixPath(project_dir.relative_to(root_dir).as_posix())
    if any(relative.full_match(pattern) for pattern in workspace.exclude):
        return False
    return any(relative.full_match(pattern) for pattern in workspace.members)


def pypi_served(declarations: Iterable[Declaration]) -> list[Declaration]:
    """Return each of the declarations that PyPI serves a release for."""
    return [declaration for declaration in declarations if declaration.names_a_release]


@archival_reporting
def pypi_projects(declarations: Iterable[Declaration]) -> Iterable[SteeredResolvedReference]:
    """Yield each of the declarations, carrying PyPI's report on the project it names."""
    for declaration in declarations:
        release = DependencyVersion.unpinned(project(declaration.dependency))
        yield SteeredResolvedReference.from_reference(declaration, release=release)


def by_workspace(pyproject_tomls: Iterable[PyprojectToml]) -> dict[Path, list[PyprojectToml]]:
    """Return the projects, grouped by the pyproject.toml of the uv workspace root governing each."""
    workspaces: dict[Path, list[PyprojectToml]] = {}
    for pyproject_toml in pyproject_tomls:
        workspaces.setdefault(_workspace_root(pyproject_toml.path), []).append(pyproject_toml)
    return workspaces


def declarations_of(file: PythonDependencyToml) -> list[Declaration]:
    """Return the file's declarations, each uv-sourced where the file, or the root of its uv workspace, sources it.

    `uv lock --script` locks a script on its own, so a script does not inherit the sources of a workspace root.
    """
    inherited = _workspace_sources(file.path) if isinstance(file, PyprojectToml) else frozenset()
    return [
        replace(declaration, uv_sourced=True)
        if normalized_python_name(declaration.dependency) in inherited
        else declaration
        for declaration in file.declarations()
    ]


def _workspace_sources(pyproject_toml: Path) -> frozenset[DependencyName]:
    """Return the names of the dependencies that the `[tool.uv] sources` of the workspace root resolve.

    A workspace member inherits these sources from the root.
    """
    config = pyproject_config.read(_workspace_root(pyproject_toml)) or PyprojectConfig()
    return config.uv.sources
