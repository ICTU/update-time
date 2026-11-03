"""The uv.lock of a uv workspace, and the versions it records for the dependencies of the workspace's projects."""

from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING

from update_time.domain.dependency import PinnedDependency
from update_time.domain.version_scheme import PEP_440
from update_time.formats import toml

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.dependency import DependencyName, VersionString


@dataclass(frozen=True)
class UvLock:
    """A uv.lock, read per operation, so a lock uv has rewritten reads back as it now is."""

    path: Path

    def read(self) -> str | None:
        """Return the lock's text, or None where there is no uv.lock."""
        return self.path.read_text() if self.path.is_file() else None

    def restore(self, text: str | None) -> None:
        """Write the text back to the lock, or remove the lock where there is no text."""
        if text is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_text(text)

    def locked_versions(self, source: str) -> list[PinnedDependency] | None:
        """Return the lowest version the lock records for each dependency of the project at the source.

        The source is the project's directory relative to the lock's. uv names a version beside a dependency where it
        locks several for the project, one per Python version or platform. Return None where there is no uv.lock to
        read.
        """
        if (lock := toml.read(self.path)) is None:
            return None
        packages: list[dict] = lock.get("package", [])
        # The lock lists the dependency groups of a workspace root without `[project]` in `[manifest]`.
        entries = (package for package in packages if source in package.get("source", {}).values())
        entry: dict = next(entries, lock.get("manifest", {}))
        locked = {package["name"]: package["version"] for package in packages if "version" in package}
        versions: dict[DependencyName, VersionString] = {}
        for dependency in _project_dependencies(entry):
            name = dependency["name"]
            if (version := dependency.get("version", locked.get(name))) is None:
                continue
            if name not in versions or PEP_440.is_downgrade(versions[name], version):
                versions[name] = version
        return [PinnedDependency(name, version) for name, version in versions.items()]


def _project_dependencies(entry: dict) -> list[dict]:
    """Return the dependencies a project's entry or the manifest of a uv.lock lists, extras and groups included."""
    groups = (
        *entry.get("optional-dependencies", {}).values(),
        *entry.get("dev-dependencies", {}).values(),
        *entry.get("dependency-groups", {}).values(),
    )
    return [*entry.get("dependencies", []), *chain.from_iterable(groups)]
