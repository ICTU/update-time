"""Unit tests for running Maven over a pom.xml."""

import re
import unittest
from unittest.mock import Mock, patch

from update_time.package_managers import maven

from tests.mutation import Mutation, kills


class PluginVersionTest(unittest.TestCase):
    """Unit tests for the plugin releases the shipped pom declares."""

    def test_the_versions_the_shipped_pom_declares(self):
        """Test that each plugin's release is read from the pom Update-time ships beside the module."""
        for version_property in ("help.plugin.version", "versions.plugin.version"):
            with self.subTest(version_property=version_property):
                self.assertRegex(maven._declared_plugin_version(version_property), r"^\d+\.\d+\.\d+$")

    def test_a_shipped_pom_declaring_no_release(self):
        """Test that a pom declaring no release raises an error naming the file, rather than one about a lookup."""
        patched = patch.object(maven.pom_xml_format, "properties", Mock(return_value={}))
        with patched, self.assertRaises(RuntimeError) as error:
            maven._declared_plugin_version("help.plugin.version")
        self.assertIn("pom.xml", str(error.exception))


class PreReleasePatternTest(unittest.TestCase):
    """Unit tests for the pattern of versions no goal may adopt."""

    @kills(
        Mutation(maven, "milestone|", "", "a milestone spelled out is adopted as if it were a release"),
        Mutation(
            maven, "|[ab][0-9])", ")", "an alpha or beta spelled as Maven's alias is adopted as if it were a release"
        ),
    )
    def test_the_pattern_matches_each_pre_release_spelling(self):
        """Test that the pattern matches a pre-release in each spelling, Maven's aliases included."""
        pre_releases = (
            "8-alpha-1",
            "8-a1",
            "8.a1",
            "8-beta-1",
            "8-b1",
            "8-milestone-1",
            "8-M1",
            "8-rc1",
            "8-cr1",
            "8-pre1",
            "8-preview",
        )
        for version in pre_releases:
            with self.subTest(version=version):
                self.assertIsNotNone(re.fullmatch(maven._PRE_RELEASES, version))

    @kills(
        Mutation(
            maven,
            "[ab][0-9])",
            "[ab])",
            "an `a` or `b` that Maven reads as an unknown qualifier, and sorts above the release, is held back",
        ),
    )
    def test_the_pattern_leaves_releases_and_unknown_qualifiers_alone(self):
        """Test that the pattern leaves a release alone, and an alias letter Maven reads as an unknown qualifier."""
        for version in ("8.1", "8-a", "8-a.1"):
            with self.subTest(version=version):
                self.assertIsNone(re.fullmatch(maven._PRE_RELEASES, version))


class RuleSetFileTest(unittest.TestCase):
    """Unit tests for the file Update-time writes the rule set to."""

    def test_the_rule_set_is_readable_until_the_file_is_removed(self):
        """Test that the file holds the rule set while the context is open, and is removed once it closes."""
        rule_set = "<ruleset/>"
        with maven._rule_set_file(rule_set) as path:
            self.assertEqual(path.read_text(), rule_set)
        self.assertFalse(path.exists())


class EffectivePomFileTest(unittest.TestCase):
    """Unit tests for the file Maven writes the effective pom to."""

    def test_the_file_is_writable_until_it_is_removed(self):
        """Test that the file takes what Maven writes while the context is open, and is removed once it closes."""
        with maven._effective_pom_file() as path:
            path.write_text("<project/>")
            self.assertEqual(path.read_text(), "<project/>")
        self.assertFalse(path.exists())
