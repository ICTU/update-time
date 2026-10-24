"""Test helpers that build the files the manifest and updater tests read."""

from unittest.mock import MagicMock, Mock

from update_time.manifests.pom_xml import coordinates

from tests.helpers import mock_path
from tests.update_time.helpers import GUAVA


def script(*specs: str, requires_python: str = ">=3.11") -> str:
    """Return a minimal .py file with a PEP 723 `# /// script` block declaring the given dependencies."""
    dependencies = "".join(f'#     "{spec}",\n' for spec in specs)
    return (
        "# /// script\n"
        f'# requires-python = "{requires_python}"\n'
        "# dependencies = [\n"
        f"{dependencies}"
        "# ]\n"
        "# ///\n"
        'print("hi")\n'
    )


def requirements_file(contents: str, *, sibling_in: bool = False) -> Mock:
    """Return a mock requirements file holding the contents, with a sibling `.in` source file where asked for."""
    requirements_txt = mock_path(contents)
    requirements_txt.stem = "requirements"  # so the sibling checked for is `requirements.in`
    requirements_txt.parent = MagicMock()
    requirements_txt.parent.__truediv__.return_value = Mock(exists=Mock(return_value=sibling_in))
    return requirements_txt


def pom_parent(parent: str) -> str:
    """Return the `<parent>` element naming the `groupId:artifactId:version` coordinates, or nothing for none.

    A coordinate given as nothing is left out of the element.
    """
    if not parent:
        return ""
    values = dict(zip(("groupId", "artifactId", "version"), parent.split(":"), strict=True))
    coordinates = "".join(f"<{tag}>{value}</{tag}>" for tag, value in values.items() if value)
    return f"  <parent>{coordinates}</parent>\n"


def pom_coordinates(
    artifact: str | None, group: str | None = None, version: str | None = None, parent: str = ""
) -> str:
    """Return the elements naming a pom: its `<parent>` element where a parent is given, then its own coordinates."""
    return pom_parent(parent) + _coordinate_lines("  ", group, artifact, version)


def _coordinate_lines(indent: str, group: str | None, artifact: str | None, version: str | None) -> str:
    """Return a line at the indent for each of the group, the artifact, and the version that is given."""
    parts = {"groupId": group, "artifactId": artifact, "version": version}
    return "".join(f"{indent}<{name}>{value}</{name}>\n" for name, value in parts.items() if value is not None)


def _coordinates_element(tag: str, indent: str, group: str | None, artifact: str, version: str | None) -> str:
    """Return a `<tag>` element at the indent, declaring the artifact, and the group and the version where given."""
    return f"{indent}<{tag}>\n{_coordinate_lines(indent + '  ', group, artifact, version)}{indent}</{tag}>\n"


def dependency_element(group: str, artifact: str, version: str | None) -> str:
    """Return a `<dependency>` element declaring the group, the artifact, and the version, where one is given."""
    return _coordinates_element("dependency", "    ", group, artifact, version)


def plugin_element(artifact: str, version: str | None, group: str | None = "org.apache.maven.plugins") -> str:
    """Return a `<plugin>` element declaring the artifact, and the version and the group where each is given."""
    return _coordinates_element("plugin", "      ", group, artifact, version)


def build_element(*plugins: str, managed: str = "") -> str:
    """Return a `<build>` element declaring the given plugin elements, and the managed ones in `<pluginManagement>`."""
    management = f"    <pluginManagement>\n      <plugins>\n{managed}      </plugins>\n    </pluginManagement>\n"
    return f"  <build>\n{management if managed else ''}    <plugins>\n{''.join(plugins)}    </plugins>\n  </build>\n"


# A plugin in Maven's default plugin group.
SUREFIRE = "org.apache.maven.plugins:maven-surefire-plugin"


def guava_element(version: str | None) -> str:
    """Return the `<dependency>` element declaring guava."""
    return dependency_element(*coordinates(GUAVA), version)


def marked_at_its_version(element: str, directives: str) -> str:
    """Return the element with a marker carrying the directives at the end of its `<version>` line."""
    return element.replace("</version>", f"</version>  <!-- update-time: {directives} -->")


