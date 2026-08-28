"""Find the files to scan for references."""

import re
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.primitives.environment import EnvVar

if TYPE_CHECKING:
    from collections.abc import Iterator

    from update_time.domain.file_type import FileType

# Private channel that passes --exclude-path from the CLI to the updater subprocesses. The scan-root-relative
# directories to skip, validated by the CLI (relative, non-escaping) so they are trusted here.
EXCLUDE_PATHS: EnvVar[list[Path]] = EnvVar(
    "_UPDATE_TIME_EXCLUDE_PATHS",
    default=[],
    parse=lambda value: [Path(entry) for entry in value.split(",") if entry],
    serialize=lambda paths: ",".join(str(path) for path in paths),
)

# Directories whose contents `glob_for` always skips, on top of hidden (dot-prefixed) folders and the directories
# passed to --exclude-path. The --exclude-path help in `io.cli` lists them, so it stays in step with this tuple.
ALWAYS_IGNORED_DIRECTORIES = ("build", "node_modules", "__pycache__")


def _named_hidden_parts(glob_pattern: str) -> set[str]:
    """Return the hidden (dot-prefixed) path segments a glob pattern names, e.g. `.devcontainer`.

    Hidden folders and files are skipped by default (see `glob_for`), but a pattern that names one (like
    `.devcontainer/devcontainer.json` or `.github/workflows/*.yml`) is asking for it, so it should be visited.
    A wildcard segment such as `.*` needs no special handling: these are compared to real path segments by exact
    equality, which a wildcard never satisfies, so including it grants no exception anyway.
    """
    return {part for part in Path(glob_pattern).parts if part.startswith(".")}


def first_line_match(path: Path, pattern: str | re.Pattern[str], group: str) -> str:
    """Return the named group of the first line in the file that matches the pattern (anchored at the start), or ''.

    A missing file yields '' too, so callers can treat it the same as a file without a matching line. Used to read a
    single value off a Dockerfile — the Node or Python base image version — without a full parser.
    """
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        if match := re.match(pattern, line):
            return match.group(group)
    return ""


def inside_git_repository(start: Path | None = None) -> bool:
    """Return whether the given directory (default: the working directory) sits inside a git repository.

    Walks up from the directory to the filesystem root looking for a `.git` entry. Uses `.exists()` rather than
    `.is_dir()` because git worktrees and submodules record `.git` as a *file* (a pointer), not a directory. Checking
    for the entry directly keeps Update-time dependency-free and works even where `git` is not installed.
    """
    directory = Path.cwd() if start is None else start
    return any((parent / ".git").exists() for parent in (directory, *directory.parents))


def glob_for(file_type: FileType) -> Iterator[Path]:
    """Return an iterator over the paths of this kind of file, walked where and how its declaration says.

    Hidden folders and files (dot-prefixed, e.g. `.git`, `.venv`) and build-output folders are skipped, so a broad
    pattern like `*.yml` doesn't reach into them. A hidden folder or file named literally in the pattern itself is
    the exception: a `.devcontainer/devcontainer.json` pattern visits `.devcontainer`, so a file type can target a
    hidden location directly instead of working around the default skip. Directories passed to `--exclude-path`
    (see `EXCLUDE_PATHS`) are skipped on top of these built-in ignores. A walk that is not recursive searches the
    start directory alone, which is what a file its format keeps in one place needs.
    """
    start = Path.cwd() / file_type.start
    excluded = EXCLUDE_PATHS.get()
    for glob_pattern in file_type.patterns:
        named_hidden = _named_hidden_parts(glob_pattern)
        walk = start.rglob if file_type.recursive else start.glob
        for path in walk(glob_pattern, case_sensitive=file_type.case_sensitive):
            relative_path = path.relative_to(start)
            if any(part.startswith(".") and part not in named_hidden for part in relative_path.parts):
                continue
            if any(part in ALWAYS_IGNORED_DIRECTORIES for part in relative_path.parts):
                continue
            if any(relative_path.is_relative_to(excluded_dir) for excluded_dir in excluded):
                continue
            yield path
