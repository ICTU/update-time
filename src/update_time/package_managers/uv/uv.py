"""Whether uv runs for a pyproject.toml, uv's cooldown setting, the layout of a uv workspace, and uv's sources.

uv also recompiles a requirements file it compiled, as `UvPipCompile`.
"""

import os
import re
from dataclasses import dataclass, replace
from enum import Enum, auto
from graphlib import CycleError, TopologicalSorter
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, ClassVar, override

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import normalized_python_name
from update_time.io.filesystem import is_scanned
from update_time.io.log import get_logger
from update_time.io.process import is_installed, run
from update_time.lock_files.uv_lock import UvLock
from update_time.manifests import pyproject_config
from update_time.manifests.pyproject_config import PyprojectConfig
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.package_managers.requirements_compiler import RequirementsCompiler
from update_time.primitives.command import Command

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping

    from update_time.domain.dependency import DependencyName
    from update_time.io.log import Logger
    from update_time.manifests.pyproject_config import UvWorkspace
    from update_time.manifests.python_dependency_toml import (
        Declaration,
        PythonDependencyToml,
    )

_LOG = get_logger("pyproject.toml")
# The signals that a Python package manager manages a pyproject.toml: its lock file, by name, and its table under
# `[tool]`, which carries the manager's name. A build backend reads its settings from the tool's table as well, so the
# lock file decides before the table, and uv's table before the others.
_LOCK_FILES = {"uv.lock": "uv", "poetry.lock": "poetry", "pdm.lock": "pdm"}
_MANAGERS = tuple(_LOCK_FILES.values())

# Update-time writes its cooldown into `[tool.uv] exclude-newer` with the managed-by comment. It recognises the comment
# there by the lower-cased `update-time` word, so the prose can change.
_MANAGED_BY_COMMENT = "managed by Update-time — remove this comment to set exclude-newer yourself"
_MANAGED_BY_COMMENT_WORD = "update-time"
# The uv setting that holds a project's own cooldown, as the log names it.
EXCLUDE_NEWER_SETTING = "uv exclude-newer"
EXCLUDE_NEWER_OPTION = "--exclude-newer"
# What `--show-settings` prints about `exclude-newer`: `Some(…)` where the project's config, the user's config, or
# `UV_EXCLUDE_NEWER` sets one, and `None` where none of them does.
_SETTINGS_EXCLUDE_NEWER = re.compile(r"exclude_newer: ExcludeNewer \{\s*global: (?P<value>None|Some)\b")


@dataclass(frozen=True)
class _Signals:
    """The lock files beside a project, and the tables of its pyproject.toml, that name a Python package manager."""

    lock_files: tuple[Path, ...]
    tables: tuple[str, ...]

    def managers(self) -> tuple[str, ...]:
        """Return the managers that the lock files name, or else uv where it has a table, or else the tables' managers.

        A `[tool.uv]` table is uv's alone, while a build backend reads the other tables as well.
        """
        if self.lock_files:
            return tuple(_LOCK_FILES[lock_file.name] for lock_file in self.lock_files)
        return ("uv",) if "uv" in self.tables else self.tables


class Decision(Enum):
    """Whether uv runs for a pyproject.toml, or why not."""

    RUN = auto()
    UNMANAGED = auto()
    SEVERAL_MANAGERS = auto()
    NOTHING_TO_RESOLVE = auto()
    ROOT_OUTSIDE_THE_SCAN = auto()


def _signals(pyproject_toml: Path, config: PyprojectConfig) -> _Signals:
    """Return the project's signals, without uv's where the config sets `managed = false`."""
    managers = [manager for manager in _MANAGERS if manager != "uv" or config.uv.managed]
    lock_files = tuple(
        lock_file for lock_file in _lock_files(pyproject_toml.parent) if _LOCK_FILES[lock_file.name] in managers
    )
    return _Signals(lock_files, tuple(table for table in managers if table in config.tools))


