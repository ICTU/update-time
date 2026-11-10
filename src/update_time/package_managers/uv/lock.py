"""Lock the pyproject.tomls of a uv workspace, or a script, and move their exact pins to the versions uv locks."""

from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Self

from update_time.domain.cooldown import COOLDOWN, cooldown_cutoff
from update_time.domain.dependency import DependencyVersion, PinnedDependency, normalized_python_name
from update_time.domain.reference import Reference
from update_time.domain.version_scheme import PEP_440
from update_time.io.process import is_installed, run
from update_time.lock_files.uv_lock import RecordedVersions, UvLock
from update_time.package_managers.moved_versions import log_moved_versions, log_new_version
from update_time.package_managers.uv import marker_policy
from update_time.package_managers.uv.uv import (
    EXCLUDE_NEWER_OPTION,
    by_workspace,
    configure_cooldown,
    pypi_served,
    settings_hold_exclude_newer,
)
from update_time.primitives.command import Command

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    from update_time.io.log import Logger
    from update_time.manifests.inline_script import InlineScript
    from update_time.manifests.pyproject_toml import PyprojectToml
    from update_time.manifests.python_dependency_toml import Declaration, PythonDependencyToml
    from update_time.package_managers.policy import ManagerPolicy
    from update_time.primitives.location import Location

# The command that locks a workspace or a script, without the options that say which.
_UV_LOCK = Command("uv", "lock")


@dataclass(frozen=True)
class _MemberSnapshot:
    """A member of what uv locks, as it was before uv ran: a pyproject.toml of the workspace, or the script.

    The member holds what its file held, and what the lock recorded for it.
    """

    file: PythonDependencyToml
    # The member's directory relative to the workspace root, as the uv.lock names it, or None for a script.
    project_directory: str | None
    text: str
    locked: list[PinnedDependency] | None

    def steered_member(
        self, locked_as_written: RecordedVersions | None, member_policy: ManagerPolicy[Declaration], log: Logger
    ) -> _Member:
        """Report the markers and the redundant bounds of the file, and return the member with its markers.

        A declaration without an exact pin takes its current version from what uv locks for the files as written.
        """
        declarations = marker_policy.report_file_markers(self.file, log, member_policy)
        member = _Member(self, declarations, locked_as_written, member_policy)
        member.warn_about_redundant_bounds(log)
        return member


@dataclass(frozen=True)
class _Member:
    """A member of what uv locks, with its declarations and the policy their markers are judged by.

    The versions uv locks for the files as written give a declaration without an exact pin its current version.
    """

    before: _MemberSnapshot
    declarations: list[Declaration]
    locked_as_written: RecordedVersions | None
    policy: ManagerPolicy[Declaration]

    @property
    def updatable_pins(self) -> list[Declaration]:
        """Return the pins Update-time moves to a locked version: those without an environment marker it may update.

        uv can lock a package at another version for each Python. A pin without an environment marker takes the lowest
        of them. A pin with one would need the version uv locked for its own Pythons, and that need not be the lowest.
        """
        return [declaration for declaration in self.narrowable_declarations if declaration.pins_a_version]

    @property
    def bounded_declarations(self) -> list[Declaration]:
        """Return the declarations whose requirement a bound narrows while uv locks, as the policy judges them."""
        return [declaration for declaration in self.declarations if self.policy.bound_redundancy(declaration) is None]

    @property
    def narrowable_declarations(self) -> list[Declaration]:
        """Return the declarations whose requirement a bound narrows, or an `ignore[update]` keeps, while uv locks."""
        bounded = self.bounded_declarations
        return [
            declaration
            for declaration in self.declarations
            if declaration in bounded or (declaration.narrowable and self.policy.keeps_the_version(declaration))
        ]

    def warn_about_redundant_bounds(self, log: Logger) -> None:
        """Warn about each bound that never has an effect, or blocks every update, at its declaration's version."""
        for declaration in self.bounded_declarations:
            if current_version := marker_policy.current_version(declaration, self.locked_as_written):
                log.warn_if_redundant_bound(replace(declaration, current_version=current_version), declaration.marker)

    @property
    def bounds(self) -> dict[Declaration, str]:
        """Return the specifier of the bound each narrowable declaration's marker sets."""
        return {declaration: self._bound_specifier(declaration) for declaration in self.narrowable_declarations}

    def _bound_specifier(self, declaration: Declaration) -> str:
        """Return the specifier of the bound the declaration's marker sets, anchored at the declaration's version.

        Where the marker holds the update back, the specifier keeps the declaration on that version, where there is one.
        """
        current_version = marker_policy.current_version(declaration, self.locked_as_written)
        if not current_version:
            return declaration.marker.version_bound.specifier_in_allow_form(current_version) or ""
        specifier = declaration.marker.update_specifier(current_version)
        return f"=={current_version}" if specifier is None else specifier

    @property
    def reportable_declarations(self) -> list[Declaration]:
        """Return the declarations uv locks in the file that PyPI serves a release for."""
        return [declaration for declaration in pypi_served(self.declarations) if not declaration.build_requirement]


