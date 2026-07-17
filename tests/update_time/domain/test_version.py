"""Unit tests for the version module."""

import unittest

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from update_time.domain.version import (
    NO_BOUND,
    DependencyVersion,
    Redundancy,
    UpdateLevel,
    Verb,
    VersionFilter,
    first_eligible,
    is_valid,
    parse_bound,
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


class ParseBoundTest(unittest.TestCase):
    """Unit tests for parsing an `update<specifier>` marker item into a version filter."""

    def test_valid_specifier(self):
        """Test that a valid specifier parses into a filter carrying the specifier and the verb."""
        self.assertEqual(VersionFilter(Verb.ALLOW, SpecifierSet("<3.13")), parse_bound(Verb.ALLOW, "update<3.13"))

    def test_compound_specifier(self):
        """Test that a compound specifier is accepted."""
        self.assertEqual(
            VersionFilter(Verb.ALLOW, SpecifierSet(">=3.10,<3.13")), parse_bound(Verb.ALLOW, "update>=3.10,<3.13")
        )

    def test_invalid_specifier_raises(self):
        """Test that an `update` bound with an unparsable specifier raises, distinct from a non-bound item's None.

        The marker parser relies on this distinction to report a malformed bound while treating an unrecognised
        item as not-a-bound; collapsing both to None would let a mistyped bound silently freeze a reference.
        """
        self.assertRaises(InvalidSpecifier, parse_bound, Verb.ALLOW, "update@@@")

    def test_item_that_is_no_bound(self):
        """Test that an item that is neither kind of bound returns None rather than raising."""
        self.assertIsNone(parse_bound(Verb.IGNORE, "stale"))


class VersionFilterTest(unittest.TestCase):
    """Unit tests for the version filter bound."""

    def test_allow_keeps_only_matching(self):
        """Test that an allow filter keeps versions the specifier matches and drops the rest."""
        version_filter = VersionFilter(Verb.ALLOW, SpecifierSet("<3.13"))
        self.assertTrue(version_filter.keeps(Version("3.12.9"), "3.12"))
        self.assertFalse(version_filter.keeps(Version("3.13"), "3.12"))

    def test_ignore_drops_matching(self):
        """Test that an ignore filter drops versions the specifier matches and keeps the rest."""
        version_filter = VersionFilter(Verb.IGNORE, SpecifierSet(">=3.13"))
        self.assertFalse(version_filter.keeps(Version("3.13"), "3.12"))
        self.assertTrue(version_filter.keeps(Version("3.12.9"), "3.12"))

    def test_str_of_allow_filter(self):
        """Test that an allow filter renders as the marker directive that expresses it."""
        self.assertEqual(
            str(VersionFilter(Verb.ALLOW, SpecifierSet("<3.13"), item="update<3.13")), "allow[update<3.13]"
        )

    def test_str_of_ignore_filter(self):
        """Test that an ignore filter renders as the marker directive that expresses it."""
        self.assertEqual(
            str(VersionFilter(Verb.IGNORE, SpecifierSet(">=3.13"), item="update>=3.13")), "ignore[update>=3.13]"
        )

    def test_str_of_no_bound(self):
        """Test that the keep-all NO_BOUND renders as the no-op allow[update] directive."""
        self.assertEqual(str(NO_BOUND), "allow[update]")

    def test_str_renders_the_specifier_as_written(self):
        """Test that a filter parsed from a marker renders its specifier as the user wrote it, not normalised.

        PEP 440 normalisation reorders a compound specifier's clauses (`>=3.10,<3.13` becomes `<3.13,>=3.10`),
        which would show the user a specifier they did not enter.
        """
        self.assertEqual(str(parse_bound(Verb.ALLOW, "update>=3.10,<3.13")), "allow[update>=3.10,<3.13]")

    def test_raw_specifier_does_not_affect_equality(self):
        """Test that the as-written specifier text is presentation metadata: differently spelled bounds are equal."""
        self.assertEqual(parse_bound(Verb.ALLOW, "update>=3.10,<3.13"), parse_bound(Verb.ALLOW, "update<3.13,>=3.10"))

    def test_is_hashable(self):
        """Test that a filter is hashable, so it can thread through the cached source lookups."""
        self.assertIsInstance(hash(VersionFilter(Verb.ALLOW, SpecifierSet("<3.13"))), int)

    def test_redundancy_no_effect(self):
        """Test that a bound admitting the current version and everything above it is flagged as having no effect."""
        self.assertEqual(Redundancy.NO_EFFECT, VersionFilter(Verb.ALLOW, SpecifierSet(">=3.12")).redundancy("3.12"))

    def test_redundancy_blocks_all_via_ignore(self):
        """Test that an ignore bound dropping the current version and everything above it is flagged as blocking all."""
        self.assertEqual(Redundancy.BLOCKS_ALL, VersionFilter(Verb.IGNORE, SpecifierSet(">=3.12")).redundancy("3.12"))

    def test_redundancy_blocks_all_via_ceiling_below_current(self):
        """Test that an allow ceiling below the current version blocks every update."""
        self.assertEqual(Redundancy.BLOCKS_ALL, VersionFilter(Verb.ALLOW, SpecifierSet("<3.13")).redundancy("3.14"))

    def test_redundancy_live_ceiling(self):
        """Test that a ceiling above the current version is live (not redundant)."""
        self.assertIsNone(VersionFilter(Verb.ALLOW, SpecifierSet("<3.13")).redundancy("3.12"))

    def test_redundancy_live_range_hole(self):
        """Test that an ignore range above the current version (a hole) is live, not redundant."""
        self.assertIsNone(VersionFilter(Verb.IGNORE, SpecifierSet(">=3.13,<3.15")).redundancy("3.14"))

    def test_redundancy_allow_range_below_current_is_live(self):
        """Test that an allow range entirely above the current version is live: it admits updates into the range."""
        self.assertIsNone(VersionFilter(Verb.ALLOW, SpecifierSet(">=3.13,<3.15")).redundancy("3.12"))

    def test_redundancy_allow_range_containing_current_is_live(self):
        """Test that an allow range containing the current version is live (it caps updates above the range)."""
        self.assertIsNone(VersionFilter(Verb.ALLOW, SpecifierSet(">=3.13,<3.15")).redundancy("3.14"))

    def test_redundancy_allow_range_above_current_blocks_all(self):
        """Test that an allow range entirely below the current version blocks every update."""
        self.assertEqual(
            Redundancy.BLOCKS_ALL, VersionFilter(Verb.ALLOW, SpecifierSet(">=3.13,<3.15")).redundancy("3.16")
        )

    def test_redundancy_star_range_below_current_is_live(self):
        """Test that an `==x.*` interval above the current version is live, not blocking."""
        self.assertIsNone(VersionFilter(Verb.ALLOW, SpecifierSet("==3.12.*")).redundancy("3.11"))

    def test_redundancy_hard_ceiling_at_current_blocks_all(self):
        """Test that a hard `<=` ceiling at the current version blocks every update (nothing above it survives)."""
        self.assertEqual(Redundancy.BLOCKS_ALL, VersionFilter(Verb.ALLOW, SpecifierSet("<=3.12")).redundancy("3.12"))

    def test_redundancy_with_post_release_current_version(self):
        """Test that a live bound on a pin with a post-release segment is classified without raising."""
        self.assertIsNone(VersionFilter(Verb.ALLOW, SpecifierSet("<5")).redundancy("4.15.0.post1"))

    def test_redundancy_with_prerelease_boundary(self):
        """Test that a live bound whose specifier boundary is a pre-release is classified without raising."""
        self.assertIsNone(VersionFilter(Verb.ALLOW, SpecifierSet(">=3.13rc1")).redundancy("3.12"))

    def test_redundancy_of_image_tag_with_suffix(self):
        """Test that a bound on an image tag with a variant suffix is classified against the tag's main version."""
        version_filter = VersionFilter(Verb.ALLOW, SpecifierSet("<3.13"))
        self.assertEqual(Redundancy.BLOCKS_ALL, version_filter.redundancy("3.14.1-bookworm-slim"))
        self.assertIsNone(version_filter.redundancy("3.12.1-bookworm-slim"))

    def test_redundancy_of_image_tag_with_label_prefix(self):
        """Test that a bound on an image tag with a label prefix is classified against the embedded version."""
        version_filter = VersionFilter(Verb.ALLOW, SpecifierSet(">=3.12"))
        self.assertEqual(Redundancy.NO_EFFECT, version_filter.redundancy("python3.14"))

    def test_redundancy_invalid_current_version(self):
        """Test that an unparsable current version yields no redundancy verdict rather than raising."""
        self.assertIsNone(VersionFilter(Verb.ALLOW, SpecifierSet("<3.13")).redundancy("not-a-version"))

    def test_redundancy_of_no_bound(self):
        """Test that the keep-all NO_BOUND (an empty specifier) trivially never has an effect.

        Whether that is worth reporting on is the logger's call: it stays silent about NO_BOUND, since that is the
        no-op default of an unmarked reference rather than a bound the user wrote.
        """
        self.assertEqual(Redundancy.NO_EFFECT, NO_BOUND.redundancy("3.12"))

    def test_redundancy_with_non_pep440_boundary(self):
        """Test that a clause whose version isn't PEP 440 (arbitrary equality) is handled without raising."""
        self.assertEqual(Redundancy.BLOCKS_ALL, VersionFilter(Verb.ALLOW, SpecifierSet("===foobar")).redundancy("1.0"))


class ParseLevelBoundTest(unittest.TestCase):
    """Unit tests for parsing a `<level>-update` marker item into a version filter."""

    def test_valid_levels(self):
        """Test that each level name parses into a filter carrying the level and the verb."""
        for name in ("major", "minor", "patch"):
            self.assertEqual(
                parse_bound(Verb.ALLOW, f"{name}-update"), VersionFilter(Verb.ALLOW, level=UpdateLevel[name.upper()])
            )

    def test_ignore_verb(self):
        """Test that the verb distinguishes an `ignore` bound from an `allow` bound."""
        self.assertEqual(parse_bound(Verb.IGNORE, "major-update"), VersionFilter(Verb.IGNORE, level=UpdateLevel.MAJOR))

    def test_unknown_level(self):
        """Test that an unknown level name is not a bound."""
        self.assertIsNone(parse_bound(Verb.ALLOW, "mega-update"))

    def test_missing_update_suffix(self):
        """Test that a bare level name without the `-update` suffix is not a bound."""
        self.assertIsNone(parse_bound(Verb.ALLOW, "major"))

    def test_bare_update(self):
        """Test that a bare `update` item parses as the keep-all bound.

        The marker parser never gets here — it interprets a bare `update` scope before trying the bounds — but as
        an item it is a specifier bound with an empty specifier, which is exactly what NO_BOUND is.
        """
        self.assertEqual(NO_BOUND, parse_bound(Verb.ALLOW, "update"))


class LevelBoundTest(unittest.TestCase):
    """Unit tests for the level-based version bounds."""

    def test_str_of_allow_filter(self):
        """Test that an allow level filter renders as the marker directive that expresses it."""
        self.assertEqual(
            str(VersionFilter(Verb.ALLOW, level=UpdateLevel.MINOR, item="minor-update")), "allow[minor-update]"
        )

    def test_str_of_ignore_filter(self):
        """Test that an ignore level filter renders as the marker directive that expresses it."""
        self.assertEqual(
            str(VersionFilter(Verb.IGNORE, level=UpdateLevel.MINOR, item="minor-update")), "ignore[minor-update]"
        )

    def test_ignore_major_keeps_the_major_line(self):
        """Test that `ignore[major-update]` keeps updates within the current major line and drops the rest."""
        version_filter = VersionFilter(Verb.IGNORE, level=UpdateLevel.MAJOR)
        self.assertTrue(version_filter.keeps(Version("3.99.0"), "3.12.1"))
        self.assertFalse(version_filter.keeps(Version("4.0.0"), "3.12.1"))

    def test_allow_minor_is_the_complement_of_ignore_major(self):
        """Test that `allow[minor-update]` keeps exactly what `ignore[major-update]` keeps."""
        allow_minor = VersionFilter(Verb.ALLOW, level=UpdateLevel.MINOR)
        ignore_major = VersionFilter(Verb.IGNORE, level=UpdateLevel.MAJOR)
        for candidate in ("3.12.9", "3.99.0", "4.0.0"):
            self.assertEqual(
                allow_minor.keeps(Version(candidate), "3.12.1"), ignore_major.keeps(Version(candidate), "3.12.1")
            )

    def test_ignore_minor_keeps_the_minor_line(self):
        """Test that `ignore[minor-update]` keeps updates within the current minor line and drops the rest."""
        version_filter = VersionFilter(Verb.IGNORE, level=UpdateLevel.MINOR)
        self.assertTrue(version_filter.keeps(Version("3.12.9"), "3.12.1"))
        self.assertFalse(version_filter.keeps(Version("3.13.0"), "3.12.1"))

    def test_allow_major_keeps_every_update(self):
        """Test that `allow[major-update]` keeps every update (`redundancy` flags it as having no effect)."""
        self.assertTrue(VersionFilter(Verb.ALLOW, level=UpdateLevel.MAJOR).keeps(Version("99.0"), "3.12.1"))

    def test_ignore_patch_keeps_only_the_current_release(self):
        """Test that `ignore[patch-update]` pins the current release exactly, dropping every update."""
        version_filter = VersionFilter(Verb.IGNORE, level=UpdateLevel.PATCH)
        self.assertTrue(version_filter.keeps(Version("3.12.1"), "3.12.1"))
        self.assertFalse(version_filter.keeps(Version("3.12.2"), "3.12.1"))

    def test_missing_components_count_as_zero(self):
        """Test that a component the current version is missing counts as zero, as version comparison pads it."""
        version_filter = VersionFilter(Verb.IGNORE, level=UpdateLevel.MINOR)
        self.assertTrue(version_filter.keeps(Version("22.0.9"), "22"))
        self.assertFalse(version_filter.keeps(Version("22.1"), "22"))

    def test_anchors_to_the_main_version_of_an_image_tag(self):
        """Test that an image tag with a label prefix or variant suffix is anchored by its main version component."""
        version_filter = VersionFilter(Verb.IGNORE, level=UpdateLevel.MINOR)
        self.assertTrue(version_filter.keeps(Version("3.14.9"), "3.14.6-alpine3.23"))
        self.assertFalse(version_filter.keeps(Version("3.15.0"), "3.14.6-alpine3.23"))
        self.assertTrue(version_filter.keeps(Version("3.12.9"), "python3.12-slim"))
        self.assertFalse(version_filter.keeps(Version("3.13.0"), "python3.12-slim"))

    def test_epoch_is_kept_in_the_anchor(self):
        """Test that a current version with an epoch anchors the bound within that epoch."""
        version_filter = VersionFilter(Verb.IGNORE, level=UpdateLevel.MAJOR)
        self.assertTrue(version_filter.keeps(Version("1!2.9"), "1!2.3"))
        self.assertFalse(version_filter.keeps(Version("1!3.0"), "1!2.3"))

    def test_unparsable_current_version_keeps_every_update(self):
        """Test that a current version without a parsable version can't anchor the bound, leaving updates unbounded."""
        self.assertTrue(VersionFilter(Verb.IGNORE, level=UpdateLevel.MINOR).keeps(Version("99.0"), "not-a-version"))

    def test_reanchors_as_the_reference_advances(self):
        """Test that the bound ratchets along: after a migration it blocks the next level from the new version."""
        version_filter = VersionFilter(Verb.IGNORE, level=UpdateLevel.MINOR)
        self.assertFalse(version_filter.keeps(Version("3.13.0"), "3.12.1"))
        self.assertTrue(version_filter.keeps(Version("3.13.1"), "3.13.0"))

    def test_redundancy_of_level_bounds(self):
        """Test that a level bound is classified by its anchored equivalent (see the logger for the reporting)."""
        self.assertEqual(Redundancy.NO_EFFECT, VersionFilter(Verb.ALLOW, level=UpdateLevel.MAJOR).redundancy("3.12"))
        self.assertEqual(
            Redundancy.BLOCKS_ALL, VersionFilter(Verb.IGNORE, level=UpdateLevel.PATCH).redundancy("3.12.1")
        )
        self.assertIsNone(VersionFilter(Verb.IGNORE, level=UpdateLevel.MINOR).redundancy("3.12"))
