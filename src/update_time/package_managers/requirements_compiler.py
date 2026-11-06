"""Recompile a requirements file that a tool compiled, steered by the markers in its sources.

The module also logs each version of a Python dependency that a package manager resolves or moves.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion, is_valid, normalized_python_name
from update_time.domain.reference import Reference
from update_time.domain.version_scheme import PEP_440
from update_time.io.log import get_logger
from update_time.io.process import is_installed, run
from update_time.lock_files import compiled_requirements_txt
from update_time.manifests import requirements_txt
from update_time.markers.marker import Marker
from update_time.primitives.command import Command
from update_time.primitives.lookup import LookedUp
from update_time.sources.pypi import get_changes, get_publication_datetime

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from update_time.domain.dependency import DependencyName, PinnedDependency, VersionString
    from update_time.io.log import Logger
    from update_time.lock_files.compiled_requirements_txt import CompiledRequirementsTxt
    from update_time.markers.reference import SteeredReference
    from update_time.primitives.location import Location

type _ByPackage = Mapping[DependencyName, Sequence[SteeredReference]]


@dataclass(frozen=True)
class RequirementsCompiler(ABC):
    """A tool that recompiles a requirements file it compiled, with the option that sets its cutoff."""

    compiled: CompiledRequirementsTxt
    tool: ClassVar[str]  # The command, as the log names it
    setting: ClassVar[str]  # The tool and its cutoff option, as the log names them
    cutoff_option: ClassVar[str]
    package_cooldown_option: ClassVar[str | None]  # None for a tool that takes a cutoff for the whole run alone
    log: ClassVar[Logger] = get_logger("requirements.txt")

    def recompile(self) -> None:
        """Run the command the header records, in the directory the file was compiled in, upgrading what it may.

        Where the tool takes the cooldown of a package, the markers alone set it, in place of each package cooldown the
        header records.
        """
        if not (command := self.compiled.recompile_command):
            return
        if not is_installed(command.executable):
            self.log.compiler_not_on_path(self.compiled.path, command.executable)
        elif not self._recompiled(command):
            self.log.compiled_file_not_recompiled(self.compiled.path, self.tool)

    @property
    def _managed_by_comment_line(self) -> str:
        """Return the line in a source that says Update-time sets the cutoff the compiled file's header records."""
        cutoff = self.cutoff_option.removeprefix("--")
        return (
            f"# Update-time sets the {cutoff} in the compiled file's header; remove this line to set the {cutoff} "
            "yourself"
        )

    def _recompiled(self, command: Command) -> bool:
        """Recompile the file with the command, and return whether the tool succeeded."""
        compiled = self.compiled
        # The cutoff of a compiled requirements file lives in its header, which a recompile rewrites. So Update-time
        # writes the managed-by comment on a line of its own at the top of the file's source instead. It recognises the
        # comment by its whole line, since a marker in a source holds `update-time` as well.
        line = self._managed_by_comment_line
        commented = any(requirements_txt.holds_line(source, line) for source in compiled.sources)
        declarations = [
            declaration for source in compiled.sources for declaration in requirements_txt.declarations(source)
        ]
        steering = _by_package(declarations)
        if option := self.package_cooldown_option:
            command = command.without_option(option)
        command, cooldown = self._managed_cutoff(command, commented=commented)
        cutoff = (self.cutoff_option, cooldown) if cooldown else ()
        options = (*cutoff, *self._package_cooldowns(steering), *self._upgrades(steering))
        result = run(Command(*command, "--quiet", *options), cwd=compiled.compile_directory)
        if not result.succeeded:
            return False
        if compiled.writes_to_standard_output:
            compiled.path.write_text(result.stdout)
        if cooldown and not commented:
            requirements_txt.prepend_line(compiled.first_source, line)
            self.log.managed_by_comment_written(compiled.first_source, compiled.path, cooldown, self.setting)
        self._report_moved_versions(command.executable, declarations)
        return True

    def _managed_cutoff(self, command: Command, *, commented: bool) -> tuple[Command, str | None]:
        """Return the command without the cutoff Update-time manages, and the cooldown to pass in its place.

        Where no source carries the managed-by comment, a cutoff the header records is the user's, and stays. Where the
        tool's settings hold a cutoff, Update-time does not pass a cooldown.
        """
        if not commented and command.has_option(self.cutoff_option):
            return command, None
        command = command.without_option(self.cutoff_option)
        if self.settings_hold_cutoff(command):
            return command, None
        return command, self.duration(COOLDOWN.get())

    @abstractmethod
    def settings_hold_cutoff(self, command: Command) -> bool:
        """Return whether the tool's settings for the command hold a cutoff, from a config file or the environment."""

    @abstractmethod
    def duration(self, days: int) -> str:
        """Return the number of days as the tool reads a cutoff relative to now."""

    def _package_cooldowns(self, declarations: _ByPackage) -> list[str]:
        """Return the tool's package cooldown option for each of the pins whose markers set a cooldown of their own."""
        if not (option := self.package_cooldown_option):
            return []
        cooldowns = ((pin.name, _marker_of(pin, declarations).cooldown.value) for pin in self.compiled.pins)
        return [
            word for name, days in cooldowns if days is not None for word in (option, f"{name}={self.duration(days)}")
        ]

    def _report_moved_versions(self, executable: str, declarations: Iterable[SteeredReference]) -> None:
        """Report where the recompile moved a version, up or back: at each line in the sources declaring the package.

        A requirement pinning an exact version is left out, since its version moved in the source and was reported
        there.
        """
        after = compiled_requirements_txt.CompiledRequirementsTxt.read(self.compiled.path).pins
        log_moved_versions(self.log, executable, self.compiled.pins, after, declarations)

    def _upgrades(self, declarations: _ByPackage) -> list[str]:
        """Return an `--upgrade-package` option for each of the file's pins that its markers do not hold back.

        A pin whose version is not PEP 440 is held back with a warning, since a bound cannot be judged against it.
        """
        upgrades: list[str] = []
        for pin in self.compiled.pins:
            if not is_valid(pin.version):
                self.log.pinned_version_unreadable(self.compiled.path, pin.name, pin.version)
                continue
            for declaration in declarations.get(normalized_python_name(pin.name), ()):
                self._warn_if_the_bound_is_redundant(declaration, pin)
            if upgrade := _upgrade(pin, _marker_of(pin, declarations)):
                upgrades.extend(("--upgrade-package", upgrade))
        return upgrades

    def _warn_if_the_bound_is_redundant(self, declaration: SteeredReference, pin: PinnedDependency) -> None:
        """Warn where the bound on a requirement without an exact pin decides nothing at the version the file pins.

        The bound on an exact pin is judged where Update-time updates the pin.
        """
        if not declaration.current_version:
            self.log.warn_if_redundant_bound(replace(declaration, current_version=pin.version), declaration.marker)


