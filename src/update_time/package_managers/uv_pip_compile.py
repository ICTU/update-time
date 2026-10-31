"""Recompile a requirements file that uv compiled, steered by the markers in its sources."""

from dataclasses import replace
from typing import TYPE_CHECKING

from packaging.version import InvalidVersion, Version

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import normalized_python_name
from update_time.io.log import get_logger
from update_time.io.process import run
from update_time.manifests import requirements_txt
from update_time.markers.marker import Marker
from update_time.package_managers import uv
from update_time.primitives.command import Command

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from update_time.domain.dependency import DependencyName, PinnedDependency
    from update_time.manifests.requirements_txt import CompiledRequirementsTxt
    from update_time.markers.reference import SteeredReference
    from update_time.primitives.location import Location

type _ByPackage = Mapping[DependencyName, Sequence[SteeredReference]]

_LOG = get_logger("requirements.txt")
_PACKAGE_COOLDOWN_OPTION = "--exclude-newer-package"
# The `exclude-newer` of a compiled requirements file lives in its header, which a recompile rewrites. So Update-time
# writes the comment on a line of its own at the top of the file's source instead.
# The comment is recognised by its whole line, since a marker in the source holds the `update-time` token as well.
_MANAGED_BY_COMMENT_LINE = f"# exclude-newer: {uv.EXCLUDE_NEWER_COMMENT}"


def recompile(compiled: CompiledRequirementsTxt) -> None:
    """Run the uv command the header records, in the directory the file was compiled in, upgrading what it may.

    The markers alone set the cooldown of a package, in place of each package cooldown the header records.
    """
    if not (command := compiled.uv_command):
        return
    commented = _carries_the_managed_by_comment(compiled.sources)
    declarations = [declaration for source in compiled.sources for declaration in requirements_txt.declarations(source)]
    steering = _by_package(declarations)
    command = _without_option(command, _PACKAGE_COOLDOWN_OPTION)
    command, cooldown = _managed_exclude_newer(compiled, command, commented=commented)
    exclude_newer = (uv.EXCLUDE_NEWER_OPTION, cooldown) if cooldown else ()
    options = (*exclude_newer, *_package_cooldowns(compiled.pins, steering), *_upgrades(compiled, steering))
    if not run(Command(*command, "--quiet", *options), cwd=compiled.compile_directory).succeeded:
        _LOG.compiled_file_not_recompiled(compiled.path)
        return
    if cooldown and not commented:
        _write_the_managed_by_comment(compiled.first_source)
        _LOG.managed_by_comment_written(compiled.first_source, compiled.path, cooldown)
    _report_moved_versions(compiled, declarations)


def _managed_exclude_newer(
    compiled: CompiledRequirementsTxt, command: Command, *, commented: bool
) -> tuple[Command, str | None]:
    """Return the command without the `exclude-newer` Update-time manages, and the cooldown to pass in its place.

    Where no source carries the managed-by comment, an `exclude-newer` the header records is the user's, and stays.
    Where uv's settings hold an `exclude-newer`, Update-time does not pass a cooldown.
    """
    if not commented and uv.EXCLUDE_NEWER_OPTION in command:
        return command, None
    command = _without_option(command, uv.EXCLUDE_NEWER_OPTION)
    if uv.settings_hold_exclude_newer(command, compiled.compile_directory, compiled.path, _LOG):
        return command, None
    return command, uv.duration(COOLDOWN.get())


def _report_moved_versions(compiled: CompiledRequirementsTxt, declarations: Iterable[SteeredReference]) -> None:
    """Report where the recompile moved a version: at each line in the sources declaring the package.

    A requirement pinning an exact version is left out, since its version moved in the source and was reported there.
    """
    before = {normalized_python_name(pin.name): pin.version for pin in compiled.pins}
    locations: dict[DependencyName, list[Location]] = {}
    for declaration in declarations:
        if not declaration.current_version:
            locations.setdefault(normalized_python_name(declaration.dependency), []).append(declaration.location)
    for pin in requirements_txt.CompiledRequirementsTxt.read(compiled.path).pins:
        name = normalized_python_name(pin.name)
        if name in locations and before.get(name) != pin.version:
            uv.log_new_version(_LOG, pin.name, pin.version, locations[name])


