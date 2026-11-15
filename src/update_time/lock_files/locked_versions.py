"""Hold the versions a lock file records per project, and read them from the report of a list command."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyName, VersionString


# The path of the root project relative to the workspace root. The locked versions of each project are keyed by the
# project's path relative to the workspace root, as the list commands of npm and pnpm report them and bun's lock
# records them.
ROOT_PROJECT = Path()

# The top-level versions of each project that a lock file records, keyed by the path of the project.
type LockedVersions = dict[Path, dict[DependencyName, VersionString]]


def listed_versions(dependencies: dict) -> dict[DependencyName, VersionString]:
    """Return the version of each dependency that a list command reports from the lock file."""
    return {package: info["version"] for package, info in dependencies.items() if "version" in info}
