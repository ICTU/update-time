"""Lock the pyproject.tomls of a uv workspace, or a script, and move their exact pins to the versions uv locks."""

from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Self

from update_time.domain.cooldown import COOLDOWN, cooldown_cutoff
from update_time.domain.dependency import PinnedDependency, normalized_python_name
from update_time.domain.version_scheme import PEP_440
from update_time.io.process import is_installed, run
from update_time.lock_files.uv_lock import RecordedVersions, UvLock
from update_time.markers.directive import Reason
from update_time.package_managers.policy import ManagerPolicy, report_markers
from update_time.package_managers.requirements_compiler import log_moved_versions, log_new_version
from update_time.package_managers.uv.uv import (
    EXCLUDE_NEWER_OPTION,
    MARKER_POLICY,
    by_workspace,
    pypi_served,
    workspace_sources,
)
from update_time.primitives.command import Command

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.io.log import Logger
    from update_time.manifests.inline_script import InlineScript
    from update_time.manifests.pyproject_toml import PyprojectToml
    from update_time.manifests.python_dependency_toml import Declaration, PythonDependencyToml
    from update_time.primitives.location import Location


@dataclass(frozen=True)
class _MemberSnapshot:
    """A member of what uv locks, with what its file held and the lock recorded for it before uv ran."""

    file: PythonDependencyToml
    # The member's directory relative to the workspace root, as the uv.lock names it, or None for a script.
    source: str | None
    text: str
    locked_before: list[PinnedDependency] | None

    def steered_member(
        self, locked_as_written: RecordedVersions | None, member_policy: ManagerPolicy[Declaration], log: Logger
    ) -> _Member:
        """Report the path and the markers of the file, and return the member with the markers it acts on.

        A declaration without an exact pin takes its current version from what uv locks for the files as written.
        """
        declarations = _report_path_and_markers(self.file, member_policy, log)
        member = _Member(
            self.file,
            self.source,
            self.text,
            declarations,
            self.locked_before,
            locked_as_written,
            member_policy,
        )
        member.warn_about_redundant_bounds(log)
        return member


@dataclass(frozen=True)
class _Member:
    """A member of what uv locks, with its declarations and the policy their markers are judged by.

    The versions locked before the run are what the report of new and moved versions compares with. The versions uv
    locks for the files as written give a declaration without an exact pin its current version.
    """

    file: PythonDependencyToml
    # The member's directory relative to the workspace root, as the uv.lock names it, or None for a script.
    source: str | None
    text: str
    declarations: list[Declaration]
    locked_before: list[PinnedDependency] | None
    locked_as_written: RecordedVersions | None
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

    def warn_about_redundant_bounds(self, log: Logger) -> None:
        """Warn about each bound that never has an effect, or blocks every update, at its declaration's version."""
        for declaration in self.narrowable_declarations:
            if current_version := _current_version(declaration, self.locked_as_written):
                log.warn_if_redundant_bound(replace(declaration, current_version=current_version), declaration.marker)

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

    def reportable_declarations(self, inherited_sources: set[DependencyName]) -> list[Declaration]:
        """Return the declarations uv locks in the file that PyPI serves a release for.

        The declarations leave out a dependency the member inherits a source for.
        """
        declarations = pypi_served(self.declarations)
        return [
            declaration
            for declaration in declarations
            if normalized_python_name(declaration.dependency) not in inherited_sources
            and not declaration.build_requirement
        ]


