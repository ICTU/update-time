"""Unit tests for reading a marker, and for what the marker it produces holds back."""

import ast
import dataclasses
import inspect
import textwrap
import unittest
from pathlib import Path
from typing import Final

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from mutation_time.mutation import kills
from update_time.domain.bound import NO_BOUND, UpdateLevel, Verb, VersionBound
from update_time.domain.marker import Marker, Scope, Threshold
from update_time.markers import parser
from update_time.markers.parser import _parse_bound, parse_directives, parse_marker
from update_time.primitives.line import Line
from update_time.primitives.location import Location

from tests.update_time.domain_objects import bound

# Advisory identifiers, for the tests of the `vulnerable=ID` bracket item. An advisory is known by identifiers of
# different shapes, and Update-time takes whichever the reader writes.
_GHSA: Final[str] = "GHSA-2gwj-7jmv-h26r"
_CVE: Final[str] = "CVE-2022-28346"


class ParseMarkerScopeTest(unittest.TestCase):
    """Unit tests for what an `ignore` directive's bracket expresses."""

    def test_an_ignore_item_holds_its_own_scope_back_alone(self):
        """Test that an `ignore` naming a scope it can hold back holds that scope back and leaves the others alone."""
        cases = {"ignore[update]": Scope.UPDATE, "ignore[yanked]": Scope.YANKED, "ignore[archived]": Scope.ARCHIVED}
        for directive, scope in cases.items():
            with self.subTest(directive=directive):
                marker = parse_marker(_line(f"humanize==4.15.0  # update-time: {directive}"))
                self.assertEqual(marker, Marker(ignored_scopes=scope))

    def test_a_directive_asking_for_the_default_is_recorded_as_written(self):
        """Test that a directive asking for the default is read, steering nothing, and recorded as the user wrote it."""
        for directive in ("ignore[hash-drift]", "ignore[floating-pin]", "allow[update]"):
            with self.subTest(directive=directive):
                marker = parse_marker(_line(f"humanize==4.15.0  # update-time: {directive}"))
                self.assertEqual(marker, Marker())
                self.assertEqual(marker.default_directives, (directive,))

    @kills.replace(
        Marker.merge,
        "default_directives=self.default_directives + other.default_directives,",
        "default_directives=self.default_directives or other.default_directives,",
        "a marker asking for the default more than once is reported for the first such directive alone",
    )
    def test_every_directive_asking_for_the_default_is_recorded_in_the_order_written(self):
        """Test that the directives asking for the default in a bracket and on the line above are all recorded."""
        marker = parse_marker(
            _line("humanize==4.15.0  # update-time: ignore[hash-drift, floating-pin]", "# update-time: allow[update]")
        )
        self.assertEqual(marker.default_directives, ("ignore[hash-drift]", "ignore[floating-pin]", "allow[update]"))

    def test_a_verb_naming_a_scope_a_set_item_steers_is_reported_as_invalid(self):
        """Test that `ignore` or `allow` naming a scope a `set` item steers, such as `stale`, is an invalid item."""
        for verb in (Verb.IGNORE, Verb.ALLOW):
            for scope in (Scope.STALE, Scope.VULNERABLE, Scope.COOLDOWN):
                with self.subTest(directive=f"{verb}[{scope}]"):
                    marker = parse_marker(_line(f"humanize==4.15.0  # update-time: {verb}[{scope}]"))
                    self.assertEqual(marker, Marker(invalid_item=str(scope)))

    def test_a_comparison_setting_a_value_is_reported_as_invalid(self):
        """Test that a comparison item, whichever verb and operator it takes, is reported as an invalid item."""
        for verb in (Verb.IGNORE, Verb.ALLOW):
            for operator in ("<", ">="):
                for keyword, value in (("stale", "90"), ("cooldown", "30"), ("vulnerable", "high")):
                    item = f"{keyword}{operator}{value}"
                    with self.subTest(verb=verb, item=item):
                        marker = parse_marker(_line(f"humanize==4.15.0  # update-time: {verb}[{item}]"))
                        self.assertEqual(marker, Marker(invalid_item=item))

    def test_an_unreadable_item_is_reported_as_invalid(self):
        """Test that an item the language cannot read is reported as an invalid item, and the items beside it apply."""
        cases = {
            "a scope typo": ("ignore[updaet]", Marker(invalid_item="updaet")),
            "a level-name typo": ("ignore[mega-update]", Marker(invalid_item="mega-update")),
            "a plural typo of `patch-update`": ("allow[patch-updates]", Marker(invalid_item="patch-updates")),
            "a bracket with nothing in it": ("ignore[]", Marker(invalid_item="")),
            "a bracket left unclosed, keeping its `[`": ("ignore[update<4", Marker(invalid_item="[update<4")),
            "a bound, without its `update` prefix": ("allow[update<<3.13]", Marker(invalid_item="<<3.13")),
            "an item in a comma list": (
                "allow[drift, update<3.13]",
                Marker(version_bound=bound(Verb.ALLOW, "update<3.13"), invalid_item="drift"),
            ),
        }
        for case, (directive, expected) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(parse_marker(_line(f"humanize==4.15.0  # update-time: {directive}")), expected)


