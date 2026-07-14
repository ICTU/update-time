"""Unit tests for the version module."""

import unittest

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from update_time.domain.version import (
    NO_BOUND,
    DependencyVersion,
    Redundancy,
    VersionFilter,
    first_eligible,
    is_valid,
    parse_version_filter,
)


class IsValidTest(unittest.TestCase):
    """Unit tests for the is_valid version checker."""

    def test_is_valid(self):
        """Test that a valid version is reported as valid."""
        self.assertTrue(is_valid("1.0"))

    def test_is_invalid(self):
        """Test that a invalid version is reported as invalid."""
        self.assertFalse(is_valid("nope-1.0"))

    def test_v_prefix_is_allowed(self):
        """Test that a version with a v-prefix is valid."""
        self.assertTrue(is_valid("v1.0"))


class FirstEligibleTest(unittest.TestCase):
    """Unit tests for the first_eligible walk."""

    def test_no_candidates(self):
        """Test that the current version is returned unchanged when there are no candidates."""
        self.assertEqual(DependencyVersion(version="1.0"), first_eligible([], DependencyVersion, "1.0"))

    def test_first_candidate_eligible(self):
        """Test that the first eligible candidate is returned."""
        self.assertEqual(DependencyVersion("3.0"), first_eligible(["3.0", "2.0"], DependencyVersion, "1.0"))

    def test_skips_ineligible_candidates(self):
        """Test that candidates that resolve to None are skipped in favour of the next one."""

        def resolve(candidate: str) -> DependencyVersion | None:
            """Resolve only the "2.0" candidate, skipping the rest."""
            return DependencyVersion(candidate) if candidate == "2.0" else None

        self.assertEqual(DependencyVersion("2.0"), first_eligible(["3.0", "2.0"], resolve, "1.0"))

    def test_no_eligible_candidate(self):
        """Test that the current version is returned unchanged when no candidate is eligible."""
        self.assertEqual(DependencyVersion(version="1.0"), first_eligible(["3.0", "2.0"], lambda _: None, "1.0"))


class ParseVersionFilterTest(unittest.TestCase):
    """Unit tests for parsing a specifier into a version filter."""

    def test_valid_specifier(self):
        """Test that a valid specifier parses into a filter carrying the specifier and the allow flag."""
        self.assertEqual(VersionFilter(SpecifierSet("<3.13"), allow=True), parse_version_filter("<3.13", allow=True))

    def test_compound_specifier(self):
        """Test that a compound specifier is accepted."""
        self.assertEqual(
            VersionFilter(SpecifierSet(">=3.10,<3.13"), allow=True), parse_version_filter(">=3.10,<3.13", allow=True)
        )

    def test_invalid_specifier(self):
        """Test that an unparsable specifier returns None rather than raising."""
        self.assertIsNone(parse_version_filter("not-a-specifier", allow=True))