def _lock_files(directory: Path) -> tuple[Path, ...]:
    """Return the lock file of each Python package manager that sits in the directory."""
    return tuple(lock_file for name in _LOCK_FILES if (lock_file := directory / name).exists())


def decision(pyproject_toml: Path, config: PyprojectConfig) -> Decision:
    """Return whether uv runs for the project, and log why not where it does not.

    The lock files beside the project's workspace root name its managers. Where none sits there, the tables of the
    root's pyproject.toml name them, uv's first. A `[tool.uv]` table that sets `managed = false` leaves uv's lock file
    and table out. A project that another manager manages, or more than one, gets its warning whether or not it
    declares a dependency. The project's own `config`, already parsed, serves where the project is its own workspace
    root.
    """
    root = _workspace_root(pyproject_toml)
    root_config = config if root == pyproject_toml else _config(root)
    signals = _signals(root, root_config)
    managers = signals.managers()
    if managers and managers != ("uv",):
        _warn_about_the_managers(pyproject_toml, root, signals)
        return Decision.SEVERAL_MANAGERS if len(managers) > 1 else Decision.UNMANAGED
    if not config.declares_a_dependency:
        _LOG.no_dependencies(pyproject_toml, "uv", "resolve")
        return Decision.NOTHING_TO_RESOLVE
    if not is_scanned(root):
        _LOG.workspace_root_outside_the_scan(pyproject_toml, "uv", root)
        return Decision.ROOT_OUTSIDE_THE_SCAN
    if managers:
        return Decision.RUN
    if root_config.uv.managed:
        _LOG.not_a_uv_project(pyproject_toml, UvLock.beside(root).path)
    else:
        _LOG.project_marked_unmanaged(pyproject_toml, root)
    return Decision.UNMANAGED


