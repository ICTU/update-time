"""Rewrite pinned version references in text, line by line.

The updaters that edit files line by line share this engine: Dockerfiles, CI configs, manifests, requirements,
workflows, and the rest. Given a regexp that captures a reference and a function that returns its new version, it
rewrites each matched reference in place, touching only the captured spans and skipping any line pinned by an
`# update-time: ignore` marker. Which version a reference should update to is `resolve.latest_version`'s
decision; this module owns the text surgery around it, reporting what it changed through a `Logger`.
"""

import re
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from update_time.domain.dependency import FloatingPin
from update_time.domain.line import located_lines
from update_time.domain.reference import DriftedPin, hash_drifted
from update_time.markers.drift import report_drift
from update_time.markers.floating import floating_pin_cause
from update_time.markers.marker import Scope, parse_marker
from update_time.primitives.text import rewrite_string
from update_time.references.match import matched_dependency, matched_reference
from update_time.references.resolve import latest_version

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.dependency import DependencyVersion
    from update_time.domain.line import Line
    from update_time.domain.reference import Reference
    from update_time.io.log import Logger
    from update_time.markers.marker import Marker, ReferenceMarker
    from update_time.primitives.location import Location


@dataclass(frozen=True)
class _Rewriter:
    """Everything needed to rewrite one kind of reference in a file.

    That is: how to resolve a new version, where to report, and what to call a reference whose regexp captures no
    `dependency` group.
    """

    get_new_version: NewVersionGetter
    logger: Logger
    dependency: str = ""

    def update_line(self, match: re.Match[str], location: Location, marker: Marker) -> str:
        """Update the matched reference's line with the new version (and digest) if any, or return it unchanged.

        Which version to update to — honouring the reference's `# update-time:` marker — is `latest_version`'s
        decision, or None to leave the line unchanged. A pin that floats is replaced by the version and digest its
        tag serves, and a reference already at its newest version is checked for digest drift.
        """
        reference = matched_reference(match, location, self.dependency)
        latest = latest_version(reference, self.get_new_version, marker, self.logger)
        if latest is None:
            return match.string
        if latest.floating is not None and latest.floating is not FloatingPin.RESOLVED:
            self.logger.unpinned_floating_tag(reference, latest, latest.floating)
            return match.string
        if latest.floating is FloatingPin.RESOLVED:
            return self._pin_floating_reference(match, marker, latest, reference)
        has_sha_group = "sha" in match.groupdict()
        pin_unpinned = has_sha_group and match.group("sha") is None and bool(latest.sha)
        if latest.version == reference.current_version and not pin_unpinned:
            return self._handle_drift(match, marker, latest, reference)
        return self._apply_update(match, latest, reference, pin_unpinned=pin_unpinned)

    def _pin_floating_reference(
        self, match: re.Match[str], marker: Marker, latest: DependencyVersion, reference: Reference
    ) -> str:
        """Return the line for a reference whose pin floats, pinned to the version and digest its tag serves.

        A reference a marker keeps floating is left as it is. One that already records a digest is judged against
        what its tag serves now, so drift is warned about or adopted first; a reference kept floating then adopts
        the digest alone.
        """
        drifted = self._drifted(match, latest)
        if (cause := floating_pin_cause(marker)) is not None:
            if drifted:
                return self._drifted_pin(match, marker, latest, reference)
            self.logger.keeping_floating_tag(reference, latest, cause)
            return match.string
        if drifted and not self._adopts_drift(match, marker, latest, reference):
            return match.string
        return self._apply_update(match, latest, reference, pin_unpinned=match.groupdict().get("sha") is None)

    @staticmethod
    def _drifted(match: re.Match[str], latest: DependencyVersion) -> bool:
        """Return whether the reference records a hash that its source no longer serves."""
        current_sha = match.groupdict().get("sha")
        return current_sha is not None and hash_drifted(latest.sha, current_sha)

    def _handle_drift(
        self, match: re.Match[str], marker: Marker, latest: DependencyVersion, reference: Reference
    ) -> str:
        """Return the line for a reference that keeps its tag, adopting or warning about a re-pushed digest.

        A pinned reference whose digest changed at the registry (a re-pushed tag) has drifted, and one whose
        digest still matches is left as it is.
        """
        if not self._drifted(match, latest):
            return match.string
        return self._drifted_pin(match, marker, latest, reference)

    def _drifted_pin(
        self, match: re.Match[str], marker: Marker, latest: DependencyVersion, reference: Reference
    ) -> str:
        """Return the line for a reference whose digest has drifted, adopting what its tag serves now or not.

        Whether the drift is adopted or only warned about is `report_drift`'s decision. The digest alone is
        replaced, so the reference keeps the tag it names, which is what a reference kept floating needs.
        """
        if self._adopts_drift(match, marker, latest, reference):
            return rewrite_string(match, {"sha": latest.sha})
        return match.string

    def _adopts_drift(
        self, match: re.Match[str], marker: Marker, latest: DependencyVersion, reference: Reference
    ) -> bool:
        """Report the reference's digest as drifted and return whether the digest the registry serves is adopted.

        Whether drift is adopted or only warned about is `report_drift`'s decision, which the reference's marker
        and the run-wide flag steer.
        """
        dependency, version = matched_dependency(match, self.dependency), match.group("version")
        drifted = DriftedPin(dependency, version, reference.location, match.group("sha"), new_sha=latest.sha)
        return report_drift(
            marker, partial(self.logger.digest_drift, drifted), partial(self.logger.adopted_drift, drifted)
        )

    def _apply_update(
        self, match: re.Match[str], latest: DependencyVersion, reference: Reference, *, pin_unpinned: bool
    ) -> str:
        """Return the line rewritten to the latest version and digest, logging the change.

        The change is reported as a pin unless the reference moved to another release, so a resolved floating pin
        is reported as a pin although its version changes: it writes down the release the reference already
        served. A reference that named no version gains the `:` attaching a tag to the image's name.
        """
        if latest.version != match.group("version") and latest.floating is None:
            self.logger.new_version(reference, latest)
        else:
            self.logger.pinned(reference, latest)
        version = f"{'' if match.group('version') else ':'}{latest.version}"
        if pin_unpinned:
            # Append the digest to a previously unpinned reference, bumping the version too if one is available.
            return rewrite_string(match, {"version": f"{version}@{latest.sha}"})
        replacements = {"version": version}
        if match.groupdict().get("sha") is not None:
            replacements["sha"] = latest.sha
        return rewrite_string(match, replacements)


