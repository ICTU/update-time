"""Find requirements files, bump the exact pins in them, and check the requirements that pin no exact version.

A requirements file uv or pip-compile compiled is recompiled once the pins in its sources are updated.

An exact pin is bumped to its latest PyPI version, with the extras, environment markers and inline comments on its
line preserved, since only the version substring is replaced. A requirement without an exact pin stays as it is.
Update-time checks its project instead, and checks the version a compiled file pins for it.
"""

from dataclasses import replace
from typing import TYPE_CHECKING

from update_time.domain.dependency import normalized_python_name
from update_time.domain.file_type import REQUIREMENTS_TXT
from update_time.domain.reference import Reference
from update_time.io.filesystem import glob_for, is_scanned
from update_time.io.log import get_logger
from update_time.manifests import requirements_txt as requirements_txt_manifest
from update_time.manifests.requirements_txt import CompiledBy, CompiledRequirementsTxt
from update_time.markers.directive import WARNING_DIRECTIVES, Reason
from update_time.markers.marker import Scope
from update_time.package_managers.pip_tools import PipCompile
from update_time.package_managers.uv import UvPipCompile
from update_time.references.delegated import warn_about_pinned_versions
from update_time.references.file import update_file
from update_time.references.match import reference_matches
from update_time.references.resolve import report_project_checks, warn_if_the_opt_in_is_redundant
from update_time.references.vulnerability import warn_about_vulnerable_references
from update_time.sources.osv import Ecosystem
from update_time.sources.pypi import get_latest_version, project, yank_state

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.domain.line import Line
    from update_time.markers.marker import Marker
    from update_time.markers.reference import SteeredReference
    from update_time.package_managers.requirements_compiler import RequirementsCompiler

_LOG = get_logger("requirements.txt")


def _warn_about_items_that_decide_nothing(
    marker: Marker, reference: Reference, compiler: RequirementsCompiler | None
) -> None:
    """Warn about each item of the requirement's marker that steers an update and decides nothing.

    Where a compiler recompiles a file from the requirement's file, a bound and an `ignore[update]` steer the recompile.
    So does a cooldown, where the compiler takes the cooldown of a package.
    """
    if compiler is None:
        _warn_about_items_that_need_a_version_to_update(marker, reference)
    elif not compiler.package_cooldown_option and (cooldown := marker.directive_for(Scope.COOLDOWN)):
        _LOG.redundant_directive(reference, cooldown, Reason.COOLDOWN_PER_RUN)
    # Update-time never pins a requirement that does not pin a version, and the requirement does not carry a hash pin.
    # So each opt-in is redundant, whether or not the update is held back.
    warn_if_the_opt_in_is_redundant(
        reference, marker, _LOG, Scope.FLOATING_PIN, Reason.RANGE_NEVER_PINNED, update_held_back=False
    )
    warn_if_the_opt_in_is_redundant(
        reference, marker, _LOG, Scope.HASH_DRIFT, Reason.NO_HASH_PIN, update_held_back=False
    )


def _warn_about_items_that_need_a_version_to_update(marker: Marker, reference: Reference) -> None:
    """Warn about each item of the requirement's marker that steers an update, which needs a version to update."""
    if bound_directive := marker.bound_directive:
        _LOG.redundant_directive(reference, bound_directive, Reason.NO_VERSION_TO_UPDATE)
    if cooldown := marker.directive_for(Scope.COOLDOWN):
        _LOG.redundant_directive(reference, cooldown, Reason.NO_VERSION_TO_UPDATE)


def _warn_about_items_that_need_a_version_to_check(marker: Marker, reference: Reference) -> None:
    """Warn about each item of the requirement's marker that steers a check, which needs a version to check."""
    for directive in WARNING_DIRECTIVES:
        if directive.without_a_version is not None and (written := marker.directive_for(directive.scope)):
            _LOG.redundant_directive(reference, written, directive.without_a_version)


def _check_loose_requirements(lines: list[Line], compiler: RequirementsCompiler | None) -> None:
    """Check each requirement that pins no exact version.

    Such a requirement pins no version to resolve an update for, so it takes the project checks instead. The items of
    its marker that steer the update are reported here, whether or not those checks run.
    """
    for line, match, marker in reference_matches(lines, requirements_txt_manifest.LOOSE_REQUIREMENT_REFERENCE):
        # No pass before this one reads these lines, so their markers are reported here.
        holds_the_update_back = compiler is not None and marker.ignores(Scope.UPDATE)
        _LOG.report_marker(match["dependency"], marker, line.location, holds_the_update_back=holds_the_update_back)
        if requirements_txt_manifest.names_an_archive(dependency := match["dependency"]):
            continue
        reference = Reference(dependency, "", line.location)
        _warn_about_items_that_decide_nothing(marker, reference, compiler)
        report_project_checks(reference, marker, _LOG, project)