def _marker_of(pin: PinnedDependency, declarations: _ByPackage) -> Marker:
    """Return the markers of the declarations of the pin's package in the sources, combined into one."""
    markers = [declaration.marker for declaration in declarations.get(normalized_python_name(pin.name), ())]
    return Marker.combined(markers, pin.version, COOLDOWN.get())


def _by_package[ReferenceT: SteeredReference](
    declarations: Iterable[ReferenceT],
) -> dict[DependencyName, list[ReferenceT]]:
    """Return the declarations, each with its marker, grouped by the normalized name of their package.

    A name uv or PyPI reports finds every declaration of its package, whichever of the spellings that normalize to
    that name each declaration uses.
    """
    grouped: dict[DependencyName, list[ReferenceT]] = {}
    for declaration in declarations:
        grouped.setdefault(normalized_python_name(declaration.dependency), []).append(declaration)
    return grouped


def _upgrade(pin: PinnedDependency, marker: Marker) -> str:
    """Return the pin's package as `--upgrade-package` takes it, narrowed by its bound, or "" to hold it back."""
    specifier = marker.update_specifier(pin.version)
    return "" if specifier is None else f"{pin.name}{specifier}"


def log_moved_versions(
    log: Logger,
    manager: str,
    before: Iterable[PinnedDependency],
    after: Iterable[PinnedDependency],
    declarations: Iterable[SteeredReference],
) -> None:
    """Log each version that moved, up or back, at each declaration of its package that does not pin an exact version.

    A package without a version before is reported as new.
    """
    versions_before = {normalized_python_name(pin.name): pin.version for pin in before}
    unpinned = _by_package(declaration for declaration in declarations if not declaration.current_version)
    locations = {name: [declaration.location for declaration in group] for name, group in unpinned.items()}
    for pin in after:
        name = normalized_python_name(pin.name)
        if name not in locations or (old := versions_before.get(name, "")) == pin.version:
            continue
        if PEP_440.is_downgrade(old, pin.version):
            for location in locations[name]:
                log.version_moved_back(manager, Reference(pin.name, old, location), pin.version)
        else:
            log_new_version(log, pin.name, pin.version, locations[name])


def log_new_version(
    log: Logger, package: DependencyName, version: VersionString, locations: Sequence[Location]
) -> None:
    """Log that a new version is available for the PyPI package, at each of the locations declaring it.

    The package is named as the package manager reports it, so one package is named one way however each line spells it.
    The changelog and publication date are fetched once, whichever locations the version is reported at.
    """
    changes = get_changes(package, version)
    published = get_publication_datetime(package, version)
    dependency_version = DependencyVersion(version, changes, publication=LookedUp(published))
    for location in locations:
        # This message renders no version the reference is pinned to, so the reference names none.
        log.new_version(Reference(package, "", location), dependency_version)
