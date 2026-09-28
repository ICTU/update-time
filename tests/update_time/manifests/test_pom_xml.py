"""Unit tests for reading a pom.xml, with file I/O mocked."""

import unittest

from update_time.formats import xml
from update_time.manifests import pom_xml

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time.helpers import (
    dependency_element,
    dependency_management_element,
    effective_dependency_element,
    effective_pom_declaring,
    guava_element,
    pom_declaring,
)

_POM_WITH_A_PROFILE = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <properties>
    <spring.version>6.1.0</spring.version>
  </properties>
  <profiles>
    <profile>
      <properties>
        <spring.version>5.0.0</spring.version>
      </properties>
    </profile>
  </profiles>
  <dependencies>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-core</artifactId>
      <version>${spring.version}</version>
    </dependency>
  </dependencies>
</project>
"""


_POM_WITH_A_PROFILE_ONLY_PROPERTY = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <profiles>
    <profile>
      <properties>
        <spring.version>6.1.0</spring.version>
        <commons.version>3.0</commons.version>
      </properties>
      <dependencies>
        <dependency>
          <groupId>org.apache.commons</groupId>
          <artifactId>commons-lang3</artifactId>
          <version>${commons.version}</version>
        </dependency>
      </dependencies>
    </profile>
  </profiles>
</project>
"""


_POM_NAMING_ITS_OWN_GROUP = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <groupId>com.acme</groupId>
  <artifactId>parent</artifactId>
  <version>1.0.0</version>
  <dependencies>
    <dependency>
      <groupId>${project.groupId}</groupId>
      <artifactId>api</artifactId>
      <version>1.2.3</version>
    </dependency>
  </dependencies>
</project>
"""


_POM_WITH_INCOMPLETE_DEPENDENCIES = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <dependencies>
    <dependency>
      <artifactId>spring-core</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-web</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId></artifactId>
      <version>6.1.0</version>
    </dependency>
    <dependency>
      <groupId>com.google.guava</groupId>
      <artifactId>guava</artifactId>
      <version>33.0.0-jre</version>
    </dependency>
  </dependencies>
</project>
"""


class PropertiesTest(unittest.TestCase):
    """Unit tests for the properties a pom declares."""

    def test_a_pom_that_does_not_parse(self):
        """Test that a pom whose XML does not parse declares no property, rather than ending the run."""
        self.assertEqual(pom_xml.properties(mock_path("<project><broken>")), {})


class ArtefactsTest(unittest.TestCase):
    """Unit tests for the coordinates a pom declares a version for."""

    @kills(
        Mutation(
            pom_xml.artefact_references,
            "if project is None:",
            "if False:",
            "an unparsable pom takes the reading of its artefacts down with it",
            raises="AttributeError: 'NoneType' object has no attribute 'descendants'",
        )
    )
    def test_a_pom_that_does_not_parse(self):
        """Test that an unparsable pom yields an empty list of artefacts, rather than ending the run."""
        self.assertEqual(pom_xml.artefacts(mock_path("<project><broken>")), [])


class DependenciesTest(unittest.TestCase):
    """Unit tests for the dependencies a pom declares."""

    @kills(
        Mutation(
            pom_xml._reference,
            "    if artifact is None or version is None:\n        return None\n",
            "",
            "an element missing a part takes the whole pom's reading down with it",
            raises="AttributeError: 'NoneType' object has no attribute 'text'",
        ),
        Mutation(
            pom_xml._reference,
            "if not group_name or not artifact_name:",
            "if not group_name:",
            "an empty artifact is read as the artefact `groupId:`, which Maven Central is then asked about",
        ),
    )
    def test_a_dependency_missing_a_part_maven_names_it_by(self):
        """Test that a dependency element missing a part is left out, and the pom is read all the same."""
        declared = pom_xml.dependencies(mock_path(_POM_WITH_INCOMPLETE_DEPENDENCIES)) or []
        self.assertEqual([reference.dependency for reference in declared], ["com.google.guava:guava"])

    def test_a_dependency_naming_the_projects_own_group(self):
        """Test that a dependency whose group names the project's own is reported under the group it resolves to."""
        declared = pom_xml.dependencies(mock_path(_POM_NAMING_ITS_OWN_GROUP)) or []
        self.assertEqual([reference.dependency for reference in declared], ["com.acme:api"])

    def test_a_dependency_resolves_a_property_its_own_profile_declares(self):
        """Test that a dependency in a profile resolves the property it names, among the several that profile holds."""
        declared = pom_xml.dependencies(mock_path(_POM_WITH_A_PROFILE_ONLY_PROPERTY)) or []
        versions = [(reference.current_version, reference.location.line_number) for reference in declared]
        self.assertEqual(versions, [("3.0", 6)])

    def test_a_property_a_profile_declares_does_not_override_the_projects_own(self):
        """Test that a dependency resolves to the project's own property, whatever value a profile gives that name."""
        declared = pom_xml.dependencies(mock_path(_POM_WITH_A_PROFILE)) or []
        versions = [(reference.current_version, reference.location.line_number) for reference in declared]
        self.assertEqual(versions, [("6.1.0", 3)])