def _line(text: str, previous_text: str = "") -> Line:
    """Return the text as a line of a file, with the text of the line above it."""
    return Line(text, previous_text, Location(Path("file"), 1))


class ParseMarkerAdvisoryTest(unittest.TestCase):
    """Unit tests for the advisory a `vulnerable=ID` bracket item names."""

    def test_the_identifier_names_the_advisory_to_silence(self):
        """Test that `ignore[vulnerable=GHSA-…]` names that advisory alone, holding no other check back."""
        marker = parse_marker(_line(f"django==3.2.0  # update-time: ignore[vulnerable={_GHSA}]"))
        self.assertEqual(marker, Marker(ignored_advisories=frozenset({_GHSA})))

    def test_an_item_per_advisory_names_them_all(self):
        """Test that a bracket holding an item per advisory names every one of them, the items combining as a union."""
        marker = parse_marker(_line(f"django==3.2.0  # update-time: ignore[vulnerable={_GHSA}, vulnerable={_CVE}]"))
        self.assertEqual(marker, Marker(ignored_advisories=frozenset({_GHSA, _CVE})))

    def test_a_list_of_identifiers_in_one_item_is_rejected(self):
        """Test that a second identifier behind a comma is reported as an invalid item, naming no advisory."""
        marker = parse_marker(_line(f"django==3.2.0  # update-time: ignore[vulnerable={_GHSA},{_CVE}]"))
        self.assertEqual(marker, Marker(ignored_advisories=frozenset({_GHSA}), invalid_item=_CVE))

    def test_an_item_with_nothing_behind_the_equals_sign_is_rejected(self):
        """Test that `ignore[vulnerable=]` names no advisory and is reported as an invalid item."""
        marker = parse_marker(_line("django==3.2.0  # update-time: ignore[vulnerable=]"))
        self.assertEqual(marker, Marker(invalid_item="vulnerable="))

    def test_allow_names_no_advisory(self):
        """Test that `allow[vulnerable=GHSA-…]` is reported as an invalid item."""
        marker = parse_marker(_line(f"django==3.2.0  # update-time: allow[vulnerable={_GHSA}]"))
        self.assertEqual(marker, Marker(invalid_item=f"vulnerable={_GHSA}"))


