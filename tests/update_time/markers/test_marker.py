"""Unit tests for reading a marker, and for what the marker it produces holds back."""

import ast
import dataclasses
import inspect
import textwrap
import unittest
from pathlib import Path

from update_time.domain.bound import Verb
from update_time.domain.line import Line
from update_time.markers import marker as marker_module
from update_time.markers.marker import Marker, Scope, Threshold, parse_directives, parse_marker
from update_time.primitives.location import Location

from tests.mutation import Mutation, kills
from tests.update_time.helpers import bound

# Advisory identifiers, for the tests of the `vulnerable=ID` bracket item. An advisory is known by identifiers of
# different shapes, and Update-time takes whichever the reader writes.
_GHSA = "GHSA-2gwj-7jmv-h26r"
_CVE = "CVE-2022-28346"


def _line(text: str, previous_text: str = "") -> Line:
    """Return the text as a line of a file, with the text of the line above it."""
    return Line(text, previous_text, Location(Path("file"), 1))


def _fields_set(marker: Marker) -> set[str]:
    """Return the names of the fields the marker sets to something other than their default."""
    default = Marker()
    names = (field.name for field in dataclasses.fields(Marker))
    return {name for name in names if getattr(marker, name) != getattr(default, name)}


def _cannot_combine(value: ast.expr) -> bool:
    """Return whether the expression takes its field from one marker rather than combining both markers' values."""
    if isinstance(value, ast.IfExp):
        return True
    return isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "merge"


def _fields_that_cannot_combine() -> set[str]:
    """Return the `Marker` fields `merge` takes from one marker rather than combining both markers' values."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(Marker.merge)))
    merged = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Marker"
    )
    return {keyword.arg for keyword in merged.keywords if keyword.arg and _cannot_combine(keyword.value)}


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
        """Return the marker the directives express, as a reference's own line carries them."""
        return parse_marker(_line(f"image: python:3.14  # update-time: {directives}"))

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


