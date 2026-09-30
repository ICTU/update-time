"""Unit tests for reading a pom.xml, with file I/O mocked."""

import unittest

from update_time.formats import xml
from update_time.manifests import pom_xml

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time.helpers import (
    EFFECTIVE_GUAVA,
    GUAVA,
    PARENT_POM_ID,
    SCANNED_POM_ID,
    SUREFIRE,
    build_element,
    dependency_element,
    dependency_management_element,
    effective_dependency_element,
    effective_plugin_element,
    effective_pom_declaring,
    guava_element,
    plugin_element,
    pom_declaring,
    properties_element,
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
      <version>6.1.0</version>
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
            pom_xml._artefact_references,
            " or []",
            "",
            "an unparsable pom takes the reading of its artefacts down with it",
            raises="TypeError: 'NoneType' object is not iterable",
        )
    )
    def test_a_pom_that_does_not_parse(self):
        """Test that an unparsable pom yields an empty list of artefacts, rather than ending the run."""
        self.assertEqual(pom_xml.artefacts(mock_path("<project><broken>")), [])


class DeclarationsTest(unittest.TestCase):
    """Unit tests for the dependencies and plugins a pom declares."""

    @kills(
        Mutation(
            pom_xml._reference,
            "    if artifact is None:\n        return None\n",
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
    def test_a_dependency_is_read_unless_it_misses_its_group_or_artifact(self):
        """Test that a dependency missing its group or artifact is left out, and one missing its version is read."""
        declared = pom_xml.declarations(mock_path(_POM_WITH_INCOMPLETE_DEPENDENCIES)) or []
        expected = ["org.springframework:spring-web", GUAVA]
        self.assertEqual([reference.dependency for reference in declared], expected)

    @kills(
        Mutation(
            pom_xml._reference,
            "artifact_name = _interpolated(artifact.text, property_elements)",
            "artifact_name = _element_holding(artifact, property_elements).text",
            "an artifact naming a property in part is read with its `${…}` left unresolved",
        ),
        Mutation(
            pom_xml._reference,
            "_interpolated(group.text, property_elements)",
            "_element_holding(group, property_elements).text",
            "a group naming a property in part is read with its `${…}` left unresolved",
        ),
    )
    def test_coordinates_naming_a_property_the_pom_declares_resolve(self):
        """Test that a group or artifact naming a pom property, wholly or in part, reads as that property's value."""
        properties = properties_element({"scala.binary": "2.13", "akka.prefix": "com.typesafe"})
        cases = {
            "the project's own group": (_POM_NAMING_ITS_OWN_GROUP, "com.acme:api"),
            "part of an artifact": (
                pom_declaring(
                    dependency_element("com.typesafe", "akka-actor_${scala.binary}", "2.6.0"), properties=properties
                ),
                "com.typesafe:akka-actor_2.13",
            ),
            "part of a group": (
                pom_declaring(dependency_element("${akka.prefix}.akka", "akka-actor", "2.6.0"), properties=properties),
                "com.typesafe.akka:akka-actor",
            ),
        }
        for case, (pom, expected) in cases.items():
            with self.subTest(case=case):
                declared = pom_xml.declarations(mock_path(pom)) or []
                self.assertEqual([reference.dependency for reference in declared], [expected])

    @kills(
        Mutation(
            pom_xml._reference,
            'own_version = "" if version is None else _interpolated(version.text, property_elements)',
            'own_version = "" if version is None else versioned_by.text',
            "a version naming a property in part is read with its `${…}` left unresolved",
        )
    )
    def test_a_version_naming_a_property_in_part_reads_as_that_propertys_value(self):
        """Test that a version naming a pom property in part reads as that property's value, at its own element."""
        spring = dependency_element("org.springframework", "spring-core", "${spring.major}.1.0")
        pom = pom_declaring(spring, properties=properties_element({"spring.major": "6"}))
        declared = pom_xml.declarations(mock_path(pom)) or []
        versions = [(reference.current_version, reference.location.line_number) for reference in declared]
        self.assertEqual(versions, [("6.1.0", 9)])

    def test_a_dependency_resolves_a_property_its_own_profile_declares(self):
        """Test that a dependency in a profile resolves the property it names, among the several that profile holds."""
        declared = pom_xml.declarations(mock_path(_POM_WITH_A_PROFILE_ONLY_PROPERTY)) or []
        versions = [(reference.current_version, reference.location.line_number) for reference in declared]
        self.assertEqual(versions, [("3.0", 6)])

    def test_a_property_a_profile_declares_does_not_override_the_projects_own(self):
        """Test that a dependency resolves to the project's own property, whatever value a profile gives that name."""
        declared = pom_xml.declarations(mock_path(_POM_WITH_A_PROFILE)) or []
        versions = [(reference.current_version, reference.location.line_number) for reference in declared]
        self.assertEqual(versions, [("6.1.0", 3)])


def _resolved(pom: str, effective_pom: str) -> list[tuple[str, str, int | None]]:
    """Return the name, the resolved version, and the line of each artefact that reading the pom returns."""
    declared = pom_xml.declarations(mock_path(pom), xml.parse(effective_pom.encode())) or []
    return [(reference.dependency, reference.current_version, reference.location.line_number) for reference in declared]


class ResolvedDependenciesTest(unittest.TestCase):
    """Unit tests for the coordinates and the versions Maven's effective pom gives the artefacts a pom declares."""

    @kills(
        Mutation(
            pom_xml._effective_artefacts,
            "declared_by == scanned and artifact",
            "artifact",
            "a dependency takes the version of whatever the parent declares on the same line of its own pom",
        )
    )
    def test_an_entry_the_parent_declares_lends_its_version_to_no_declaration(self):
        """Test that an effective pom entry located in the parent leaves the declaration on the same line alone."""
        pom = pom_declaring(managed=dependency_management_element(guava_element("${guava.version}")))
        guava = effective_dependency_element(GUAVA, "33.0.0-jre", line=6)
        inherited = effective_dependency_element(GUAVA, "32.1.0-jre", line=6, declared_by=PARENT_POM_ID)
        # The managed section comes before `<dependencies>`, so the inherited guava is read after the managed one.
        effective_pom = effective_pom_declaring(inherited, managed=dependency_management_element(guava))
        self.assertEqual(_resolved(pom, effective_pom), [(GUAVA, "33.0.0-jre", 7)])

    @kills(
        Mutation(
            pom_xml._EffectiveArtefacts.get,
            "self.declared.get(_ArtifactAtLine(artifact_name, artifact.line))",
            "{key.artifact: pinned for key, pinned in self.declared.items()}.get(artifact_name)",
            "two declarations of one artefact share a version, so OSV is asked about one of them at the other's",
        )
    )
    def test_each_declaration_of_a_name_takes_the_version_the_effective_pom_gives_it(self):
        """Test that two declarations of one artefact, each versioned by a parent's property, keep their own version."""
        managed = dependency_management_element(guava_element("${guava.managed.version}"))
        pom = pom_declaring(guava_element("${guava.version}"), managed=managed)
        managed_guava = effective_dependency_element(GUAVA, "33.0.0-jre", line=6)
        guava = effective_dependency_element(GUAVA, "32.1.0-jre", line=14)
        effective_pom = effective_pom_declaring(guava, managed=dependency_management_element(managed_guava))
        expected = [(GUAVA, "33.0.0-jre", 7), (GUAVA, "32.1.0-jre", 15)]
        self.assertEqual(_resolved(pom, effective_pom), expected)

    @kills(
        Mutation(
            pom_xml._EffectiveArtefacts.get,
            "self.declared.get(_ArtifactAtLine(artifact_name, artifact.line))",
            "{key.line: pinned for key, pinned in self.declared.items()}.get(artifact.line)",
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
        commons_io = effective_dependency_element("commons-io:commons-io", "2.11.0", line=3)
        junit = effective_dependency_element("junit:junit", "4.12", line=3)
        expected = [("commons-io:commons-io", "2.11.0", 3), ("junit:junit", "4.12", 3)]
        self.assertEqual(_resolved(pom, effective_pom_declaring(commons_io, junit)), expected)

    @kills(
        Mutation(
            pom_xml._EffectiveArtefacts.get,
            "_interpolated(artifact.text, self.property_elements)",
            "_element_holding(artifact, self.property_elements).text",
            "an artifact holding a parent's property inside its name keeps the property, so it goes unchecked",
        )
    )
    def test_an_artifact_holding_a_parent_property_in_its_name_is_matched_to_its_effective_entry(self):
        """Test that an artifact spelling part of its name as a parent's property takes the effective pom's name."""
        pom = pom_declaring(dependency_element("org.apache.spark", "spark-core_${scala.binary.version}", "3.5.0"))
        spark = effective_dependency_element("org.apache.spark:spark-core_2.13", "3.5.0", line=5)
        properties = properties_element({"scala.binary.version": "2.13"})
        expected = [("org.apache.spark:spark-core_2.13", "3.5.0", 6)]
        self.assertEqual(_resolved(pom, effective_pom_declaring(spark, properties=properties)), expected)

    @kills(
        Mutation(
            pom_xml._effective_artefacts,
            " if managed_by == scanned else None",
            "",
            "a line of the pom lends its version to a dependency the parent versions, as their line numbers match",
        )
    )
    def test_a_version_the_parent_manages_is_not_read_from_the_poms_line_of_that_number(self):
        """Test that a dependency without a version takes the effective pom's version where the parent manages it."""
        pom = pom_declaring(guava_element(None), guava_element("32.0.0-jre"))
        # The parent manages guava's version on line 10, where the scanned pom declares guava a second time.
        guava = effective_dependency_element(GUAVA, "33.0.0-jre", line=5, managed_at=(PARENT_POM_ID, 10))
        second_guava = effective_dependency_element(GUAVA, "32.0.0-jre", line=9)
        expected = [(GUAVA, "33.0.0-jre", 3), (GUAVA, "32.0.0-jre", 10)]
        self.assertEqual(_resolved(pom, effective_pom_declaring(guava, second_guava)), expected)

    @kills(
        Mutation(
            pom_xml._effective_artefacts,
            "_own_versions(project, effective_property_elements)",
            "_own_versions(project, property_elements)",
            "a managed artifact naming the parent's property keeps the version Maven moved away from",
        ),
        Mutation(
            pom_xml._own_versions,
            "_interpolated(artifact.text, property_elements)",
            "_element_holding(artifact, property_elements).text",
            "a managed artifact holding a parent's property inside its name keeps the version Maven moved away from",
        ),
    )
    def test_a_version_the_pom_manages_under_a_parent_property_is_read_from_the_pom(self):
        """Test that a managed declaration whose artifact names a parent's property lends the version it holds."""
        properties = properties_element({"guava.artifact": "guava", "guava.flavour": "jre"})
        cases = {
            "a parent's artifact": ("${guava.artifact}", "guava"),
            "a parent's property inside the artifact": ("guava-${guava.flavour}", "guava-jre"),
        }
        for case, (declared, artifact) in cases.items():
            with self.subTest(case=case):
                # The effective pom holds the model from before the run, which the managed declaration has moved from.
                artefact = f"com.google.guava:{artifact}"
                managed = effective_dependency_element(artefact, "33.0.0-jre", line=6)
                guava = effective_dependency_element(artefact, "33.0.0-jre", line=14, managed_at=(SCANNED_POM_ID, 7))
                effective_pom = effective_pom_declaring(
                    guava, properties=properties, managed=dependency_management_element(managed)
                )
                managed_guava = dependency_element("com.google.guava", declared, "33.7.1-jre")
                pom = pom_declaring(
                    dependency_element("com.google.guava", artifact, None),
                    managed=dependency_management_element(managed_guava),
                )
                expected = [(artefact, "33.7.1-jre", 7), (artefact, "33.7.1-jre", 12)]
                self.assertEqual(_resolved(pom, effective_pom), expected)

    @kills(
        Mutation(
            pom_xml._own_versions,
            "for tag in _ARTEFACT_ELEMENTS",
            'for tag in ("dependency",)',
            "a plugin is checked at the version its property held before the run, which Maven updated away from",
        )
    )
    def test_a_version_the_pom_holds_for_a_plugin_is_read_from_the_pom(self):
        """Test that a plugin takes the version its own pom holds, in the plugin or in its `<pluginManagement>`."""
        properties = properties_element({"surefire.version": "3.5.2"})
        versioned = plugin_element("maven-surefire-plugin", "${surefire.version}")
        unversioned = plugin_element("maven-surefire-plugin", None)
        # The effective pom holds the version from before the run, which the property has moved from. The pom
        # manages surefire's version on line 13, and declares surefire without a version on line 18.
        effective_surefire = effective_plugin_element(SUREFIRE, "3.2.5", line=11)
        effective_managed = effective_plugin_element(SUREFIRE, "3.2.5", line=12)
        effective_unversioned = effective_plugin_element(SUREFIRE, "3.2.5", line=20, managed_at=(SCANNED_POM_ID, 13))
        cases = {
            "its own version": (build_element(versioned), build_element(effective_surefire), [(SUREFIRE, "3.5.2", 3)]),
            "a managed version": (
                build_element(unversioned, managed=versioned),
                build_element(effective_unversioned, managed=effective_managed),
                [(SUREFIRE, "3.5.2", 3), (SUREFIRE, "3.5.2", 18)],
            ),
        }
        for case, (build, effective_build, expected) in cases.items():
            with self.subTest(case=case):
                pom = pom_declaring(properties=properties, build=build)
                effective_pom = effective_pom_declaring(build=effective_build)
                self.assertEqual(_resolved(pom, effective_pom), expected)

    @kills(
        Mutation(
            pom_xml._version_as_left,
            "_interpolated(own_element.text, property_elements)",
            "own_element.text",
            "Update-time checks the version Maven moved away from, where the pom manages it through a property",
        ),
        Mutation(
            pom_xml._version_as_left,
            "_interpolated(own_element.text, property_elements)",
            "_element_holding(own_element, property_elements).text",
            "Update-time checks the version Maven moved away from, where the managed version names a property in part",
        ),
    )
    def test_a_version_the_pom_manages_through_its_own_property_is_read_from_that_property(self):
        """Test that a dependency without a version takes the value its own pom manages it with, through a property."""
        effective_managed = effective_dependency_element(GUAVA, "33.0.0-jre", line=9)
        guava = effective_dependency_element(GUAVA, "33.0.0-jre", line=17, managed_at=(SCANNED_POM_ID, 10))
        effective_pom = effective_pom_declaring(guava, managed=dependency_management_element(effective_managed))
        # Each case names the property, the managed version naming it, and the line the managed version is read at.
        cases = {
            "the whole version": ({"guava.version": "33.7.1-jre"}, "${guava.version}", 3),
            "part of the version": ({"guava.major": "33"}, "${guava.major}.7.1-jre", 10),
        }
        for case, (values, version, line) in cases.items():
            with self.subTest(case=case):
                managed = dependency_management_element(guava_element(version))
                pom = pom_declaring(guava_element(None), properties=properties_element(values), managed=managed)
                expected = [
                    (GUAVA, "33.7.1-jre", line),
                    (GUAVA, "33.7.1-jre", 15),
                ]
                self.assertEqual(_resolved(pom, effective_pom), expected)

    @kills(
        Mutation(
            pom_xml._effective_artefacts,
            "own_versions.get(managed_at) if",
            "{key.line: element for key, element in own_versions.items()}.get(managed_line) if",
            "a dependency takes the version of whichever dependency its own pom manages last on the same line",
        )
    )
    def test_a_version_the_pom_manages_on_a_shared_line_is_read_from_its_own_declaration(self):
        """Test that a dependency without a version takes its own managed version, not another on the same line."""
        one_line = guava_element("33.7.1-jre") + dependency_element("junit", "junit", "4.13.2")
        managed = dependency_management_element(one_line.replace("\n", "").replace("  ", "") + "\n")
        pom = pom_declaring(guava_element(None), managed=managed)
        # Maven locates each `<version>` on the shared line 4, and does not name a column to tell them apart.
        managed_at = (SCANNED_POM_ID, 4)
        effective_guava = effective_dependency_element(GUAVA, "33.0.0-jre", line=4, managed_at=managed_at)
        effective_junit = effective_dependency_element("junit:junit", "4.13.2", line=4, managed_at=managed_at)
        guava = effective_dependency_element(GUAVA, "33.0.0-jre", line=10, managed_at=managed_at)
        effective_managed = dependency_management_element(effective_guava + effective_junit)
        expected = [
            (GUAVA, "33.7.1-jre", 4),
            ("junit:junit", "4.13.2", 4),
            (GUAVA, "33.7.1-jre", 8),
        ]
        self.assertEqual(_resolved(pom, effective_pom_declaring(guava, managed=effective_managed)), expected)

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
        guava = EFFECTIVE_GUAVA
        # Maven locates what it adds itself in its own model rather than on a line of a pom.
        bindings = "org.apache.maven:maven-core:3.9.16:default-lifecycle-bindings"
        unlocated = dependency_element("org.example", "injected", "1.0").replace(
            "<artifactId>injected</artifactId>", f"<artifactId>injected</artifactId>  <!-- {bindings} -->"
        )
        pom = pom_declaring(guava_element("${guava.version}"))
        self.assertEqual(_resolved(pom, effective_pom_declaring(guava, unlocated)), [(GUAVA, "33.0.0-jre", 6)])