def _resolved(pom: str, effective_pom: str) -> list[tuple[str, str, int | None]]:
    """Return the name, the resolved version, and the line of each dependency the pom declares."""
    declared = pom_xml.dependencies(mock_path(pom), xml.parse(effective_pom.encode())) or []
    return [(reference.dependency, reference.current_version, reference.location.line_number) for reference in declared]


class ResolvedDependenciesTest(unittest.TestCase):
    """Unit tests for the versions Maven's effective pom gives the dependencies a pom declares."""

    @kills(
        Mutation(
            pom_xml._effective_versions,
            "declared_by == scanned and artifact",
            "artifact",
            "a dependency takes the version of whatever the parent declares on the same line of its own pom",
        )
    )
    def test_an_entry_the_parent_declares_lends_its_version_to_no_declaration(self):
        """Test that an effective pom entry located in the parent leaves the declaration on the same line alone."""
        pom = pom_declaring(managed=dependency_management_element(guava_element("${guava.version}")))
        guava = effective_dependency_element("com.google.guava", "guava", "33.0.0-jre", line=6)
        parent = "org.example:parent:1.0"
        inherited = effective_dependency_element("com.google.guava", "guava", "32.1.0-jre", line=6, declared_by=parent)
        # The managed section comes before `<dependencies>`, so the inherited guava is read after the managed one.
        effective_pom = effective_pom_declaring(inherited, managed=dependency_management_element(guava))
        self.assertEqual(_resolved(pom, effective_pom), [("com.google.guava:guava", "33.0.0-jre", 7)])

    @kills(
        Mutation(
            pom_xml._reference,
            "effective_versions.get(_ArtifactDeclaration(artifact_name, artifact.line), versioned_by.text)",
            "{key.artifact: text for key, text in effective_versions.items()}.get(artifact_name, versioned_by.text)",
            "two declarations of one artefact share a version, so OSV is asked about one of them at the other's",
        )
    )
    def test_each_declaration_of_a_name_takes_the_version_the_effective_pom_gives_it(self):
        """Test that two declarations of one artefact, each versioned by a parent's property, keep their own version."""
        managed = dependency_management_element(guava_element("${guava.managed.version}"))
        pom = pom_declaring(guava_element("${guava.version}"), managed=managed)
        managed_guava = effective_dependency_element("com.google.guava", "guava", "33.0.0-jre", line=6)
        guava = effective_dependency_element("com.google.guava", "guava", "32.1.0-jre", line=14)
        effective_pom = effective_pom_declaring(guava, managed=dependency_management_element(managed_guava))
        expected = [("com.google.guava:guava", "33.0.0-jre", 7), ("com.google.guava:guava", "32.1.0-jre", 15)]
        self.assertEqual(_resolved(pom, effective_pom), expected)

    @kills(
        Mutation(
            pom_xml._reference,
            "effective_versions.get(_ArtifactDeclaration(artifact_name, artifact.line), versioned_by.text)",
            "{key.line: text for key, text in effective_versions.items()}.get(artifact.line, versioned_by.text)",
            "a dependency takes the version of whichever dependency is declared last on its line",
        )
    )
    def test_dependencies_declared_on_one_line_each_take_their_own_version(self):
        """Test that two dependencies on one line each take the version the effective pom gives that artefact."""
        one_line = dependency_element("commons-io", "commons-io", "${commons.version}") + dependency_element(
            "junit", "junit", "4.12"
        )
        pom = pom_declaring(one_line.replace("\n", "").replace("  ", "") + "\n")
        # Maven's input location names the line and does not name the column, so both entries are located on line 3.
        commons_io = effective_dependency_element("commons-io", "commons-io", "2.11.0", line=3)
        junit = effective_dependency_element("junit", "junit", "4.12", line=3)
        expected = [("commons-io:commons-io", "2.11.0", 3), ("junit:junit", "4.12", 3)]
        self.assertEqual(_resolved(pom, effective_pom_declaring(commons_io, junit)), expected)

    @kills(
        Mutation(
            pom_xml._input_location,
            '("", 0) if input_location is None else (input_location["pom"], int(input_location["line"]))',
            '(input_location["pom"], int(input_location["line"]))',
            "an entry Maven adds from its own model ends the run, since its input location lacks a line",
            raises="TypeError: 'NoneType' object is not subscriptable",
        )
    )
    def test_an_entry_whose_input_location_lacks_a_line_is_skipped(self):
        """Test that an entry whose input location lacks a line is skipped, and the entries beside it are read."""
        guava = effective_dependency_element("com.google.guava", "guava", "33.0.0-jre", line=5)
        # Maven locates what it adds itself in its own model rather than on a line of a pom.
        bindings = "org.apache.maven:maven-core:3.9.16:default-lifecycle-bindings"
        unlocated = dependency_element("org.example", "injected", "1.0").replace(
            "<artifactId>injected</artifactId>", f"<artifactId>injected</artifactId>  <!-- {bindings} -->"
        )
        pom = pom_declaring(guava_element("${guava.version}"))
        self.assertEqual(
            _resolved(pom, effective_pom_declaring(guava, unlocated)), [("com.google.guava:guava", "33.0.0-jre", 6)]
        )