class ParseMarkerScopeTest(unittest.TestCase):
    """Unit tests for what an `ignore` directive's bracket expresses."""

    def test_an_ignore_item_holds_its_own_scope_back_alone(self):
        """Test that an `ignore` naming a scope it can hold back holds that scope back and leaves the others alone."""
        cases = {"ignore[update]": Scope.UPDATE, "ignore[yanked]": Scope.YANKED, "ignore[archived]": Scope.ARCHIVED}
        for directive, scope in cases.items():
            with self.subTest(directive=directive):
                marker = parse_marker(_line(f"humanize==4.15.0  # update-time: {directive}"))
                self.assertEqual(marker, Marker(ignored_scopes=scope))

    def test_a_scope_naming_its_own_default_is_recognised(self):
        """Test that the `ignore` of a scope that is off by default is read, and recorded as the user wrote it."""
        for scope in (Scope.HASH_DRIFT, Scope.FLOATING_PIN):
            with self.subTest(scope=scope):
                marker = parse_marker(_line(f"humanize==4.15.0  # update-time: ignore[{scope}]"))
                self.assertEqual(marker, Marker(written_scopes=scope))

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

    def test_unrecognised_scope_is_reported_as_invalid(self):
        """Test that a typo'd scope is reported as an invalid item rather than read as a directive."""
        marker = parse_marker(_line("humanize==4.15.0  # update-time: ignore[updaet]"))
        self.assertEqual(marker, Marker(invalid_item="updaet"))

    def test_unterminated_bracket_is_reported_as_invalid(self):
        """Test that a bracket left unclosed is reported as an invalid item, the item keeping its `[`."""
        marker = parse_marker(_line("humanize==4.15.0  # update-time: ignore[update<4"))
        self.assertEqual(marker, Marker(invalid_item="[update<4"))

    def test_an_unreadable_bound_is_reported_without_its_update_prefix(self):
        """Test that an unreadable bound is reported as the specifier it holds."""
        marker = parse_marker(_line("humanize==4.15.0  # update-time: allow[update<<3.13]"))
        self.assertEqual(marker, Marker(invalid_item="<<3.13"))


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
                marker = parse_marker(_line(f"humanize==4.15.0  # update-time: set[{item}]"))
                self.assertEqual(marker, expected)

    @kills(
        Mutation(
            marker_module,
            "(?P<key>{_STALE_AFTER}|{_COOLDOWN})",
            r"(?P<key>[\w-]+)",
            "a `set` item takes any key, so one naming a scope rather than an option sets a value",
        ),
        Mutation(
            marker_module,
            r"=(?P<days>\d+)",
            r"=(?P<days>-?\d+)",
            "a `set` item takes a negative day count",
        ),
        Mutation(
            marker_module,
            "(?P<level>{'|'.join([*RISK_LEVELS, NO_RISK_LEVEL])})",
            r"(?P<level>\w+)",
            "a `set` item takes any word as a risk level",
        ),
    )
    def test_an_unreadable_item_is_rejected(self):
        """Test that a `set` item Update-time cannot read is reported as an invalid item."""
        cases = {
            "a key without a value": "cooldown",
            "a key naming a scope": "stale=90",
            "a key naming a bound": "update=3.13",
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


class ParseMarkerRawTest(unittest.TestCase):
    """Unit tests that `parse_marker` captures the directive text verbatim."""

    def test_verbatim_directives(self):
        """Test that the parsed marker keeps the directives exactly as written."""
        marker = parse_marker(_line("image: python:3.12  # update-time: ignore[update] set[stale-after=0]"))
        self.assertEqual(marker.raw, "ignore[update] set[stale-after=0]")

    def test_comma_combined_items_kept_together(self):
        """Test that comma-combined bracket items are kept as one directive, not split apart."""
        marker = parse_marker(_line("image: python:3.14  # update-time: allow[update<3.15, hash-drift]"))
        self.assertEqual(marker.raw, "allow[update<3.15, hash-drift]")

    def test_unrecognised_scope_kept_as_written(self):
        """Test that a typo'd scope is kept as written, so the user is shown the item they actually typed."""
        marker = parse_marker(_line("image: python:3.12  # update-time: ignore[updaet]"))
        self.assertEqual(marker.raw, "ignore[updaet]")

    def test_trailing_reason_left_out(self):
        """Test that free text after the last directive (a reason) is not part of the captured text."""
        marker = parse_marker(_line("image: python:3.12  # update-time: set[stale-after=0] (until the migration)"))
        self.assertEqual(marker.raw, "set[stale-after=0]")

    def test_directives_across_two_lines_are_ordered_inline_first(self):
        """Test that a marker split over a line and the comment above it captures both, inline directives first."""
        marker = parse_marker(
            _line("image: python:3.14  # update-time: allow[update<3.15]", "# update-time: set[stale-after=0]")
        )
        self.assertEqual(marker.raw, "allow[update<3.15] set[stale-after=0]")

    def test_no_marker_has_empty_raw(self):
        """Test that a line without a marker parses to an empty verbatim text, so nothing is echoed for it."""
        self.assertEqual(parse_marker(_line("image: python:3.12")).raw, "")


class ParseMarkerCommentTest(unittest.TestCase):
    """Unit tests for the comments a marker can be written in."""

    def test_an_xml_comment_carries_a_marker(self):
        """Test that a marker in an XML comment is read."""
        marker = parse_marker(_line("<version>6.1.0</version>  <!-- update-time: set[stale-after=0] -->"))
        self.assertEqual(marker, Marker(stale=Threshold(value=0)))


class ParseDirectivesTest(unittest.TestCase):
    """Unit tests for reading a directive list that carries no `# update-time:` prefix."""

    # Each case: the directive list, the marker it expresses, and the verbatim text the marker keeps.
    CASES = (
        ("ignore", Marker(invalid_item="ignore"), "ignore"),
        ("allow", Marker(), ""),
        ("set", Marker(), ""),
        ("ignore[yanked] allow it for now", Marker(ignored_scopes=Scope.YANKED), "ignore[yanked]"),
        ("ignore[yanked] set by the security team", Marker(ignored_scopes=Scope.YANKED), "ignore[yanked]"),
        (
            "ignore ignore[yanked]",
            Marker(ignored_scopes=Scope.YANKED, invalid_item="ignore"),
            "ignore ignore[yanked]",
        ),
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