def apply_marker(  # noqa: PLR0913 — a marker named elsewhere in the file cannot be read from the line
    line: Line,
    match: re.Match[str],
    update_line: Callable[[re.Match[str], Location, Marker], str],
    logger: Logger,
    dependency: str = "",
    *,
    marker: Marker | None = None,
) -> str:
    """Read a matched reference's `# update-time:` marker and update it, or leave the line unchanged when held back.

    The gate every reference goes through. It reads a marker written as a comment, on the reference's own line or
    above it, and takes one the file names elsewhere from the updater that read it, in `marker`.

    An unreadable item holds the update back, in case it was meant to bound one. It silences nothing, since an
    unreadable marker holds back what Update-time would write, never what it would tell you. The marker reaches
    `update_line` whatever it holds back, so its bound, its `allow` directives, and what it gets wrong are still
    acted on (see `latest_version`).
    """
    marker = parse_marker(line) if marker is None else marker
    location = line.location
    dependency = matched_dependency(match, dependency)
    if marker.invalid_item is not None:
        logger.invalid_bracket_item(dependency, marker.invalid_item, location)
        return update_line(match, location, marker.frozen)
    logger.recognised_marker(dependency, marker, location)
    if marker.ignores(Scope.UPDATE):
        logger.ignored(dependency, marker, location)
    return update_line(match, location, marker)


def updated_lines(  # noqa: PLR0913 — a reference named elsewhere in the file cannot be read from the line
    lines: list[Line],
    regexp: str | re.Pattern[str],
    update_line: Callable[[re.Match[str], Location, Marker], str],
    logger: Logger,
    dependency: str = "",
    *,
    reference_marker: ReferenceMarker | None = None,
) -> list[str]:
    """Return the lines with `update_line` applied to each line carrying a reference, honouring any marker.

    A line that carries no reference (no `regexp` match) is left untouched — a marker on it is for the line below. A
    line that does carry one goes to the shared `apply_marker` gate together with `update_line`, which the gate reads
    the marker for, logs, and either holds back or runs. `dependency` names the reference for a regexp that captures
    none. `reference_marker` is the marker a file names for a reference of its own, carrying where that reference
    sits. It limits the pass to that line and that column, so another match, on that line or elsewhere, is left as
    it is.
    """
    marker = None if reference_marker is None else reference_marker.marker
    # A start position is something only a compiled pattern takes, and `re.compile` hands back one it is given.
    pattern = re.compile(regexp)
    result = []
    for line in lines:
        match = _reference_match(pattern, line, reference_marker)
        if match is None:
            result.append(line.text)  # No reference to update here; a marker on this line applies to the one below.
            continue
        result.append(apply_marker(line, match, update_line, logger, dependency, marker=marker))
    return result


def _reference_match(
    pattern: re.Pattern[str], line: Line, reference_marker: ReferenceMarker | None
) -> re.Match[str] | None:
    """Return the reference the pattern matches on the line, or None where the line carries none to update.

    A file that names a reference of its own is read at that reference alone, so another line is passed over, and
    so is a match sitting before it on its own line.
    """
    if reference_marker is None:
        return pattern.search(line.text)
    if not reference_marker.reference_location.is_on_the_same_line_as(line.location):
        return None
    return pattern.search(line.text, reference_marker.reference_location.column)


def update_references_in_lines(
    lines: list[Line],
    *regexps: str | re.Pattern[str],
    get_new_version: NewVersionGetter,
    logger: Logger,
    dependency: str = "",
    reference_marker: ReferenceMarker | None = None,
) -> list[str]:
    """Return the lines with each unpinned reference updated to its new version (and digest).

    Several regexps are applied in turn, each to the result of the previous, so a file that pins more than one kind
    of reference (a devcontainer.json's base `image` and its `features`) is rewritten in one pass over its content.
    Each pass locates the lines afresh, so the text a marker is read from is the text the pass before it left; the
    file they belong to is the one they already record, so no path has to be passed alongside them.
    """
    if not lines:
        return []
    path = lines[0].location.path
    update_line = _Rewriter(get_new_version, logger, dependency).update_line
    for regexp in regexps:
        updated = updated_lines(lines, regexp, update_line, logger, dependency, reference_marker=reference_marker)
        lines = located_lines(path, updated)
    return [line.text for line in lines]
