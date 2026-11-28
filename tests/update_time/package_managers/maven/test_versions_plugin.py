"""Unit tests for the version patterns and the rule sets of the versions plugin."""

import re
import unittest

from mutation_time.mutation import kills
from update_time.package_managers.maven import versions_plugin


class PreReleasePatternTest(unittest.TestCase):
    """Unit tests for the pattern of versions no goal may adopt."""

    @kills.delete(
        versions_plugin,
        "milestone|",
        "a milestone spelled out is adopted as if it were a release",
        case="8-milestone-1",
    )
    @kills.delete(
        versions_plugin,
        "|[ab][0-9]",
        "an alpha or beta spelled as Maven's alias is adopted as if it were a release",
        case="8.a1",
    )
    @kills.delete(
        versions_plugin, "alpha|", "an alpha spelled out is adopted as if it were a release", case="8-alpha-1"
    )
    @kills.delete(versions_plugin, "beta|", "a beta spelled out is adopted as if it were a release", case="8-beta-1")
    @kills.delete(versions_plugin, "rc|", "a release candidate is adopted as if it were a release", case="8-rc1")
    @kills.delete(versions_plugin, "cr|", "a candidate release is adopted as if it were a release", case="8-cr1")
    @kills.delete(
        versions_plugin, "m|", "a milestone spelled as Maven's alias is adopted as if it were a release", case="8-M1"
    )
    @kills.delete(versions_plugin, "pre|", "a pre-release is adopted as if it were a release", case="8-pre1")
    @kills.delete(versions_plugin, "preview|", "a preview is adopted as if it were a release", case="8-preview")
    @kills.replace(
        versions_plugin,
        'rf"[-.](alpha',
        'rf"-(alpha',
        "a pre-release whose qualifier follows a dot is adopted as if it were a release",
        case="8.a1",
    )
    @kills.replace(
        versions_plugin,
        "[-.]?({_NUMBER})?",
        "[-.]?([0-9]+)?",
        "a pre-release whose qualifier holds a dotted number, as a build's timestamp does, is adopted as a release",
        case="2.4.0-b180830.0359",
    )
    def test_the_pattern_matches_each_pre_release_spelling(self):
        """Test that the pattern matches a pre-release in each spelling, Maven's aliases included."""
        pre_releases = (
            "8-alpha-1",
            "8.a1",
            "8-beta-1",
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
                self.assertIsNotNone(re.fullmatch(versions_plugin._PRE_RELEASES, version))

    @kills.replace(
        versions_plugin,
        "[ab][0-9])",
        "[ab])",
        "an `a` or `b` that Maven reads as an unknown qualifier, and sorts above the release, is held back",
    )
    def test_the_pattern_leaves_an_unknown_qualifier_alone(self):
        """Test that the pattern leaves alone an `a` without a number, which Maven reads as an unknown qualifier."""
        self.assertIsNone(re.fullmatch(versions_plugin._PRE_RELEASES, "8-a"))


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
                self.assertIsNotNone(re.fullmatch(versions_plugin._other_variants(current), version))

    @kills.replace(
        versions_plugin,
        "(?i:",
        "(?:",
        "a release qualifier spelled in capitals counts as a variant",
        case="5.2.9.RELEASE to 5.3.0",
    )
    @kills.delete(
        versions_plugin,
        "ga|",
        "a dependency on a plain release ignores a release qualified as GA",
        case="1.18.10 to 1.18.14-GA",
    )
    @kills.delete(
        versions_plugin,
        "|final",
        "a dependency on a plain release ignores a release qualified as Final",
        case="5.1.1.SP2 to 7.0.0.Final",
    )
    @kills.delete(
        versions_plugin,
        "|release",
        "a dependency on a release qualified as RELEASE ignores the plain releases",
        case="5.2.9.RELEASE to 5.3.0",
    )
    @kills.replace(
        versions_plugin,
        '"v[0-9]{8}',
        '"[0-9]{8}',
        "a dependency on a release dated after a v holds back every update",
        case="3.10.0.v20140604-1726 to 3.11.0.v20150212-0800",
    )
    @kills.delete(
        versions_plugin,
        "(-[0-9]{4})?",
        "a dependency on a release dated to the minute holds back every update",
        case="3.10.0.v20140604-1726 to 3.11.0.v20150212-0800",
    )
    @kills.delete(
        versions_plugin,
        "|sp[0-9]*",
        "a dependency on a service pack holds back every later release",
        case="5.1.1.SP2 to 7.0.0.Final",
    )
    @kills.delete(
        versions_plugin,
        "|-[0-9]+",
        "a dependency on a revision holds back every release but that revision",
        case="3.3.7-1 to 5.3.8",
    )
    @kills.delete(
        versions_plugin._variant_pattern,
        r"({_PRE_RELEASE_QUALIFIER}(?![a-z]))?",
        "a dependency on a pre-release never moves to its release",
        case="2.0.0-rc1 to 2.0.0",
    )
    @kills.replace(
        versions_plugin._variant_pattern,
        "(?![a-z]))?",
        ".*)?",
        "a dependency on a pre-release of a variant moves to the plain release",
        case="4.0-mysql to 4.1-mysql",
    )
    @kills.delete(
        versions_plugin._variant_pattern,
        "(?![a-z])",
        "a variant starting with an m is read as a milestone",
        case="4.0-mysql to 4.1-mysql",
    )
    @kills.replace(
        versions_plugin._variant_pattern,
        "(?![a-z]))?",
        "(?=[-._]))?",
        "a dependency on a pre-release ending its version never moves to its release",
        case="2.0.0-rc1 to 2.0.0",
    )
    @kills.delete(
        versions_plugin._variant_pattern,
        ".removesuffix(_SNAPSHOT)",
        "a property on a snapshot never moves to a release",
        case="1.18.10-SNAPSHOT to 1.18.15",
    )
    def test_the_pattern_leaves_a_version_on_the_same_variant_alone(self):
        """Test that the pattern leaves a version on the current variant alone.

        A qualified release, such as `7.0.0.Final`, is on the plain variant. A pre-release and a snapshot are on the
        variant of their release.
        """
        for current, version in (
            ("1.18.10", "1.18.14-GA"),
            ("5.2.9.RELEASE", "5.3.0"),
            ("3.10.0.v20140604-1726", "3.11.0.v20150212-0800"),
            ("2.0.0-rc1", "2.0.0"),
            ("4.0-mysql", "4.1-mysql"),
            ("1.18.10-SNAPSHOT", "1.18.15"),
            ("5.1.1.SP2", "7.0.0.Final"),
            ("3.3.7-1", "5.3.8"),
        ):
            with self.subTest(f"{current} to {version}"):
                self.assertIsNone(re.fullmatch(versions_plugin._other_variants(current), version))

    @kills.replace(
        versions_plugin._other_variants,
        '"|".join(unique(map(_variant_pattern, versions)))',
        "_variant_pattern(versions[0])",
        "an artefact declared on two variants holds back every version on the second",
    )
    def test_the_pattern_for_two_variants_leaves_a_version_on_either_alone(self):
        """Test that the pattern for versions on two variants matches a version on neither, and leaves either alone."""
        pattern = versions_plugin._other_variants("33.0.0-jre", "33.0.0-android")
        self.assertIsNotNone(re.fullmatch(pattern, "33.1.0"))
        self.assertIsNone(re.fullmatch(pattern, "33.1.0-jre"))
        self.assertIsNone(re.fullmatch(pattern, "33.1.0-android"))

    @kills.replace(
        versions_plugin._other_variants,
        "if not all(",
        "if not any(",
        "a declaration without a number holds back what its artefact's other declaration does not keep",
    )
    def test_the_pattern_for_a_version_without_a_number_is_empty(self):
        """Test that the pattern is empty for a version that does not start with a number, and beside one that does."""
        self.assertEqual(versions_plugin._other_variants("v1.2.3"), "")
        self.assertEqual(versions_plugin._other_variants("v1.2.3", "1.2.3"), "")


class RuleSetFileTest(unittest.TestCase):
    """Unit tests for the file Update-time writes the rule set to."""

    def test_the_rule_set_is_readable_until_the_file_is_removed(self):
        """Test that the file holds the rule set while the context is open, and is removed once it closes."""
        rule_set = "<ruleset/>"
        with versions_plugin._rule_set_file(rule_set) as path:
            self.assertEqual(path.read_text(), rule_set)
        self.assertFalse(path.exists())
