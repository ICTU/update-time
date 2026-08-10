"""Unit tests for the marker domain object."""

import unittest
from pathlib import Path

from update_time.domain.bound import Verb
from update_time.domain.line import Line
from update_time.domain.marker import Marker, Threshold, parse_marker
from update_time.primitives.location import Location

from tests.update_time.helpers import bound

# Advisory identifiers, for the tests of the `vulnerable=ID` bracket item. An advisory is known by identifiers of
# different shapes, and Update-time takes whichever the reader writes.
_GHSA = "GHSA-2gwj-7jmv-h26r"
_CVE = "CVE-2022-28346"


def line(text: str, previous_text: str = "") -> Line:
    """Return the text as a line of a file, with the text of the line above it."""
    return Line(text, previous_text, Location(Path("conf.py"), 1))


class MergeTest(unittest.TestCase):
    """Unit tests for folding two markers into one."""

    def test_raw_texts_concatenate_in_order(self):
        """Test that merging keeps both verbatim directive runs, this marker's first, so the echo reads in order."""
        merged = Marker(raw="allow[update<3.13]").merge(Marker(raw="ignore[stale]"))
        self.assertEqual(merged.raw, "allow[update<3.13] ignore[stale]")

    def test_raw_is_excluded_from_equality(self):
        """Test that two markers meaning the same thing compare equal however their directives were spelled."""
        self.assertEqual(Marker(ignore_update=True, raw="ignore[update]"), Marker(ignore_update=True))


class HoldsEverythingBackTest(unittest.TestCase):
    """Unit tests for whether a marker leaves any check to run, and so any source to query."""

    def test_only_a_marker_naming_every_scope_holds_everything_back(self):
        """Test that a bare `ignore`, and every scope named together, hold everything back where one scope does not."""
        cases = {
            "ignore": True,
            "ignore[update, stale, yanked, vulnerable]": True,
            "ignore[update]": False,
            "ignore[stale]": False,
            "ignore[yanked]": False,
            "ignore[vulnerable]": False,
        }
        for directive, expected in cases.items():
            with self.subTest(directive=directive):
                marker = parse_marker(line(f"dependency  # update-time: {directive}"))
                self.assertEqual(marker.holds_everything_back, expected)


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

    def test_ignore_yanked(self):
        """Test that `ignore[yanked]` holds back only the yank warning, leaving the update and staleness live."""
        marker = parse_marker(line("humanize==4.15.0  # update-time: ignore[yanked]"))
        self.assertEqual(marker, Marker(ignore_yanked=True))

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

    def test_an_inline_directive_beats_one_on_the_line_above(self):
        """Test that of two markers setting the same value, the inline one wins, for every value one can carry.

        These are the values that cannot combine: the three a reference sets deliberately, and the three reporting
        a comparison or an item Update-time could not honour. The hold-backs combine as unions instead, so there is
        nothing for one line to win over the other about.
        """
        cases = (
            ("allow[update<3.13]", "allow[update<4]", Marker(version_bound=bound(Verb.ALLOW, "update<3.13"))),
            ("ignore[stale<30]", "ignore[stale<90]", Marker(stale=Threshold(value=30))),
            ("ignore[cooldown<30]", "ignore[cooldown<90]", Marker(cooldown=Threshold(value=30))),
            ("ignore[stale>=30]", "ignore[stale>=90]", Marker(stale=Threshold(inverted_item="stale>=30"))),
            ("ignore[cooldown>=30]", "ignore[cooldown>=90]", Marker(cooldown=Threshold(inverted_item="cooldown>=30"))),
            ("ignore[stlae]", "ignore[updaet]", Marker(invalid_item="stlae")),
        )
        for inline, above, expected in cases:
            with self.subTest(inline=inline):
                marker = parse_marker(line(f"image: python:3.12  # update-time: {inline}", f"# update-time: {above}"))
                self.assertEqual(marker, expected)


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
