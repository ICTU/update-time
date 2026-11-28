"""Build what a test hands the code under test: declarations, references, bounds, vulnerabilities, and instants."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from update_time.domain.dependency import NO_PROJECT, NO_VERSION, DependencyVersion, Project
from update_time.domain.marker import NO_MARKER, Marker
from update_time.domain.reference import Reference, ResolvedReference
from update_time.domain.vulnerability import Vulnerability
from update_time.manifests.python_dependency_toml import Declaration
from update_time.markers.parser import _parse_bound
from update_time.primitives.location import Location

if TYPE_CHECKING:
    from unittest.mock import Mock

    from update_time.domain.bound import Verb, VersionBound


def declaration(  # noqa: PLR0913 — a declaration is that many facts
    dependency: str,
    version: str,
    path: Mock,
    line: int,
    position: int,
    *,
    uv_sourced: bool = False,
    direct_url: bool = False,
    build_requirement: bool = False,
    environment_marker: str = "",
) -> Declaration:
    """Return the declaration a file makes at the line, by default of a dependency PyPI serves a release for."""
    return Declaration(
        dependency,
        version,
        Location(path, line),
        uv_sourced=uv_sourced,
        direct_url=direct_url,
        build_requirement=build_requirement,
        environment_marker=environment_marker,
        position=position,
    )


def reference(dependency: str, location: Location, version: str = "") -> Reference:
    """Return the reference a log method is handed, for the dependency pinned at the location."""
    return Reference(dependency, version, location)


def resolved_reference(
    dependency: str,
    location: Location,
    version: DependencyVersion = NO_VERSION,
    marker: Marker = NO_MARKER,
    project: Project = NO_PROJECT,
) -> ResolvedReference:
    """Return the resolved reference a check is handed: the reference, its version, its marker, and its project."""
    return ResolvedReference(dependency, "", location, version=version, marker=marker, project=project)


def bound(verb: Verb, item: str) -> VersionBound:
    """Return the version bound the marker item expresses, for tests that need a bound as input."""
    version_bound = _parse_bound(verb, item)
    if version_bound is None:
        message = f"Not a version bound item: {item!r}"
        raise ValueError(message)
    return version_bound


def vulnerability(advisory: str, summary: str, level: str, aliases: list[str] | None = None) -> Vulnerability:
    """Return the vulnerability Update-time reads an advisory as, known by the aliases where it has them."""
    return Vulnerability(advisory, summary, level, f"https://osv.dev/{advisory}", frozenset(aliases or []))


def days_ago(days: int) -> datetime:
    """Return the instant the given number of days ago."""
    return datetime.now(UTC) - timedelta(days=days)
