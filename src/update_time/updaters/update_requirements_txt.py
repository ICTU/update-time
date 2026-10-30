"""Find requirements files, bump the exact pins in them, and check the requirements that pin no exact version.

A compiled requirements file is left as it is, and the sources it names are updated instead.

An exact pin is bumped to its latest PyPI version, with the extras, environment markers and inline comments on its
line preserved, since only the version substring is replaced. A requirement that pins no exact version is rewritten
not at all: the project behind it is checked instead.
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain.file_type import REQUIREMENTS_TXT
from update_time.domain.reference import Reference
from update_time.io.filesystem import glob_for, is_scanned
from update_time.io.log import get_logger
from update_time.manifests import requirements_txt as requirements_txt_manifest
from update_time.markers.directive import DIRECTIVES, Reason
from update_time.markers.marker import Scope
from update_time.references.file import update_file
from update_time.references.match import reference_matches
from update_time.references.resolve import report_project_checks, warn_if_the_opt_in_is_redundant
from update_time.references.vulnerability import warn_about_vulnerable_references
from update_time.sources.osv import Ecosystem
from update_time.sources.pypi import get_latest_version, project

if TYPE_CHECKING:
    from update_time.domain.line import Line
    from update_time.markers.marker import Marker

_LOG = get_logger("requirements.txt")


def _warn_about_items_that_decide_nothing(marker: Marker, reference: Reference) -> None:
    """Warn about each item of the requirement's marker that decides nothing."""
    if bound_directive := marker.bound_directive:
        _LOG.redundant_directive(reference, bound_directive, Reason.NO_VERSION_TO_UPDATE)
    for directive in DIRECTIVES:
        if directive.without_a_version is not None and (written := marker.directive_for(directive.scope)):
            _LOG.redundant_directive(reference, written, directive.without_a_version)
    # An `ignore[update]` holds nothing back on a requirement that does not pin a version.
    warn_if_the_opt_in_is_redundant(
        reference, marker, _LOG, Scope.FLOATING_PIN, Reason.RANGE_NEVER_PINNED, update_held_back=False
    )
    warn_if_the_opt_in_is_redundant(
        reference, marker, _LOG, Scope.HASH_DRIFT, Reason.NO_HASH_PIN, update_held_back=False
    )


def _check_loose_requirements(lines: list[Line]) -> None:
    """Check each requirement that pins no exact version.

    Such a requirement pins no version to resolve an update for, so it takes the project checks instead. What its
    marker gets wrong is reported whether or not those checks run, since that needs no release.
    """
    for line, match, marker in reference_matches(lines, requirements_txt_manifest.LOOSE_REQUIREMENT_REFERENCE):
        # No pass before this one reads these lines, so their markers are reported here.
        _LOG.report_marker(match["dependency"], marker, line.location, holds_the_update_back=False)
        if requirements_txt_manifest.names_an_archive(dependency := match["dependency"]):
            continue
        reference = Reference(dependency, "", line.location)
        _warn_about_items_that_decide_nothing(marker, reference)
        report_project_checks(reference, marker, _LOG, project)


def _compile_directory(compiled: Path) -> Path:
    """Return the directory the file was compiled in: its path, less the output path its header records.

    Where the header does not record an output that ends the file's path, the file's own directory is returned.
    """
    output = requirements_txt_manifest.output_name(compiled)
    output_parts = Path(os.path.normpath(output)).parts if output else ()
    if output_parts and compiled.parts[-len(output_parts) :] == output_parts:
        return Path(*compiled.parts[: -len(output_parts)])
    return compiled.parent


def _sources(compiled: Path) -> list[Path]:
    """Return the sources of a compiled requirements file the run reaches, and log them and the ones it cannot reach.

    The line about the file names the sources it updates the pins in, which leaves out a source that is compiled too.
    """
    if not (source_names := requirements_txt_manifest.source_names(compiled)):
        _LOG.compiled_file_without_a_source(compiled)
        return []
    directory = _compile_directory(compiled)
    sources = []
    for source_name in source_names:
        source = Path(os.path.normpath(directory / source_name))
        if not is_scanned(source):
            _LOG.source_outside_the_scan(compiled, source)
        elif not source.is_file():
            _LOG.source_not_found(compiled, source)
        else:
            sources.append(source)
    if plain_sources := [source for source in sources if not requirements_txt_manifest.is_compiled(source)]:
        _LOG.compiled_file_left_as_it_is(compiled, plain_sources)
    return sources


def _update_pins(requirements_txt: Path) -> None:
    """Update the exact pins in a single requirements file, and check its other requirements."""
    exact_pin = requirements_txt_manifest.EXACT_PIN_REFERENCE
    lines = update_file(requirements_txt, exact_pin, get_new_version=get_latest_version, logger=_LOG)
    _check_loose_requirements(lines)
    warn_about_vulnerable_references(lines, exact_pin, Ecosystem.PYPI, _LOG)


def update_requirements_txts() -> None:
    """Find all requirements files and update the exact pins in them, or in their sources where they are compiled."""
    pending = list(set(glob_for(REQUIREMENTS_TXT)))
    seen = set(pending)
    while pending:
        requirements_txt = pending.pop()
        if not requirements_txt_manifest.is_compiled(requirements_txt):
            _update_pins(requirements_txt)
            continue
        for source in _sources(requirements_txt):
            if source not in seen:
                seen.add(source)
                pending.append(source)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's requirements files."""
    update_requirements_txts()


if __name__ == "__main__":  # pragma: no cover
    main()
