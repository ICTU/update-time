"""Move the exact pins of a uv workspace's pyproject.tomls to the versions uv locks, and report the new versions."""

from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Self

from update_time.domain.dependency import PinnedDependency, normalized_python_name
from update_time.domain.version_scheme import PEP_440
from update_time.io.log import get_logger
from update_time.io.process import is_installed, run
from update_time.lock_files.uv_lock import UvLock
from update_time.markers.directive import Reason
from update_time.package_managers.policy import ManagerPolicy, report_markers
from update_time.package_managers.requirements_compiler import log_moved_versions, log_new_version
from update_time.package_managers.uv.uv import by_workspace, marker_policy, pypi_served, workspace_sources
from update_time.primitives.command import Command

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.manifests.pyproject_toml import PyprojectToml
    from update_time.manifests.python_dependency_toml import Declaration, PythonDependencyToml
    from update_time.primitives.location import Location

type _RecordedVersions = Mapping[DependencyName, Sequence[VersionString]]

_LOG = get_logger("pyproject.toml")


@dataclass(frozen=True)
class _MemberSnapshot:
    """A member of a uv workspace, with what its pyproject.toml held and the uv.lock recorded for it before uv ran."""

    pyproject_toml: PyprojectToml
    source: str  # The member's directory relative to the workspace root, as the uv.lock names it.
    text: str
    locked_before: list[PinnedDependency] | None

    def steered_member(self, locked_as_written: _RecordedVersions | None) -> _Member:
        """Report the path and the markers of the pyproject.toml, and return the member with the markers it acts on.

        A declaration without an exact pin takes its current version from what uv locks for the files as written.
        """
        member_policy = _member_policy(self.pyproject_toml, locked_as_written)
        declarations = _report_path_and_markers(self.pyproject_toml, member_policy)
        member = _Member(
            self.pyproject_toml,
            self.source,
            self.text,
            declarations,
            self.locked_before,
            locked_as_written,
            member_policy,
        )
        member.warn_about_redundant_bounds()
        return member


@dataclass(frozen=True)
class _Member:
    """A member of a uv workspace, with its declarations and the policy their markers are judged by.

    The versions locked before the run are what the report of new and moved versions compares with. The versions uv
    locks for the files as written give a declaration without an exact pin its current version.
    """

    pyproject_toml: PyprojectToml
    source: str  # The member's directory relative to the workspace root, as the uv.lock names it.
    text: str
    declarations: list[Declaration]
    locked_before: list[PinnedDependency] | None
    locked_as_written: _RecordedVersions | None
    policy: ManagerPolicy[Declaration]

    @property
    def lockable_pins(self) -> list[Declaration]:
        """Return the declarations uv locks that pin an exact version their marker leaves to update."""
        return [
            declaration
            for declaration in self.declarations
            if declaration.pins_a_version and declaration.updatable and not declaration.build_requirement
        ]

    @property
    def updatable_pins(self) -> list[Declaration]:
        """Return the lockable pins without an environment marker, the ones Update-time moves to a locked version.

        uv can lock a package at another version for each Python. A pin without an environment marker takes the lowest
        of them. A pin with one would need the version uv locked for its own Pythons, and that need not be the lowest.
        """
        return [declaration for declaration in self.narrowable_declarations if declaration.pins_a_version]

    @property
    def narrowable_declarations(self) -> list[Declaration]:
        """Return the declarations whose requirement a bound narrows while uv locks."""
        return [declaration for declaration in self.declarations if declaration.narrowable]

    def warn_about_redundant_bounds(self) -> None:
        """Warn about each bound that never has an effect, or blocks every update, at its declaration's version."""
        for declaration in self.narrowable_declarations:
            if current_version := _current_version(declaration, self.locked_as_written):
                _LOG.warn_if_redundant_bound(replace(declaration, current_version=current_version), declaration.marker)

    @property
    def bounds(self) -> dict[Declaration, str]:
        """Return the specifier of the bound each narrowable declaration's marker sets."""
        return {declaration: self._bound_specifier(declaration) for declaration in self.narrowable_declarations}

    def _bound_specifier(self, declaration: Declaration) -> str:
        """Return the specifier of the bound the declaration's marker sets, anchored at the declaration's version.

        Where the marker holds the update back, the specifier keeps the declaration on that version, where there is one.
        """
        current_version = _current_version(declaration, self.locked_as_written)
        if not current_version:
            return declaration.marker.version_bound.specifier_in_allow_form(current_version) or ""
        specifier = declaration.marker.update_specifier(current_version)
        return f"=={current_version}" if specifier is None else specifier

    def reportable_declarations(self) -> list[Declaration]:
        """Return the declarations uv locks in the pyproject.toml that PyPI serves a release for.

        The root's sources reach every member, so the declarations leave out a dependency the workspace root sources.
        """
        inherited = workspace_sources(self.pyproject_toml.path)
        declarations = pypi_served(self.declarations)
        return [
            declaration
            for declaration in declarations
            if normalized_python_name(declaration.dependency) not in inherited and not declaration.build_requirement
        ]