@dataclass(frozen=True)
class _LockTarget:
    """What uv locks: the command that locks it, and the lock file that command writes.

    The cooldown options pass the cooldown to the locks that resolve versions, where uv does not read it from the
    config.
    """

    command: Command
    uv_lock: UvLock
    cooldown_options: tuple[str, ...]
    uv_lock_text: str | None

    @classmethod
    def workspace(cls, root: Path, log: Logger) -> Self | None:
        """Return the uv workspace of the root pyproject.toml as what uv locks, or None where its lock is unreadable.

        uv reads the cooldown of a workspace with a lock from its root's config. One without gets it as a cutoff, as a
        script does.
        """
        command = Command(*_UV_LOCK, "--directory", str(root.parent))
        if (target := cls._read(command, UvLock.beside(root), log=log)) is None:
            return None
        return target if target.keeps_the_lock else _with_cutoff(target, root, log)

    @classmethod
    def script(cls, path: Path, log: Logger) -> Self | None:
        """Return the script as what uv locks, with the cooldown as a cutoff, or None where its lock is unreadable.

        Where uv's settings for the script hold an `exclude-newer` already, uv reads that one instead.
        """
        uv_lock = UvLock(path.with_name(f"{path.name}.lock"))
        target = cls._read(Command(*_UV_LOCK, "--script", str(path)), uv_lock, log=log)
        return None if target is None else _with_cutoff(target, path, log)

    @classmethod
    def _read(cls, command: Command, uv_lock: UvLock, *, log: Logger) -> Self | None:
        """Return what uv locks, with the text its lock holds, or None where the lock is unreadable.

        An unreadable lock is logged at error.
        """
        try:
            uv_lock_text = uv_lock.read()
        except (OSError, UnicodeDecodeError) as error:
            log.lock_not_read(uv_lock.path, error)
            return None
        return cls(command, uv_lock, (), uv_lock_text)

    @property
    def keeps_the_lock(self) -> bool:
        """Return whether the run keeps a lock: the one there was before the run."""
        return self.uv_lock_text is not None

    def lock(self, files: Iterable[PythonDependencyToml], log: Logger, *options: str) -> bool:
        """Lock with the uv options, and return whether uv locked.

        A failure is logged at error for each of the files.
        """
        command = Command(*self.command, *options, "--quiet", "--no-progress")
        if run(command).succeeded:
            return True
        for file in files:
            log.uv_dependencies_not_updated(file.path, str(_UV_LOCK))
        return False


def _with_cutoff[TargetT: _LockTarget](target: TargetT, file: Path, log: Logger) -> TargetT:
    """Return what uv locks with the cooldown as a cutoff, unless uv's settings hold an `exclude-newer` already."""
    if settings_hold_exclude_newer(target.command, file, log):
        return target
    return replace(target, cooldown_options=(EXCLUDE_NEWER_OPTION, cooldown_cutoff(COOLDOWN.get())))


