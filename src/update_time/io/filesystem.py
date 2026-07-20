"""Find the files to scan for references."""

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# Private channel that passes --exclude-path from the CLI to the updater subprocesses; not a user-facing setting
# (use --exclude-path instead). The leading underscore marks it internal. Entries are the comma-joined, scan-root
# relative directories to skip.
EXCLUDE_PATHS_ENV_VAR = "_UPDATE_TIME_EXCLUDE_PATHS"

YAML_GLOB_PATTERNS = ("*.yml", "*.yaml")
# Dockerfiles are conventionally named `Dockerfile`, or `<purpose>.Dockerfile` / `Dockerfile.<purpose>` when a
# project has more than one (e.g. `python.Dockerfile`, `Dockerfile.dev`). The three patterns don't overlap for any
# realistic name, so a file is discovered once. Shared by the base-image and Node-engine updaters.
DOCKERFILE_NAME = "Dockerfile"
DOCKERFILE_GLOB_PATTERNS = (DOCKERFILE_NAME, f"*.{DOCKERFILE_NAME}", f"{DOCKERFILE_NAME}.*")

# Directories whose contents `glob` always skips, on top of hidden (dot-prefixed) folders and the directories passed
# to --exclude-path. The --exclude-path help in `io.cli` lists them, so it stays in step with this tuple.
ALWAYS_IGNORED_DIRECTORIES = ("build", "node_modules", "__pycache__")


def _named_hidden_parts(glob_pattern: str) -> set[str]:
    """Return the hidden (dot-prefixed) path segments a glob pattern names, e.g. `.devcontainer`.

    Hidden folders and files are skipped by default (see `glob`), but a pattern that names one (like
    `.devcontainer/devcontainer.json` or `.github/workflows/*.yml`) is asking for it, so it should be visited.
    A wildcard segment such as `.*` needs no special handling: these are compared to real path segments by exact
    equality, which a wildcard never satisfies, so including it grants no exception anyway.
    """
    return {part for part in Path(glob_pattern).parts if part.startswith(".")}


def excluded_paths() -> list[Path]:
    """Return the user-excluded directories (relative to the scan root), passed down from the CLI via the environment.

    `glob` skips every file under one of these. The set extends the built-in ignores (`build`, `node_modules`,
    `__pycache__`, hidden folders); it does not replace them. The CLI validates the entries (relative, non-escaping),
    so they are trusted here.
    """
    value = os.environ.get(EXCLUDE_PATHS_ENV_VAR, "")
    return [Path(entry) for entry in value.split(",") if entry]


def inside_git_repository(start: Path | None = None) -> bool:
    """Return whether the given directory (default: the working directory) sits inside a git repository.

    Walks up from the directory to the filesystem root looking for a `.git` entry. Uses `.exists()` rather than
    `.is_dir()` because git worktrees and submodules record `.git` as a *file* (a pointer), not a directory. Checking
    for the entry directly keeps Update-time dependency-free and works even where `git` is not installed.
    """
    directory = Path.cwd() if start is None else start
    return any((parent / ".git").exists() for parent in (directory, *directory.parents))


def glob(*glob_patterns: str, start: Path | None = None, case_sensitive: bool | None = None) -> Iterator[Path]:
    """Return an iterator over all paths matching any of the given glob patterns.

    Hidden folders and files (dot-prefixed, e.g. `.git`, `.venv`) and build-output folders are skipped, so a broad
    pattern like `*.yml` doesn't reach into them. A hidden folder or file named literally in the pattern itself is
    the exception: `glob(".devcontainer/devcontainer.json")` visits `.devcontainer`, so callers can target hidden
    locations directly instead of working around the default skip. Directories passed to `--exclude-path` (see
    `excluded_paths`) are skipped on top of these built-in ignores.
    """
    if start is None:
        start = Path.cwd()
    excluded = excluded_paths()
    for glob_pattern in glob_patterns:
        named_hidden = _named_hidden_parts(glob_pattern)
        for path in start.rglob(glob_pattern, case_sensitive=case_sensitive):
            relative_path = path.relative_to(start)
            if any(part.startswith(".") and part not in named_hidden for part in relative_path.parts):
                continue
            if any(part in ALWAYS_IGNORED_DIRECTORIES for part in relative_path.parts):
                continue
            if any(relative_path.is_relative_to(excluded_dir) for excluded_dir in excluded):
                continue
            yield path