@dataclass(frozen=True)
class _WorkspaceSnapshot:
    """A uv workspace, with what its members and its uv.lock held before uv ran."""

    root: Path
    uv_lock: UvLock
    uv_lock_text: str | None
    members: list[_MemberSnapshot]

    @classmethod
    def read(cls, root: Path, pyproject_tomls: Iterable[PyprojectToml]) -> Self:
        """Read the pyproject.tomls, the uv.lock, and the versions the uv.lock records."""
        uv_lock = UvLock(root.parent / "uv.lock")
        members = []
        for pyproject_toml in pyproject_tomls:
            source = pyproject_toml.path.parent.relative_to(root.parent).as_posix()
            members.append(
                _MemberSnapshot(pyproject_toml, source, pyproject_toml.read(), uv_lock.locked_versions(source))
            )
        return cls(root, uv_lock, uv_lock.read(), members)

    def update(self) -> dict[PythonDependencyToml, ManagerPolicy[Declaration]]:
        """Lock the files as written, report the markers, and update the workspace where uv locked the files.

        Return the policy each member's markers are judged by.
        """
        locked = self._lock_as_written()
        # Where uv did not lock the files as written, the uv.lock is as it was before the run.
        members = [member.steered_member(self.uv_lock.recorded_versions(member.source)) for member in self.members]
        workspace = Workspace(self.root, self.uv_lock, self.uv_lock_text, members)
        if locked:
            workspace.update()
        return workspace.policies

    def _lock_as_written(self) -> bool:
        """Lock the files as written, and return whether uv locked them.

        Without uv on the path, and where uv fails to lock, each pyproject.toml gets an error.
        """
        pyproject_tomls = [member.pyproject_toml for member in self.members]
        if not is_installed("uv"):
            for pyproject_toml in pyproject_tomls:
                _LOG.uv_not_on_path(pyproject_toml.path)
            return False
        return _locked(self.root, pyproject_tomls)


