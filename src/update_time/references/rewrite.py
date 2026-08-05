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

from update_time.domain.drift import DriftedPin, hash_drifted, report_drift
from update_time.domain.line import located_lines
from update_time.domain.marker import parse_marker
from update_time.domain.version import Reference
from update_time.primitives.text import rewrite_string
from update_time.references.resolve import latest_version

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.line import Line
    from update_time.domain.marker import Marker
    from update_time.domain.version import DependencyVersion
    from update_time.io.log import Logger
    from update_time.primitives.location import Location


@dataclass(frozen=True)
class _Rewriter:
    """Everything needed to rewrite one kind of reference in a file.

    That is: how to resolve a new version, where to report, and — for a regexp that captures no `dependency` group —
    what to call the reference.
    """

    get_new_version: NewVersionGetter
    logger: Logger
    dependency: str = ""

    def update_line(self, match: re.Match[str], location: Location, marker: Marker) -> str:
        """Update the matched reference's line with the new version (and digest) if any, or return it unchanged.

        `updated_lines` has already matched the reference, so its line is `match.string` and its `location` points at
        that line. Which version to update to — honouring the reference's `# update-time:` marker — is
        `latest_version`'s decision, or None to leave the line unchanged. A reference that is already up to date is
        checked for digest drift, and one with an update, or without its available digest, is rewritten.
        """
        reference = matched_reference(match, self.dependency)
        latest = latest_version(reference, self.get_new_version, marker, location, self.logger)
        if latest is None:
            return match.string
        has_sha_group = "sha" in match.groupdict()
        pin_unpinned = has_sha_group and match.group("sha") is None and bool(latest.sha)
        if latest.version == reference.current_version and not pin_unpinned:
            return self._handle_drift(match, marker, latest, location)
        return self._apply_update(match, latest, location, pin_unpinned=pin_unpinned)

    def _handle_drift(self, match: re.Match[str], marker: Marker, latest: DependencyVersion, location: Location) -> str:
        """Return the line for an up-to-date reference, adopting or warning about a re-pushed digest.

        A pinned reference whose digest changed at the registry (a re-pushed tag) has drifted; whether that is
        adopted or only warned about is `report_drift`'s decision, and the line is rewritten only when it is adopted.
        """
        current_sha = match.groupdict().get("sha")
        if current_sha is None or not hash_drifted(latest.sha, current_sha):
            return match.string
        dependency, version = matched_dependency(match, self.dependency), match.group("version")
        drifted = DriftedPin(Reference(dependency, version, current_sha), latest.sha, location)
        adopted = report_drift(
            marker, partial(self.logger.digest_drift, drifted), partial(self.logger.adopted_drift, drifted)
        )
        return rewrite_string(match, {"sha": latest.sha}) if adopted else match.string

    def _apply_update(
        self, match: re.Match[str], latest: DependencyVersion, location: Location, *, pin_unpinned: bool
    ) -> str:
        """Return the line rewritten to the latest version and digest, logging the change.

        With `pin_unpinned` the regexp has an optional `sha` group that did not match, so the reference is unpinned
        and the available digest is appended to pin it, even when the version itself is already up to date.
        Otherwise the version is replaced in place, and so is the digest of an already-pinned reference.
        """
        dependency = matched_dependency(match, self.dependency)
        version_changed = latest.version != match.group("version")
        if pin_unpinned:
            # Append the digest to a previously unpinned reference, bumping the version too if one is available.
            if version_changed:
                self.logger.new_version(dependency, latest, location)
            else:
                self.logger.pinned(dependency, latest, location)
            replacements = {"version": f"{latest.version}@{latest.sha}"}
        else:
            self.logger.new_version(dependency, latest, location)
            replacements = {"version": latest.version}
            if match.groupdict().get("sha") is not None:
                replacements["sha"] = latest.sha
        return rewrite_string(match, replacements)

    def updated_lines(self, lines: list[Line], regexp: str | re.Pattern[str]) -> list[str]:
        """Return the lines with every reference the regexp matches updated, honouring each one's marker."""
        return updated_lines(lines, regexp, self.update_line, self.logger, self.dependency)


def matched_dependency(match: re.Match[str], dependency: str = "") -> str:
    """Return the dependency the match captured in its `dependency` group, or `dependency` when it captures none."""
    return dependency or match.group("dependency")


def matched_reference(match: re.Match[str], dependency: str = "") -> Reference:
    """Return the reference the match captured in its `dependency` and `version` named groups."""
    return Reference(matched_dependency(match, dependency), match.group("version"))


def apply_marker(
    line: Line,
    match: re.Match[str],
    update_line: Callable[[re.Match[str], Location, Marker], str],
    logger: Logger,
    dependency: str = "",
) -> str:
    """Read a matched reference's `# update-time:` marker and update it, or leave the line unchanged when held back.

    The marker gate shared by every line-based reference, and the one place that reads one, so no updater has to
    remember the placement rule (see `parse_marker`). An item that could not be parsed is reported (here, where
    the logger and location are available, unlike in the pure `parse_marker`) and leaves the reference unchanged.
    Otherwise the marker is reported as recognised at the debug level, as is the held-back update when the marker
    holds the update back, so users can tell a marker that was understood from one that suppressed something. A
    marker holding back every scope — the update, the staleness warning, and the yank warning — returns the line
    without calling `update_line`, so the source is never even queried. Any other marker is handed to `update_line`
    along with the match and the line's location, so the checks it doesn't hold back still run, with its bound and
    its `allow` directives. The dependency comes from the regexp's `dependency` group; a regexp that captures none —
    a pre-commit `rev:` takes it from the `repo:` above, a `.python-version` entry is a bare version — names it in
    `dependency` instead.
    """
    marker = parse_marker(line)
    location = line.location
    dependency = matched_dependency(match, dependency)
    if marker.invalid_specifier is not None:
        logger.invalid_specifier(dependency, marker.invalid_specifier, location)
        return line.text
    logger.recognised_marker(dependency, marker, location)
    if marker.ignore_update:
        logger.ignored(dependency, marker, location)
    if marker.ignore_update and marker.ignore_stale and marker.ignore_yanked:
        return line.text
    return update_line(match, location, marker)


def updated_lines(
    lines: list[Line],
    regexp: str | re.Pattern[str],
    update_line: Callable[[re.Match[str], Location, Marker], str],
    logger: Logger,
    dependency: str = "",
) -> list[str]:
    """Return the lines with `update_line` applied to each line carrying a reference, honouring any marker.

    A line that carries no reference (no `regexp` match) is left untouched — a marker on it is for the line below. A
    line that does carry one goes to the shared `apply_marker` gate together with `update_line`, which the gate reads
    the marker for, logs, and either holds back or runs. `dependency` names the reference for a regexp that captures
    none.
    """
    result = []
    for line in lines:
        match = re.search(regexp, line.text)
        if match is None:
            result.append(line.text)  # No reference on this line; a marker here applies to the line below it.
            continue
        result.append(apply_marker(line, match, update_line, logger, dependency))
    return result


def update_references_in_lines(
    lines: list[Line],
    *regexps: str | re.Pattern[str],
    get_new_version: NewVersionGetter,
    logger: Logger,
    dependency: str = "",
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
    rewriter = _Rewriter(get_new_version, logger, dependency)
    for regexp in regexps:
        lines = located_lines(path, rewriter.updated_lines(lines, regexp))
    return [line.text for line in lines]
