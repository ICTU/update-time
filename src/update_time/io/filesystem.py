"""File manipulation methods."""

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from update_time.domain.version import DependencyVersion
    from update_time.io.log import Logger

YAML_GLOB_PATTERNS = ("*.yml", "*.yaml")


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


def _update_line(
    line: str, regexp: str, get_new_version: Callable[[str, str], DependencyVersion], logger: Logger, path: Path
) -> str:
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


def update_file(
    path: Path, regexp: str, get_new_version: Callable[[str, str], DependencyVersion], logger: Logger
) -> int:
    """Update the lines in the file and write back the file if the new lines are different from the old lines."""
    logger.path(path)
    old_lines = path.read_text().splitlines()
    new_lines = [_update_line(line, regexp, get_new_version, logger, path) for line in old_lines]
    if old_lines != new_lines:
        path.write_text("\n".join(new_lines) + "\n")
    return 0


def update_files(
    *glob_patterns: str,
    regexp: str,
    get_new_version: Callable[[str, str], DependencyVersion],
    logger: Logger,
    start: Path | None = None,
) -> int:
    """Update the files using the regexp to find the current version and get_new_version to find new versions."""
    results = {update_file(path, regexp, get_new_version, logger) for path in glob(*glob_patterns, start=start)}
    return max(results, default=0)
