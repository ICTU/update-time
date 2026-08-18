"""Unit tests for the marker domain object."""

import ast
import dataclasses
import inspect
import textwrap
import unittest
from pathlib import Path

from update_time.domain import marker as marker_module
from update_time.domain.bound import Verb
from update_time.domain.line import Line
from update_time.domain.marker import _IGNORABLE_SCOPES, Marker, Scope, Threshold, parse_marker
from update_time.primitives.location import Location

from tests.mutation import Mutation, kills
from tests.update_time.fixtures import BARE_IGNORE
from tests.update_time.helpers import bound

# Advisory identifiers, for the tests of the `vulnerable=ID` bracket item. An advisory is known by identifiers of
# different shapes, and Update-time takes whichever the reader writes.
_GHSA = "GHSA-2gwj-7jmv-h26r"
_CVE = "CVE-2022-28346"


def line(text: str, previous_text: str = "") -> Line:
    """Return the text as a line of a file, with the text of the line above it."""
    return Line(text, previous_text, Location(Path("conf.py"), 1))


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
        merged = Marker(raw="allow[update<3.13]").merge(Marker(raw="ignore[stale]"))
        self.assertEqual(merged.raw, "allow[update<3.13] ignore[stale]")

    def test_raw_is_excluded_from_equality(self):
        """Test that two markers meaning the same thing compare equal however their directives were spelled."""
        self.assertEqual(Marker(ignored_scopes=Scope.UPDATE, raw="ignore[update]"), Marker(ignored_scopes=Scope.UPDATE))


class HoldsEverythingBackTest(unittest.TestCase):
    """Unit tests for whether a marker leaves any check to run, and so any source to query."""

    def test_only_a_marker_naming_every_scope_holds_everything_back(self):
        """Test that a bare `ignore` and every scope named together hold everything back, one scope short does not."""
        cases = {
            "ignore": True,
            "ignore[update, stale, yanked, vulnerable]": True,
            "ignore[stale, yanked, vulnerable]": False,
            "ignore[update, yanked, vulnerable]": False,
            "ignore[update, stale, vulnerable]": False,
            "ignore[update, stale, yanked]": False,
            "ignore[update]": False,
            "ignore[stale]": False,
            "ignore[yanked]": False,
            "ignore[vulnerable]": False,
        }
        for directive, expected in cases.items():
            with self.subTest(directive=directive):
                marker = parse_marker(line(f"dependency  # update-time: {directive}"))
                self.assertEqual(marker.holds_everything_back, expected)


class HoldsBackSourceChecksTest(unittest.TestCase):
    """Unit tests for whether a marker holds back the update, the staleness warning, and the yank warning alike."""

    def test_only_update_stale_and_yanked_together_hold_back_the_source_checks(self):
        """Test that all three must be held back, the `vulnerable` scope not counting either way."""
        cases = {
            "ignore": True,
            "ignore[update, stale, yanked]": True,
            "ignore[update, stale, yanked, vulnerable]": True,
            "ignore[stale, yanked]": False,
            "ignore[update, yanked]": False,
            "ignore[update, stale]": False,
            "ignore[vulnerable]": False,
        }
        for directive, expected in cases.items():
            with self.subTest(directive=directive):
                marker = parse_marker(line(f"dependency  # update-time: {directive}"))
                self.assertEqual(marker.holds_back_source_checks, expected)


class AsWrittenTest(unittest.TestCase):
    """Unit tests for the marker with the scopes it never spelled out cleared."""

    @kills(
        Mutation(
            marker_module,
            "Marker(ignored_scopes=scope, written_scopes=scope)",
            "Marker(ignored_scopes=scope)",
            "a scope the reader spelled out is discarded, instead of being kept",
        )
    )
    def test_only_the_scopes_the_marker_spelled_out_survive(self):
        """Test that a scope a bare `ignore` implies is cleared, while one written beside it is kept."""
        cases = {
            "ignore": Marker(),
            "ignore[yanked]": Marker(ignored_scopes=Scope.YANKED),
            "ignore ignore[yanked]": Marker(ignored_scopes=Scope.YANKED),
            "ignore[update, stale, yanked, vulnerable]": BARE_IGNORE,
        }
        for directive, expected in cases.items():
            with self.subTest(directive=directive):
                marker = parse_marker(line(f"dependency  # update-time: {directive}"))
                self.assertEqual(marker.as_written, expected)


