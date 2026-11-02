"""Read the versions a uv.lock records for the dependencies of the projects in a uv workspace."""

from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING, Self

from update_time.domain.dependency import PinnedDependency
from update_time.domain.version_scheme import PEP_440
from update_time.formats import toml

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.dependency import DependencyName, VersionString


@dataclass(frozen=True)
class UvLock:
    """A uv.lock, with the packages it locks and the manifest of its workspace."""

    packages: list[dict]
    manifest: dict

    @classmethod
    def read(cls, path: Path) -> Self | None:
        """Return the uv.lock at the path, or None where there is no uv.lock to read."""
        if (lock := toml.read(path)) is None:
            return None
        return cls(lock.get("package", []), lock.get("manifest", {}))

    def locked_versions(self, source: str) -> list[PinnedDependency]:
        """Return the lowest version the lock records for each dependency of the project at the source.

        The source is the project's directory relative to the lock's. uv names a version beside a dependency where it
        locks several for the project, one per Python version or platform.
        """
        # The lock lists the dependency groups of a workspace root without `[project]` in `[manifest]`.
        entries = (package for package in self.packages if source in package.get("source", {}).values())
        entry: dict = next(entries, self.manifest)
        locked = {package["name"]: package["version"] for package in self.packages if "version" in package}
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