@dataclass(frozen=True)
class Workspace:
    """A uv workspace, with the members whose markers were reported, and what its uv.lock held before the run."""

    root: Path
    uv_lock: UvLock
    uv_lock_text: str | None
    members: list[_Member]

    @property
    def policies(self) -> dict[PythonDependencyToml, ManagerPolicy[Declaration]]:
        """Return the policy each member's markers are judged by."""
        return {member.pyproject_toml: member.policy for member in self.members}

    def update(self) -> None:
        """Update the exact pins to the versions uv locks and report the new versions, or give the files back."""
        _LOG.path(self.uv_lock.path)
        self._report_pins_with_environment_markers()
        if not self._update_pins():
            self._restore()
            return
        self._report_new_pins()
        self._report_moved_versions()

    def _update_pins(self) -> bool:
        """Move the exact pins of the pyproject.tomls to the versions uv locks; return whether uv locked them."""
        if not self._write_locked_requirements() or not self._lock("--upgrade"):
            return False
        return self._pin_locked_versions() and self._lock()

    def _locked_versions(self, member: _Member) -> list[PinnedDependency] | None:
        """Return the lowest version the uv.lock records for each of the member's dependencies.

        Return None where there is no uv.lock to read.
        """
        return self.uv_lock.locked_versions(member.source)

    def _lock(self, *options: str) -> bool:
        """Lock the workspace with the uv options, and return whether uv locked."""
        return _locked(self.root, [member.pyproject_toml for member in self.members], *options)

    def _restore(self) -> None:
        """Give the uv.lock and the pyproject.tomls back the text they held, and log each one that cannot get it."""
        restores = [
            (self.uv_lock.path, partial(self.uv_lock.restore, self.uv_lock_text)),
            *(
                (member.pyproject_toml.path, partial(member.pyproject_toml.restore, member.text))
                for member in self.members
            ),
        ]
        for path, restore in restores:
            try:
                restore()
            except OSError as error:
                _LOG.file_not_restored(path, error)

    def _write_locked_requirements(self) -> bool:
        """Write the requirements for uv to lock, each narrowed by the bound of its declaration's marker."""

        def text_to_lock(member: _Member) -> str:
            return member.pyproject_toml.with_locked_requirements(member.text, member.bounds)

        return self._rewrite(text_to_lock)

    def _pin_locked_versions(self) -> bool:
        """Pin the updatable exact pins of the pyproject.tomls to the versions the uv.lock records."""

        def pinned(member: _Member) -> str:
            versions = {declaration: pin.version for declaration, pin in self._moved_pins(member).items()}
            return member.pyproject_toml.with_pinned_versions(member.text, versions)

        return self._rewrite(pinned)

    def _rewrite(self, rewritten: Callable[[_Member], str]) -> bool:
        """Rewrite the text each pyproject.toml held before uv ran, write it, and return whether every write succeeded.

        A pyproject.toml that cannot be written is logged, and the pyproject.tomls after it are not written.
        """
        for member in self.members:
            try:
                member.pyproject_toml.restore(rewritten(member))
            except OSError as error:
                _LOG.file_not_written(member.pyproject_toml.path, error)
                return False
        return True

    def _moved_pins(self, member: _Member) -> dict[Declaration, PinnedDependency]:
        """Return the version the uv.lock moved each updatable exact pin of the member to.

        Where a pin spells the locked version less precisely, such as 1.0 for 1.0.0, the pin did not move.
        """
        locked = {normalized_python_name(pin.name): pin for pin in self._locked_versions(member) or []}
        return {
            declaration: locked[name]
            for declaration in member.updatable_pins
            if (name := normalized_python_name(declaration.dependency)) in locked
            and PEP_440.order(locked[name].version) != PEP_440.order(declaration.current_version)
        }

    def _report_pins_with_environment_markers(self) -> None:
        """Log each lockable pin of the pyproject.tomls that Update-time leaves, since it has an environment marker."""
        for member in self.members:
            for declaration in member.lockable_pins:
                if declaration.environment_marker:
                    _LOG.pin_with_environment_marker_left_as_is(declaration)

    def _report_new_pins(self) -> None:
        """Log each updatable exact pin of the pyproject.tomls that the uv.lock moved to a new version PyPI serves."""
        for member in self.members:
            reportable = member.reportable_declarations()
            moved_pins = self._moved_pins(member)
            _log_new_pins({declaration: pin for declaration, pin in moved_pins.items() if declaration in reportable})

    def _report_moved_versions(self) -> None:
        """Log where the uv.lock moved each dependency of the pyproject.tomls without an exact pin.

        Without a uv.lock before the run, no version is logged.
        """
        for member in self.members:
            if member.locked_before is None:
                continue
            locked_after = self._locked_versions(member) or []
            log_moved_versions(_LOG, "uv", member.locked_before, locked_after, member.reportable_declarations())