class DirectiveTest(unittest.TestCase):
    """Unit tests for the directive a marker names when one of its scopes is reported."""

    def marker(self, directives: str) -> Marker:
        """Return the marker the directives express, as a reference's own line carries them."""
        return parse_marker(line(f"image: python:3.14  # update-time: {directives}"))

    @kills(
        Mutation(
            marker_module,
            'return _directive(Verb.IGNORE, str(scope)) if self.ignores(scope) else ""',
            "return _directive(Verb.IGNORE, str(scope))",
            "a directive is returned for a scope though that scope has no directive in the marker",
        )
    )
    def test_a_scope_the_marker_leaves_live_is_named_by_nothing(self):
        """Test that a scope the marker does not hold back names no directive."""
        self.assertEqual(self.marker("ignore[stale]").scope_directive(Scope.YANKED), "")

    def test_a_scope_is_named_alone(self):
        """Test that a scope names its own directive, leaving out an `ignore` beside it that holds plenty back."""
        self.assertEqual(self.marker("ignore[update] ignore[yanked]").scope_directive(Scope.YANKED), "ignore[yanked]")

    def test_a_threshold_is_named_as_the_reader_wrote_it(self):
        """Test that a `stale` threshold is named as the item the reader wrote, whichever verb set it."""
        for directive in ("ignore[stale<90]", "allow[stale>=90]"):
            with self.subTest(directive=directive):
                self.assertEqual(self.marker(f"{directive} allow[hash-drift]").stale_directive, directive)

    def test_the_bare_stale_scope_is_named_over_a_threshold_beside_it(self):
        """Test that a marker carrying both is reported under the bare scope, which silences the warning outright."""
        self.assertEqual(self.marker("ignore[stale] ignore[stale<90]").stale_directive, "ignore[stale]")

    @kills(
        Mutation(
            marker_module,
            "        return self.cooldown.directive",
            "        return self.raw",
            "the cooldown is reported as the whole marker text rather than the cooldown item alone, so a "
            "neighbouring directive is included",
        )
    )
    def test_a_cooldown_item_is_named_as_the_reader_wrote_it(self):
        """Test that a cooldown is named as the item the reader wrote, leaving out a directive beside it."""
        for directive in ("ignore[cooldown<30]", "allow[cooldown>=30]"):
            with self.subTest(directive=directive):
                self.assertEqual(self.marker(f"{directive} allow[hash-drift]").cooldown_directive, directive)

    def test_every_vulnerable_form_the_marker_carries_is_named(self):
        """Test that each `vulnerable` form is named, the advisories sharing a bracket and sorted within it."""
        marker = self.marker("ignore[vulnerable, vulnerable=CVE-2, vulnerable=CVE-1, vulnerable<high]")
        self.assertEqual(
            marker.vulnerable_directives,
            "ignore[vulnerable] ignore[vulnerable=CVE-1, vulnerable=CVE-2] ignore[vulnerable<high]",
        )


class RawTextTest(unittest.TestCase):
    """Unit tests for reading a marker's verbatim directive text back out of its `raw` text."""

    def test_str_is_the_whole_marker(self):
        """Test that a marker renders as the whole of its verbatim directive text, every verb's directives kept."""
        marker = Marker(raw="ignore[update] allow[hash-drift]")
        self.assertEqual(str(marker), "ignore[update] allow[hash-drift]")

    def test_keeps_only_the_given_verbs_directives(self):
        """Test that with a verb only that verb's directives are kept, the other verb's directive left out."""
        marker = Marker(raw="ignore[update] allow[hash-drift]")
        self.assertEqual(marker.raw_directives(Verb.IGNORE), "ignore[update]")
        self.assertEqual(marker.raw_directives(Verb.ALLOW), "allow[hash-drift]")

    def test_combined_scopes_stay_as_written(self):
        """Test that combined scopes are kept verbatim, not collapsed to a bare `ignore`."""
        marker = Marker(raw="ignore[update] ignore[stale]")
        self.assertEqual(marker.raw_directives(Verb.IGNORE), "ignore[update] ignore[stale]")

    def test_empty_without_a_matching_directive(self):
        """Test that a marker whose `raw` holds no directive of the verb yields an empty text."""
        self.assertEqual(Marker(raw="allow[update<3.13]").raw_directives(Verb.IGNORE), "")


