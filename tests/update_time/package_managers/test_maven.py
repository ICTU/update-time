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
        patched = patch.object(maven.pom_xml_manifest, "properties", Mock(return_value={}))
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
            "2.4.0-b180830.0359",
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


class OtherVariantsPatternTest(unittest.TestCase):
    """Unit tests for the pattern of the versions on a variant none of the current versions is on."""

    def test_the_pattern_matches_a_version_on_another_variant(self):
        """Test that the pattern matches a version on another variant, on none, on a respelled one, or a pre-release."""
        for current, version in (
            ("1.18.10", "1.18.14-jdk5"),
            ("2.4-groovy-4.0", "2.5"),
            ("2.4-groovy-4.0", "2.5-groovy-6.0"),
            ("2.0.62.android8", "2.0.65-android8"),
            ("2.4-groovy-4.0", "2.5-M1-groovy-4.0"),
        ):
            with self.subTest(current=current, version=version):
                self.assertIsNotNone(re.fullmatch(maven._other_variants(current), version))

    @kills(
        Mutation(maven, "(?i:", "(?:", "a release qualifier spelled in capitals counts as a variant"),
        Mutation(maven, "ga|", "", "a dependency on a plain release ignores a release qualified as GA"),
        Mutation(maven, "|final", "", "a dependency on a plain release ignores a release qualified as Final"),
        Mutation(maven, "|release", "", "a dependency on a release qualified as RELEASE ignores the plain releases"),
        Mutation(maven, '"v[0-9]{8}', '"[0-9]{8}', "a dependency on a release dated after a v holds back every update"),
        Mutation(maven, "(-[0-9]{4})?", "", "a dependency on a release dated to the minute holds back every update"),
        Mutation(maven, "|sp[0-9]*", "", "a dependency on a service pack holds back every later release"),
        Mutation(maven, "|-[0-9]+)?", ")?", "a dependency on a revision holds back every release but that revision"),
        Mutation(
            maven._variant_pattern,
            r"({_PRE_RELEASE_QUALIFIER}(?![a-z]))?",
            "",
            "a dependency on a pre-release never moves to its release",
        ),
        Mutation(
            maven._variant_pattern,
            "(?![a-z]))?",
            ".*)?",
            "a dependency on a pre-release of a variant moves to the plain release",
        ),
        Mutation(
            maven._variant_pattern,
            "(?![a-z]))?",
            ")?",
            "a variant starting with an m is read as a milestone",
        ),
        Mutation(
            maven._variant_pattern,
            "(?![a-z]))?",
            "(?=[-._]))?",
            "a dependency on a pre-release ending its version never moves to its release",
        ),
        Mutation(
            maven._variant_pattern,
            "version.removesuffix(_SNAPSHOT)",
            "version",
            "a property on a snapshot never moves to a release",
        ),
    )
    def test_the_pattern_leaves_a_version_on_the_same_variant_alone(self):
        """Test that the pattern leaves a version on the current variant alone.

        A qualified release, such as `7.0.0.Final`, is on the plain variant. A pre-release and a snapshot are on the
        variant of their release.
        """
        for current, version in (
            ("1.18.10", "1.18.14"),
            ("2.4-groovy-4.0", "2.5-groovy-4.0"),
            ("1.18.10", "1.18.14.Final"),
            ("1.18.10", "1.18.14-GA"),
            ("5.2.9.RELEASE", "5.3.0"),
            ("9.4.58.v20250814", "9.4.59.v20251001"),
            ("3.10.0.v20140604-1726", "3.11.0.v20150212-0800"),
            ("2.0.0-rc1", "2.0.0"),
            ("2.4.0-b180830.0359", "2.4.0"),
            ("2.4-M1-groovy-4.0", "2.4-groovy-4.0"),
            ("4.0-mysql", "4.1-mysql"),
            ("1.18.10-SNAPSHOT", "1.18.15"),
            ("2.4-groovy-4.0-SNAPSHOT", "2.5-groovy-4.0"),
            ("5.1.1.SP2", "7.0.0.Final"),
            ("3.3.7-1", "5.3.8"),
        ):
            with self.subTest(current=current, version=version):
                self.assertIsNone(re.fullmatch(maven._other_variants(current), version))

    @kills(
        Mutation(
            maven._other_variants,
            '"|".join(unique(map(_variant_pattern, versions)))',
            "_variant_pattern(versions[0])",
            "an artefact declared on two variants holds back every version on the second",
        )
    )
    def test_the_pattern_for_two_variants_leaves_a_version_on_either_alone(self):
        """Test that the pattern for versions on two variants matches a version on neither, and leaves either alone."""
        pattern = maven._other_variants("33.0.0-jre", "33.0.0-android")
        self.assertIsNotNone(re.fullmatch(pattern, "33.1.0"))
        self.assertIsNone(re.fullmatch(pattern, "33.1.0-jre"))
        self.assertIsNone(re.fullmatch(pattern, "33.1.0-android"))

    @kills(
        Mutation(
            maven._other_variants,
            "if not all(",
            "if not any(",
            "a declaration without a number holds back what its artefact's other declaration does not keep",
        )
    )
    def test_the_pattern_for_a_version_without_a_number_is_empty(self):
        """Test that the pattern is empty for a version that does not start with a number, and beside one that does."""
        self.assertEqual(maven._other_variants("v1.2.3"), "")
        self.assertEqual(maven._other_variants("v1.2.3", "1.2.3"), "")


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