def dependency_management_element(*dependencies: str) -> str:
    """Return a `<dependencyManagement>` element declaring the given dependency elements."""
    declared = "".join(dependencies)
    return f"  <dependencyManagement>\n    <dependencies>\n{declared}    </dependencies>\n  </dependencyManagement>\n"


def pom_declaring(
    *dependencies: str, coordinates: str = "", properties: str = "", managed: str = "", build: str = ""
) -> str:
    """Return a pom declaring the given coordinates, properties, managed dependencies, dependencies, and plugins.

    Without coordinates the properties come first, so the first property sits on line 3.
    """
    return (
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        f"{coordinates}"
        f"{properties}"
        f"{managed}"
        "  <dependencies>\n"
        f"{''.join(dependencies)}"
        "  </dependencies>\n"
        f"{build}"
        "</project>\n"
    )


# Each input location names the scanned pom by this id. The pom is a child inheriting its parent's group.
SCANNED_POM_ID = "org.example:child:1.0"


# An input location names the scanned pom's parent by this id, where the child inherits the element.
PARENT_POM_ID = "org.example:parent:1.0"


# The elements of a pom that Maven names by `SCANNED_POM_ID`, on lines 2 and 3.
SCANNED_POM_COORDINATES = pom_coordinates("child", parent=PARENT_POM_ID)


def effective_dependency_element(
    artefact: str,
    version: str,
    line: int,
    declared_by: str = SCANNED_POM_ID,
    *,
    managed_at: tuple[str, int] | None = None,
) -> str:
    """Return a `<dependency>` of the effective pom, its `<artifactId>` located on the line of the declaring pom.

    Its `<version>` is located on the next line, or on the line of the pom `managed_at` names for a dependency that
    leaves its version to a `<dependencyManagement>` section.
    """
    locations = {
        "groupId": (declared_by, line - 1),
        "artifactId": (declared_by, line),
        "version": managed_at or (declared_by, line + 1),
    }
    return f"    <dependency>\n{_located_coordinates(artefact, version, locations)}    </dependency>\n"


def effective_plugin_element(
    artefact: str, version: str, line: int, *, managed_at: tuple[str, int] | None = None
) -> str:
    """Return a `<plugin>` of the effective pom, located in the scanned pom as a dependency is.

    Maven leaves out the `<groupId>` of a plugin in its default plugin group.
    """
    group = {} if artefact.startswith("org.apache.maven.plugins:") else {"groupId": (SCANNED_POM_ID, line - 1)}
    locations = group | {"artifactId": (SCANNED_POM_ID, line), "version": managed_at or (SCANNED_POM_ID, line + 1)}
    return f"      <plugin>\n{_located_coordinates(artefact, version, locations)}      </plugin>\n"


def _located_coordinates(artefact: str, version: str, locations: dict[str, tuple[str, int]]) -> str:
    """Return the coordinate elements of an effective pom entry, each followed by the input location given for it."""
    group, artifact = coordinates(artefact)
    values = {"groupId": group, "artifactId": artifact, "version": version}
    return "".join(
        f"      <{tag}>{values[tag]}</{tag}>  <!-- {pom}, line {located} -->\n"
        for tag, (pom, located) in locations.items()
    )


# A pom declaring guava alone gets this entry in its effective pom, with guava's `<artifactId>` on line 5.
EFFECTIVE_GUAVA = effective_dependency_element(GUAVA, "33.0.0-jre", line=5)


def effective_pom_declaring(*dependencies: str, properties: str = "", managed: str = "", build: str = "") -> str:
    """Return the effective pom Maven writes for the scanned pom, holding the given elements.

    Maven lists every property the parent declares, and this lists only the ones given. Update-time reads them only to
    resolve an artifact's name, so a test passes the properties its artifacts name.
    """
    header = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!-- Effective POM for project \'org.example:child:jar:1.0\' -->\n'
    )
    located = (
        f"  <groupId>org.example</groupId>  <!-- {PARENT_POM_ID}, line 2 -->\n"
        f"  <artifactId>child</artifactId>  <!-- {SCANNED_POM_ID}, line 3 -->\n"
    )
    return header + pom_declaring(
        *dependencies, coordinates=located, properties=properties, managed=managed, build=build
    )