class ParseMarkerSetTest(unittest.TestCase):
    """Unit tests for the value a `set` bracket item sets for the reference carrying it."""

    def test_each_item_sets_its_value(self):
        """Test that each `set` item sets the value of the option it names."""
        cases = {
            "stale-after=90": Marker(stale=Threshold(value=90)),
            "stale-after=0": Marker(stale=Threshold(value=0)),
            "stale-after=00": Marker(stale=Threshold(value=0)),
            "cooldown=30": Marker(cooldown=Threshold(value=30)),
            "vulnerability-level=high": Marker(vulnerable=Threshold(value="high")),
            "vulnerability-level=none": Marker(vulnerable=Threshold(value="none")),
        }
        for item, expected in cases.items():
            with self.subTest(item=item):
                self.assertEqual(parse_marker(_line(f"humanize==4.15.0  # update-time: set[{item}]")), expected)

    @kills.replace(
        parser,
        r"{_COOLDOWN})=(?P<days>\d+)",
        r"{_COOLDOWN})=?(?P<days>\d*)",
        "a `set` item without a value ends the run",
        raises="ValueError: invalid literal for int() with base 10: ''",
        case="a key without a value",
    )
    @kills.replace(
        parser,
        "(?P<key>{_STALE_AFTER}|{_COOLDOWN})",
        r"(?P<key>[\w-]+)",
        "a `set` item takes any key, so one naming a scope rather than an option sets a value",
        case="a key naming a scope",
    )
    @kills.append(parser, r"=(?P<days>", r"-?", "a `set` item takes a negative day count", case="a negative day count")
    @kills.replace(
        parser._parse_setting_item,
        "_SETTING_DAY_COUNT_ITEM.fullmatch(item)",
        "_SETTING_DAY_COUNT_ITEM.match(item)",
        "a `set` item takes a fractional day count, rounded down to the whole days in front of the point",
        case="a fractional day count",
    )
    @kills.replace(
        parser,
        "(?P<level>{'|'.join([*RISK_LEVELS, NO_RISK_LEVEL])})",
        r"(?P<level>\w+)",
        "a `set` item takes any word as a risk level",
        case="a misspelled risk level",
    )
    @kills.replace(
        parser,
        "{_COOLDOWN})=(?P<days>",
        "{_COOLDOWN}).(?P<days>",
        "a `set` item takes a comparison for its `=`, so `cooldown<30` sets a cooldown of 30 days",
        case="a comparison",
    )
    def test_an_unreadable_item_is_rejected(self):
        """Test that a `set` item Update-time cannot read is reported as an invalid item."""
        cases = {
            "a key without a value": "cooldown",
            "a key naming a scope": "stale=90",
            "a negative day count": "cooldown=-5",
            "a fractional day count": "stale-after=1.5",
            "a misspelled risk level": "vulnerability-level=hgih",
            "a comparison": "cooldown<30",
        }
        for case, item in cases.items():
            with self.subTest(case=case):
                marker = parse_marker(_line(f"humanize==4.15.0  # update-time: set[{item}]"))
                self.assertEqual(marker, Marker(invalid_item=item))


class ParseMarkerPrecedenceTest(unittest.TestCase):
    """Unit tests that a directive on the reference's own line wins over one on the line above."""

    # The inline directive, the one on the line above, and the marker the two of them merge to.
    CASES = (
        ("allow[update<3.13]", "allow[update<4]", Marker(version_bound=bound(Verb.ALLOW, "update<3.13"))),
        ("set[stale-after=30]", "set[stale-after=90]", Marker(stale=Threshold(value=30))),
        ("set[cooldown=30]", "set[cooldown=90]", Marker(cooldown=Threshold(value=30))),
        (
            "set[vulnerability-level=high]",
            "set[vulnerability-level=critical]",
            Marker(vulnerable=Threshold(value="high")),
        ),
        ("ignore[stlae]", "ignore[updaet]", Marker(invalid_item="stlae")),
    )

    def test_an_inline_directive_beats_one_on_the_line_above(self):
        """Test that of two markers setting the same value, the inline one wins."""
        for inline, above, expected in self.CASES:
            with self.subTest(inline=inline):
                marker = parse_marker(_line(f"image: python:3.12  # update-time: {inline}", f"# update-time: {above}"))
                self.assertEqual(marker, expected)

    def test_every_field_that_cannot_combine_has_a_case(self):
        """Test that the cases pin every field `Marker.merge` takes from one marker."""
        cannot_combine = _fields_that_cannot_combine()
        self.assertNotEqual(cannot_combine, set())  # An empty read would pass the check below without checking.
        pinned = {name for _inline, _above, expected in self.CASES for name in _fields_set(expected)}
        self.assertEqual(pinned, cannot_combine)


