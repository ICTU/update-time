"""Rewrite pinned version references in text, line by line.

The updaters that edit files line by line (Dockerfiles, CI configs, manifests, requirements, workflows, ...) share
this engine: given a regexp that captures a reference and a function that returns its new version, it rewrites each
matched reference in place — touching only the captured spans and skipping any line pinned by an `# update-time:
ignore` marker. Which version a reference should update to is `resolve.latest_version`'s decision; this module owns
the text surgery around it, reporting what it changed through a `Logger`.
"""

import re
from dataclasses import dataclass
from functools import partial
from itertools import pairwise
from typing import TYPE_CHECKING

from update_time.domain.bound import Verb
from update_time.domain.location import Location
from update_time.domain.marker import parse_marker
from update_time.domain.version import Reference
from update_time.io.log import attribute_logs_to_caller
from update_time.primitives.environment import EnvVar
from update_time.references.resolve import latest_version

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.marker import Marker
    from update_time.domain.version import DependencyVersion
    from update_time.io.log import Logger

attribute_logs_to_caller(__file__)  # This engine logs on behalf of the updaters, so records point at the updater.

# Private channel that passes --allow-image-digest-drift from the CLI to the updater subprocesses: whether a
# re-pushed image digest should be adopted repo-wide.
ALLOW_IMAGE_DIGEST_DRIFT = EnvVar(
    "_UPDATE_TIME_ALLOW_IMAGE_DIGEST_DRIFT",
    default=False,
    parse=lambda value: value == "1",
    serialize=lambda allow: "1" if allow else "0",
)


@dataclass(frozen=True)
class _Rewriter:
    """Everything needed to rewrite one kind of reference in a file.

    That is: which references to match (`regexp`), how to resolve a new version, and where to report.
    """

    regexp: str
    get_new_version: NewVersionGetter
    logger: Logger
    path: Path

    def update_line(self, match: re.Match[str], marker: Marker, line_number: int) -> str:
        """Update the matched reference's line with the new version (and digest) if any, or return it unchanged.

        `updated_lines` has already matched the reference, so its line is `match.string` and its 1-based position is
        `line_number` (reported so a logged reference points at its own line). Which version to update to — honouring
        the reference's `# update-time:` marker — is `latest_version`'s decision, or None to leave the line unchanged.
        A reference that is already up to date is checked for digest drift (see `_handle_drift`); one with an update,
        or without its available digest, is rewritten (see `_apply_update`).
        """
        dependency = match.group("dependency")
        version = match.group("version")
        reference = Reference(dependency, version)
        location = Location(self.path, line_number)
        latest = latest_version(reference, self.get_new_version, marker, location, self.logger)
        if latest is None:
            return match.string
        has_sha_group = "sha" in match.groupdict()
        pin_unpinned = has_sha_group and match.group("sha") is None and bool(latest.sha)
        if latest.version == version and not pin_unpinned:
            return self._handle_drift(match, marker, latest, location)
        return self._apply_update(match, latest, location, pin_unpinned=pin_unpinned)

    def _handle_drift(self, match: re.Match[str], marker: Marker, latest: DependencyVersion, location: Location) -> str:
        """Return the line for an up-to-date reference, adopting or warning about a re-pushed digest.

        When the reference is pinned and only its digest changed at the registry (a re-pushed tag), the drift is
        warned about but the pin is left unchanged — unless the reference opted in (`marker.allow_drift` or the
        global flag), in which case the new digest is adopted.
        """
        current_sha = match.groupdict().get("sha")
        if current_sha is None or not latest.digest_differs_from(current_sha):
            return match.string
        dependency, version = match.group("dependency"), match.group("version")
        if marker.allow_drift or ALLOW_IMAGE_DIGEST_DRIFT.get():
            # The reference opted in, so adopt the re-pushed digest instead of only warning about it. The
            # per-reference marker is the more specific opt-in, so its `allow` directives are named verbatim as the
            # cause when both apply; the digest-drift opt-in is among them.
            cause = (
                f"update-time: {marker.raw_marker(Verb.ALLOW)}" if marker.allow_drift else "--allow-image-digest-drift"
            )
            self.logger.adopted_drift(dependency, version, current_sha, latest.sha, location, cause)
            return self._replace_groups(match, {"sha": latest.sha})
        # The tag was re-pushed with a different digest; warn but leave the immutable pin unchanged.
        self.logger.digest_drift(dependency, version, current_sha, latest.sha, location)
        return match.string

    def _apply_update(
        self, match: re.Match[str], latest: DependencyVersion, location: Location, *, pin_unpinned: bool
    ) -> str:
        """Return the line rewritten to the latest version and digest, logging the change.

        With `pin_unpinned` the regexp has an optional `sha` group that did not match, so the reference is unpinned
        and the available digest is appended to pin it, even when the version itself is already up to date;
        otherwise the version (and the digest of an already-pinned reference) is replaced in place.
        """
        dependency = match.group("dependency")
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
        return self._replace_groups(match, replacements)

    @staticmethod
    def _replace_groups(match: re.Match[str], replacements: dict[str, str]) -> str:
        """Replace the named groups within the matched region of the line, leaving the rest of the line untouched."""
        line = match.string
        return line[: match.start()] + rewrite_match(match, replacements) + line[match.end() :]


