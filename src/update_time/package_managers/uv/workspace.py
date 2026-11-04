"""Move the exact pins of a uv workspace's pyproject.tomls to the versions uv locks, and report the new versions."""

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Self

from update_time.domain.dependency import PinnedDependency, normalized_python_name
from update_time.domain.version_scheme import PEP_440
from update_time.io.log import get_logger
from update_time.io.process import is_installed, run
from update_time.lock_files.uv_lock import UvLock
from update_time.package_managers.requirements_compiler import log_moved_versions, log_new_version
from update_time.package_managers.uv.uv import by_workspace, pypi_served, report_file_markers, workspace_sources
from update_time.primitives.command import Command

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    from update_time.manifests.pyproject_toml import PyprojectToml
    from update_time.manifests.python_dependency_toml import Declaration
    from update_time.primitives.location import Location

_LOG = get_logger("pyproject.toml")


@dataclass(frozen=True)
class _Member:
    """A member of a uv workspace, with what its pyproject.toml held and the uv.lock recorded for it before uv ran."""

    pyproject_toml: PyprojectToml
    source: str  # The member's directory relative to the workspace root, as the uv.lock names it.
    text: str
    declarations: list[Declaration]
    locked_before: list[PinnedDependency] | None

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
        return [declaration for declaration in self.lockable_pins if not declaration.environment_marker]

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
class Workspace:
    """A uv workspace, with what its members and its uv.lock held before uv ran."""

    root: Path
    uv_lock: UvLock
    uv_lock_text: str | None
    members: list[_Member]

    @classmethod
    def read(cls, root: Path, pyproject_tomls: Iterable[PyprojectToml]) -> Self:
        """Read the pyproject.tomls, the uv.lock, and the versions the uv.lock records, and report the markers."""
        uv_lock = UvLock(root.parent / "uv.lock")
        members = []
        for pyproject_toml in pyproject_tomls:
            source = pyproject_toml.path.parent.relative_to(root.parent).as_posix()
            declarations = _report_path_and_markers(pyproject_toml)
            members.append(
                _Member(pyproject_toml, source, pyproject_toml.read(), declarations, uv_lock.locked_versions(source))
            )
        return cls(root, uv_lock, uv_lock.read(), members)

    def update(self) -> None:
        """Update the exact pins to the versions uv locks and report the new versions, or give the files back.

        Without uv on the path, each pyproject.toml gets an error.
        """
        if not is_installed("uv"):
            for member in self.members:
                _LOG.uv_not_on_path(member.pyproject_toml.path)
            return
        _LOG.path(self.uv_lock.path)
        self._report_pins_with_environment_markers()
        if not self._update_pins():
            self._restore()
            return
        self._report_new_pins()
        self._report_moved_versions()

    def _update_pins(self) -> bool:
        """Move the exact pins of the pyproject.tomls to the versions uv locks; return whether uv locked them."""
        if not self._relax_pins() or not self._lock("--upgrade"):
            return False
        return self._pin_locked_versions() and self._lock()

    def _locked_versions(self, member: _Member) -> list[PinnedDependency] | None:
        """Return the lowest version the uv.lock records for each of the member's dependencies.

        Return None where there is no uv.lock to read.
        """
        return self.uv_lock.locked_versions(member.source)

    def _lock(self, *options: str) -> bool:
        """Lock the workspace with the uv options, and return whether uv locked.

        A failure is logged at error for each of the pyproject.tomls.
        """
        uv_lock = Command("uv", "lock", "--directory", str(self.root.parent), *options, "--quiet", "--no-progress")
        if run(uv_lock).succeeded:
            return True
        for member in self.members:
            _LOG.uv_dependencies_not_updated(member.pyproject_toml.path, uv_lock.name)
        return False

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

    def _relax_pins(self) -> bool:
        """Relax the updatable exact pins of the pyproject.tomls from `==version` to `>=version`."""

        def relaxed(member: _Member) -> str:
            return member.pyproject_toml.with_relaxed_pins(member.text, member.updatable_pins)

        return self._rewrite(relaxed)

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


def _log_new_pins(moved_pins: Mapping[Declaration, PinnedDependency]) -> None:
    """Log each new version of the moved pins, at the declarations that pin it."""
    locations: dict[PinnedDependency, list[Location]] = {}
    for declaration, pin in moved_pins.items():
        locations.setdefault(pin, []).append(declaration.location)
    for pin, pin_locations in locations.items():
        log_new_version(_LOG, pin.name, pin.version, pin_locations)


def _report_path_and_markers(pyproject_toml: PyprojectToml) -> list[Declaration]:
    """Log the path and the markers of the pyproject.toml, and return its declarations with the effective markers."""
    _LOG.path(pyproject_toml.path)
    return report_file_markers(pyproject_toml, _LOG)


def read_workspaces(pyproject_tomls: Iterable[PyprojectToml]) -> list[Workspace]:
    """Return the uv workspaces of the projects."""
    return [Workspace.read(root, members) for root, members in by_workspace(pyproject_tomls).items()]
