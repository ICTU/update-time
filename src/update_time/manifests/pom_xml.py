"""Read the dependencies, plugins, properties, parent, and source repository a pom.xml declares.

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

# The elements that declare an artefact, keyed by tag. Each maps to the group Maven gives it where it names none.
# A dependency names its own group.
_ARTEFACT_ELEMENTS = {"dependency": "", "plugin": "org.apache.maven.plugins"}

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


def declarations(path: Path, effective_pom: XmlElement | None = None) -> list[Reference] | None:
    """Return a reference to each dependency and plugin the pom declares, or None when the pom's XML does not parse.

    An element that does not name its artifact is left out, and so is a dependency that does not name its group. A
    plugin that does not name its group is in Maven's default plugin group. Maven's effective pom, where one is given,
    supplies the coordinates and the version the pom leaves to Maven to resolve.
    """
    project = xml.read(path)
    if project is None:
        return None
    property_elements = _resolvable_elements(project)
    effective_artefacts = _effective_artefacts(effective_pom, project)
    declared = (
        _reference(path, element, property_elements, effective_artefacts, default_group)
        for tag, default_group in _ARTEFACT_ELEMENTS.items()
        for element in project.descendants(tag)
    )
    return [reference for reference in declared if reference is not None]


@dataclass(frozen=True)
class _ArtifactAtLine:
    """An `<artifactId>` text and the line of that element or of its `<version>`, which an input location names.

    The line alone is not enough, since two dependencies can be declared on one line.
    """

    artifact: str
    line: int


@dataclass(frozen=True)
class _EffectiveArtefacts:
    """The coordinates and the version Maven's effective pom gives each dependency and plugin the pom declares."""

    declared: Mapping[_ArtifactAtLine, PinnedDependency]
    # The properties Maven resolved the pom with, the parent's included.
    property_elements: dict[str, XmlElement]

    def get(self, artifact: XmlElement) -> PinnedDependency | None:
        """Return what the effective pom gives the dependency or plugin that the `<artifactId>` element declares."""
        artifact_name = _interpolated(artifact.text, self.property_elements)
        return self.declared.get(_ArtifactAtLine(artifact_name, artifact.line))


def _effective_artefacts(effective_pom: XmlElement | None, project: XmlElement) -> _EffectiveArtefacts:
    """Return the coordinates and the version the effective pom gives each dependency and plugin the pom declares.

    A project never inherits its own `<artifactId>`, so the input location of that element names the scanned pom.
    """
    if effective_pom is None:
        return _EffectiveArtefacts({}, {})
    scanned, _ = _input_location(effective_pom.child("artifactId"))
    property_elements = _resolvable_elements(project)
    effective_property_elements = _resolvable_elements(effective_pom)
    own_versions = _own_versions(project, effective_property_elements)
    artefacts = {}
    entries = (
        (entry, default_group)
        for tag, default_group in _ARTEFACT_ELEMENTS.items()
        for entry in effective_pom.descendants(tag)
    )
    for entry, default_group in entries:
        group = entry.child("groupId")
        artifact = entry.child("artifactId")
        version = entry.child("version")
        declared_by, line = _input_location(artifact)
        if declared_by == scanned and artifact is not None and version is not None:
            managed_by, managed_line = _input_location(version)
            managed_at = _ArtifactAtLine(artifact.text, managed_line)
            own_element = own_versions.get(managed_at) if managed_by == scanned else None
            group_name = default_group if group is None else group.text
            artefacts[_ArtifactAtLine(artifact.text, line)] = PinnedDependency(
                _artefact(group_name, artifact.text), _version_as_left(version, own_element, property_elements)
            )
    return _EffectiveArtefacts(artefacts, effective_property_elements)


def _own_versions(project: XmlElement, property_elements: dict[str, XmlElement]) -> dict[_ArtifactAtLine, XmlElement]:
    """Return the `<version>` element of each dependency and plugin declaring one, keyed by where that element sits."""
    return {
        _ArtifactAtLine(_interpolated(artifact.text, property_elements), version.line): version
        for tag in _ARTEFACT_ELEMENTS
        for element in project.descendants(tag)
        if (artifact := element.child("artifactId")) is not None and (version := element.child("version")) is not None
    }


