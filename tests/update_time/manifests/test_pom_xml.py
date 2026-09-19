"""Unit tests for reading a pom.xml, with file I/O mocked."""

import unittest

from update_time.manifests import pom_xml

from tests.helpers import mock_path
from tests.mutation import Mutation, kills

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


_POM_WITH_AN_INCOMPLETE_DEPENDENCY = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <dependencies>
    <dependency>
      <artifactId>spring-core</artifactId>
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


class DependenciesTest(unittest.TestCase):
    """Unit tests for the dependencies a pom declares."""

    @kills(
        Mutation(
            pom_xml,
            "    if not group_name or artifact is None or version is None:\n        return None\n",
            "",
            "an element missing a part takes the whole pom's reading down with it",
            raises="AttributeError: 'NoneType' object has no attribute 'text'",
        )
    )
    def test_a_dependency_missing_a_part_maven_names_it_by(self):
        """Test that a dependency element missing a part is left out, and the pom is read all the same."""
        declared = pom_xml.dependencies(mock_path(_POM_WITH_AN_INCOMPLETE_DEPENDENCY)) or []
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