class ParseMarkerScopeTest(unittest.TestCase):
    """Unit tests for what an `ignore` directive's bracket expresses."""

    def test_a_bare_item_holds_its_own_scope_back_alone(self):
        """Test that each scope named alone holds that scope back and no other."""
        for scope in _IGNORABLE_SCOPES:
            with self.subTest(scope=scope):
                marker = parse_marker(line(f"humanize==4.15.0  # update-time: ignore[{scope}]"))
                self.assertEqual(marker, Marker(ignored_scopes=scope))

    def test_a_scope_taking_no_bare_item_is_reported_as_invalid(self):
        """Test that a scope with no bare form is reported as an invalid item."""
        for scope in set(Scope) - set(_IGNORABLE_SCOPES):
            with self.subTest(scope=scope):
                marker = parse_marker(line(f"humanize==4.15.0  # update-time: ignore[{scope}]"))
                self.assertEqual(marker, Marker(invalid_item=str(scope)))

    def test_unrecognised_scope_is_reported_as_invalid(self):
        """Test that a typo'd scope is reported as an invalid item rather than standing in for a bare `ignore`."""
        marker = parse_marker(line("humanize==4.15.0  # update-time: ignore[updaet]"))
        self.assertEqual(marker, Marker(invalid_item="updaet"))

    def test_unterminated_bracket_is_reported_as_invalid(self):
        """Test that a bracket left unclosed is reported as an invalid item, the item keeping its `[`."""
        marker = parse_marker(line("humanize==4.15.0  # update-time: ignore[update<4"))
        self.assertEqual(marker, Marker(invalid_item="[update<4"))

    def test_only_a_bound_is_reported_without_its_update_prefix(self):
        """Test that an unreadable bound is reported as the specifier it holds, and every other item whole."""
        for case, (item, reported) in {
            "a bound": ("update<<3.13", "<<3.13"),
            "a day count": ("stale>=1.5", "stale>=1.5"),
            "a risk level": ("vulnerable<hgih", "vulnerable<hgih"),
        }.items():
            with self.subTest(case=case):
                marker = parse_marker(line(f"humanize==4.15.0  # update-time: allow[{item}]"))
                self.assertEqual(marker, Marker(invalid_item=reported))


class ParseMarkerAdvisoryTest(unittest.TestCase):
    """Unit tests for the advisory a `vulnerable=ID` bracket item names."""

    def test_the_identifier_names_the_advisory_to_silence(self):
        """Test that `ignore[vulnerable=GHSA-…]` names that advisory alone, holding no other check back."""
        marker = parse_marker(line(f"django==3.2.0  # update-time: ignore[vulnerable={_GHSA}]"))
        self.assertEqual(marker, Marker(ignored_advisories=frozenset({_GHSA})))

    def test_an_item_per_advisory_names_them_all(self):
        """Test that a bracket holding an item per advisory names every one of them, the items combining as a union."""
        marker = parse_marker(line(f"django==3.2.0  # update-time: ignore[vulnerable={_GHSA}, vulnerable={_CVE}]"))
        self.assertEqual(marker, Marker(ignored_advisories=frozenset({_GHSA, _CVE})))

    def test_a_list_of_identifiers_in_one_item_is_rejected(self):
        """Test that a second identifier behind a comma is reported as an invalid item, naming no advisory."""
        marker = parse_marker(line(f"django==3.2.0  # update-time: ignore[vulnerable={_GHSA},{_CVE}]"))
        self.assertEqual(marker, Marker(ignored_advisories=frozenset({_GHSA}), invalid_item=_CVE))

    def test_an_item_with_nothing_behind_the_equals_sign_is_rejected(self):
        """Test that `ignore[vulnerable=]` names no advisory and is reported as an invalid item."""
        marker = parse_marker(line("django==3.2.0  # update-time: ignore[vulnerable=]"))
        self.assertEqual(marker, Marker(invalid_item="vulnerable="))

    def test_allow_names_no_advisory(self):
        """Test that `allow[vulnerable=GHSA-…]` is reported as an invalid item."""
        marker = parse_marker(line(f"django==3.2.0  # update-time: allow[vulnerable={_GHSA}]"))
        self.assertEqual(marker, Marker(invalid_item=f"vulnerable={_GHSA}"))