@dataclass(frozen=True)
class _LockSnapshot:
    """What uv locks, with what its members held before uv ran: the pyproject.tomls of the workspace, or the script."""

    target: _LockTarget
    members: list[_MemberSnapshot]
    log: Logger

    @classmethod
    def read(
        cls, target: _LockTarget, project_directories: Mapping[PythonDependencyToml, str | None], log: Logger
    ) -> Self:
        """Read each file, and the versions the lock records for its project at the directory the lock names it by."""
        members = [
            _MemberSnapshot(file, project_directory, file.read(), target.uv_lock.locked_versions(project_directory))
            for file, project_directory in project_directories.items()
        ]
        return cls(target, members, log)

    def update(self) -> dict[PythonDependencyToml, ManagerPolicy[Declaration]]:
        """Lock the files as written, report the markers, and update the pins where uv locked the files.

        Return the policy each member's markers are judged by.
        """
        locked = self._lock_as_written()
        # Where uv did not lock the files as written, the lock on disk records nothing about them as they are now.
        members = []
        for member in self.members:
            locked_as_written = self.target.uv_lock.recorded_versions(member.project_directory) if locked else None
            policy = (
                marker_policy.kept_lock_policy(locked_as_written)
                if self.target.keeps_the_lock
                else marker_policy.UNKEPT_LOCK_POLICY
            )
            members.append(member.steered_member(locked_as_written, policy, self.log))
        lock_update = _LockUpdate(self, members)
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
                self.log.package_manager_not_on_path(file.path, "uv")
            return False
        return self.target.lock(files, self.log, *self.target.cooldown_options)


@dataclass(frozen=True)
class _LockUpdate:
    """What uv locks, as it was before uv ran, with the members whose markers were reported."""

    before: _LockSnapshot
    members: list[_Member]

    @property
    def policies(self) -> dict[PythonDependencyToml, ManagerPolicy[Declaration]]:
        """Return the policy each member's markers are judged by."""
        return {member.before.file: member.policy for member in self.members}

    def update(self) -> None:
        """Update the exact pins to the versions uv locks and report the new versions, or give the files back.

        The reports read the versions the lock records, so the run removes a lock it created after them.
        """
        if self.before.target.keeps_the_lock:
            self.before.log.path(self.before.target.uv_lock.path)
        self._report_pins_with_environment_markers()
        if not self._update_pins():
            self._restore()
            return
        self._report_new_pins()
        self._report_moved_versions()
        if not self.before.target.keeps_the_lock:
            self._restore_lock()

    def _update_pins(self) -> bool:
        """Move the exact pins of the files to the versions uv locks; return whether uv locked them."""
        if not self._write_locked_requirements() or not self._lock("--upgrade", *self.before.target.cooldown_options):
            return False
        return self._pin_locked_versions() and (not self.before.target.keeps_the_lock or self._lock())

    def _lock(self, *options: str) -> bool:
        """Lock the files with the uv options, and return whether uv locked."""
        return self.before.target.lock([member.before.file for member in self.members], self.before.log, *options)

    def _restore(self) -> None:
        """Give the lock and the files back the text they held, and log each one that cannot get it."""
        self._restore_lock()
        for member in self.members:
            self._give_back(
                member.before.file.path,
                partial(member.before.file.write_if_changed, member.before.text),
                self.before.log.file_not_restored,
            )

    def _restore_lock(self) -> None:
        """Give the lock back the text it held before the run, removing a lock that held none, and log a failure."""
        target, log = self.before.target, self.before.log
        failure = log.lock_not_removed if target.uv_lock_text is None else log.file_not_restored
        self._give_back(target.uv_lock.path, partial(target.uv_lock.restore, target.uv_lock_text), failure)

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
            return member.before.file.with_locked_requirements(member.before.text, member.bounds)

        return self._rewrite(text_to_lock)

    def _pin_locked_versions(self) -> bool:
        """Pin the updatable exact pins of the files to the versions the lock records."""

        def pinned(member: _Member) -> str:
            versions = {declaration: pin.version for declaration, pin in self._moved_pins(member).items()}
            return member.before.file.with_pinned_versions(member.before.text, versions)

        return self._rewrite(pinned)

    def _rewrite(self, rewritten: Callable[[_Member], str]) -> bool:
        """Rewrite the text each file held before uv ran, write it, and return whether every write succeeded.

        A file that cannot be written is logged, and the files after it are not written.
        """
        for member in self.members:
            try:
                member.before.file.write_if_changed(rewritten(member))
            except OSError as error:
                self.before.log.file_not_written(member.before.file.path, error)
                return False
        return True

    def _moved_pins(self, member: _Member) -> dict[Declaration, PinnedDependency]:
        """Return the version the lock moved each updatable exact pin of the member to.

        Where a pin spells the locked version less precisely, such as 1.0 for 1.0.0, the pin did not move.
        """
        locked = {
            normalized_python_name(pin.name): pin
            for pin in self.before.target.uv_lock.locked_versions(member.before.project_directory) or []
        }
        return {
            declaration: locked[name]
            for declaration in member.updatable_pins
            if (name := normalized_python_name(declaration.dependency)) in locked
            and PEP_440.order(locked[name].version) != PEP_440.order(declaration.current_version)
        }

    def _report_pins_with_environment_markers(self) -> None:
        """Log each pin uv locks in the files that Update-time leaves as it is, since it has an environment marker."""
        for member in self.members:
            for declaration in member.declarations:
                if declaration.pins_a_version and declaration.environment_marker and not declaration.build_requirement:
                    self.before.log.pin_with_environment_marker_left_as_is(declaration)

    def _report_new_pins(self) -> None:
        """Log each updatable exact pin of the files that the lock moved to a new version.

        PyPI is asked for the changelog and the publication date of a version only where it serves the dependency.
        """
        log = self.before.log
        for member in self.members:
            moved_pins = self._moved_pins(member)
            served = pypi_served(moved_pins)
            _log_new_pins({declaration: moved_pins[declaration] for declaration in served}, log)
            for declaration, pin in moved_pins.items():
                if declaration not in served:
                    log.new_version(Reference(pin.name, "", declaration.location), DependencyVersion(pin.version))

    def _report_moved_versions(self) -> None:
        """Log where the lock moved each dependency of the files without an exact pin.

        Without a lock before the run, no version is logged.
        """
        for member in self.members:
            if member.before.locked is None:
                continue
            locked_after = self.before.target.uv_lock.locked_versions(member.before.project_directory) or []
            reportable = member.reportable_declarations
            log_moved_versions(self.before.log, "uv", member.before.locked, locked_after, reportable)