@dataclass(frozen=True)
class _LockTarget:
    """What uv locks: the command that locks it, the lock file that command writes, and the sources members inherit.

    The inherited sources are the names of the dependencies that the `[tool.uv] sources` of the workspace root resolve.
    The cooldown options pass the cooldown to the locks that resolve versions, where uv does not read it from the
    config. `keeps_a_new_lock` says whether a run keeps the lock uv writes where there was none before the run, and
    `uv_lock_text` is what the lock held before the run, or None where there was no lock.
    """

    command: Command
    uv_lock: UvLock
    inherited_sources: set[DependencyName]
    cooldown_options: tuple[str, ...]
    keeps_a_new_lock: bool
    uv_lock_text: str | None

    @classmethod
    def workspace(cls, root: Path) -> Self:
        """Return the uv workspace with the root pyproject.toml as what uv locks."""
        directory = root.parent
        uv_lock = UvLock(directory / "uv.lock")
        return cls(
            Command("uv", "lock", "--directory", str(directory)),
            uv_lock,
            workspace_sources(root),
            cooldown_options=(),
            keeps_a_new_lock=True,
            uv_lock_text=uv_lock.read(),
        )

    @classmethod
    def script(cls, path: Path) -> Self:
        """Return the script as what uv locks, with the cooldown as a cutoff on the command line."""
        uv_lock = UvLock(path.with_name(f"{path.name}.lock"))
        return cls(
            Command("uv", "lock", "--script", str(path)),
            uv_lock,
            set(),
            cooldown_options=(EXCLUDE_NEWER_OPTION, cooldown_cutoff(COOLDOWN.get())),
            keeps_a_new_lock=False,
            uv_lock_text=uv_lock.read(),
        )

    @property
    def keeps_the_lock(self) -> bool:
        """Return whether the run leaves a lock: the one there was before the run, or a new one it keeps."""
        return self.uv_lock_text is not None or self.keeps_a_new_lock

    def lock(self, files: Iterable[PythonDependencyToml], log: Logger, *options: str) -> bool:
        """Lock with the uv options, and return whether uv locked.

        A failure is logged at error for each of the files.
        """
        command = Command(*self.command, *options, "--quiet", "--no-progress")
        if run(command).succeeded:
            return True
        for file in files:
            log.uv_dependencies_not_updated(file.path, command.name)
        return False


@dataclass(frozen=True)
class _LockSnapshot:
    """What uv locks, with what its members held before uv ran."""

    target: _LockTarget
    members: list[_MemberSnapshot]
    log: Logger

    @classmethod
    def read(cls, target: _LockTarget, sources: Mapping[PythonDependencyToml, str | None], log: Logger) -> Self:
        """Read each file, and the versions the lock records for it at the source the lock names it by."""
        members = [
            _MemberSnapshot(file, source, file.read(), target.uv_lock.locked_versions(source))
            for file, source in sources.items()
        ]
        return cls(target, members, log)

    def update(self) -> dict[PythonDependencyToml, ManagerPolicy[Declaration]]:
        """Lock the files as written, report the markers, and update the pins where uv locked the files.

        Return the policy each member's markers are judged by.
        """
        locked = self._lock_as_written()
        # Where uv did not lock the files as written, the lock is as it was before the run.
        members = []
        for member in self.members:
            locked_as_written = self.target.uv_lock.recorded_versions(member.source)
            policy = _member_policy(locked_as_written) if self.target.keeps_the_lock else _UNKEPT_LOCK_POLICY
            members.append(member.steered_member(locked_as_written, policy, self.log))
        lock_update = _LockUpdate(self.target, members, self.log)
        if locked:
            lock_update.update()
        return lock_update.policies

    def _lock_as_written(self) -> bool:
        """Lock the files as written, and return whether uv locked them.

        Without uv on the path, and where uv fails to lock, each file gets an error.
        """
        files = [member.file for member in self.members]
        if not is_installed("uv"):
            for file in files:
                self.log.uv_not_on_path(file.path)
            return False
        return self.target.lock(files, self.log, *self.target.cooldown_options)