class VersionFilterTest(unittest.TestCase):
    """Unit tests for the version filter bound."""

    def test_allow_keeps_only_matching(self):
        """Test that an allow filter keeps versions the specifier matches and drops the rest."""
        version_filter = VersionFilter(SpecifierSet("<3.13"), allow=True)
        self.assertTrue(version_filter.keeps(Version("3.12.9")))
        self.assertFalse(version_filter.keeps(Version("3.13")))

    def test_ignore_drops_matching(self):
        """Test that an ignore filter drops versions the specifier matches and keeps the rest."""
        version_filter = VersionFilter(SpecifierSet(">=3.13"), allow=False)
        self.assertFalse(version_filter.keeps(Version("3.13")))
        self.assertTrue(version_filter.keeps(Version("3.12.9")))

    def test_is_hashable(self):
        """Test that a filter is hashable, so it can thread through the cached source lookups."""
        self.assertIsInstance(hash(VersionFilter(SpecifierSet("<3.13"), allow=True)), int)

    def test_redundancy_no_effect(self):
        """Test that a bound admitting the current version and everything above it is flagged as having no effect."""
        self.assertEqual(Redundancy.NO_EFFECT, VersionFilter(SpecifierSet(">=3.12"), allow=True).redundancy("3.12"))

    def test_redundancy_blocks_all_via_ignore(self):
        """Test that an ignore bound dropping the current version and everything above it is flagged as blocking all."""
        self.assertEqual(Redundancy.BLOCKS_ALL, VersionFilter(SpecifierSet(">=3.12"), allow=False).redundancy("3.12"))

    def test_redundancy_blocks_all_via_ceiling_below_current(self):
        """Test that an allow ceiling below the current version blocks every update."""
        self.assertEqual(Redundancy.BLOCKS_ALL, VersionFilter(SpecifierSet("<3.13"), allow=True).redundancy("3.14"))

    def test_redundancy_live_ceiling(self):
        """Test that a ceiling above the current version is live (not redundant)."""
        self.assertIsNone(VersionFilter(SpecifierSet("<3.13"), allow=True).redundancy("3.12"))

    def test_redundancy_live_range_hole(self):
        """Test that an ignore range above the current version (a hole) is live, not redundant."""
        self.assertIsNone(VersionFilter(SpecifierSet(">=3.13,<3.15"), allow=False).redundancy("3.14"))

    def test_redundancy_allow_range_below_current_is_live(self):
        """Test that an allow range entirely above the current version is live: it admits updates into the range."""
        self.assertIsNone(VersionFilter(SpecifierSet(">=3.13,<3.15"), allow=True).redundancy("3.12"))

    def test_redundancy_allow_range_containing_current_is_live(self):
        """Test that an allow range containing the current version is live (it caps updates above the range)."""
        self.assertIsNone(VersionFilter(SpecifierSet(">=3.13,<3.15"), allow=True).redundancy("3.14"))

    def test_redundancy_allow_range_above_current_blocks_all(self):
        """Test that an allow range entirely below the current version blocks every update."""
        self.assertEqual(
            Redundancy.BLOCKS_ALL, VersionFilter(SpecifierSet(">=3.13,<3.15"), allow=True).redundancy("3.16")
        )

    def test_redundancy_star_range_below_current_is_live(self):
        """Test that an `==x.*` interval above the current version is live, not blocking."""
        self.assertIsNone(VersionFilter(SpecifierSet("==3.12.*"), allow=True).redundancy("3.11"))

    def test_redundancy_hard_ceiling_at_current_blocks_all(self):
        """Test that a hard `<=` ceiling at the current version blocks every update (nothing above it survives)."""
        self.assertEqual(Redundancy.BLOCKS_ALL, VersionFilter(SpecifierSet("<=3.12"), allow=True).redundancy("3.12"))

    def test_redundancy_with_post_release_current_version(self):
        """Test that a live bound on a pin with a post-release segment is classified without raising."""
        self.assertIsNone(VersionFilter(SpecifierSet("<5"), allow=True).redundancy("4.15.0.post1"))

    def test_redundancy_with_prerelease_boundary(self):
        """Test that a live bound whose specifier boundary is a pre-release is classified without raising."""
        self.assertIsNone(VersionFilter(SpecifierSet(">=3.13rc1"), allow=True).redundancy("3.12"))

    def test_redundancy_of_image_tag_with_suffix(self):
        """Test that a bound on an image tag with a variant suffix is classified against the tag's main version."""
        version_filter = VersionFilter(SpecifierSet("<3.13"), allow=True)
        self.assertEqual(Redundancy.BLOCKS_ALL, version_filter.redundancy("3.14.1-bookworm-slim"))
        self.assertIsNone(version_filter.redundancy("3.12.1-bookworm-slim"))

    def test_redundancy_of_image_tag_with_label_prefix(self):
        """Test that a bound on an image tag with a label prefix is classified against the embedded version."""
        version_filter = VersionFilter(SpecifierSet(">=3.12"), allow=True)
        self.assertEqual(Redundancy.NO_EFFECT, version_filter.redundancy("python3.14"))

    def test_redundancy_invalid_current_version(self):
        """Test that an unparsable current version yields no redundancy verdict rather than raising."""
        self.assertIsNone(VersionFilter(SpecifierSet("<3.13"), allow=True).redundancy("not-a-version"))

    def test_redundancy_of_no_bound_is_none(self):
        """Test that the keep-all NO_BOUND (an empty specifier) is not reported as a redundant bound."""
        self.assertIsNone(NO_BOUND.redundancy("3.12"))

    def test_redundancy_with_non_pep440_boundary(self):
        """Test that a clause whose version isn't PEP 440 (arbitrary equality) is handled without raising."""
        self.assertEqual(Redundancy.BLOCKS_ALL, VersionFilter(SpecifierSet("===foobar"), allow=True).redundancy("1.0"))