class ParseMarkerVulnerabilityThresholdTest(unittest.TestCase):
    """Unit tests for the risk level a `vulnerable` bracket item sets as the threshold for the reference carrying it."""

    def test_either_verb_sets_the_threshold(self):
        """Test that `ignore[vulnerable<high]` and `allow[vulnerable>=high]` both set a threshold of high."""
        for verb, item in ((Verb.IGNORE, "vulnerable<high"), (Verb.ALLOW, "vulnerable>=high")):
            with self.subTest(item=item):
                marker = parse_marker(line(f"django==3.2.0  # update-time: {verb}[{item}]"))
                self.assertEqual(marker, Marker(vulnerable=Threshold(value="high")))

    def test_inverted_directions_set_no_threshold(self):
        """Test that an inverted comparison sets no threshold, the item carried for the caller to report."""
        for verb, item in ((Verb.ALLOW, "vulnerable<high"), (Verb.IGNORE, "vulnerable>=high")):
            with self.subTest(item=item):
                marker = parse_marker(line(f"django==3.2.0  # update-time: {verb}[{item}]"))
                self.assertEqual(marker, Marker(vulnerable=Threshold(inverted_item=item)))

    def test_a_level_the_command_line_takes_but_the_marker_language_does_not_is_rejected(self):
        """Test that a word only the command line accepts is reported, so a marker names a risk level alone."""
        for case, level in {"the command line's off switch": "none", "a level in upper case": "HIGH"}.items():
            item = f"vulnerable<{level}"
            with self.subTest(case=case):
                marker = parse_marker(line(f"django==3.2.0  # update-time: ignore[{item}]"))
                self.assertEqual(marker, Marker(invalid_item=item))

    def test_unreadable_risk_level_is_rejected(self):
        """Test that a level Update-time cannot read is reported, for either verb and either operator."""
        for verb in Verb:
            for operator in ("<", ">="):
                for level in ("hgih", ""):
                    item = f"vulnerable{operator}{level}"
                    with self.subTest(verb=verb, item=item):
                        marker = parse_marker(line(f"django==3.2.0  # update-time: {verb}[{item}]"))
                        self.assertEqual(marker, Marker(invalid_item=item))


class ParseMarkerStaleThresholdTest(unittest.TestCase):
    """Unit tests for the staleness threshold a `stale` bracket item sets for the reference carrying it."""

    def test_either_verb_sets_the_threshold(self):
        """Test that `ignore[stale<90]` and `allow[stale>=90]` both set a staleness threshold of 90 days."""
        for verb, item in ((Verb.IGNORE, "stale<90"), (Verb.ALLOW, "stale>=90")):
            with self.subTest(item=item):
                marker = parse_marker(line(f"humanize==4.15.0  # update-time: {verb}[{item}]"))
                self.assertEqual(marker, Marker(stale=Threshold(value=90)))

    def test_inverted_directions_set_no_threshold(self):
        """Test that `allow[stale<90]` and `ignore[stale>=90]` set no threshold, each carried as its inverted item."""
        for verb, item in ((Verb.ALLOW, "stale<90"), (Verb.IGNORE, "stale>=90")):
            with self.subTest(item=item):
                marker = parse_marker(line(f"humanize==4.15.0  # update-time: {verb}[{item}]"))
                self.assertEqual(marker, Marker(stale=Threshold(inverted_item=item)))


class ParseMarkerCooldownTest(unittest.TestCase):
    """Unit tests for the cooldown a `cooldown` bracket item sets for the reference carrying it."""

    def test_either_verb_sets_the_cooldown(self):
        """Test that `ignore[cooldown<30]` and `allow[cooldown>=30]` both set a cooldown of 30 days."""
        for verb, item in ((Verb.IGNORE, "cooldown<30"), (Verb.ALLOW, "cooldown>=30")):
            with self.subTest(item=item):
                marker = parse_marker(line(f"humanize==4.15.0  # update-time: {verb}[{item}]"))
                self.assertEqual(marker, Marker(cooldown=Threshold(value=30)))

    def test_inverted_directions_set_no_cooldown(self):
        """Test that `allow[cooldown<30]` and `ignore[cooldown>=30]` set no cooldown, each carried as its item."""
        for verb, item in ((Verb.ALLOW, "cooldown<30"), (Verb.IGNORE, "cooldown>=30")):
            with self.subTest(item=item):
                marker = parse_marker(line(f"humanize==4.15.0  # update-time: {verb}[{item}]"))
                self.assertEqual(marker, Marker(cooldown=Threshold(inverted_item=item)))

    @kills(
        Mutation(
            marker_module,
            "for scope in _IGNORABLE_SCOPES",
            "for scope in Scope",
            "a bare cooldown is accepted as a scope rather than rejected, because the check ranges over every "
            "scope instead of only the ignorable ones",
        )
    )
    def test_a_bare_cooldown_item_is_rejected(self):
        """Test that `cooldown` without a day count sets no cooldown, whichever verb it follows."""
        for verb in Verb:
            with self.subTest(verb=verb):
                marker = parse_marker(line(f"humanize==4.15.0  # update-time: {verb}[cooldown]"))
                self.assertEqual(marker, Marker(invalid_item="cooldown"))