def _warn_about_the_managers(pyproject_toml: Path, root: Path, signals: _Signals) -> None:
    """Warn that the signals name a manager other than uv, or more than one, and point a member at its root's warning.

    A member whose workspace root lies outside the scan gets the warning itself, since the root gets none.
    """
    if pyproject_toml != root and is_scanned(root):
        _LOG.left_with_the_workspace_root(pyproject_toml, "uv", root)
    elif len(managers := signals.managers()) > 1:
        _LOG.signals_of_more_than_one_manager(pyproject_toml, signals.lock_files, signals.tables)
    else:
        _LOG.unsupported_package_manager(pyproject_toml, managers[0], "uv")


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
    the user's own and is left untouched, as is an already-current value carrying it (which is not rewritten). The
    setting that holds the user's own cooldown is logged.
    """
    if os.environ.get("UV_EXCLUDE_NEWER"):
        _LOG.project_cooldown(pyproject_toml, "UV_EXCLUDE_NEWER", COOLDOWN.get())
        return
    if not is_installed("uv"):
        return
    file = PyprojectToml(pyproject_toml)
    existing = file.tool_key("uv", "exclude-newer")
    cooldown = _duration(COOLDOWN.get())
    if existing is None and settings_hold_exclude_newer(Command("uv", "lock"), pyproject_toml, _LOG):
        # An exclude-newer set outside the file, such as in a uv.toml: leave it alone.
        _LOG.project_cooldown(pyproject_toml, EXCLUDE_NEWER_SETTING, COOLDOWN.get())
        return
    if existing is not None:
        value, comment = existing
        if _MANAGED_BY_COMMENT_WORD not in comment.lower():
            # A user-set exclude-newer: leave it alone.
            _LOG.project_cooldown(pyproject_toml, EXCLUDE_NEWER_SETTING, COOLDOWN.get())
            return
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
    setting: ClassVar[str] = EXCLUDE_NEWER_SETTING
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
    return _config(pyproject_toml).uv.workspace


def _config(pyproject_toml: Path) -> PyprojectConfig:
    """Return the config of the pyproject.toml, or an empty config where the file cannot be read or is not valid."""
    return pyproject_config.read(pyproject_toml) or PyprojectConfig()


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


def by_workspace(pyproject_tomls: Iterable[PyprojectToml]) -> list[dict[Path, list[PyprojectToml]]]:
    """Return the projects by the pyproject.toml of the uv workspace root governing each, in groups of workspaces.

    A group holds the workspaces whose projects source each other by path in a cycle, or else one workspace. Each
    group comes after the groups whose projects its projects source by path.
    """
    workspaces: dict[Path, list[PyprojectToml]] = {}
    for pyproject_toml in pyproject_tomls:
        workspaces.setdefault(_workspace_root(pyproject_toml.path), []).append(pyproject_toml)
    return _ordered_workspaces(workspaces)


def _ordered_workspaces(workspaces: Mapping[Path, list[PyprojectToml]]) -> list[dict[Path, list[PyprojectToml]]]:
    """Return the workspaces in groups, each after the groups holding a project its projects source by path.

    The workspaces in a cycle of path sources form one group, in the order the scan found them in.
    """
    sourced_roots = {root: _path_sourced_roots(projects) & workspaces.keys() for root, projects in workspaces.items()}
    group_of = {root: frozenset({root}) for root in workspaces}
    while True:
        sourced_groups = {
            group: {group_of[sourced] for root in group for sourced in sourced_roots[root]} - {group}
            for group in dict.fromkeys(group_of.values())
        }
        try:
            order = list(TopologicalSorter(sourced_groups).static_order())
        except CycleError as error:
            cycle = frozenset(root for group in error.args[1] for root in group)
            _LOG.path_source_cycle(root for root in workspaces if root in cycle)
            group_of |= dict.fromkeys(cycle, cycle)
            continue
        return [{root: projects for root, projects in workspaces.items() if root in group} for group in order]


def _path_sourced_roots(projects: Iterable[PyprojectToml]) -> set[Path]:
    """Return the workspace root of each project that the projects source by path.

    uv passes over a source for a dependency that no project of the workspace declares, and so does the order.
    """
    pyproject_tomls = {path for project in projects for path in _sourcing_files(project.path)}
    declared = {
        normalized_python_name(declaration.dependency)
        for pyproject_toml in pyproject_tomls
        for declaration in PyprojectToml(pyproject_toml).declarations()
    }
    return {
        _workspace_root(path)
        for pyproject_toml in pyproject_tomls
        for path in _path_sourced_projects(pyproject_toml, declared)
    }


def _path_sourced_projects(pyproject_toml: Path, declared: Collection[DependencyName]) -> list[Path]:
    """Return the pyproject.toml of each project the `[tool.uv] sources` of the project resolve from a path.

    A source counts where its dependency is one of the dependencies declared.
    """
    return [
        Path(os.path.normpath(pyproject_toml.parent / source.path)) / "pyproject.toml"
        for source in _config(pyproject_toml).uv.path_sources
        if source.dependency in declared
    ]


def declarations_of(file: PythonDependencyToml) -> list[Declaration]:
    """Return the file's declarations, each uv-sourced where the file, or the root of its uv workspace, sources it.

    `uv lock --script` locks a script on its own, so a script does not inherit the sources of a workspace root.
    """
    inherited = _applying_sources(file.path) if isinstance(file, PyprojectToml) else frozenset()
    return [
        replace(declaration, uv_sourced=True)
        if normalized_python_name(declaration.dependency) in inherited
        else declaration
        for declaration in file.declarations()
    ]


def _applying_sources(pyproject_toml: Path) -> frozenset[DependencyName]:
    """Return the names of the dependencies that the `[tool.uv] sources` applying to the project resolve."""
    return frozenset().union(*(_config(path).uv.sources for path in _sourcing_files(pyproject_toml)))


def _sourcing_files(pyproject_toml: Path) -> set[Path]:
    """Return the pyproject.toml whose `[tool.uv] sources` apply to the project: its own and its workspace root's.

    A workspace member inherits the sources of the root.
    """
    return {pyproject_toml, _workspace_root(pyproject_toml)}
