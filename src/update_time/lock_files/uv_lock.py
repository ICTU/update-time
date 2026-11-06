"""The lock uv writes for a workspace or a script, and the versions it records for their dependencies."""

from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING

from update_time.domain.dependency import PinnedDependency
from update_time.domain.version_scheme import PEP_440
from update_time.formats import toml

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from update_time.domain.dependency import DependencyName, VersionString

# Each version a lock records for each dependency, in the lock's order.
type RecordedVersions = dict[DependencyName, list[VersionString]]


@dataclass(frozen=True)
class UvLock:
    """A lock uv writes, read per operation, so a lock uv has rewritten reads back as it now is."""

    path: Path

    def read(self) -> str | None:
        """Return the lock's text, or None where there is no lock."""
        return self.path.read_text() if self.path.is_file() else None

    def restore(self, text: str | None) -> None:
        """Write the text back to the lock, or remove the lock where there is no text."""
        if text is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_text(text)

    def locked_versions(self, source: str | None) -> list[PinnedDependency] | None:
        """Return the lowest version the lock records for each dependency of the project at the source.

        Return None where there is no lock to read.
        """
        if (recorded := self.recorded_versions(source)) is None:
            return None
        return [PinnedDependency(name, _lowest(versions)) for name, versions in recorded.items()]

    def recorded_versions(self, source: str | None) -> RecordedVersions | None:
        """Return each version the lock records for each dependency of the project at the source, in the lock's order.

        The source is the project's directory relative to the lock's, or None for a script, whose lock lists its
        dependencies in `[manifest]`. uv names a version beside a dependency where the lock holds several versions of
        its package, one per Python version or platform. A dependency without one takes each version the lock holds,
        as a script's dependency does where uv forks its resolution. Return None where there is no lock to read.
        """
        if (lock := toml.read(self.path)) is None:
            return None
        packages: list[dict] = lock.get("package", [])
        # The lock lists the dependency groups of a workspace root without `[project]` in `[manifest]`.
        entries = (
            package for package in packages if source is not None and source in package.get("source", {}).values()
        )
        entry: dict = next(entries, lock.get("manifest", {}))
        locked = _grouped(
            PinnedDependency(package["name"], package["version"]) for package in packages if "version" in package
        )
        return _grouped(
            PinnedDependency(dependency["name"], version)
            for dependency in _project_dependencies(entry)
            for version in _versions_of(dependency, locked)
        )


def _grouped(pins: Iterable[PinnedDependency]) -> RecordedVersions:
    """Return the versions of the pins, grouped by name, in the order of the pins."""
    grouped: RecordedVersions = {}
    for pin in pins:
        grouped.setdefault(pin.name, []).append(pin.version)
    return grouped


def _versions_of(dependency: dict, locked: RecordedVersions) -> list[VersionString]:
    """Return the version the lock names beside the dependency, or else each version it holds of the package."""
    return [dependency["version"]] if "version" in dependency else locked.get(dependency["name"], [])


def _lowest(versions: Sequence[VersionString]) -> VersionString:
    """Return the lowest of the versions, the first where PEP 440 does not order them."""
    lowest, *others = versions
    for version in others:
        if PEP_440.is_downgrade(lowest, version):
            lowest = version
    return lowest


def _project_dependencies(entry: dict) -> list[dict]:
    """Return the dependencies a project's entry or the manifest of a lock lists, extras and groups included."""
    groups = (
        *entry.get("optional-dependencies", {}).values(),
        *entry.get("dev-dependencies", {}).values(),
        *entry.get("dependency-groups", {}).values(),
    )
    return [*entry.get("dependencies", []), *entry.get("requirements", []), *chain.from_iterable(groups)]