@dataclass(frozen=True)
class _LockUpdate:
    """What uv locks, with the members whose markers were reported."""

    target: _LockTarget
    members: list[_Member]
    log: Logger

    @property
    def policies(self) -> dict[PythonDependencyToml, ManagerPolicy[Declaration]]:
        """Return the policy each member's markers are judged by."""
        return {member.file: member.policy for member in self.members}

    def update(self) -> None:
        """Update the exact pins to the versions uv locks and report the new versions, or give the files back.

        The reports read the versions the lock records, so the run removes a new lock it does not keep after them.
        """
        if self.target.keeps_the_lock:
            self.log.path(self.target.uv_lock.path)
        self._report_pins_with_environment_markers()
        if not self._update_pins():
            self._restore()
            return
        self._report_new_pins()
        self._report_moved_versions()
        if not self.target.keeps_the_lock:
            self._restore_lock()

    def _update_pins(self) -> bool:
        """Move the exact pins of the files to the versions uv locks; return whether uv locked them."""
        if not self._write_locked_requirements() or not self._lock("--upgrade", *self.target.cooldown_options):
            return False
        return self._pin_locked_versions() and (not self.target.keeps_the_lock or self._lock())

    def _locked_versions(self, member: _Member) -> list[PinnedDependency] | None:
        """Return the lowest version the lock records for each of the member's dependencies.

        Return None where there is no lock to read.
        """
        return self.target.uv_lock.locked_versions(member.source)

    def _lock(self, *options: str) -> bool:
        """Lock the files with the uv options, and return whether uv locked."""
        return self.target.lock([member.file for member in self.members], self.log, *options)

    def _restore(self) -> None:
        """Give the lock and the files back the text they held, and log each one that cannot get it."""
        self._restore_lock()
        for member in self.members:
            self._give_back(member.file.path, partial(member.file.restore, member.text), self.log.file_not_restored)

    def _restore_lock(self) -> None:
        """Give the lock back the text it held before the run, removing a lock that held none, and log a failure."""
        failure = self.log.lock_not_removed if self.target.uv_lock_text is None else self.log.file_not_restored
        self._give_back(
            self.target.uv_lock.path, partial(self.target.uv_lock.restore, self.target.uv_lock_text), failure
        )

    @staticmethod
    def _give_back(path: Path, restore: Callable[[], None], failure: Callable[[Path, OSError], object]) -> None:
        """Give the file at the path back its text with the restore, and log the failure where that fails."""
        try:
            restore()
        except OSError as error:
            failure(path, error)

    def _write_locked_requirements(self) -> bool:
        """Write the requirements for uv to lock, each narrowed by the bound of its declaration's marker."""

        def text_to_lock(member: _Member) -> str:
            return member.file.with_locked_requirements(member.text, member.bounds)

        return self._rewrite(text_to_lock)

    def _pin_locked_versions(self) -> bool:
        """Pin the updatable exact pins of the files to the versions the lock records."""

        def pinned(member: _Member) -> str:
            versions = {declaration: pin.version for declaration, pin in self._moved_pins(member).items()}
            return member.file.with_pinned_versions(member.text, versions)

        return self._rewrite(pinned)

    def _rewrite(self, rewritten: Callable[[_Member], str]) -> bool:
        """Rewrite the text each file held before uv ran, write it, and return whether every write succeeded.

        A file that cannot be written is logged, and the files after it are not written.
        """
        for member in self.members:
            try:
                member.file.restore(rewritten(member))
            except OSError as error:
                self.log.file_not_written(member.file.path, error)
                return False
        return True

    def _moved_pins(self, member: _Member) -> dict[Declaration, PinnedDependency]:
        """Return the version the lock moved each updatable exact pin of the member to.

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
        """Log each lockable pin of the files that Update-time leaves, since it has an environment marker."""
        for member in self.members:
            for declaration in member.lockable_pins:
                if declaration.environment_marker:
                    self.log.pin_with_environment_marker_left_as_is(declaration)

    def _report_new_pins(self) -> None:
        """Log each updatable exact pin of the files that the lock moved to a new version PyPI serves."""
        for member in self.members:
            reportable = member.reportable_declarations(self.target.inherited_sources)
            moved_pins = self._moved_pins(member)
            moved_reportable_pins = {
                declaration: pin for declaration, pin in moved_pins.items() if declaration in reportable
            }
            _log_new_pins(moved_reportable_pins, self.log)

    def _report_moved_versions(self) -> None:
        """Log where the lock moved each dependency of the files without an exact pin.

        Without a lock before the run, no version is logged.
        """
        for member in self.members:
            if member.locked_before is None:
                continue
            locked_after = self._locked_versions(member) or []
            reportable = member.reportable_declarations(self.target.inherited_sources)
            log_moved_versions(self.log, "uv", member.locked_before, locked_after, reportable)


def _log_new_pins(moved_pins: Mapping[Declaration, PinnedDependency], log: Logger) -> None:
    """Log each new version of the moved pins, at the declarations that pin it."""
    locations: dict[PinnedDependency, list[Location]] = {}
    for declaration, pin in moved_pins.items():
        locations.setdefault(pin, []).append(declaration.location)
    for pin, pin_locations in locations.items():
        log_new_version(log, pin.name, pin.version, pin_locations)


def _member_policy(locked_as_written: RecordedVersions | None) -> ManagerPolicy[Declaration]:
    """Return the policy for the markers of a member's file, where the run keeps the lock.

    An `ignore[update]` keeps a declaration where it has a version to keep: an exact pin, or the version uv locks for a
    narrowable declaration as written. A level bound needs that version to anchor at.
    """

    def hold_back_redundancy(declaration: Declaration) -> Reason | None:
        if declaration.pins_a_version or not declaration.narrowable:
            return MARKER_POLICY.hold_back_redundancy(declaration)
        return _current_version_redundancy(declaration, locked_as_written)

    def bound_redundancy(declaration: Declaration) -> Reason | None:
        if reason := MARKER_POLICY.bound_redundancy(declaration):
            return reason
        if declaration.marker.version_bound.level is None:
            return None
        return _current_version_redundancy(declaration, locked_as_written)

    return replace(MARKER_POLICY, hold_back_redundancy=hold_back_redundancy, bound_redundancy=bound_redundancy)


def _unkept_lock_bound_redundancy(declaration: Declaration) -> Reason | None:
    """Return why a bound decides nothing where the run does not keep the lock: uv resolves a range afresh each run."""
    if reason := declaration.unnarrowable_reason:
        return reason
    return None if declaration.pins_a_version else Reason.BOUND_DECIDES_NOTHING


# A run can remove the lock it created. What uv locks for a dependency without an exact pin then lasts for the run
# alone, so an `ignore[update]` or a bound on one holds nothing back. This policy judges the markers of such a run.
_UNKEPT_LOCK_POLICY = replace(MARKER_POLICY, bound_redundancy=_unkept_lock_bound_redundancy)


def _report_path_and_markers(
    file: PythonDependencyToml, member_policy: ManagerPolicy[Declaration], log: Logger
) -> list[Declaration]:
    """Log the path and the markers of the file, and return its declarations with the effective markers."""
    log.path(file.path)
    return report_markers(file.declarations(), log, member_policy)


def _current_version(declaration: Declaration, locked_as_written: RecordedVersions | None) -> VersionString:
    """Return the version the declaration pins, or else the one version uv locks for it as written, if it locks one."""
    if declaration.current_version:
        return declaration.current_version
    versions = _versions_locked_for(declaration, locked_as_written)
    return versions.pop() if len(versions) == 1 else ""


def _current_version_redundancy(declaration: Declaration, locked_as_written: RecordedVersions | None) -> Reason | None:
    """Return why a directive needing the declaration's current version does not have one, or None where it has one."""
    if _current_version(declaration, locked_as_written):
        return None
    several = len(_versions_locked_for(declaration, locked_as_written)) > 1
    return Reason.SEVERAL_LOCKED_VERSIONS if several else Reason.NO_LOCKED_VERSION