def _locked(root: Path, pyproject_tomls: Iterable[PyprojectToml], *options: str) -> bool:
    """Lock the uv workspace with the root pyproject.toml, with the uv options, and return whether uv locked.

    A failure is logged at error for each of the pyproject.tomls.
    """
    uv_lock = Command("uv", "lock", "--directory", str(root.parent), *options, "--quiet", "--no-progress")
    if run(uv_lock).succeeded:
        return True
    for pyproject_toml in pyproject_tomls:
        _LOG.uv_dependencies_not_updated(pyproject_toml.path, uv_lock.name)
    return False


def _log_new_pins(moved_pins: Mapping[Declaration, PinnedDependency]) -> None:
    """Log each new version of the moved pins, at the declarations that pin it."""
    locations: dict[PinnedDependency, list[Location]] = {}
    for declaration, pin in moved_pins.items():
        locations.setdefault(pin, []).append(declaration.location)
    for pin, pin_locations in locations.items():
        log_new_version(_LOG, pin.name, pin.version, pin_locations)


def _member_policy(
    pyproject_toml: PyprojectToml, locked_as_written: _RecordedVersions | None
) -> ManagerPolicy[Declaration]:
    """Return the policy for the markers of the workspace member's pyproject.toml.

    An `ignore[update]` keeps a declaration where it has a version to keep: an exact pin, or the version uv locks for a
    narrowable declaration as written. A level bound needs that version to anchor at.
    """
    file_policy = marker_policy(pyproject_toml)

    def hold_back_redundancy(declaration: Declaration) -> Reason | None:
        if declaration.pins_a_version or not declaration.narrowable:
            return file_policy.hold_back_redundancy(declaration)
        return _current_version_redundancy(declaration, locked_as_written)

    def bound_redundancy(declaration: Declaration) -> Reason | None:
        if reason := file_policy.bound_redundancy(declaration):
            return reason
        if declaration.marker.version_bound.level is None:
            return None
        return _current_version_redundancy(declaration, locked_as_written)

    return replace(file_policy, hold_back_redundancy=hold_back_redundancy, bound_redundancy=bound_redundancy)


def _report_path_and_markers(
    pyproject_toml: PyprojectToml, member_policy: ManagerPolicy[Declaration]
) -> list[Declaration]:
    """Log the path and the markers of the pyproject.toml, and return its declarations with the effective markers."""
    _LOG.path(pyproject_toml.path)
    return report_markers(pyproject_toml.declarations(), _LOG, member_policy)


def _current_version(declaration: Declaration, locked_as_written: _RecordedVersions | None) -> VersionString:
    """Return the version the declaration pins, or else the one version uv locks for it as written, if it locks one."""
    if declaration.current_version:
        return declaration.current_version
    versions = _versions_locked_for(declaration, locked_as_written)
    return versions.pop() if len(versions) == 1 else ""


def _current_version_redundancy(declaration: Declaration, locked_as_written: _RecordedVersions | None) -> Reason | None:
    """Return why a directive needing the declaration's current version does not have one, or None where it has one."""
    if _current_version(declaration, locked_as_written):
        return None
    several = len(_versions_locked_for(declaration, locked_as_written)) > 1
    return Reason.SEVERAL_LOCKED_VERSIONS if several else Reason.NO_LOCKED_VERSION


def _versions_locked_for(declaration: Declaration, locked_as_written: _RecordedVersions | None) -> set[VersionString]:
    """Return the versions uv locks for the declaration's dependency as written."""
    by_name = {normalized_python_name(name): versions for name, versions in (locked_as_written or {}).items()}
    return set(by_name.get(normalized_python_name(declaration.dependency), []))


def read_workspaces(pyproject_tomls: Iterable[PyprojectToml]) -> list[_WorkspaceSnapshot]:
    """Return the uv workspaces of the projects, as they are before uv runs."""
    return [_WorkspaceSnapshot.read(root, members) for root, members in by_workspace(pyproject_tomls).items()]
