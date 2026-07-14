"""Rewrite pinned version references in text, line by line.

The updaters that edit files line by line (Dockerfiles, CI configs, manifests, requirements, workflows, ...) share
this engine: given a regexp that captures a reference and a function that returns its new version, it rewrites each
matched reference in place — touching only the captured spans and skipping any line pinned by an `# update-time:
ignore` marker. It's pure text processing, but it reports what it changed through a `Logger`, so it lives in `io`
rather than `domain`.
"""

import os
import re
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from update_time.domain.marker import parse_marker

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from update_time.domain.marker import Marker
    from update_time.domain.version import DependencyVersion, NewVersionGetter
    from update_time.io.log import Logger

# Private channel that passes --allow-image-digest-drift from the CLI to the updater subprocesses; not a user-facing
# setting (use --allow-image-digest-drift instead). The leading underscore marks it internal.
ALLOW_IMAGE_DIGEST_DRIFT_ENV_VAR = "_UPDATE_TIME_ALLOW_IMAGE_DIGEST_DRIFT"


def allow_image_digest_drift() -> bool:
    """Return whether a re-pushed image digest should be adopted repo-wide (the --allow-image-digest-drift flag)."""
    return os.environ.get(ALLOW_IMAGE_DIGEST_DRIFT_ENV_VAR) == "1"


@dataclass(frozen=True)
class _Rewriter:
    """Everything needed to rewrite one kind of reference in a file.

    That is: which references to match (`regexp`), how to resolve a new version, and where to report.
    """

    regexp: str
    get_new_version: NewVersionGetter
    logger: Logger
    path: Path

    def update_line(self, line: str, marker: Marker) -> str:
        """Update the line with the new version (and digest) if any, or return the line unchanged.

        `marker` carries the reference's `# update-time:` directives (see `parse_marker`): `ignore_update` holds
        back the update, `ignore_stale` the staleness warning, and a `version_filter` bounds the source's version
        selection. A reference that is already up to date is checked for digest drift (see `_handle_drift`); one
        with an update, or without its available digest, is rewritten (see `_apply_update`).
        """
        if not (match := re.search(self.regexp, line)):
            return line
        dependency = match.group("dependency")
        version = match.group("version")
        self.logger.warn_if_redundant_bound(dependency, marker.version_filter, version, self.path)
        latest_version = self.get_new_version(dependency, version, marker.version_filter)
        if not marker.ignore_stale:
            self.logger.warn_if_stale(dependency, latest_version, self.path)
        if marker.ignore_update:
            return line
        has_sha_group = "sha" in match.groupdict()
        pin_unpinned = has_sha_group and match.group("sha") is None and bool(latest_version.sha)
        if latest_version.version == version and not pin_unpinned:
            return self._handle_drift(line, match, marker, latest_version)
        return self._apply_update(line, match, latest_version, pin_unpinned=pin_unpinned)

    def _handle_drift(self, line: str, match: re.Match[str], marker: Marker, latest: DependencyVersion) -> str:
        """Return the line for an up-to-date reference, adopting or warning about a re-pushed digest.

        When the reference is pinned and only its digest changed at the registry (a re-pushed tag), the drift is
        warned about but the pin is left unchanged — unless the reference opted in (`marker.allow_drift` or the
        global flag), in which case the new digest is adopted.
        """
        current_sha = match.groupdict().get("sha")
        if current_sha is None or not latest.digest_differs_from(current_sha):
            return line
        dependency, version = match.group("dependency"), match.group("version")
        if marker.allow_drift or allow_image_digest_drift():
            # The reference opted in, so adopt the re-pushed digest instead of only warning about it. The
            # per-reference marker is the more specific opt-in, so it is named as the cause when both apply.
            cause = "update-time: allow[digest-drift]" if marker.allow_drift else "--allow-image-digest-drift"
            self.logger.adopted_drift(dependency, version, current_sha, latest.sha, self.path, cause)
            return self._replace_groups(line, match, {"sha": latest.sha})
        # The tag was re-pushed with a different digest; warn but leave the immutable pin unchanged.
        self.logger.digest_drift(dependency, version, current_sha, latest.sha, self.path)
        return line

    def _apply_update(self, line: str, match: re.Match[str], latest: DependencyVersion, *, pin_unpinned: bool) -> str:
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
                self.logger.new_version(dependency, latest, self.path)
            else:
                self.logger.pinned(dependency, latest, self.path)
            replacements = {"version": f"{latest.version}@{latest.sha}"}
        else:
            self.logger.new_version(dependency, latest, self.path)
            replacements = {"version": latest.version}
            if match.groupdict().get("sha") is not None:
                replacements["sha"] = latest.sha
        return self._replace_groups(line, match, replacements)

    @staticmethod
    def _replace_groups(line: str, match: re.Match[str], replacements: dict[str, str]) -> str:
        """Replace the named groups within the matched region of the line, leaving the rest of the line untouched."""
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


def updated_lines(
    lines: list[str],
    regexp: str | re.Pattern[str],
    update_line: Callable[[str, Marker], str],
    logger: Logger,
    path: Path,
) -> list[str]:
    """Return the lines with `update_line` applied to each line, honouring any `# update-time:` marker.

    A marker that holds back both the update and the staleness warning (a bare `ignore`, or the two scopes
    combined) leaves the line untouched without even querying the source; a marker holding back just one still
    calls `update_line`, passing the `Marker` so the other check runs (and so an `allow[digest-drift]` opt-in or a
    `version_filter` reaches `update_line`). When the line carries a reference (matched by `regexp`), its marker is
    logged at the debug level, as is the held-back update when the marker holds the update back, so users can
    confirm a marker is recognised. A marker with an item that could not be parsed is reported
    here (where the logger and path are available, unlike in the pure `parse_marker`) and leaves the reference
    unchanged. Each line is paired with the line before it, so a standalone marker comment can apply to the line
    below it.
    """
    result = []
    for previous_line, line in pairwise(["", *lines]):
        marker = parse_marker(line, previous_line)
        match = re.search(regexp, line)
        if marker.invalid_specifier is not None:
            if match:
                logger.invalid_specifier(match.group("dependency"), marker.invalid_specifier, path)
            result.append(line)  # An unparsable marker item leaves the reference unchanged.
            continue
        if match:
            logger.applying_marker(match.group("dependency"), marker, path)
            if marker.ignore_update:
                logger.ignored(match.group("dependency"), marker, path)
        result.append(line if marker.ignore_update and marker.ignore_stale else update_line(line, marker))
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