def _versions_locked_for(declaration: Declaration, locked_as_written: RecordedVersions | None) -> set[VersionString]:
    """Return the versions uv locks for the declaration's dependency as written."""
    by_name = {normalized_python_name(name): versions for name, versions in (locked_as_written or {}).items()}
    return set(by_name.get(normalized_python_name(declaration.dependency), []))


def read_workspaces(pyproject_tomls: Iterable[PyprojectToml], log: Logger) -> list[_LockSnapshot]:
    """Return the uv workspaces of the projects, as they are before uv runs."""
    return [_read_workspace(root, members, log) for root, members in by_workspace(pyproject_tomls).items()]


def _read_workspace(root: Path, pyproject_tomls: Iterable[PyprojectToml], log: Logger) -> _LockSnapshot:
    """Return the uv workspace with the root pyproject.toml, each member named by its directory relative to the root."""
    sources: dict[PythonDependencyToml, str | None] = {
        pyproject_toml: pyproject_toml.path.parent.relative_to(root.parent).as_posix()
        for pyproject_toml in pyproject_tomls
    }
    return _LockSnapshot.read(_LockTarget.workspace(root), sources, log)


def read_script(script: InlineScript, log: Logger) -> _LockSnapshot:
    """Return the script as uv locks it on its own, as it is before uv runs."""
    return _LockSnapshot.read(_LockTarget.script(script.path), {script: None}, log)
