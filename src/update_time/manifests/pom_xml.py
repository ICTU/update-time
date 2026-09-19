"""Read the dependencies and properties a pom.xml declares.

This module owns what a pom's elements mean. Reading the XML itself is the formats layer's concern.
"""

import re
from typing import TYPE_CHECKING

from update_time.domain.reference import Reference
from update_time.formats import xml
from update_time.primitives.location import Location

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.dependency import DependencyName
    from update_time.formats.xml import XmlElement

# A `<version>` that names one of the pom's properties rather than a version, such as `${spring.version}`.
_PROPERTY_REFERENCE = re.compile(r"\$\{(?P<name>[^}]+)\}")

# The group Maven gives a plugin that declares none. A dependency names its own group.
_PLUGIN_GROUP = "org.apache.maven.plugins"


def dependencies(path: Path) -> list[Reference] | None:
    """Return a reference to each dependency the pom declares, or None when the pom's XML does not parse.

    Both `<dependencies>` and `<dependencyManagement>` hold their dependencies in a `<dependency>` element, so
    reading the element wherever it sits reaches the dependencies of either.
    """
    project = xml.read(path)
    if project is None:
        return None
    property_elements = _property_elements(project) | _own_coordinates(project)
    declared = (_reference(path, element, property_elements) for element in project.descendants("dependency"))
    return [dependency for dependency in declared if dependency is not None]


def artefacts(path: Path) -> list[DependencyName]:
    """Return the coordinates of each dependency and plugin the pom declares a version for.

    A pom can name a group or an artifact with a property, such as `${spring.group}`. Update-time reads only the
    pom's own properties, so a parent's property stays unresolved.
    """
    project = xml.read(path)
    if project is None:
        return []
    property_elements = _property_elements(project) | _own_coordinates(project)
    declared = (
        _reference(path, element, property_elements, default_group)
        for tag, default_group in (("dependency", ""), ("plugin", _PLUGIN_GROUP))
        for element in project.descendants(tag)
    )
    named = (reference.dependency for reference in declared if reference is not None)
    return [artefact for artefact in named if not _PROPERTY_REFERENCE.search(artefact)]


def _own_coordinates(project: XmlElement) -> dict[str, XmlElement]:
    """Return the project's own coordinates, which a dependency on a sibling module names as `${project.groupId}`."""
    named = ("groupId", "artifactId", "version")
    return {f"project.{tag}": element for tag in named if (element := project.child(tag)) is not None}


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


def _reference(
    path: Path, element: XmlElement, property_elements: dict[str, XmlElement], default_group: str = ""
) -> Reference | None:
    """Return the reference the element declares, or None where it leaves out a part Maven needs.

    A `<dependency>` and a `<plugin>` name their artefact alike, as `groupId:artifactId`, and version it alike. The
    caller passes the group to fall back on for an element whose group Maven defaults.
    """
    group = element.child("groupId")
    artifact = element.child("artifactId")
    version = element.child("version")
    group_name = default_group if group is None else _resolved(group, property_elements).text
    if not group_name or artifact is None or version is None:
        return None
    versioned_by = _resolved(version, property_elements)
    location = Location(path, versioned_by.line, versioned_by.column)
    return Reference(f"{group_name}:{_resolved(artifact, property_elements).text}", versioned_by.text, location)


def _resolved(element: XmlElement, property_elements: dict[str, XmlElement]) -> XmlElement:
    """Return the element holding the value: the element itself, or the property it names.

    An element naming a property this pom does not declare, such as one a parent holds, stays as it is.
    """
    named_property = _PROPERTY_REFERENCE.fullmatch(element.text)
    if named_property is None:
        return element
    return property_elements.get(named_property["name"], element)
