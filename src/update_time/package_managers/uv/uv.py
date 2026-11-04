"""uv's cooldown setting, the layout of a uv workspace, and uv's marker policy.

uv also recompiles a requirements file it compiled, as `UvPipCompile`.
"""

import os
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, ClassVar, override

from update_time.domain.archival import archival_reporting
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion, normalized_python_name
from update_time.formats import toml
from update_time.io.log import get_logger
from update_time.io.process import is_installed, run
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.manifests.python_dependency_toml import is_well_formed
from update_time.markers.directive import Reason
from update_time.markers.reference import SteeredResolvedReference
from update_time.package_managers.policy import ManagerPolicy, report_markers
from update_time.package_managers.requirements_compiler import RequirementsCompiler
from update_time.primitives.command import Command
from update_time.sources.pypi import project

if TYPE_CHECKING:
    from collections.abc import Iterable

    from update_time.domain.dependency import DependencyName
    from update_time.io.log import Logger
    from update_time.manifests.python_dependency_toml import (
        Declaration,
        PythonDependencyToml,
    )
    from update_time.markers.marker import Scope

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


def manager_name(pyproject_toml: Path, config: dict) -> str:
    """Return the project's Python dependency manager (uv, poetry, pdm), defaulting to uv.

    A `[tool.poetry]`/`[tool.pdm]` section is authoritative; otherwise a sibling lockfile is used as a fallback. The
    already-parsed `config` is passed in so the file isn't read again.
    """
    tool = config.get("tool", {})
    for section in _NON_UV_TOOL_SECTIONS:
        if section in tool:
            return section
    for lockfile, manager in _NON_UV_LOCKFILES.items():
        if (pyproject_toml.parent / lockfile).exists():
            return manager
    return "uv"


def configure_cooldown(pyproject_tomls: Iterable[PyprojectToml]) -> None:
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
    for root in by_workspace(pyproject_tomls):
        _persist_exclude_newer(root)


def _persist_exclude_newer(pyproject_toml: Path) -> None:
    """Write `exclude-newer = "<N> days"`, with the managed-by comment, to the pyproject.toml, unless a user set it.

    A file whose `tool` or `tool.uv` is not a table gets a warning instead. Without uv on the path, nothing is written,
    and the uv run that follows reports uv missing.

    A value carrying the managed-by comment is kept in sync with the cooldown; a value without the comment is
    the user's own and is left untouched, as is an already-current value carrying it (which is not rewritten).
    """
    if not is_installed("uv"):
        return
    file = PyprojectToml(pyproject_toml)
    existing = file.tool_key("uv", "exclude-newer")
    cooldown = _duration(COOLDOWN.get())
    uv_lock = Command("uv", "lock")
    if existing is None and _settings_hold_exclude_newer(uv_lock, pyproject_toml.parent, pyproject_toml, _LOG):
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
        for pattern in workspace.get("members", [])
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


def _workspace_table(pyproject_toml: Path) -> dict | None:
    """Return the `[tool.uv.workspace]` table of the pyproject.toml, or None if it has none or can't be read.

    A missing (probing an ancestor) or malformed pyproject.toml reads back as None, so resolution keeps walking up
    rather than aborting.
    """
    config = toml.read(pyproject_toml)
    if config is None or not is_well_formed(config):
        return None
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


def report_file_markers(file: PythonDependencyToml, log: Logger) -> list[Declaration]:
    """Report each declaration's marker, and return the declarations, each with the directives Update-time acts on."""
    return report_markers(file.declarations(), log, POLICY)


def _unchecked_scopes(declaration: Declaration) -> dict[Scope, Reason]:
    """Return each warning scope that Update-time leaves unchecked for the declaration, with the reason.

    Where PyPI does not serve a release for the declaration, Update-time leaves every warning scope unchecked.
    """
    return {} if declaration.names_a_release else ManagerPolicy.every_warning_scope(Reason.NO_PYPI_RELEASE)


# uv resolves the version an exact pin names, so a pin Update-time leaves as the file writes it stays where it is.
POLICY = ManagerPolicy(keeps_held_back_pins=True, unchecked_scopes=_unchecked_scopes)


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


def workspace_sources(pyproject_toml: Path) -> set[DependencyName]:
    """Return the names of the dependencies that the `[tool.uv] sources` of the workspace root resolve.

    A workspace member inherits these sources from the root.
    """
    config = toml.read(_workspace_root(pyproject_toml)) or {}
    return {normalized_python_name(name) for name in config.get("tool", {}).get("uv", {}).get("sources", {})}