def _version_as_left(
    version: XmlElement, own_element: XmlElement | None, property_elements: dict[str, XmlElement]
) -> str:
    """Return the version the pom's own `<version>` element holds, where there is one, else the effective pom's.

    The pom may have changed since Maven wrote the effective pom, so the pom's own value wins. A version naming a
    property only the parent declares keeps the effective pom's value.
    """
    held = None if own_element is None else _interpolated(own_element.text, property_elements)
    return held if held is not None and _is_resolved(held) else version.text


def _input_location(element: XmlElement | None) -> tuple[str, int]:
    """Return the pom and the line the element's input location names, or an empty pom and line 0 where it has none."""
    input_location = _INPUT_LOCATION.fullmatch(element.comment) if element else None
    return ("", 0) if input_location is None else (input_location["pom"], int(input_location["line"]))


def has_input_locations(effective_pom: XmlElement) -> bool:
    """Return whether the effective pom names the pom declaring each element, as Maven writes it when verbose."""
    declared_by, _ = _input_location(effective_pom.child("artifactId"))
    return bool(declared_by)


def with_resolved_coordinates(declared: list[Reference]) -> list[Reference]:
    """Return the references whose coordinates resolve, since a repository serves nothing under the others."""
    return [reference for reference in declared if _is_resolved(reference.dependency)]


def leaves_coordinates_unresolved(path: Path) -> bool:
    """Return whether the pom declares a dependency or plugin by a group or an artifact it does not resolve itself."""
    return not all(_is_resolved(reference.dependency) for reference in _artefact_references(path))


def _artefact_references(path: Path, effective_pom: XmlElement | None = None) -> list[Reference]:
    """Return the pom's declarations, or an empty list where its XML does not parse."""
    return declarations(path, effective_pom) or []


def artefacts(path: Path, effective_pom: XmlElement | None = None) -> list[DependencyName]:
    """Return the coordinates of each dependency and plugin the pom declares a version for.

    The effective pom, where one is given, supplies the coordinates. Whether a dependency declares a version is read
    off the pom alone, since the effective pom gives one to each dependency that declares none.
    """
    readings = zip(_artefact_references(path), _artefact_references(path, effective_pom), strict=True)
    return [
        resolved.dependency for own, resolved in readings if own.current_version and _is_resolved(resolved.dependency)
    ]


def _is_resolved(value: str) -> bool:
    """Return whether the pom resolved every property the value names."""
    return not _PROPERTY_REFERENCE.search(value)


def fully_resolved(pinned: PinnedDependency) -> bool:
    """Return whether the pom named the pinned dependency whole: its group, its artifact, and its version.

    A part that still names a property, such as `${spring.version}`, counts as unnamed.
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
    effective_artefacts: _EffectiveArtefacts,
    default_group: str,
) -> Reference | None:
    """Return the reference the element declares, or None where it leaves out its artifact or its group.

    A `<dependency>` and a `<plugin>` name their artefact alike, as `groupId:artifactId`, and version it alike. The
    coordinates and the version are the ones the effective pom gives the element, and the pom's own where the
    effective pom gives the element none. An element without a `<version>` is located at its own line.
    """
    group = element.child("groupId")
    artifact = element.child("artifactId")
    version = element.child("version")
    if artifact is None:
        return None
    group_name = default_group if group is None else _interpolated(group.text, property_elements)
    artifact_name = _interpolated(artifact.text, property_elements)
    if not group_name or not artifact_name:
        return None
    versioned_by = element if version is None else _element_holding(version, property_elements)
    own_version = "" if version is None else _interpolated(version.text, property_elements)
    effective = effective_artefacts.get(artifact)
    pinned = effective or PinnedDependency(_artefact(group_name, artifact_name), own_version)
    location = Location(path, versioned_by.line, versioned_by.column)
    return Reference(pinned.name, pinned.version, location)


def _interpolated(text: str, property_elements: dict[str, XmlElement]) -> str:
    """Return the text with each `${name}` it holds replaced by the value of that property, where there is one."""

    def value(reference: re.Match[str]) -> str:
        element = property_elements.get(reference["name"])
        return reference[0] if element is None else element.text

    return _PROPERTY_REFERENCE.sub(value, text)


def _element_holding(element: XmlElement, property_elements: dict[str, XmlElement]) -> XmlElement:
    """Return the element holding the value: the element itself, or the property it names.

    An element naming a property this pom does not declare, such as one a parent holds, stays as it is.
    """
    named_property = _PROPERTY_REFERENCE.fullmatch(element.text)
    if named_property is None:
        return element
    return property_elements.get(named_property["name"], element)