def _sources(compiled: CompiledRequirementsTxt) -> list[Path]:
    """Return the sources of a compiled requirements file the run reaches, and log them and the ones it cannot reach.

    A file with a compiled source is skipped, so none of its sources is returned. A file Update-time does not recompile
    gets a line naming the sources whose pins are updated.
    """
    if not compiled.sources:
        _LOG.compiled_file_without_a_source(compiled.path)
        return []
    sources = []
    for source in compiled.sources:
        if not is_scanned(source):
            _LOG.source_outside_the_scan(compiled.path, source)
        elif not source.is_file():
            _LOG.source_not_found(compiled.path, source)
        else:
            sources.append(source)
    if compiled_source := next((source for source in sources if requirements_txt_manifest.is_compiled(source)), None):
        _LOG.compiled_source_unsupported(compiled.path, compiled_source)
        return []
    if sources and (reason := compiled.unrecompilable) is not None:
        _LOG.compiled_file_left_as_it_is(compiled.path, sources, reason)
    return sources


def _update_pins(requirements_txt: Path, compiler: RequirementsCompiler | None) -> None:
    """Update the exact pins in a single requirements file, and check its other requirements."""
    exact_pin = requirements_txt_manifest.EXACT_PIN_REFERENCE
    lines = update_file(requirements_txt, exact_pin, get_new_version=get_latest_version, logger=_LOG)
    _check_loose_requirements(lines, compiler)
    warn_about_vulnerable_references(lines, exact_pin, Ecosystem.PYPI, _LOG)


def _compiler_for(compiled: CompiledRequirementsTxt) -> RequirementsCompiler:
    """Return the tool that recompiles the file: the one its header names."""
    if compiled.compiled_by is CompiledBy.PIP_COMPILE:
        return PipCompile(compiled)
    return UvPipCompile(compiled)


def update_requirements_txts() -> None:
    """Find all requirements files, update the pins in them or in their sources, and recompile the compiled ones.

    A compiled file is recompiled, and its sources checked at the versions it pins, only where the run reaches every
    source it names. The check for yanks and vulnerabilities runs once the recompile is done.
    """
    plain_files: dict[Path, None] = {}
    compiled_files: list[CompiledRequirementsTxt] = []
    for requirements_txt in set(glob_for(REQUIREMENTS_TXT)):
        if not requirements_txt_manifest.is_compiled(requirements_txt):
            plain_files[requirements_txt] = None
            continue
        compiled = CompiledRequirementsTxt.read(requirements_txt)
        sources = _sources(compiled)
        if compiled.sources and len(sources) == len(compiled.sources):
            compiled_files.append(compiled)
        plain_files.update(dict.fromkeys(sources))
    recompilable = [compiled for compiled in compiled_files if compiled.recompile_command]
    compilers = [_compiler_for(compiled) for compiled in recompilable]
    compiler_of = {source: compiler for compiler in compilers for source in compiler.compiled.sources}
    for requirements_txt in plain_files:
        _update_pins(requirements_txt, compiler_of.get(requirements_txt))
    for compiler in compilers:
        compiler.recompile()
    _check_pinned_versions(plain_files, compiled_files)


def _check_pinned_versions(
    requirements_files: Iterable[Path], compiled_files: Iterable[CompiledRequirementsTxt]
) -> None:
    """Check the versions compiled files pin for the requirements without an exact pin.

    Where a compiled file does not pin the requirement, the checks lack a version, and an item steering them is
    redundant.
    """
    pins_of = _pins_by_source(compiled_files)
    batches = []
    for requirements_file in requirements_files:
        pins = pins_of.get(requirements_file, {})
        pinned: list[SteeredReference] = []
        for declaration in requirements_txt_manifest.declarations(requirements_file):
            if declaration.current_version or requirements_txt_manifest.names_an_archive(declaration.dependency):
                continue
            if versions := pins.get(normalized_python_name(declaration.dependency)):
                pinned.extend(replace(declaration, current_version=version) for version in sorted(versions))
            else:
                _warn_about_items_that_need_a_version_to_check(declaration.marker, declaration)
        batches.append(pinned)
    warn_about_pinned_versions(batches, _LOG, yank_state, Ecosystem.PYPI)


def _pins_by_source(
    compiled_files: Iterable[CompiledRequirementsTxt],
) -> dict[Path, dict[DependencyName, set[VersionString]]]:
    """Return the versions the compiled files pin, read anew, by the sources they name and by package."""
    pins_of: dict[Path, dict[DependencyName, set[VersionString]]] = {}
    for compiled in compiled_files:
        pins = CompiledRequirementsTxt.read(compiled.path).pins
        for source in compiled.sources:
            for pin in pins:
                pins_of.setdefault(source, {}).setdefault(normalized_python_name(pin.name), set()).add(pin.version)
    return pins_of


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's requirements files."""
    update_requirements_txts()


if __name__ == "__main__":  # pragma: no cover
    main()
