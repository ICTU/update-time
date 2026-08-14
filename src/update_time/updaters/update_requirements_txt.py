"""Find requirements files, bump the exact pins in them, and check the requirements that pin no exact version.

An exact pin is bumped to its latest PyPI version, with the extras, environment markers and inline comments on its
line preserved, since only the version substring is replaced. A requirement that pins no exact version is rewritten
not at all: its package is checked for staleness instead.
"""

from typing import TYPE_CHECKING

from update_time.domain.reference import Reference, ResolvedReference
from update_time.domain.staleness import NO_STALENESS_CHECK, STALE_AFTER
from update_time.file_formats import requirements_txt as requirements_txt_format
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.references.match import reference_matches
from update_time.references.resolve import warn_about_inverted_items
from update_time.references.vulnerability import warn_about_vulnerable_references
from update_time.sources.osv import Ecosystem
from update_time.sources.pypi import get_latest_version, newest_release

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.line import Line
    from update_time.domain.marker import Marker

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
# Requirements files follow the flat conventions `requirements.txt`, `requirements-<purpose>.txt` (e.g.
# `requirements-dev.txt`) and `<purpose>-requirements.txt` (e.g. `dev-requirements.txt`), plus a nested
# `requirements/` directory. The purpose is hyphen-separated on both sides for symmetry, and matching is
# restricted to the `.txt` extension, so unrelated files such as `constraints.txt`, `requirements.in`, or an
# arbitrary `requirementsfoo.txt` are not picked up.
_REQUIREMENTS_GLOB_PATTERNS = ("requirements.txt", "requirements-*.txt", "*-requirements.txt", "requirements/*.txt")


def _warn_about_items_that_decide_nothing(marker: Marker, reference: Reference) -> None:
    """Warn about each item of the requirement's marker that decides nothing, so it is reported rather than acted on.

    An item Update-time cannot read is left to say nothing, and a comparison item running the wrong way round sets
    nothing either. A `yanked` or `vulnerable` scope holds nothing back, since both checks need the version such a
    requirement does not pin. The caller hands the marker as written, so a scope a bare `ignore` only implies is
    reported for none of this.
    """
    if marker.invalid_item is not None:
        _LOG.invalid_bracket_item(reference.dependency, marker.invalid_item, reference.location)
    warn_about_inverted_items(marker, reference, _LOG)
    if bound_directive := marker.bound_directive:
        _LOG.redundant_without_an_update(reference, bound_directive)
    if marker.sets_cooldown:
        _LOG.redundant_without_an_update(reference, marker.cooldown_directive)
    if marker.ignore_yanked:
        _LOG.redundant_yank_without_a_version(reference, marker)
    if marker.suppresses_vulnerabilities:
        _LOG.redundant_vulnerable_without_a_version(reference, marker)


def _warn_about_stale_loose_requirements(lines: list[Line]) -> None:
    """Warn about each requirement declared without an exact pin whose package's newest release is old.

    A loose requirement pins no version to resolve an update for, so the release its staleness is measured against
    is read from PyPI here. Staleness is the only check such a requirement gets, so PyPI is left unasked for one
    whose threshold in force is 0, and for one whose marker holds every check back. What the marker gets wrong is
    reported either way, since that needs no release.
    """
    run_wide_threshold = STALE_AFTER.get()
    for line, match, marker in reference_matches(lines, _LOOSE_REQUIREMENT_RE, _LOG):
        if (dependency := match["dependency"]).endswith(_ARCHIVE_SUFFIXES):
            continue
        reference = Reference(dependency, "", line.location)
        _warn_about_items_that_decide_nothing(marker.as_written, reference)
        if marker.holds_everything_back:
            continue  # A bare `ignore` holds the staleness check back, so PyPI is not asked for a release date.
        if (threshold := marker.stale.value_or(run_wide_threshold)) == NO_STALENESS_CHECK:
            continue
        if (release := newest_release(reference.dependency)) is not None:
            resolved = ResolvedReference(**vars(reference), release=release)
            _LOG.report_staleness(resolved, marker, threshold)


def _update_requirements_txt(requirements_txt: Path) -> None:
    """Update the exact pins in a single requirements file, unless it is compiled or locked."""
    if requirements_txt_format.is_compiled(requirements_txt):
        _LOG.skipped(requirements_txt, "compiled or hash-pinned requirements file")
        return
    lines = update_file(requirements_txt, _REQUIREMENT_RE, get_new_version=get_latest_version, logger=_LOG)
    _warn_about_stale_loose_requirements(lines)
    warn_about_vulnerable_references(lines, _REQUIREMENT_RE, Ecosystem.PYPI, _LOG)


def update_requirements_txts() -> None:
    """Find all requirements files and update the exact pins in them."""
    requirements_files = set(glob(*_REQUIREMENTS_GLOB_PATTERNS, case_sensitive=True))
    for requirements_txt in requirements_files:
        _update_requirements_txt(requirements_txt)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's requirements files."""
    update_requirements_txts()


if __name__ == "__main__":  # pragma: no cover
    main()