class ParseMarkerDayCountTest(unittest.TestCase):
    """Unit tests for the day count both the `stale` and the `cooldown` bracket items take."""

    def test_malformed_day_count_is_rejected(self):
        """Test that a day count that is not a whole number of days is reported, for either item and either operator."""
        for keyword in ("stale", "cooldown"):
            for operator in ("<", ">="):
                for days in ("-5", "1.5", "10,<30"):
                    item = f"{keyword}{operator}{days}"
                    with self.subTest(item=item):
                        marker = parse_marker(line(f"humanize==4.15.0  # update-time: ignore[{item}]"))
                        self.assertEqual(marker, Marker(invalid_item=item))


class ParseMarkerPrecedenceTest(unittest.TestCase):
    """Unit tests that a directive on the reference's own line wins over one on the line above."""

    # The inline directive, the one on the line above, and the marker the two of them merge to.
    CASES = (
        ("allow[update<3.13]", "allow[update<4]", Marker(version_bound=bound(Verb.ALLOW, "update<3.13"))),
        ("ignore[stale<30]", "ignore[stale<90]", Marker(stale=Threshold(value=30))),
        ("ignore[cooldown<30]", "ignore[cooldown<90]", Marker(cooldown=Threshold(value=30))),
        ("ignore[vulnerable<high]", "ignore[vulnerable<critical]", Marker(vulnerable=Threshold(value="high"))),
        ("ignore[stale>=30]", "ignore[stale>=90]", Marker(stale=Threshold(inverted_item="stale>=30"))),
        ("ignore[cooldown>=30]", "ignore[cooldown>=90]", Marker(cooldown=Threshold(inverted_item="cooldown>=30"))),
        (
            "ignore[vulnerable>=high]",
            "ignore[vulnerable>=critical]",
            Marker(vulnerable=Threshold(inverted_item="vulnerable>=high")),
        ),
        ("ignore[stlae]", "ignore[updaet]", Marker(invalid_item="stlae")),
    )

    def test_an_inline_directive_beats_one_on_the_line_above(self):
        """Test that of two markers setting the same value, the inline one wins."""
        for inline, above, expected in self.CASES:
            with self.subTest(inline=inline):
                marker = parse_marker(line(f"image: python:3.12  # update-time: {inline}", f"# update-time: {above}"))
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
        marker = parse_marker(line("image: python:3.12  # update-time: ignore[update] ignore[stale]"))
        self.assertEqual(marker.raw, "ignore[update] ignore[stale]")

    def test_comma_combined_items_kept_together(self):
        """Test that comma-combined bracket items are kept as one directive, not split apart."""
        marker = parse_marker(line("image: python:3.14  # update-time: allow[update<3.15, hash-drift]"))
        self.assertEqual(marker.raw, "allow[update<3.15, hash-drift]")

    def test_unrecognised_scope_kept_as_written(self):
        """Test that a typo'd scope is kept as written, so the user is shown the item they actually typed."""
        marker = parse_marker(line("image: python:3.12  # update-time: ignore[updaet]"))
        self.assertEqual(marker.raw, "ignore[updaet]")

    def test_trailing_reason_left_out(self):
        """Test that free text after the last directive (a reason) is not part of the captured text."""
        marker = parse_marker(line("image: python:3.12  # update-time: ignore[stale] (until the migration)"))
        self.assertEqual(marker.raw, "ignore[stale]")

    def test_directives_across_two_lines_are_ordered_inline_first(self):
        """Test that a marker split over a line and the comment above it captures both, inline directives first."""
        marker = parse_marker(
            line("image: python:3.14  # update-time: allow[update<3.15]", "# update-time: ignore[stale]")
        )
        self.assertEqual(marker.raw, "allow[update<3.15] ignore[stale]")

    def test_no_marker_has_empty_raw(self):
        """Test that a line without a marker parses to an empty verbatim text, so nothing is echoed for it."""
        self.assertEqual(parse_marker(line("image: python:3.12")).raw, "")
