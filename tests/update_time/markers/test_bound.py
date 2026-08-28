"""Unit tests for parsing a marker item into a version bound, and spelling a bound back as one."""

import unittest

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from update_time.domain.bound import NO_BOUND, UpdateLevel, Verb, VersionBound
from update_time.markers.bound import parse_bound, spell

from tests.update_time.helpers import bound


class ParseBoundTest(unittest.TestCase):
    """Unit tests for parsing an `update<specifier>` marker item into a version bound."""

    def test_valid_specifier(self):
        """Test that a valid specifier parses into a bound carrying the specifier and the verb."""
        self.assertEqual(parse_bound(Verb.ALLOW, "update<3.13"), VersionBound(Verb.ALLOW, SpecifierSet("<3.13")))

    def test_compound_specifier(self):
        """Test that a compound specifier is accepted."""
        self.assertEqual(
            parse_bound(Verb.ALLOW, "update>=3.10,<3.13"), VersionBound(Verb.ALLOW, SpecifierSet(">=3.10,<3.13"))
        )

    def test_invalid_specifier_raises(self):
        """Test that an `update` bound with an unparsable specifier raises, distinct from a non-bound item's None."""
        self.assertRaises(InvalidSpecifier, parse_bound, Verb.ALLOW, "update@@@")

    def test_item_that_is_no_bound(self):
        """Test that an item that is neither kind of bound returns None rather than raising."""
        self.assertIsNone(parse_bound(Verb.IGNORE, "stale"))

    def test_the_item_as_written_does_not_affect_equality(self):
        """Test that the item as written is presentation metadata: differently spelled bounds are equal."""
        self.assertEqual(parse_bound(Verb.ALLOW, "update>=3.10,<3.13"), parse_bound(Verb.ALLOW, "update<3.13,>=3.10"))


class ParseLevelBoundTest(unittest.TestCase):
    """Unit tests for parsing a `<level>-update` marker item into a version bound."""

    def test_valid_levels(self):
        """Test that each level name parses into a bound carrying the level and the verb."""
        for name in ("major", "minor", "patch"):
            with self.subTest(name=name):
                self.assertEqual(
                    parse_bound(Verb.ALLOW, f"{name}-update"),
                    VersionBound(Verb.ALLOW, level=UpdateLevel[name.upper()]),
                )

    def test_ignore_verb(self):
        """Test that the verb distinguishes an `ignore` bound from an `allow` bound."""
        self.assertEqual(parse_bound(Verb.IGNORE, "major-update"), VersionBound(Verb.IGNORE, level=UpdateLevel.MAJOR))

    def test_unknown_level(self):
        """Test that an unknown level name is not a bound."""
        self.assertIsNone(parse_bound(Verb.ALLOW, "mega-update"))

    def test_missing_update_suffix(self):
        """Test that a bare level name without the `-update` suffix is not a bound."""
        self.assertIsNone(parse_bound(Verb.ALLOW, "major"))

    def test_bare_update(self):
        """Test that a bare `update` item parses as the keep-all bound."""
        self.assertEqual(parse_bound(Verb.ALLOW, "update"), NO_BOUND)


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
