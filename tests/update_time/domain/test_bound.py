"""Unit tests for the version bound."""

import unittest

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from update_time.domain.bound import NO_BOUND, Redundancy, UpdateLevel, Verb, VersionBound


class VersionBoundTest(unittest.TestCase):
    """Unit tests for the version bound."""

    def test_allow_keeps_only_matching(self):
        """Test that an allow bound keeps versions the specifier matches and drops the rest."""
        version_bound = VersionBound(Verb.ALLOW, SpecifierSet("<3.13"))
        self.assertTrue(version_bound.keeps(Version("3.12.9"), "3.12"))
        self.assertFalse(version_bound.keeps(Version("3.13"), "3.12"))

    def test_ignore_drops_matching(self):
        """Test that an ignore bound drops versions the specifier matches and keeps the rest."""
        version_bound = VersionBound(Verb.IGNORE, SpecifierSet(">=3.13"))
        self.assertFalse(version_bound.keeps(Version("3.13"), "3.12"))
        self.assertTrue(version_bound.keeps(Version("3.12.9"), "3.12"))

    def test_is_hashable(self):
        """Test that a bound is hashable, so it can thread through the cached source lookups."""
        self.assertIsInstance(hash(VersionBound(Verb.ALLOW, SpecifierSet("<3.13"))), int)

    def test_redundancy_no_effect(self):
        """Test that a bound admitting the current version and everything above it is flagged as having no effect."""
        self.assertEqual(Redundancy.NO_EFFECT, VersionBound(Verb.ALLOW, SpecifierSet(">=3.12")).redundancy("3.12"))

    def test_redundancy_blocks_all_via_ignore(self):
        """Test that an ignore bound dropping the current version and everything above it is flagged as blocking all."""
        self.assertEqual(Redundancy.BLOCKS_ALL, VersionBound(Verb.IGNORE, SpecifierSet(">=3.12")).redundancy("3.12"))

    def test_redundancy_blocks_all_via_ceiling_below_current(self):
        """Test that an allow ceiling below the current version blocks every update."""
        self.assertEqual(Redundancy.BLOCKS_ALL, VersionBound(Verb.ALLOW, SpecifierSet("<3.13")).redundancy("3.14"))

    def test_redundancy_live_ceiling(self):
        """Test that a ceiling above the current version is live (not redundant)."""
        self.assertIsNone(VersionBound(Verb.ALLOW, SpecifierSet("<3.13")).redundancy("3.12"))

    def test_redundancy_live_range_hole(self):
        """Test that an ignore range above the current version (a hole) is live, not redundant."""
        self.assertIsNone(VersionBound(Verb.IGNORE, SpecifierSet(">=3.13,<3.15")).redundancy("3.14"))

    def test_redundancy_allow_range_below_current_is_live(self):
        """Test that an allow range entirely above the current version is live: it admits updates into the range."""
        self.assertIsNone(VersionBound(Verb.ALLOW, SpecifierSet(">=3.13,<3.15")).redundancy("3.12"))

    def test_redundancy_allow_range_containing_current_is_live(self):
        """Test that an allow range containing the current version is live (it caps updates above the range)."""
        self.assertIsNone(VersionBound(Verb.ALLOW, SpecifierSet(">=3.13,<3.15")).redundancy("3.14"))

    def test_redundancy_allow_range_above_current_blocks_all(self):
        """Test that an allow range entirely below the current version blocks every update."""
        self.assertEqual(
            Redundancy.BLOCKS_ALL, VersionBound(Verb.ALLOW, SpecifierSet(">=3.13,<3.15")).redundancy("3.16")
        )

    def test_redundancy_star_range_below_current_is_live(self):
        """Test that an `==x.*` interval above the current version is live, not blocking."""
        self.assertIsNone(VersionBound(Verb.ALLOW, SpecifierSet("==3.12.*")).redundancy("3.11"))

    def test_redundancy_hard_ceiling_at_current_blocks_all(self):
        """Test that a hard `<=` ceiling at the current version blocks every update (nothing above it survives)."""
        self.assertEqual(Redundancy.BLOCKS_ALL, VersionBound(Verb.ALLOW, SpecifierSet("<=3.12")).redundancy("3.12"))

    def test_redundancy_with_post_release_current_version(self):
        """Test that a live bound on a pin with a post-release segment is classified without raising."""
        self.assertIsNone(VersionBound(Verb.ALLOW, SpecifierSet("<5")).redundancy("4.15.0.post1"))

    def test_redundancy_with_prerelease_boundary(self):
        """Test that a live bound whose specifier boundary is a pre-release is classified without raising."""
        self.assertIsNone(VersionBound(Verb.ALLOW, SpecifierSet(">=3.13rc1")).redundancy("3.12"))

    def test_redundancy_of_image_tag_with_suffix(self):
        """Test that a bound on an image tag with a variant suffix is classified against the tag's main version."""
        version_bound = VersionBound(Verb.ALLOW, SpecifierSet("<3.13"))
        self.assertEqual(Redundancy.BLOCKS_ALL, version_bound.redundancy("3.14.1-bookworm-slim"))
        self.assertIsNone(version_bound.redundancy("3.12.1-bookworm-slim"))

    def test_redundancy_of_image_tag_with_label_prefix(self):
        """Test that a bound on an image tag with a label prefix is classified against the embedded version."""
        version_bound = VersionBound(Verb.ALLOW, SpecifierSet(">=3.12"))
        self.assertEqual(Redundancy.NO_EFFECT, version_bound.redundancy("python3.14"))

    def test_redundancy_invalid_current_version(self):
        """Test that an unparsable current version yields no redundancy verdict rather than raising."""
        self.assertIsNone(VersionBound(Verb.ALLOW, SpecifierSet("<3.13")).redundancy("not-a-version"))

    def test_redundancy_of_no_bound(self):
        """Test that the keep-all NO_BOUND (an empty specifier) trivially never has an effect."""
        self.assertEqual(Redundancy.NO_EFFECT, NO_BOUND.redundancy("3.12"))

    def test_redundancy_with_non_pep440_boundary(self):
        """Test that a clause whose version isn't PEP 440 (arbitrary equality) is handled without raising."""
        self.assertEqual(Redundancy.BLOCKS_ALL, VersionBound(Verb.ALLOW, SpecifierSet("===foobar")).redundancy("1.0"))


