"""Find requirements files, bump the exact pins in them, and check the requirements that pin no exact version.

A requirements file uv or pip-compile compiled is recompiled once the pins in its sources are updated.

An exact pin is bumped to its latest PyPI version, with the extras, environment markers and inline comments on its
line preserved, since only the version substring is replaced. A requirement that pins no exact version is rewritten
not at all: the project behind it is checked instead.
"""

from typing import TYPE_CHECKING

from update_time.domain.file_type import REQUIREMENTS_TXT
from update_time.domain.reference import Reference
from update_time.io.filesystem import glob_for, is_scanned
from update_time.io.log import get_logger
from update_time.manifests import requirements_txt as requirements_txt_manifest
from update_time.manifests.requirements_txt import CompiledBy, CompiledRequirementsTxt
from update_time.markers.directive import DIRECTIVES, Reason
from update_time.markers.marker import Scope
from update_time.package_managers.pip_tools import PipCompile
from update_time.package_managers.uv import UvPipCompile
from update_time.references.file import update_file
from update_time.references.match import reference_matches
from update_time.references.resolve import report_project_checks, warn_if_the_opt_in_is_redundant
from update_time.references.vulnerability import warn_about_vulnerable_references
from update_time.sources.osv import Ecosystem
from update_time.sources.pypi import get_latest_version, project

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.line import Line
    from update_time.markers.marker import Marker
    from update_time.package_managers.requirements_compiler import RequirementsCompiler

_LOG = get_logger("requirements.txt")


def _warn_about_items_that_decide_nothing(
    marker: Marker, reference: Reference, compiler: RequirementsCompiler | None
) -> None:
    """Warn about each item of the requirement's marker that decides nothing.

    Where a compiler recompiles a file from the requirement's file, a bound and an `ignore[update]` steer the recompile,
    and so does a cooldown where the compiler takes the cooldown of a package.
    """
    if not compiler and (bound_directive := marker.bound_directive):
        _LOG.redundant_directive(reference, bound_directive, Reason.NO_VERSION_TO_UPDATE)
    if compiler and not compiler.package_cooldown_option and (cooldown := marker.directive_for(Scope.COOLDOWN)):
        _LOG.redundant_directive(reference, cooldown, Reason.COOLDOWN_PER_RUN)
    for directive in DIRECTIVES:
        if compiler and directive.scope is Scope.COOLDOWN:
            continue
        if directive.without_a_version is not None and (written := marker.directive_for(directive.scope)):
            _LOG.redundant_directive(reference, written, directive.without_a_version)
    # Update-time never pins a requirement that does not pin a version, and the requirement does not carry a hash pin.
    # So each opt-in is redundant, whether or not the update is held back.
    warn_if_the_opt_in_is_redundant(
        reference, marker, _LOG, Scope.FLOATING_PIN, Reason.RANGE_NEVER_PINNED, update_held_back=False
    )
    warn_if_the_opt_in_is_redundant(
        reference, marker, _LOG, Scope.HASH_DRIFT, Reason.NO_HASH_PIN, update_held_back=False
    )


def _check_loose_requirements(lines: list[Line], compiler: RequirementsCompiler | None) -> None:
    """Check each requirement that pins no exact version.

    Such a requirement pins no version to resolve an update for, so it takes the project checks instead. What its
    marker gets wrong is reported whether or not those checks run, since that needs no release.
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

    A compiled file is recompiled only where the run reaches every source it names.
    """
    plain_files: dict[Path, None] = {}
    recompilable = []
    for requirements_txt in set(glob_for(REQUIREMENTS_TXT)):
        if not requirements_txt_manifest.is_compiled(requirements_txt):
            plain_files[requirements_txt] = None
            continue
        compiled = CompiledRequirementsTxt.read(requirements_txt)
        sources = _sources(compiled)
        if compiled.recompile_command and compiled.sources and len(sources) == len(compiled.sources):
            recompilable.append(compiled)
        plain_files.update(dict.fromkeys(sources))
    compilers = [_compiler_for(compiled) for compiled in recompilable]
    compiler_of = {source: compiler for compiler in compilers for source in compiler.compiled.sources}
    for requirements_txt in plain_files:
        _update_pins(requirements_txt, compiler_of.get(requirements_txt))
    for compiler in compilers:
        compiler.recompile()


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's requirements files."""
    update_requirements_txts()


if __name__ == "__main__":  # pragma: no cover
    main()
