"""Log each version of a Python dependency that a package manager resolves or moves."""

from typing import TYPE_CHECKING

from update_time.domain.dependency import normalized_python_name
from update_time.domain.reference import Reference
from update_time.domain.version_scheme import PEP_440
from update_time.sources import pypi

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from update_time.domain.dependency import DependencyName, PinnedDependency, VersionString
    from update_time.io.log import Logger
    from update_time.primitives.location import Location


def by_package[ReferenceT: Reference](
    declarations: Iterable[ReferenceT],
) -> dict[DependencyName, list[ReferenceT]]:
    """Return the declarations, each with its marker, grouped by the normalized name of their package.

    A name uv or PyPI reports finds every declaration of its package, whichever of the spellings that normalize to
    that name each declaration uses.
    """
    grouped: dict[DependencyName, list[ReferenceT]] = {}
    for declaration in declarations:
        grouped.setdefault(normalized_python_name(declaration.dependency), []).append(declaration)
    return grouped


def log_moved_versions(
    log: Logger,
    manager: str,
    before: Iterable[PinnedDependency],
    after: Iterable[PinnedDependency],
    declarations: Iterable[Reference],
) -> None:
    """Log each version that moved, up or back, at each declaration of its package that does not pin an exact version.

    A package without a version before is reported as new.
    """
    versions_before = {normalized_python_name(pin.name): pin.version for pin in before}
    unpinned = by_package(declaration for declaration in declarations if not declaration.current_version)
    locations = {name: [declaration.location for declaration in group] for name, group in unpinned.items()}
    for pin in after:
        name = normalized_python_name(pin.name)
        if name not in locations or (old := versions_before.get(name, "")) == pin.version:
            continue
        if PEP_440.is_downgrade(old, pin.version):
            for location in locations[name]:
                log.version_moved_back(manager, Reference(pin.name, old, location), pin.version)
        else:
            log_new_version(log, pin.name, pin.version, locations[name])


def log_new_version(
    log: Logger, package: DependencyName, version: VersionString, locations: Sequence[Location]
) -> None:
    """Log that a new version is available for the PyPI package, at each of the locations declaring it.

    The package is named as the package manager reports it, so one package is named one way however each line spells it.
    The changelog and publication date are fetched once, whichever locations the version is reported at.
    """
    dependency_version = pypi.SOURCE.release(package, version)
    for location in locations:
        # This message renders no version the reference is pinned to, so the reference names none.
        log.new_version(Reference(package, "", location), dependency_version)
