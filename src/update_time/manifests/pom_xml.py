"""Read the dependencies, properties, parent, and source repository a pom.xml declares.

This module owns what a pom's elements mean, whether Update-time scans the pom or a registry serves it. Reading the
XML itself is the formats layer's concern.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from update_time.domain.dependency import PinnedDependency
from update_time.domain.reference import Reference
from update_time.formats import xml
from update_time.primitives.location import Location

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from update_time.domain.dependency import DependencyName
    from update_time.formats.xml import XmlElement

# An element naming one of the pom's properties rather than holding its own value, such as `${spring.version}`.
_PROPERTY_REFERENCE = re.compile(r"\$\{(?P<name>[^}]+)\}")

# Maven's verbose effective pom writes an input location after each element: the pom and the line declaring it.
_INPUT_LOCATION = re.compile(r"(?P<pom>\S+), line (?P<line>\d+)")

# The group Maven gives a plugin that declares none. A dependency names its own group.
_PLUGIN_GROUP = "org.apache.maven.plugins"

# Maven's own prefix on an `<scm>` value: `scm:<provider>:` in front of the URL that provider reads.
_SCM_PREFIX = re.compile(r"^scm:[^:]+:")

# Update-time reads these children of `<scm>`, in this order, to find where the project's source lives.
_SCM_URL_TAGS = ("url", "connection", "developerConnection")

# The children that name an artefact and its version, in the order `groupId:artifactId` names them.
_COORDINATE_TAGS = ("groupId", "artifactId", "version")


def source_urls(project: XmlElement) -> list[str]:
    """Return the URLs that may name where the project's source lives, the `<scm>` URLs first.

    The project's own `<url>` comes after them, because a module's `<url>` may name its umbrella project.
    """
    urls = scm_urls(project)
    project_url = project.child("url")
    return urls if project_url is None else [*urls, project_url.text]


def scm_urls(project: XmlElement) -> list[str]:
    """Return the URLs the pom's `<scm>` element names.

    Each is stripped of the `scm:<provider>:` prefix Maven writes in front of it, leaving the URL that provider reads.
    """
    scm = project.child("scm")
    named = [] if scm is None else [scm.child(tag) for tag in _SCM_URL_TAGS]
    return [_SCM_PREFIX.sub("", url.text) for url in named if url is not None]


def parent(project: XmlElement) -> PinnedDependency | None:
    """Return the artefact and version the pom's `<parent>` element names in full, or None where it does not."""
    element = project.child("parent")
    if element is None:
        return None
    group_id, artifact_id, version = (
        "" if (child := element.child(tag)) is None else child.text for tag in _COORDINATE_TAGS
    )
    pinned = PinnedDependency(_artefact(group_id, artifact_id), version)
    return pinned if fully_resolved(pinned) else None


def dependencies(path: Path, effective_pom: XmlElement | None = None) -> list[Reference] | None:
    """Return a reference to each dependency the pom declares, or None when the pom's XML does not parse.

    Both `<dependencies>` and `<dependencyManagement>` hold their dependencies in a `<dependency>` element, so
    reading the element wherever it sits reaches the dependencies of either. Maven's effective pom, where one is
    given, supplies a version the parent declares or manages.
    """
    project = xml.read(path)
    if project is None:
        return None
    return _references(path, project, {"dependency": ""}, _effective_versions(effective_pom, project))


@dataclass(frozen=True)
class _ArtifactDeclaration:
    """Where a pom declares an artefact: its `<artifactId>` text, and the line of that element or its `<version>`.

    The line alone is not enough, since two dependencies can be declared on one line.
    """

    artifact: str
    line: int


# The version Maven's effective pom gives each dependency, keyed by where its `<artifactId>` sits.
type _EffectiveVersions = Mapping[_ArtifactDeclaration, str]


def _effective_versions(effective_pom: XmlElement | None, project: XmlElement) -> _EffectiveVersions:
    """Return the version the effective pom gives each dependency the pom declares.

    A project never inherits its own `<artifactId>`, so the input location of that element names the scanned pom.
    """
    if effective_pom is None:
        return {}
    scanned, _ = _input_location(effective_pom.child("artifactId"))
    property_elements = _resolvable_elements(project)
    own_versions = _own_versions(project, property_elements)
    versions = {}
    for dependency in effective_pom.descendants("dependency"):
        artifact = dependency.child("artifactId")
        version = dependency.child("version")
        declared_by, line = _input_location(artifact)
        if declared_by == scanned and artifact is not None and version is not None:
            managed_by, managed_line = _input_location(version)
            managed_at = _ArtifactDeclaration(artifact.text, managed_line)
            own_element = own_versions.get(managed_at) if managed_by == scanned else None
            versions[_ArtifactDeclaration(artifact.text, line)] = _version_as_left(
                version, own_element, property_elements
            )
    return versions


def _own_versions(
    project: XmlElement, property_elements: dict[str, XmlElement]
) -> dict[_ArtifactDeclaration, XmlElement]:
    """Return the `<version>` element of each dependency the pom declares one for, keyed by where that element sits."""
    return {
        _ArtifactDeclaration(_resolved(artifact, property_elements).text, version.line): version
        for dependency in project.descendants("dependency")
        if (artifact := dependency.child("artifactId")) is not None
        and (version := dependency.child("version")) is not None
    }


def _version_as_left(
    version: XmlElement, own_element: XmlElement | None, property_elements: dict[str, XmlElement]
) -> str:
    """Return the version the pom's own `<version>` element holds, where there is one, else the effective pom's.

    The pom may have changed since Maven wrote the effective pom, so the pom's own value wins. A version naming a
    property only the parent declares keeps the effective pom's value.
    """
    held = None if own_element is None else _resolved(own_element, property_elements).text
    return held if held is not None and _is_resolved(held) else version.text