def _package_cooldowns(pins: Iterable[PinnedDependency], declarations: _ByPackage) -> list[str]:
    """Return an `--exclude-newer-package` option for each of the pins whose markers set a cooldown of their own."""
    cooldowns = ((pin.name, _marker_of(pin, declarations).cooldown.value) for pin in pins)
    return [
        word
        for name, days in cooldowns
        if days is not None
        for word in (_PACKAGE_COOLDOWN_OPTION, f"{name}={uv.duration(days)}")
    ]


def _without_option(command: Command, option: str) -> Command:
    """Return the command without each occurrence of the option and its value."""
    words: list[str] = []
    remaining = iter(command)
    for word in remaining:
        if word == option:
            next(remaining, None)
        else:
            words.append(word)
    return Command(*words)


def _upgrades(compiled: CompiledRequirementsTxt, declarations: _ByPackage) -> list[str]:
    """Return an `--upgrade-package` option for each of the file's pins that its markers do not hold back.

    A pin whose version is not PEP 440 is held back with a warning, since a bound cannot be judged against it.
    """
    upgrades: list[str] = []
    for pin in compiled.pins:
        try:
            version = Version(pin.version)
        except InvalidVersion:
            _LOG.pinned_version_unreadable(compiled.path, pin.name, pin.version)
            continue
        for declaration in declarations.get(normalized_python_name(pin.name), ()):
            _warn_if_the_bound_is_redundant(declaration, pin)
        if upgrade := _upgrade(pin, version, _marker_of(pin, declarations)):
            upgrades.extend(("--upgrade-package", upgrade))
    return upgrades


def _warn_if_the_bound_is_redundant(declaration: SteeredReference, pin: PinnedDependency) -> None:
    """Warn where the bound on a requirement without an exact pin decides nothing at the version the file pins.

    The bound on an exact pin is judged where Update-time updates the pin.
    """
    if not declaration.current_version:
        _LOG.warn_if_redundant_bound(replace(declaration, current_version=pin.version), declaration.marker)


def _marker_of(pin: PinnedDependency, declarations: _ByPackage) -> Marker:
    """Return the markers of the declarations of the pin's package in the sources, combined into one."""
    markers = [declaration.marker for declaration in declarations.get(normalized_python_name(pin.name), ())]
    return Marker.combined(markers, pin.version, COOLDOWN.get())


def _by_package(declarations: Iterable[SteeredReference]) -> dict[DependencyName, list[SteeredReference]]:
    """Return the requirements, each with its marker, grouped by the normalized name of their package."""
    by_package: dict[DependencyName, list[SteeredReference]] = {}
    for declaration in declarations:
        by_package.setdefault(normalized_python_name(declaration.dependency), []).append(declaration)
    return by_package


def _upgrade(pin: PinnedDependency, version: Version, marker: Marker) -> str:
    """Return the pin's package as `--upgrade-package` takes it, narrowed by the marker's bound, or "" to hold it back.

    A bound leaving out the pinned version holds the package back, since uv would move the pin down into the bound.
    """
    bound = marker.version_bound
    if marker.holds_the_update_back or not bound.keeps(version, pin.version):
        return ""
    specifier = bound.specifier_in_allow_form(pin.version)
    return "" if specifier is None else f"{pin.name}{specifier}"


def _carries_the_managed_by_comment(sources: Iterable[Path]) -> bool:
    """Return whether one of the sources says that Update-time manages uv's `exclude-newer`."""
    return any(_MANAGED_BY_COMMENT_LINE in source.read_text().splitlines() for source in sources)


def _write_the_managed_by_comment(source: Path) -> None:
    """Write the line saying that Update-time manages uv's `exclude-newer` at the top of the source."""
    source.write_text(f"{_MANAGED_BY_COMMENT_LINE}\n{source.read_text()}")