def _fields_that_cannot_combine() -> set[str]:
    """Return the `Marker` fields `merge` takes from one marker rather than combining both markers' values."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(Marker.merge)))
    merged = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Marker"
    )
    return {keyword.arg for keyword in merged.keywords if keyword.arg and _cannot_combine(keyword.value)}


def _cannot_combine(value: ast.expr) -> bool:
    """Return whether the expression takes its field from one marker rather than combining both markers' values."""
    if isinstance(value, ast.IfExp):
        return True
    return isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "merge"


def _fields_set(marker: Marker) -> set[str]:
    """Return the names of the fields the marker sets to something other than their default."""
    default = Marker()
    names = (field.name for field in dataclasses.fields(Marker))
    return {name for name in names if getattr(marker, name) != getattr(default, name)}


class ParseMarkerRawTest(unittest.TestCase):
    """Unit tests that `parse_marker` captures the directive text verbatim."""

    def test_directives_across_two_lines_are_ordered_inline_first(self):
        """Test that a marker split over a line and the comment above it captures both, inline directives first."""
        marker = parse_marker(
            _line("image: python:3.14  # update-time: allow[update<3.15]", "# update-time: set[stale-after=0]")
        )
        self.assertEqual(marker.raw, "allow[update<3.15] set[stale-after=0]")


class ParseMarkerCommentTest(unittest.TestCase):
    """Unit tests for the comments a marker can be written in."""

    def test_an_xml_comment_carries_a_marker(self):
        """Test that a marker in an XML comment is read."""
        marker = parse_marker(_line("<version>6.1.0</version>  <!-- update-time: set[stale-after=0] -->"))
        self.assertEqual(marker, Marker(stale=Threshold(value=0)))

    @kills.replace(
        parser._parse_marker_contents,
        "marker = marker.merge(parse_directives(text[prefix.end() :]))",
        "return parse_directives(text[prefix.end() :])",
        "a comment repeating the prefix drops the directives after the second prefix",
    )
    def test_a_repeated_prefix_combines_the_directives_after_it(self):
        """Test that a comment repeating the `# update-time:` prefix combines the directives after each prefix."""
        marker = parse_marker(
            _line("image: python:3.12  # update-time: ignore[update>=3.13] # update-time: set[stale-after=0]")
        )
        self.assertEqual(marker, Marker(stale=Threshold(value=0), version_bound=bound(Verb.IGNORE, "update>=3.13")))


class ParseDirectivesTest(unittest.TestCase):
    """Unit tests for reading a directive list that carries no `# update-time:` prefix."""

    # Each case: the directive list, the marker it expresses, and the verbatim text the marker keeps.
    CASES = (
        ("ignore", Marker(invalid_item="ignore"), "ignore"),
        ("allow", Marker(), ""),
        ("set", Marker(), ""),
        ("ignore[yanked] allow it for now", Marker(ignored_scopes=Scope.YANKED), "ignore[yanked]"),
        ("ignore[yanked] set by the security team", Marker(ignored_scopes=Scope.YANKED), "ignore[yanked]"),
        ("ignore ignore[yanked]", Marker(ignored_scopes=Scope.YANKED, invalid_item="ignore"), "ignore ignore[yanked]"),
        ("ignore (pinned on purpose)", Marker(invalid_item="ignore"), "ignore"),
        ("set[stale-after=0]", Marker(stale=Threshold(value=0)), "set[stale-after=0]"),
        (
            "set[stale-after=0] allow[update<23]",
            Marker(stale=Threshold(value=0), version_bound=bound(Verb.ALLOW, "update<23")),
            "set[stale-after=0] allow[update<23]",
        ),
        (
            "allow[update<3.15, hash-drift]",
            Marker(version_bound=bound(Verb.ALLOW, "update<3.15"), allowed_scopes=Scope.HASH_DRIFT),
            "allow[update<3.15, hash-drift]",
        ),
        (
            "set[stale-after=90, cooldown=30]",
            Marker(stale=Threshold(value=90), cooldown=Threshold(value=30)),
            "set[stale-after=90, cooldown=30]",
        ),
        (
            "ignore[archived, update>=3.13]",
            Marker(ignored_scopes=Scope.ARCHIVED, version_bound=bound(Verb.IGNORE, "update>=3.13")),
            "ignore[archived, update>=3.13]",
        ),
        (
            "ignore[update>=3.13,<3.15, archived]",
            Marker(ignored_scopes=Scope.ARCHIVED, version_bound=bound(Verb.IGNORE, "update>=3.13,<3.15")),
            "ignore[update>=3.13,<3.15, archived]",
        ),
        ("set[stale-after=0] alloww[hash-drift]", Marker(stale=Threshold(value=0)), "set[stale-after=0]"),
        ("ignore[updaet]", Marker(invalid_item="updaet"), "ignore[updaet]"),
        ("set[stale-after=0] (until the migration)", Marker(stale=Threshold(value=0)), "set[stale-after=0]"),
        ("", Marker(), ""),
    )

    def test_the_list_expresses_the_marker_its_directives_name(self):
        """Test that a list without the prefix parses to the marker its directives express, verbatim text included."""
        for directives, expected, raw in self.CASES:
            with self.subTest(directives=directives):
                marker = parse_directives(directives)
                self.assertEqual(marker, expected)
                self.assertEqual(marker.raw, raw)


