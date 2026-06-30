"""File manipulation methods."""

import re
from functools import partial
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from update_time.domain.version import NewVersionGetter
    from update_time.io.log import Logger

YAML_GLOB_PATTERNS = ("*.yml", "*.yaml")

# An `# update-time: ignore` comment pins a reference so it is left unchanged. It works inline on the reference's
# own line (valid in YAML and requirements) or as a standalone comment on the line directly above it (the form
# Dockerfiles need, as they reject inline comments). `#` is the comment character in every format we update;
# trailing text after `ignore` (a reason) is allowed.
_IGNORE_MARKER = re.compile(r"#\s*update-time:\s*ignore\b")


def glob(*glob_patterns: str, start: Path | None = None) -> Iterator[Path]:
    """Return an iterator over all paths matching any of the given glob patterns."""
    if start is None:
        start = Path.cwd()
    path_parts_to_ignore = {"build", "node_modules", "__pycache__"}
    for glob_pattern in glob_patterns:
        for path in start.rglob(glob_pattern):
            relative_path = path.relative_to(start)
            if any(part.startswith(".") for part in relative_path.parts):
                continue
            if any(part in path_parts_to_ignore for part in relative_path.parts):
                continue
            yield path


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
        return line.replace(f"{dependency}:{version}", f"{dependency}:{latest_version.version}@{latest_version.sha}")
    logger.new_version(dependency, latest_version, path)
    if current_sha is not None:
        line = line.replace(current_sha, latest_version.sha)
    return line.replace(version, latest_version.version)


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


def update_file(path: Path, regexp: str, get_new_version: NewVersionGetter, logger: Logger) -> int:
    """Update the lines in the file and write back the file if the new lines are different from the old lines."""
    logger.path(path)
    old_lines = path.read_text().splitlines()
    update_line = partial(_update_line, regexp=regexp, get_new_version=get_new_version, logger=logger, path=path)
    new_lines = updated_lines(old_lines, regexp, update_line, logger, path)
    if old_lines != new_lines:
        path.write_text("\n".join(new_lines) + "\n")
    return 0


def update_files(
    *glob_patterns: str,
    regexp: str,
    get_new_version: NewVersionGetter,
    logger: Logger,
    start: Path | None = None,
) -> int:
    """Update the files using the regexp to find the current version and get_new_version to find new versions."""
    results = {update_file(path, regexp, get_new_version, logger) for path in glob(*glob_patterns, start=start)}
    return max(results, default=0)
