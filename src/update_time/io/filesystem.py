"""Find and update files."""

from pathlib import Path
from typing import TYPE_CHECKING

from update_time.io.rewrite import update_references_in_lines

if TYPE_CHECKING:
    from collections.abc import Iterator

    from update_time.domain.version import NewVersionGetter
    from update_time.io.log import Logger

YAML_GLOB_PATTERNS = ("*.yml", "*.yaml")
# Dockerfiles are conventionally named `Dockerfile`, or `<purpose>.Dockerfile` / `Dockerfile.<purpose>` when a
# project has more than one (e.g. `python.Dockerfile`, `Dockerfile.dev`). The three patterns don't overlap for any
# realistic name, so a file is discovered once. Shared by the base-image and Node-engine updaters.
DOCKERFILE_GLOB_PATTERNS = ("Dockerfile", "*.Dockerfile", "Dockerfile.*")


def _named_hidden_parts(glob_pattern: str) -> set[str]:
    """Return the hidden (dot-prefixed) path segments a glob pattern names, e.g. `.devcontainer`.

    Hidden folders and files are skipped by default (see `glob`), but a pattern that names one (like
    `.devcontainer/devcontainer.json` or `.github/workflows/*.yml`) is asking for it, so it should be visited.
    A wildcard segment such as `.*` needs no special handling: these are compared to real path segments by exact
    equality, which a wildcard never satisfies, so including it grants no exception anyway.
    """
    return {part for part in Path(glob_pattern).parts if part.startswith(".")}


def glob(*glob_patterns: str, start: Path | None = None) -> Iterator[Path]:
    """Return an iterator over all paths matching any of the given glob patterns.

    Hidden folders and files (dot-prefixed, e.g. `.git`, `.venv`) and build-output folders are skipped, so a broad
    pattern like `*.yml` doesn't reach into them. A hidden folder or file named literally in the pattern itself is
    the exception: `glob(".devcontainer/devcontainer.json")` visits `.devcontainer`, so callers can target hidden
    locations directly instead of working around the default skip.
    """
    if start is None:
        start = Path.cwd()
    path_parts_to_ignore = {"build", "node_modules", "__pycache__"}
    for glob_pattern in glob_patterns:
        named_hidden = _named_hidden_parts(glob_pattern)
        for path in start.rglob(glob_pattern):
            relative_path = path.relative_to(start)
            if any(part.startswith(".") and part not in named_hidden for part in relative_path.parts):
                continue
            if any(part in path_parts_to_ignore for part in relative_path.parts):
                continue
            yield path


def update_file(path: Path, *regexps: str, get_new_version: NewVersionGetter, logger: Logger) -> int:
    """Update the references in the file and write it back if the new lines differ from the old lines.

    Multiple regexps are applied in turn to the same content, so a file that pins more than one kind of reference (a
    devcontainer.json's base `image` and its `features`) is read and written once, not once per regexp.
    """
    logger.path(path)
    old_lines = path.read_text().splitlines()
    new_lines = update_references_in_lines(
        old_lines, *regexps, get_new_version=get_new_version, logger=logger, path=path
    )
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
    results = {
        update_file(path, regexp, get_new_version=get_new_version, logger=logger)
        for path in glob(*glob_patterns, start=start)
    }
    return max(results, default=0)