class ParseBoundTest(unittest.TestCase):
    """Unit tests for parsing an `update<specifier>` marker item into a version bound."""

    def test_valid_specifier(self):
        """Test that a valid specifier parses into a bound carrying the specifier and the verb."""
        self.assertEqual(_parse_bound(Verb.ALLOW, "update<3.13"), VersionBound(Verb.ALLOW, SpecifierSet("<3.13")))

    def test_compound_specifier(self):
        """Test that a compound specifier is accepted."""
        self.assertEqual(
            _parse_bound(Verb.ALLOW, "update>=3.10,<3.13"), VersionBound(Verb.ALLOW, SpecifierSet(">=3.10,<3.13"))
        )

    def test_invalid_specifier_raises(self):
        """Test that an `update` bound with an unparsable specifier raises, distinct from a non-bound item's None."""
        self.assertRaises(InvalidSpecifier, _parse_bound, Verb.ALLOW, "update@@@")

    def test_item_that_is_no_bound(self):
        """Test that an item that is neither kind of bound returns None rather than raising."""
        self.assertIsNone(_parse_bound(Verb.IGNORE, "stale"))

    def test_the_item_as_written_does_not_affect_equality(self):
        """Test that the item as written is presentation metadata: differently spelled bounds are equal."""
        self.assertEqual(_parse_bound(Verb.ALLOW, "update>=3.10,<3.13"), _parse_bound(Verb.ALLOW, "update<3.13,>=3.10"))


class ParseLevelBoundTest(unittest.TestCase):
    """Unit tests for parsing a `<level>-update` marker item into a version bound."""

    def test_valid_levels(self):
        """Test that each level name parses into a bound carrying the level and the verb."""
        for name in ("major", "minor", "patch"):
            with self.subTest(name=name):
                self.assertEqual(
                    _parse_bound(Verb.ALLOW, f"{name}-update"),
                    VersionBound(Verb.ALLOW, level=UpdateLevel[name.upper()]),
                )

    def test_ignore_verb(self):
        """Test that the verb distinguishes an `ignore` bound from an `allow` bound."""
        self.assertEqual(_parse_bound(Verb.IGNORE, "major-update"), VersionBound(Verb.IGNORE, level=UpdateLevel.MAJOR))

    def test_unknown_level(self):
        """Test that an unknown level name is not a bound."""
        self.assertIsNone(_parse_bound(Verb.ALLOW, "mega-update"))

    def test_missing_update_suffix(self):
        """Test that a bare level name without the `-update` suffix is not a bound."""
        self.assertIsNone(_parse_bound(Verb.ALLOW, "major"))

    def test_bare_update(self):
        """Test that a bare `update` item parses as the keep-all bound."""
        self.assertEqual(_parse_bound(Verb.ALLOW, "update"), NO_BOUND)