def _input_location(element: XmlElement | None) -> tuple[str, int]:
    """Return the pom and the line the element's input location names, or an empty pom and line 0 where it has none."""
    input_location = _INPUT_LOCATION.fullmatch(element.comment) if element else None
    return ("", 0) if input_location is None else (input_location["pom"], int(input_location["line"]))


def artefact_references(path: Path) -> list[Reference]:
    """Return a reference to each dependency and plugin the pom declares.

    Coordinates holding an unresolved property are left out, since a repository serves nothing under them.
    """
    project = xml.read(path)
    if project is None:
        return []
    declared = _references(path, project, {"dependency": "", "plugin": _PLUGIN_GROUP}, {})
    return [reference for reference in declared if _is_resolved(reference.dependency)]


def _references(
    path: Path,
    project: XmlElement,
    default_groups: dict[str, str],
    effective_versions: _EffectiveVersions,
) -> list[Reference]:
    """Return the reference each named element declares, dropping the ones that leave out their artifact or group.

    `default_groups` maps the tag of each element to read to the group Maven gives it when it declares none.
    """
    property_elements = _resolvable_elements(project)
    declared = (
        _reference(path, element, property_elements, effective_versions, default_group)
        for tag, default_group in default_groups.items()
        for element in project.descendants(tag)
    )
    return [reference for reference in declared if reference is not None]


def artefacts(path: Path) -> list[DependencyName]:
    """Return the coordinates of each dependency and plugin the pom declares a version for."""
    return [reference.dependency for reference in artefact_references(path) if reference.current_version]


def _is_resolved(value: str) -> bool:
    """Return whether the pom resolved the value, rather than leaving a property its parent declares."""
    return not _PROPERTY_REFERENCE.search(value)


def fully_resolved(pinned: PinnedDependency) -> bool:
    """Return whether the pom named the pinned dependency whole: its group, its artifact, and its version.

    A part naming a property counts as unnamed, since this does not resolve the pom's properties.
    """
    group_id, artifact_id = coordinates(pinned.name)
    return all(part and _is_resolved(part) for part in (group_id, artifact_id, pinned.version))


def _resolvable_elements(project: XmlElement) -> dict[str, XmlElement]:
    """Return the element each `${name}` the pom can resolve itself names: its properties and its own coordinates."""
    return _property_elements(project) | _own_coordinates(project)


def _own_coordinates(project: XmlElement) -> dict[str, XmlElement]:
    """Return the project's own coordinates, which a dependency on a sibling module names as `${project.groupId}`."""
    return {f"project.{tag}": element for tag in _COORDINATE_TAGS if (element := project.child(tag)) is not None}


def properties(path: Path) -> dict[str, str]:
    """Return the value of each property the pom declares, keyed by the property's name."""
    project = xml.read(path)
    return {} if project is None else {name: element.text for name, element in _property_elements(project).items()}


def _property_elements(project: XmlElement) -> dict[str, XmlElement]:
    """Return the element declaring each property the pom holds, keyed by name, the project's own winning.

    A profile declares properties of its own, and whether that profile is active is Maven's to decide, so a name
    the project itself declares wins over a profile's.
    """
    anywhere = {element.tag: element for section in project.descendants("properties") for element in section.children}
    own = project.child("properties")
    return anywhere | ({element.tag: element for element in own.children} if own else {})


def coordinates(artefact: DependencyName) -> tuple[str, str]:
    """Split an artefact's `groupId:artifactId` name into its group and its artifact."""
    group_id, _, artifact_id = artefact.partition(":")
    return group_id, artifact_id


def _artefact(group_id: str, artifact_id: str) -> DependencyName:
    """Join a group and an artifact into the artefact's `groupId:artifactId` name."""
    return f"{group_id}:{artifact_id}"


def _reference(
    path: Path,
    element: XmlElement,
    property_elements: dict[str, XmlElement],
    effective_versions: _EffectiveVersions,
    default_group: str = "",
) -> Reference | None:
    """Return the reference the element declares, or None where it leaves out its artifact or its group.

    A `<dependency>` and a `<plugin>` name their artefact alike, as `groupId:artifactId`, and version it alike. The
    caller passes the group to fall back on for an element whose group Maven defaults. The version is the one the
    effective pom gives the element, and the pom's own where there is no effective pom. An element without a
    `<version>` is located at its own line.
    """
    group = element.child("groupId")
    artifact = element.child("artifactId")
    version = element.child("version")
    if artifact is None:
        return None
    group_name = default_group if group is None else _resolved(group, property_elements).text
    artifact_name = _resolved(artifact, property_elements).text
    if not group_name or not artifact_name:
        return None
    versioned_by = element if version is None else _resolved(version, property_elements)
    own_version = "" if version is None else versioned_by.text
    current_version = effective_versions.get(_ArtifactDeclaration(artifact_name, artifact.line), own_version)
    location = Location(path, versioned_by.line, versioned_by.column)
    return Reference(_artefact(group_name, artifact_name), current_version, location)


def _resolved(element: XmlElement, property_elements: dict[str, XmlElement]) -> XmlElement:
    """Return the element holding the value: the element itself, or the property it names.

    An element naming a property this pom does not declare, such as one a parent holds, stays as it is.
    """
    named_property = _PROPERTY_REFERENCE.fullmatch(element.text)
    if named_property is None:
        return element
    return property_elements.get(named_property["name"], element)
