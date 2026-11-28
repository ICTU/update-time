"""Lock the pyproject.tomls of a uv workspace, or a script, and move their exact pins to the versions uv locks."""

from dataclasses import dataclass, replace
from functools import partial
from itertools import chain
from typing import TYPE_CHECKING, Final, Self

from update_time.domain.cooldown import COOLDOWN, cooldown_cutoff
from update_time.domain.dependency import DependencyVersion, PinnedDependency, normalized_python_name
from update_time.domain.reference import Reference
from update_time.domain.version_scheme import PEP_440
from update_time.io.log import ToolFailure, ToolTask
from update_time.io.process import is_installed, run
from update_time.lock_files.uv_lock import RecordedVersions, UvLock
from update_time.markers.report import warn_if_the_bound_is_redundant
from update_time.package_managers.moved_versions import log_moved_versions, log_new_version
from update_time.primitives.command import Command

from .marker_policy import UNKEPT_LOCK_POLICY, current_version, kept_lock_policy, report_file_markers
from .uv import (
    EXCLUDE_NEWER_OPTION,
    EXCLUDE_NEWER_SETTING,
    by_workspace,
    configure_cooldown,
    pypi_served,
    settings_hold_exclude_newer,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from update_time.io.log import Logger
    from update_time.manifests.inline_script import InlineScript
    from update_time.manifests.pyproject_toml import PyprojectToml
    from update_time.manifests.python_dependency_toml import Declaration, PythonDependencyToml
    from update_time.package_managers.policy import ManagerPolicy
    from update_time.primitives.location import Location

# The command that locks a workspace or a script, without the options that say which.
_UV_LOCK: Final[Command] = Command("uv", "lock")


@dataclass(frozen=True, slots=True, kw_only=True)
class _LockTarget:
    """What uv locks: the command that locks it, and the lock file that command writes.

    The cooldown options pass the cooldown to the locks that resolve versions, where uv does not read it from the
    config.
    """

    command: Command
    uv_lock: UvLock
    cooldown_options: tuple[str, ...]
    uv_lock_text: str | None

    @property
    def keeps_the_lock(self) -> bool:
        """Return whether the run keeps a lock: the one there was before the run."""
        return self.uv_lock_text is not None

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
    def _read(cls, command: Command, uv_lock: UvLock, *, log: Logger) -> Self | None:
        """Return what uv locks, with the text its lock holds, or None where the lock is unreadable.

        An unreadable lock is logged at error.
        """
        try:
            uv_lock_text = uv_lock.text()
        except (OSError, UnicodeDecodeError) as error:
            log.lock_not_read(uv_lock.path, error)
            return None
        return cls(command=command, uv_lock=uv_lock, cooldown_options=(), uv_lock_text=uv_lock_text)

    @classmethod
    def script(cls, path: Path, log: Logger) -> Self | None:
        """Return the script as what uv locks, with the cooldown as a cutoff, or None where its lock is unreadable.

        Where uv's settings for the script hold an `exclude-newer` already, uv reads that one instead.
        """
        uv_lock = UvLock(path.with_name(f"{path.name}.lock"))
        target = cls._read(Command(*_UV_LOCK, "--script", str(path)), uv_lock, log=log)
        return None if target is None else _with_cutoff(target, path, log)

    def lock(self, files: Iterable[PythonDependencyToml], log: Logger, *options: str) -> bool:
        """Lock with the uv options, and return whether uv locked."""
        if run(Command(*self.command, *options, "--quiet", "--no-progress")).succeeded:
            return True
        for file in files:
            log.tool_failed(file.path, ToolTask.UPDATE, str(_UV_LOCK), ToolFailure.FAILED)
        return False


def _with_cutoff[TargetT: _LockTarget](target: TargetT, file: Path, log: Logger) -> TargetT:
    """Return what uv locks with the cooldown as a cutoff, unless uv's settings hold an `exclude-newer` already."""
    if settings_hold_exclude_newer(target.command, file, log):
        log.project_cooldown(file, EXCLUDE_NEWER_SETTING, COOLDOWN.get())
        return target
    return replace(target, cooldown_options=(EXCLUDE_NEWER_OPTION, cooldown_cutoff(COOLDOWN.get())))


def update_workspaces(
    pyproject_tomls: Iterable[PyprojectToml], log: Logger
) -> dict[PythonDependencyToml, ManagerPolicy[Declaration]]:
    """Update the uv workspaces of the projects, and return the policy each project's markers are judged by.

    Each workspace is read, and gets its cooldown, before uv locks any of them. A workspace with an unreadable lock is
    left as it is.
    """
    return _update(
        [
            [_read_workspace(root, members, log) for root, members in group.items()]
            for group in by_workspace(pyproject_tomls)
        ]
    )


def _update(
    groups: Iterable[Iterable[_LockBeforeUpdate | None]],
) -> dict[PythonDependencyToml, ManagerPolicy[Declaration]]:
    """Update what uv locks for each group of locks, and return the policy each file's markers are judged by.

    uv locks the files of every lock as written before it upgrades any. So uv reads the pins of a project that
    another sources by path as the run found them, when it locks the other's files as written.
    """
    lock_update_groups = [[lock.steered_update() for lock in group if lock is not None] for group in groups]
    for lock_updates in lock_update_groups:
        _update_together(lock_updates)
    return {
        file: policy
        for lock_update in chain.from_iterable(lock_update_groups)
        for file, policy in lock_update.policies.items()
    }


def _update_together(lock_updates: Sequence[_LockUpdate]) -> None:
    """Move the pins of what uv locks for each lock update, and report them.

    Several lock updates are the workspaces of projects that source each other by path. Their requirements are all
    relaxed before uv upgrades any, so each upgrade sees the others relaxed. Each lock but the last is locked once
    more, so it records the pins the others moved to after it. Where uv cannot lock one of them, they all get their
    files back.
    """
    together = len(lock_updates) > 1
    if together:
        for lock_update in lock_updates:
            lock_update.relax()
    steps = [(lock_update, lock_update.move_pins) for lock_update in lock_updates]
    steps += [(lock_update, lock_update.lock) for lock_update in lock_updates[:-1]]
    for lock_update, step in steps:
        if not step():
            if together:
                _roll_back(lock_updates, lock_update)
            return
    for lock_update in lock_updates:
        lock_update.report()


def _roll_back(lock_updates: Iterable[_LockUpdate], failed: _LockUpdate) -> None:
    """Give each lock update its files back, and report the files of each but the failed one, whose uv lock failed."""
    for lock_update in lock_updates:
        lock_update.roll_back()
        if lock_update is not failed:
            for path in lock_update.paths:
                lock_update.before.log.rolled_back_with_cycle(path, failed.paths)


def _read_workspace(root: Path, pyproject_tomls: Iterable[PyprojectToml], log: Logger) -> _LockBeforeUpdate | None:
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
    return _LockBeforeUpdate.read(target, project_directories, log)


@dataclass(frozen=True, slots=True, kw_only=True)
class _LockBeforeUpdate:
    """What uv locks, with what its members held before uv ran: the pyproject.tomls of the workspace, or the script."""

    target: _LockTarget
    members: list[_MemberBeforeUpdate]
    log: Logger

    @classmethod
    def read(
        cls, target: _LockTarget, project_directories: Mapping[PythonDependencyToml, str | None], log: Logger
    ) -> Self:
        """Read each file, and the versions the lock records for its project at the directory the lock names it by."""
        members = [
            _MemberBeforeUpdate(
                file=file,
                project_directory=project_directory,
                text=file.text(),
                locked=target.uv_lock.locked_versions(project_directory),
            )
            for file, project_directory in project_directories.items()
        ]
        return cls(target=target, members=members, log=log)

    def steered_update(self) -> _LockUpdate:
        """Lock the files as written, report the markers and redundant bounds, and return the update of the pins.

        A declaration without an exact pin takes its current version from what uv locks for the files as written.
        """
        locked = self._lock_as_written()
        # Where uv did not lock the files as written, the lock on disk records nothing about them as they are now.
        members = []
        for before in self.members:
            locked_as_written = self.target.uv_lock.recorded_versions(before.project_directory) if locked else None
            policy = kept_lock_policy(locked_as_written) if self.target.keeps_the_lock else UNKEPT_LOCK_POLICY
            declarations = report_file_markers(before.file, self.log, policy)
            member = _Member(
                before=before, declarations=declarations, locked_as_written=locked_as_written, policy=policy
            )
            member.warn_about_redundant_bounds(self.log)
            members.append(member)
        return _LockUpdate(before=self, members=members, lock_as_written_succeeded=locked)

    def _lock_as_written(self) -> bool:
        """Lock the files as written, and return whether uv locked them.

        Without uv on the path, and where uv fails to lock, each file gets an error.
        """
        files = [member.file for member in self.members]
        if not is_installed("uv"):
            for file in files:
                self.log.tool_failed(file.path, ToolTask.UPDATE, "uv", ToolFailure.NOT_ON_PATH)
            return False
        return self.target.lock(files, self.log, *self.target.cooldown_options)


@dataclass(frozen=True, slots=True, kw_only=True)
class _MemberBeforeUpdate:
    """A member of what uv locks, as it was before uv ran: a pyproject.toml of the workspace, or the script.

    The member holds what its file held, and what the lock recorded for it.
    """

    file: PythonDependencyToml
    # The member's directory relative to the workspace root, as the uv.lock names it, or None for a script.
    project_directory: str | None
    text: str
    locked: list[PinnedDependency] | None


@dataclass(frozen=True, slots=True, kw_only=True)
class _Member:
    """A member of what uv locks, with its declarations and the policy their markers are judged by.

    The versions uv locks for the files as written give a declaration without an exact pin its current version.
    """

    before: _MemberBeforeUpdate
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
    def narrowable_declarations(self) -> list[Declaration]:
        """Return the declarations whose requirement a bound narrows, or an `ignore[update]` keeps, while uv locks."""
        bounded = self.bounded_declarations
        return [
            declaration
            for declaration in self.declarations
            if declaration in bounded or (declaration.narrowable and self.policy.keeps_the_version(declaration))
        ]

    @property
    def bounded_declarations(self) -> list[Declaration]:
        """Return the declarations whose requirement a bound narrows while uv locks, as the policy judges them."""
        return [declaration for declaration in self.declarations if self.policy.bound_redundancy(declaration) is None]

    @property
    def reportable_declarations(self) -> list[Declaration]:
        """Return the declarations uv locks in the file that PyPI serves a release for."""
        return [declaration for declaration in pypi_served(self.declarations) if not declaration.build_requirement]

    @property
    def bounds(self) -> dict[Declaration, str]:
        """Return the specifier of the bound each narrowable declaration's marker sets."""
        return {declaration: self._bound_specifier(declaration) for declaration in self.narrowable_declarations}

    def _bound_specifier(self, declaration: Declaration) -> str:
        """Return the specifier of the bound the declaration's marker sets, anchored at the declaration's version.

        Where the marker holds the update back, the specifier keeps the declaration on that version, where there is one.
        """
        version = current_version(declaration, self.locked_as_written)
        if not version:
            return declaration.marker.version_bound.specifier_in_allow_form(version) or ""
        specifier = declaration.marker.update_specifier(version)
        return f"=={version}" if specifier is None else specifier

    def warn_about_redundant_bounds(self, log: Logger) -> None:
        """Warn about each bound that never has an effect, or blocks every update, at its declaration's version."""
        for declaration in self.bounded_declarations:
            if version := current_version(declaration, self.locked_as_written):
                warn_if_the_bound_is_redundant(replace(declaration, current_version=version), log)


@dataclass(frozen=True, slots=True, kw_only=True)
class _LockUpdate:
    """What uv locks, as it was before uv ran, with the members whose markers were reported.

    `lock_as_written_succeeded` says whether uv locked the files as written.
    """

    before: _LockBeforeUpdate
    members: list[_Member]
    lock_as_written_succeeded: bool

    @property
    def policies(self) -> dict[PythonDependencyToml, ManagerPolicy[Declaration]]:
        """Return the policy each member's markers are judged by."""
        return {member.before.file: member.policy for member in self.members}

    @property
    def paths(self) -> list[Path]:
        """Return the path of each file uv locks."""
        return [member.before.file.path for member in self.members]

    def move_pins(self) -> bool:
        """Move the exact pins to the versions uv locks, or give the files back; return whether the pins moved.

        Where uv did not lock the files as written, nothing moves.
        """
        if not self.lock_as_written_succeeded:
            return False
        if self.before.target.keeps_the_lock:
            self.before.log.path(self.before.target.uv_lock.path)
        self._report_pins_with_environment_markers()
        if not self._update_pins():
            self.roll_back()
            return False
        return True

    def _report_pins_with_environment_markers(self) -> None:
        """Log each pin uv locks in the files that Update-time leaves as it is, since it has an environment marker."""
        for member in self.members:
            for declaration in member.declarations:
                if declaration.pins_a_version and declaration.environment_marker and not declaration.build_requirement:
                    self.before.log.pin_with_environment_marker_left_as_is(declaration)

    def _update_pins(self) -> bool:
        """Move the exact pins of the files to the versions uv locks; return whether uv locked them."""
        if not self.relax() or not self.lock("--upgrade", *self.before.target.cooldown_options):
            return False
        return self._pin_locked_versions() and (not self.before.target.keeps_the_lock or self.lock())

    def relax(self) -> bool:
        """Write the requirements for uv to lock, each narrowed by the bound of its declaration's marker."""

        def text_to_lock(member: _Member) -> str:
            return member.before.file.with_locked_requirements(member.before.text, member.bounds)

        return self._rewrite(text_to_lock)

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

    def lock(self, *options: str) -> bool:
        """Lock the files with the uv options, and return whether uv locked."""
        return self.before.target.lock([member.before.file for member in self.members], self.before.log, *options)

    def _pin_locked_versions(self) -> bool:
        """Pin the updatable exact pins of the files to the versions the lock records."""

        def pinned(member: _Member) -> str:
            versions = {declaration: pin.version for declaration, pin in self._moved_pins(member).items()}
            return member.before.file.with_pinned_versions(member.before.text, versions)

        return self._rewrite(pinned)

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

    def roll_back(self) -> None:
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

    def report(self) -> None:
        """Report the new versions of the pins that moved.

        The reports read the versions the lock records, so the run removes a lock it created after them.
        """
        self._report_new_pins()
        self._report_moved_versions()
        if not self.before.target.keeps_the_lock:
            self._restore_lock()

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


def update_scripts(
    scripts: Iterable[InlineScript], log: Logger
) -> dict[PythonDependencyToml, ManagerPolicy[Declaration]]:
    """Update each script on its own, and return the policy each script's markers are judged by.

    A script with an unreadable lock is left as it is.
    """
    return _update([[_read_script(script, log)] for script in scripts])


def _read_script(script: InlineScript, log: Logger) -> _LockBeforeUpdate | None:
    """Return the script as uv locks it on its own, as it is before uv runs, or None where its lock is unreadable."""
    if (target := _LockTarget.script(script.path, log)) is None:
        return None
    return _LockBeforeUpdate.read(target, {script: None}, log)