def rewrite_match(match: re.Match[str], replacements: dict[str, str]) -> str:
    """Return the matched text with the named groups replaced, leaving the rest of the match untouched.

    Only the spans the regex captured are rewritten, so a value that also occurs elsewhere within the match — the
    `18` in `FROM node:18 AS build-18`, or a version that reappears in the span a multi-line jsDelivr match covers —
    is left alone. Groups are replaced right-to-left so an earlier replacement doesn't shift the spans still to come.
    """
    text = match.group(0)
    offset = match.start()
    for group in sorted(replacements, key=match.start, reverse=True):
        start, end = match.span(group)
        text = text[: start - offset] + replacements[group] + text[end - offset :]
    return text


def apply_marker(  # noqa: PLR0913
    line: str, dependency: str, marker: Marker, location: Location, logger: Logger, update: Callable[[], str]
) -> str:
    """Report a matched reference's `# update-time:` marker and update it, or leave the line unchanged when held back.

    The marker gate shared by every line-based reference. An item that could not be parsed is reported (here, where
    the logger and location are available, unlike in the pure `parse_marker`) and leaves the reference unchanged.
    Otherwise the marker is reported as recognised at the debug level, as is the held-back update when the marker
    holds the update back, so users can tell a marker that was understood from one that suppressed something. A
    marker holding back every scope — the update, the staleness warning, and the yank warning — returns the line
    without calling `update`, so the source is never even queried;
    any other marker hands off to `update` — the caller's thunk that performs the rewrite — so the checks the marker
    doesn't hold back still run. Callers that discover the reference themselves (a pre-commit `rev:`, a
    `.python-version` entry) pass the `dependency` name; `updated_lines` reads it from the regexp's match group.
    """
    if marker.invalid_specifier is not None:
        logger.invalid_specifier(dependency, marker.invalid_specifier, location)
        return line
    logger.recognised_marker(dependency, marker, location)
    if marker.ignore_update:
        logger.ignored(dependency, marker, location)
    if marker.ignore_update and marker.ignore_stale and marker.ignore_yanked:
        return line
    return update()


def updated_lines(
    lines: list[str],
    regexp: str | re.Pattern[str],
    update_line: Callable[[re.Match[str], Marker, int], str],
    logger: Logger,
    path: Path,
) -> list[str]:
    """Return the lines with `update_line` applied to each line carrying a reference, honouring any marker.

    Each line is paired with the line before it, so a standalone marker comment can apply to the line below it, and
    numbered (1-based) so a logged reference points at its own line. A line that carries no reference (no `regexp`
    match) is left untouched — a marker on it is for the line below. A line that does carry one is run through the
    shared `apply_marker` gate, which logs its marker, holds it back, or hands off to `update_line` (bound here to
    the line, its `Marker`, and its number, so an `allow[digest-drift]` opt-in or a `version_bound` still reaches it).
    """
    result = []
    for line_number, (previous_line, line) in enumerate(pairwise(["", *lines]), start=1):
        match = re.search(regexp, line)
        if match is None:
            result.append(line)  # No reference on this line; a marker here applies to the line below it.
            continue
        marker = parse_marker(line, previous_line)
        update = partial(update_line, match, marker, line_number)
        location = Location(path, line_number)
        result.append(apply_marker(line, match.group("dependency"), marker, location, logger, update))
    return result


def update_references_in_lines(
    lines: list[str], *regexps: str, get_new_version: NewVersionGetter, logger: Logger, path: Path
) -> list[str]:
    """Return the lines with each unpinned reference updated to its new version (and digest).

    Several regexps are applied in turn, each to the result of the previous, so a file that pins more than one kind
    of reference (a devcontainer.json's base `image` and its `features`) is rewritten in one pass over its content.
    """
    for regexp in regexps:
        rewriter = _Rewriter(regexp, get_new_version, logger, path)
        lines = updated_lines(lines, regexp, rewriter.update_line, logger, path)
    return lines
