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

    from update_time.formats.xml import XmlElement

# A `<version>` that names one of the pom's properties rather than a version, such as `${spring.version}`.
_PROPERTY_REFERENCE = re.compile(r"\$\{(?P<name>[^}]+)\}")


def dependencies(path: Path) -> list[Reference] | None:
    """Return a reference to each dependency the pom declares, or None when the pom's XML does not parse.

    Both `<dependencies>` and `<dependencyManagement>` hold their dependencies in a `<dependency>` element, so
    reading the element wherever it sits reaches the dependencies of either.
    """
    project = xml.read(path)
    if project is None:
        return None
    property_elements = _property_elements(project) | _own_coordinates(project)
    declared = (_dependency(path, element, property_elements) for element in project.descendants("dependency"))
    return [dependency for dependency in declared if dependency is not None]


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


def _dependency(path: Path, element: XmlElement, property_elements: dict[str, XmlElement]) -> Reference | None:
    """Return the reference the `<dependency>` element declares, or None where it leaves out a part Maven needs.

    Maven names a dependency `groupId:artifactId`.
    """
    group = element.child("groupId")
    artifact = element.child("artifactId")
    version = element.child("version")
    if group is None or artifact is None or version is None:
        return None
    versioned_by = _resolved(version, property_elements)
    location = Location(path, versioned_by.line, versioned_by.column)
    name = f"{_resolved(group, property_elements).text}:{_resolved(artifact, property_elements).text}"
    return Reference(name, versioned_by.text, location)


def _resolved(element: XmlElement, property_elements: dict[str, XmlElement]) -> XmlElement:
    """Return the element holding the value: the element itself, or the property it names.

    An element naming a property this pom does not declare, such as one a parent holds, stays as it is.
    """
    named_property = _PROPERTY_REFERENCE.fullmatch(element.text)
    if named_property is None:
        return element
    return property_elements.get(named_property["name"], element)
