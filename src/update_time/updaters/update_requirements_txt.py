"""Find requirements files, bump the exact pins in them, and check the requirements that pin no exact version.

An exact pin is bumped to its latest PyPI version, with the extras, environment markers and inline comments on its
line preserved, since only the version substring is replaced. A requirement that pins no exact version is rewritten
not at all: its package is checked for staleness instead.
"""

from typing import TYPE_CHECKING

from update_time.domain.file_type import REQUIREMENTS_TXT
from update_time.domain.reference import Reference
from update_time.file_formats import requirements_txt as requirements_txt_format
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.markers.directive import DIRECTIVES, Reason
from update_time.references.file import update_file
from update_time.references.match import reference_matches
from update_time.references.resolve import report_project_checks, warn_about_inverted_items
from update_time.references.vulnerability import warn_about_vulnerable_references
from update_time.sources.osv import Ecosystem
from update_time.sources.pypi import get_latest_version, newest_release

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.line import Line
    from update_time.markers.marker import Marker

_LOG = get_logger("requirements.txt")
# Where a requirement line starts: the package name, optionally indented and optionally extra'd, and whatever space
# follows it. Indentation counts as pip strips a line before parsing it (`req_file.ignore_comments`), so an indented
# line is a requirement like any other. Both patterns below start here rather than spelling the name twice.
_REQUIREMENT_NAME = r"^\s*(?P<dependency>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*"
# Match an exact pin: the name followed by `==` and a version. `===` (arbitrary equality) and every other operator
# is left to the pattern below, so only `==` pins are updated.
_REQUIREMENT_RE = _REQUIREMENT_NAME + r"==\s*(?P<version>[A-Za-z0-9][A-Za-z0-9._!+-]*)"
# Match a requirement that pins no exact version: the name followed by any PEP 440 operator but `==`, or by no
# specifier at all — an environment marker, a comment, or the end of the line. Such a requirement pins no version
# to update, but its package has a newest release to measure staleness against. An option line (`-r`,
# `--index-url`), a comment line, and a direct reference (`name @ url`, a bare URL) match none of that.
_LOOSE_REQUIREMENT_RE = _REQUIREMENT_NAME + r"(?:===|[<>!~]=|[<>]|[;#]|$)"
# The filename suffixes pip installs as a local archive rather than resolving as a requirement (its
# `ARCHIVE_EXTENSIONS`). A filename with no path in front of it, `mypkg-1.0.tar.gz`, is spelled like a bare
# requirement, so the pattern above matches it and PyPI would be asked for a package by that name.
_ARCHIVE_SUFFIXES = (".zip", ".whl", ".tar.gz", ".tar.bz2", ".tar", ".tgz", ".tar.xz", ".txz", ".tlz", ".tar.lzma")


def _warn_about_items_that_decide_nothing(marker: Marker, reference: Reference) -> None:
    """Warn about each item of the requirement's marker that decides nothing, so it is reported rather than acted on.

    An item Update-time cannot read is left to say nothing, and a comparison item running the wrong way round sets
    nothing either. A `yanked` or `vulnerable` scope holds nothing back, since both checks need the version such a
    requirement does not pin.
    """
    as_written = marker.as_written
    if as_written.invalid_item is not None:
        _LOG.invalid_bracket_item(reference.dependency, as_written.invalid_item, reference.location)
    warn_about_inverted_items(as_written, reference, _LOG)
    if bound_directive := as_written.bound_directive:
        _LOG.redundant_directive(reference, bound_directive, Reason.NO_VERSION_TO_UPDATE)
    for directive in DIRECTIVES:
        if directive.without_a_version is not None and as_written.directive_for(directive.scope):
            _LOG.redundant_directive(reference, as_written.directive_for(directive.scope), directive.without_a_version)


def _check_loose_requirements(lines: list[Line]) -> None:
    """Check each requirement that pins no exact version.

    Such a requirement pins no version to resolve an update for, so it takes the project checks instead. What its
    marker gets wrong is reported whether or not those checks run, since that needs no release.
    """
    for line, match, marker in reference_matches(lines, _LOOSE_REQUIREMENT_RE, _LOG):
        if (dependency := match["dependency"]).endswith(_ARCHIVE_SUFFIXES):
            continue
        reference = Reference(dependency, "", line.location)
        _warn_about_items_that_decide_nothing(marker, reference)
        report_project_checks(reference, marker, _LOG, newest_release)


def _update_requirements_txt(requirements_txt: Path) -> None:
    """Update the exact pins in a single requirements file, unless it is compiled or locked."""
    if requirements_txt_format.is_compiled(requirements_txt):
        _LOG.skipped(requirements_txt, "compiled or hash-pinned requirements file")
        return
    lines = update_file(requirements_txt, _REQUIREMENT_RE, get_new_version=get_latest_version, logger=_LOG)
    _check_loose_requirements(lines)
    warn_about_vulnerable_references(lines, _REQUIREMENT_RE, Ecosystem.PYPI, _LOG)


def update_requirements_txts() -> None:
    """Find all requirements files and update the exact pins in them."""
    requirements_files = set(glob_for(REQUIREMENTS_TXT))
    for requirements_txt in requirements_files:
        _update_requirements_txt(requirements_txt)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's requirements files."""
    update_requirements_txts()


if __name__ == "__main__":  # pragma: no cover
    main()
