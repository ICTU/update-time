"""Unit tests for the marker a reference carries, and for spelling a directive as the `# update-time:` language does."""

import unittest

from packaging.specifiers import SpecifierSet

from update_time.domain.bound import NO_BOUND, Verb, VersionBound
from update_time.domain.marker import Marker, Scope, spell
from update_time.markers.parser import parse_directives

from tests.update_time.helpers import bound


class MergeTest(unittest.TestCase):
    """Unit tests for folding two markers into one."""

    def test_raw_texts_concatenate_in_order(self):
        """Test that merging keeps both verbatim directive runs, this marker's first, so the echo reads in order."""
        merged = Marker(raw="allow[update<3.13]").merge(Marker(raw="ignore[yanked]"))
        self.assertEqual(merged.raw, "allow[update<3.13] ignore[yanked]")

    def test_raw_is_excluded_from_equality(self):
        """Test that two markers meaning the same thing compare equal however their directives were spelled."""
        self.assertEqual(Marker(ignored_scopes=Scope.UPDATE, raw="ignore[update]"), Marker(ignored_scopes=Scope.UPDATE))


class DirectiveTest(unittest.TestCase):
    """Unit tests for the directive a marker names when one of its scopes is reported."""

    def marker(self, directives: str) -> Marker:
        """Return the marker the directives express."""
        return parse_directives(directives)

    def test_a_scope_the_marker_leaves_live_is_named_by_nothing(self):
        """Test that a scope the marker does not hold back names no directive."""
        self.assertEqual(self.marker("set[stale-after=0]").scope_directive(Scope.YANKED), "")

    def test_a_scope_is_named_alone(self):
        """Test that a scope names its own directive, leaving out an `ignore` beside it that holds plenty back."""
        self.assertEqual(self.marker("ignore[update] ignore[yanked]").scope_directive(Scope.YANKED), "ignore[yanked]")

    def test_a_threshold_is_named_alone(self):
        """Test that a staleness threshold is named as its own directive, leaving out a directive beside it."""
        marker = self.marker("set[stale-after=90] allow[hash-drift]")
        self.assertEqual(marker.directive_for(Scope.STALE), "set[stale-after=90]")

    def test_a_cooldown_is_named_alone(self):
        """Test that a cooldown is named as its own directive, leaving out a directive beside it."""
        marker = self.marker("set[cooldown=30] allow[hash-drift]")
        self.assertEqual(marker.cooldown_directive, "set[cooldown=30]")

    def test_every_vulnerable_form_the_marker_carries_is_named(self):
        """Test that each `vulnerable` form is named, the advisories sharing a bracket and sorted within it."""
        marker = self.marker("ignore[vulnerable=CVE-2, vulnerable=CVE-1] set[vulnerability-level=high]")
        self.assertEqual(
            marker.directive_for(Scope.VULNERABLE),
            "ignore[vulnerable=CVE-1, vulnerable=CVE-2] set[vulnerability-level=high]",
        )


class RawTextTest(unittest.TestCase):
    """Unit tests for reading a marker's verbatim directive text back out of its `raw` text."""

    def test_str_is_the_whole_marker(self):
        """Test that a marker renders as the whole of its verbatim directive text, every verb's directives kept."""
        marker = Marker(raw="ignore[update] allow[hash-drift]")
        self.assertEqual(str(marker), "ignore[update] allow[hash-drift]")


class SpellBoundTest(unittest.TestCase):
    """Unit tests for spelling a version bound back as the marker directive that expresses it."""

    def test_a_bound_is_spelled_as_its_item_was_written(self):
        """Test that a bound parsed from a marker spells its item as the user wrote it, not normalised.

        The specifier here is a compound one, which PEP 440 normalisation would reorder to `<3.13,>=3.10`.
        """
        self.assertEqual(spell(bound(Verb.ALLOW, "update>=3.10,<3.13")), "allow[update>=3.10,<3.13]")

    def test_a_bound_is_spelled_as_the_directive_expressing_it(self):
        """Test that a bound is spelled as the marker directive that expresses it, whichever verb it carries."""
        cases = (
            (VersionBound(Verb.ALLOW, SpecifierSet("<3.13"), item="update<3.13"), "allow[update<3.13]"),
            (VersionBound(Verb.IGNORE, SpecifierSet(">=3.13"), item="update>=3.13"), "ignore[update>=3.13]"),
        )
        for version_bound, expected in cases:
            with self.subTest(verb=version_bound.verb):
                self.assertEqual(spell(version_bound), expected)

    def test_the_keep_all_bound_is_spelled_as_the_no_op_directive(self):
        """Test that the keep-all NO_BOUND is spelled as the no-op allow[update] directive."""
        self.assertEqual(spell(NO_BOUND), "allow[update]")
