"""Find the files to scan for references."""

from pathlib import Path, PurePath
from typing import TYPE_CHECKING

from update_time.primitives.environment import EnvVar

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from update_time.domain.file_type import FileType
    from update_time.io.log import Logger

# Private channel that passes --exclude-path from the CLI to the updater subprocesses. The scan-root-relative
# directories to skip, validated by the CLI (relative, non-escaping) so they are trusted here.
_EXCLUDE_PATHS: EnvVar[list[Path]] = EnvVar(
    "_UPDATE_TIME_EXCLUDE_PATHS",
    default=[],
    parse=lambda value: [Path(entry) for entry in value.split(",") if entry],
    serialize=lambda paths: ",".join(str(path) for path in paths),
)

# Directories whose contents `glob_for` always skips.
ALWAYS_IGNORED_DIRECTORIES = ("build", "target", "node_modules", "__pycache__")


def _hidden(names: Iterable[str]) -> list[str]:
    """Return the hidden (dot-prefixed) names."""
    return [name for name in names if name.startswith(".")]


def _skipped_as_hidden(relative_path: Path, hidden_segments: list[str], *, case_sensitive: bool | None) -> bool:
    """Return whether a hidden part of the path does not match any of the pattern's hidden segments."""
    return any(
        not any(PurePath(part).full_match(segment, case_sensitive=case_sensitive) for segment in hidden_segments)
        for part in _hidden(relative_path.parts)
    )


def _skipped(
    relative_path: Path, hidden_segments: list[str], excluded: list[Path], *, case_sensitive: bool | None
) -> bool:
    """Return whether the path holds a hidden part the pattern does not name, or an ignored or excluded directory."""
    if _skipped_as_hidden(relative_path, hidden_segments, case_sensitive=case_sensitive):
        return True
    if any(part in ALWAYS_IGNORED_DIRECTORIES for part in relative_path.parts):
        return True
    return any(relative_path.is_relative_to(excluded_dir) for excluded_dir in excluded)


def is_scanned(path: Path) -> bool:
    """Return whether the scan reaches the file, under the scan root and outside the directories it skips."""
    root = Path.cwd()
    excluded = _EXCLUDE_PATHS.get()
    return path.is_relative_to(root) and not _skipped(path.relative_to(root), [], excluded, case_sensitive=None)


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
    pattern like `*.yml` doesn't reach into them. A hidden folder or file that a hidden segment of the pattern matches
    is the exception: a `.devcontainer/devcontainer.json` pattern visits `.devcontainer`, and a `.*.Dockerfile` pattern
    visits `.gitpod.Dockerfile`. Directories passed to `--exclude-path` (see `_EXCLUDE_PATHS`) are skipped on top of
    these built-in ignores. A walk that is not recursive searches the start directory alone, which is what a file its
    format keeps in one place needs.
    """
    start = Path.cwd() / file_type.start
    excluded = _EXCLUDE_PATHS.get()
    for glob_pattern in file_type.patterns:
        hidden_segments = _hidden(PurePath(glob_pattern).parts)
        case_sensitive = file_type.case_sensitivity(glob_pattern)
        walk = start.rglob if file_type.recursive else start.glob
        for path in walk(glob_pattern, case_sensitive=case_sensitive):
            if not _skipped(path.relative_to(start), hidden_segments, excluded, case_sensitive=case_sensitive):
                yield path


def configure_excluded_paths(paths: list[Path], logger: Logger) -> None:
    """Set the excluded directories that exist for `glob_for` to skip, and log each directory given.

    A directory is checked relative to the current one. One that does not exist is warned about and left out.
    """
    existing = []
    for path in paths:
        if path.exists():
            logger.excluded_path(path)
            existing.append(path)
        else:
            logger.missing_excluded_path(path)
    _EXCLUDE_PATHS.set(existing)