def _log_new_pins(moved_pins: Mapping[Declaration, PinnedDependency], log: Logger) -> None:
    """Log each new version of the moved pins, at the declarations that pin it."""
    locations: dict[PinnedDependency, list[Location]] = {}
    for declaration, pin in moved_pins.items():
        locations.setdefault(pin, []).append(declaration.location)
    for pin, pin_locations in locations.items():
        log_new_version(log, pin.name, pin.version, pin_locations)


def update_workspaces(
    pyproject_tomls: Iterable[PyprojectToml], log: Logger
) -> dict[PythonDependencyToml, ManagerPolicy[Declaration]]:
    """Update the uv workspaces of the projects, and return the policy each project's markers are judged by.

    Each workspace is read, and gets its cooldown, before uv locks any of them. A workspace with an unreadable lock is
    left as it is.
    """
    return _update([_read_workspace(root, members, log) for root, members in by_workspace(pyproject_tomls).items()])


def update_scripts(
    scripts: Iterable[InlineScript], log: Logger
) -> dict[PythonDependencyToml, ManagerPolicy[Declaration]]:
    """Update each script on its own, and return the policy each script's markers are judged by.

    A script with an unreadable lock is left as it is.
    """
    return _update(_read_script(script, log) for script in scripts)


def _update(snapshots: Iterable[_LockSnapshot | None]) -> dict[PythonDependencyToml, ManagerPolicy[Declaration]]:
    """Update what uv locks for each snapshot, and return the policy each file's markers are judged by."""
    policies: dict[PythonDependencyToml, ManagerPolicy[Declaration]] = {}
    for snapshot in snapshots:
        if snapshot is not None:
            policies.update(snapshot.update())
    return policies


def _read_workspace(root: Path, pyproject_tomls: Iterable[PyprojectToml], log: Logger) -> _LockSnapshot | None:
    """Return the uv workspace with the root pyproject.toml, each member named by its directory relative to the root.

    A workspace with a lock gets the cooldown in its root's config before its files are read. Return None where the
    workspace's lock is unreadable.
    """
    if (target := _LockTarget.workspace(root, log)) is None:
        return None
    if target.keeps_the_lock:
        configure_cooldown(root)
    project_directories: dict[PythonDependencyToml, str | None] = {
        pyproject_toml: pyproject_toml.path.parent.relative_to(root.parent).as_posix()
        for pyproject_toml in pyproject_tomls
    }
    return _LockSnapshot.read(target, project_directories, log)


def _read_script(script: InlineScript, log: Logger) -> _LockSnapshot | None:
    """Return the script as uv locks it on its own, as it is before uv runs, or None where its lock is unreadable."""
    if (target := _LockTarget.script(script.path, log)) is None:
        return None
    return _LockSnapshot.read(target, {script: None}, log)
