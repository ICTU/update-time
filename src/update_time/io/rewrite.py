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

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from update_time.domain.version import NewVersionGetter
    from update_time.io.log import Logger

# Private channel that passes --allow-image-digest-drift from the CLI to the updater subprocesses; not a user-facing
# setting (use --allow-image-digest-drift instead). The leading underscore marks it internal.
ALLOW_IMAGE_DIGEST_DRIFT_ENV_VAR = "_UPDATE_TIME_ALLOW_IMAGE_DIGEST_DRIFT"


def allow_image_digest_drift() -> bool:
    """Return whether a re-pushed image digest should be adopted repo-wide (the --allow-image-digest-drift flag)."""
    return os.environ.get(ALLOW_IMAGE_DIGEST_DRIFT_ENV_VAR) == "1"


@dataclass(frozen=True)
class Marker:
    """The `# update-time:` directives affecting a line (see `_marker`).

    `ignore_scope` is the `ignore[...]` scope: None (no marker), `""` (a bare `ignore`), `"update"`, or `"stale"`.
    `allow_drift` is whether an `allow[digest-drift]` marker opts the reference into adopting a re-pushed digest.
    """

    ignore_scope: str | None = None
    allow_drift: bool = False


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

        When the regexp has an optional `sha` group that did not match, the reference is unpinned. If a digest is
        available it is appended to pin the reference, even when the version itself is already up to date. When the
        reference is already pinned and only its digest changed at the registry (a re-pushed tag), the drift is
        warned about but the pin is left unchanged — unless the reference opted in (`marker.allow_drift` or the
        global flag), in which case the new digest is adopted. `marker` carries the reference's `# update-time:`
        directives (see `_marker`): an `ignore` scope of `"update"` holds back the update, `"stale"` the staleness
        warning.
        """
        if not (match := re.search(self.regexp, line)):
            return line
        logger, path = self.logger, self.path
        dependency = match.group("dependency")
        version = match.group("version")
        latest_version = self.get_new_version(dependency, version)
        if marker.ignore_scope != "stale":
            logger.warn_if_stale(dependency, latest_version, path)
        if marker.ignore_scope == "update":
            return line
        has_sha_group = "sha" in match.groupdict()
        current_sha = match.group("sha") if has_sha_group else None
        pin_unpinned = has_sha_group and current_sha is None and bool(latest_version.sha)
        version_changed = latest_version.version != version
        if not version_changed and not pin_unpinned:
            if current_sha is not None and latest_version.digest_differs_from(current_sha):
                if marker.allow_drift or allow_image_digest_drift():
                    # The reference opted in, so adopt the re-pushed digest instead of only warning about it.
                    logger.adopted_drift(dependency, version, current_sha, latest_version.sha, path)
                    return self._replace_groups(line, match, {"sha": latest_version.sha})
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

# An `# update-time: allow[digest-drift]` comment opts a reference into adopting a re-pushed image digest: when only
# the digest has drifted (same tag, same version, different registry digest), the new digest is pinned instead of
# only warned about (see `_Rewriter.update_line`). It follows the same placement and comment-lead rules as `ignore`.
_ALLOW_DRIFT_MARKER = re.compile(r"(?:#|//)\s*update-time:\s*allow\[digest-drift\]")


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


def _marker(line: str, previous_line: str) -> Marker:
    """Return the `# update-time:` directives affecting the line as a `Marker`.

    A directive is read inline on the line, or from the line directly above it when that is a standalone comment
    (the form Dockerfiles need, since they reject inline comments); requiring the preceding line to start with a
    comment lead (`#`, or `//`) keeps an inline marker from also affecting the line below it. Both an `ignore[...]`
    and an `allow[digest-drift]` directive are recognised independently, each wherever it appears (inline or above).
    """
    texts = [line]
    if previous_line.lstrip().startswith(("#", "//")):
        texts.append(previous_line)
    ignore_scope: str | None = None
    allow_drift = False
    for text in texts:
        if ignore_scope is None and (ignore := _IGNORE_MARKER.search(text)) is not None:
            ignore_scope = ignore.group("scope") or ""  # "" for a bare `ignore`, else "update" or "stale"
        allow_drift = allow_drift or _ALLOW_DRIFT_MARKER.search(text) is not None
    return Marker(ignore_scope, allow_drift)


def updated_lines(
    lines: list[str],
    regexp: str | re.Pattern[str],
    update_line: Callable[[str, Marker], str],
    logger: Logger,
    path: Path,
) -> list[str]:
    """Return the lines with `update_line` applied to each line, honouring any `# update-time:` marker.

    A bare `ignore` (which holds back both the update and the staleness warning) leaves the line untouched without
    even querying the source; a scoped marker still calls `update_line`, passing the `Marker` so the update or the
    staleness warning is held back (and so an `allow[digest-drift]` opt-in reaches the drift branch). When the update
    is held back and the line carries a reference (matched by `regexp`), that is logged at the debug level. Each line
    is paired with the line before it, so a standalone marker comment can apply to the line below it.
    """
    result = []
    for previous_line, line in pairwise(["", *lines]):
        marker = _marker(line, previous_line)
        if marker.ignore_scope in ("", "update") and (match := re.search(regexp, line)):
            logger.ignored(match.group("dependency"), path)
        result.append(line if marker.ignore_scope == "" else update_line(line, marker))
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