class LevelBoundTest(unittest.TestCase):
    """Unit tests for the level-based version bounds."""

    def test_ignore_keeps_the_line_at_the_named_level(self):
        """Test that `ignore[<level>-update]` keeps the current line at that level and drops the rest.

        The two minor-level cases anchor at different versions, pinning that the bound ratchets along as the
        reference advances.
        """
        cases = (
            (UpdateLevel.MAJOR, "3.12.1", "3.99.0", "4.0.0"),
            (UpdateLevel.MINOR, "3.12.1", "3.12.9", "3.13.0"),
            (UpdateLevel.MINOR, "3.13.0", "3.13.1", "3.14.0"),
            (UpdateLevel.PATCH, "3.12.1", "3.12.1", "3.12.2"),
        )
        for level, current_version, kept, dropped in cases:
            with self.subTest(level=level, current_version=current_version):
                version_bound = VersionBound(Verb.IGNORE, level=level)
                self.assertTrue(version_bound.keeps(Version(kept), current_version))
                self.assertFalse(version_bound.keeps(Version(dropped), current_version))

    def test_allow_minor_is_the_complement_of_ignore_major(self):
        """Test that `allow[minor-update]` keeps exactly what `ignore[major-update]` keeps."""
        allow_minor = VersionBound(Verb.ALLOW, level=UpdateLevel.MINOR)
        ignore_major = VersionBound(Verb.IGNORE, level=UpdateLevel.MAJOR)
        for candidate in ("3.12.9", "3.99.0", "4.0.0"):
            with self.subTest(candidate=candidate):
                self.assertEqual(
                    allow_minor.keeps(Version(candidate), "3.12.1"),
                    ignore_major.keeps(Version(candidate), "3.12.1"),
                )

    def test_allow_major_keeps_every_update(self):
        """Test that `allow[major-update]` keeps every update (`redundancy` flags it as having no effect)."""
        self.assertTrue(VersionBound(Verb.ALLOW, level=UpdateLevel.MAJOR).keeps(Version("99.0"), "3.12.1"))

    def test_missing_components_count_as_zero(self):
        """Test that a component the current version is missing counts as zero, as version comparison pads it."""
        version_bound = VersionBound(Verb.IGNORE, level=UpdateLevel.MINOR)
        self.assertTrue(version_bound.keeps(Version("22.0.9"), "22"))
        self.assertFalse(version_bound.keeps(Version("22.1"), "22"))

    def test_anchors_to_the_main_version_of_an_image_tag(self):
        """Test that an image tag with a label prefix or variant suffix is anchored by its main version component."""
        version_bound = VersionBound(Verb.IGNORE, level=UpdateLevel.MINOR)
        self.assertTrue(version_bound.keeps(Version("3.14.9"), "3.14.6-alpine3.23"))
        self.assertFalse(version_bound.keeps(Version("3.15.0"), "3.14.6-alpine3.23"))
        self.assertTrue(version_bound.keeps(Version("3.12.9"), "python3.12-slim"))
        self.assertFalse(version_bound.keeps(Version("3.13.0"), "python3.12-slim"))

    def test_epoch_is_kept_in_the_anchor(self):
        """Test that a current version with an epoch anchors the bound within that epoch."""
        version_bound = VersionBound(Verb.IGNORE, level=UpdateLevel.MAJOR)
        self.assertTrue(version_bound.keeps(Version("1!2.9"), "1!2.3"))
        self.assertFalse(version_bound.keeps(Version("1!3.0"), "1!2.3"))

    def test_unparsable_current_version_keeps_every_update(self):
        """Test that a current version without a parsable version can't anchor the bound, leaving updates unbounded."""
        self.assertTrue(VersionBound(Verb.IGNORE, level=UpdateLevel.MINOR).keeps(Version("99.0"), "not-a-version"))

    def test_redundancy_of_level_bounds(self):
        """Test that a level bound is classified by its anchored equivalent (see the logger for the reporting)."""
        self.assertEqual(Redundancy.NO_EFFECT, VersionBound(Verb.ALLOW, level=UpdateLevel.MAJOR).redundancy("3.12"))
        self.assertEqual(Redundancy.BLOCKS_ALL, VersionBound(Verb.IGNORE, level=UpdateLevel.PATCH).redundancy("3.12.1"))
        self.assertIsNone(VersionBound(Verb.IGNORE, level=UpdateLevel.MINOR).redundancy("3.12"))
