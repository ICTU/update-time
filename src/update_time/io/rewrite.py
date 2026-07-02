"""Rewrite pinned version references in text, line by line.

The updaters that edit files line by line (Dockerfiles, CI configs, manifests, requirements, workflows, ...) share
this engine: given a regexp that captures a reference and a function that returns its new version, it rewrites each
matched reference in place — touching only the captured spans and skipping any line pinned by an `# update-time:
ignore` marker. It's pure text processing, but it reports what it changed through a `Logger`, so it lives in `io`
rather than `domain`.
"""

import re
from functools import partial
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from update_time.domain.version import NewVersionGetter
    from update_time.io.log import Logger

# An `# update-time: ignore` comment pins a reference so it is left unchanged. It works inline on the reference's
# own line (valid in YAML and requirements) or as a standalone comment on the line directly above it (the form
# Dockerfiles need, as they reject inline comments). `#` is the comment character in every format we update;
# trailing text after `ignore` (a reason) is allowed.
_IGNORE_MARKER = re.compile(r"#\s*update-time:\s*ignore\b")


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


def _replace_groups(line: str, match: re.Match[str], replacements: dict[str, str]) -> str:
    """Replace the named groups within the matched region of the line, leaving the rest of the line untouched."""
    return line[: match.start()] + rewrite_match(match, replacements) + line[match.end() :]


def _update_line(line: str, regexp: str, get_new_version: NewVersionGetter, logger: Logger, path: Path) -> str:
    """Update the line with the new version (and digest) if any, or return the line unchanged.

    When the regexp has an optional `sha` group that did not match, the reference is unpinned. If a digest is
    available it is appended to pin the reference, even when the version itself is already up to date.
    """
    if not (match := re.search(regexp, line)):
        return line
    dependency = match.group("dependency")
    version = match.group("version")
    latest_version = get_new_version(dependency, version)
    has_sha_group = "sha" in match.groupdict()
    current_sha = match.group("sha") if has_sha_group else None
    pin_unpinned = has_sha_group and current_sha is None and bool(latest_version.sha)
    version_changed = latest_version.version != version
    if not version_changed and not pin_unpinned:
        return line
    if pin_unpinned:
        # Append the digest to a previously unpinned reference, bumping the version too if a newer one is available.
        if version_changed:
            logger.new_version(dependency, latest_version, path)
        else:
            logger.pinned(dependency, latest_version, path)
        return _replace_groups(line, match, {"version": f"{latest_version.version}@{latest_version.sha}"})
    logger.new_version(dependency, latest_version, path)
    replacements = {"version": latest_version.version}
    if current_sha is not None:
        replacements["sha"] = latest_version.sha
    return _replace_groups(line, match, replacements)


def _is_pinned(line: str, previous_line: str) -> bool:
    """Return whether the reference on the line is pinned by an `# update-time: ignore` marker.

    The marker pins a line either inline (`image: …  # update-time: ignore`) or as a standalone comment on the line
    directly above it (the form Dockerfiles need, since they reject inline comments). Requiring the preceding line
    to start with `#` keeps an inline marker from also pinning the line below it.
    """
    if _IGNORE_MARKER.search(line):
        return True
    return previous_line.lstrip().startswith("#") and bool(_IGNORE_MARKER.search(previous_line))


def updated_lines(
    lines: list[str],
    regexp: str | re.Pattern[str],
    update_line: Callable[[str], str],
    logger: Logger,
    path: Path,
) -> list[str]:
    """Return the lines with `update_line` applied to each line not pinned by an `# update-time: ignore` marker.

    A pinned line is kept as-is; if it carries a reference (matched by `regexp`), ignoring it is logged at the debug
    level with the dependency name. Each line is paired with the line before it, so the marker can pin its own line
    (inline) or the line below a standalone marker comment.
    """
    result = []
    for previous_line, line in pairwise(["", *lines]):
        if _is_pinned(line, previous_line):
            if match := re.search(regexp, line):
                logger.ignored(match.group("dependency"), path)
            result.append(line)
        else:
            result.append(update_line(line))
    return result


def update_references_in_lines(
    lines: list[str], regexp: str, get_new_version: NewVersionGetter, logger: Logger, path: Path
) -> list[str]:
    """Return the lines with each unpinned reference updated to its new version (and digest)."""
    update_line = partial(_update_line, regexp=regexp, get_new_version=get_new_version, logger=logger, path=path)
    return updated_lines(lines, regexp, update_line, logger, path)
