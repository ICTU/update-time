"""Rewrite pinned version references in text, line by line.

The updaters that edit files line by line (Dockerfiles, CI configs, manifests, requirements, workflows, ...) share
this engine: given a regexp that captures a reference and a function that returns its new version, it rewrites each
matched reference in place — touching only the captured spans and skipping any line pinned by an `# update-time:
ignore` marker. It's pure text processing, but it reports what it changed through a `Logger`, so it lives in `io`
rather than `domain`.
"""

import re
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from update_time.domain.version import NewVersionGetter
    from update_time.io.log import Logger


@dataclass(frozen=True)
class _Rewriter:
    """Everything needed to rewrite one kind of reference in a file.

    That is: which references to match (`regexp`), how to resolve a new version, and where to report.
    """

    regexp: str
    get_new_version: NewVersionGetter
    logger: Logger
    path: Path

    def update_line(self, line: str, scope: str | None) -> str:
        """Update the line with the new version (and digest) if any, or return the line unchanged.

        When the regexp has an optional `sha` group that did not match, the reference is unpinned. If a digest is
        available it is appended to pin the reference, even when the version itself is already up to date. When the
        reference is already pinned and only its digest changed at the registry (a re-pushed tag), the drift is
        warned about but the pin is left unchanged, so a re-pushed digest is never silently adopted. `scope` is the
        reference's `# update-time: ignore[...]` scope (see `_ignore_marker`): `"update"` holds back the update,
        `"stale"` the staleness warning.
        """
        if not (match := re.search(self.regexp, line)):
            return line
        logger, path = self.logger, self.path
        dependency = match.group("dependency")
        version = match.group("version")
        latest_version = self.get_new_version(dependency, version)
        if scope != "stale":
            logger.warn_if_stale(dependency, latest_version, path)
        if scope == "update":
            return line
        has_sha_group = "sha" in match.groupdict()
        current_sha = match.group("sha") if has_sha_group else None
        pin_unpinned = has_sha_group and current_sha is None and bool(latest_version.sha)
        version_changed = latest_version.version != version
        if not version_changed and not pin_unpinned:
            if current_sha is not None and latest_version.digest_differs_from(current_sha):
                # The tag was re-pushed with a different digest; warn but leave the immutable pin unchanged.
                logger.digest_drift(dependency, version, current_sha, latest_version.sha, path)
            return line
        if pin_unpinned:
            # Append the digest to a previously unpinned reference, bumping the version too if one is available.
            if version_changed:
                logger.new_version(dependency, latest_version, path)
            else:
                logger.pinned(dependency, latest_version, path)
            replacements = {"version": f"{latest_version.version}@{latest_version.sha}"}
        else:
            logger.new_version(dependency, latest_version, path)
            replacements = {"version": latest_version.version}
            if current_sha is not None:
                replacements["sha"] = latest_version.sha
        return self._replace_groups(line, match, replacements)

    @staticmethod
    def _replace_groups(line: str, match: re.Match[str], replacements: dict[str, str]) -> str:
        """Replace the named groups within the matched region of the line, leaving the rest of the line untouched."""
        return line[: match.start()] + rewrite_match(match, replacements) + line[match.end() :]


# An `# update-time: ignore` comment holds a reference back. It works inline on the reference's own line (valid in
# YAML and requirements) or as a standalone comment on the line directly above it (the form Dockerfiles need, as
# they reject inline comments). The comment lead is `#` in most formats we update, or `//` in devcontainer.json
# (which is JSONC); trailing text after the marker (a reason) is allowed. An optional `[stale]` or `[update]` scope
# narrows what is held back: bare `ignore` skips both the update and the staleness warning, `ignore[update]` skips
# only the update (still warning when the dependency is stale), and `ignore[stale]` skips only the staleness warning.
_IGNORE_MARKER = re.compile(r"(?:#|//)\s*update-time:\s*ignore\b(?:\[(?P<scope>stale|update)\])?")


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


def _ignore_marker(line: str, previous_line: str) -> str | None:
    """Return the `# update-time: ignore[...]` scope affecting the line: None, `""`, `"update"`, or `"stale"`.

    None when no marker applies; `""` for a bare `ignore` (holds back both the update and the staleness warning);
    `"update"` or `"stale"` for the scoped forms (hold back only that one). The marker is read inline on the line,
    or from the line directly above it when that is a standalone comment (the form Dockerfiles need, since they
    reject inline comments); requiring the preceding line to start with a comment lead (`#`, or `//`) keeps an
    inline marker from also affecting the line below it.
    """
    marker = _IGNORE_MARKER.search(line)
    if marker is None and previous_line.lstrip().startswith(("#", "//")):
        marker = _IGNORE_MARKER.search(previous_line)
    if marker is None:
        return None
    return marker.group("scope") or ""  # "" for a bare `ignore`, else "update" or "stale"


def updated_lines(
    lines: list[str],
    regexp: str | re.Pattern[str],
    update_line: Callable[[str, str | None], str],
    logger: Logger,
    path: Path,
) -> list[str]:
    """Return the lines with `update_line` applied to each line, honouring any `# update-time: ignore[...]` marker.

    A bare `ignore` (which holds back both the update and the staleness warning) leaves the line untouched without
    even querying the source; a scoped marker still calls `update_line`, passing its scope so the update or the
    staleness warning is held back. When the update is held back and the line carries a reference (matched by
    `regexp`), that is logged at the debug level. Each line is paired with the line before it, so a standalone
    marker comment can apply to the line below it.
    """
    result = []
    for previous_line, line in pairwise(["", *lines]):
        scope = _ignore_marker(line, previous_line)
        if scope in ("", "update") and (match := re.search(regexp, line)):
            logger.ignored(match.group("dependency"), path)
        result.append(line if scope == "" else update_line(line, scope))
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
