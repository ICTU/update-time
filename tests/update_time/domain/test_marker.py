"""Unit tests for the marker domain object."""

import unittest
from pathlib import Path

from update_time.domain.bound import Verb
from update_time.domain.line import Line
from update_time.domain.marker import DayCount, Marker, parse_marker
from update_time.primitives.location import Location

from tests.update_time.helpers import bound


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
        self.assertEqual(marker, Marker(invalid_specifier="updaet"))

    def test_unterminated_bracket_is_reported_as_invalid(self):
        """Test that a bracket left unclosed is reported as an invalid item, the item keeping its `[`."""
        marker = parse_marker(line("humanize==4.15.0  # update-time: ignore[update<4"))
        self.assertEqual(marker, Marker(invalid_specifier="[update<4"))


class ParseMarkerStaleThresholdTest(unittest.TestCase):
    """Unit tests for the staleness threshold a `stale` bracket item sets for the reference carrying it."""

    def test_day_count_sets_the_threshold(self):
        """Test that `ignore[stale<90]` sets the reference's own staleness threshold to 90 days."""
        marker = parse_marker(line("humanize==4.15.0  # update-time: ignore[stale<90]"))
        self.assertEqual(marker, Marker(stale=DayCount(days=90)))

    def test_the_complement_verb_spells_the_same_threshold(self):
        """Test that `allow[stale>=90]` sets the same 90-day threshold as `ignore[stale<90]`.

        The verbs are exact complements, so the two spellings name the same ages to warn about: 90 days and older.
        """
        marker = parse_marker(line("humanize==4.15.0  # update-time: allow[stale>=90]"))
        self.assertEqual(marker, Marker(stale=DayCount(days=90)))

    def test_inverted_directions_set_no_threshold(self):
        """Test that `allow[stale<90]` and `ignore[stale>=90]` set no threshold."""
        allowed = parse_marker(line("humanize==4.15.0  # update-time: allow[stale<90]"))
        self.assertEqual(allowed, Marker(stale=DayCount(inverted_item="stale<90")))
        ignored = parse_marker(line("humanize==4.15.0  # update-time: ignore[stale>=90]"))
        self.assertEqual(ignored, Marker(stale=DayCount(inverted_item="stale>=90")))


class ParseMarkerCooldownTest(unittest.TestCase):
    """Unit tests for the cooldown a `cooldown` bracket item sets for the reference carrying it."""

    def test_day_count_sets_the_cooldown(self):
        """Test that `ignore[cooldown<30]` sets the reference's own cooldown to 30 days."""
        marker = parse_marker(line("humanize==4.15.0  # update-time: ignore[cooldown<30]"))
        self.assertEqual(marker, Marker(cooldown=DayCount(days=30)))

    def test_the_complement_verb_spells_the_same_cooldown(self):
        """Test that `allow[cooldown>=30]` sets the same 30-day cooldown as `ignore[cooldown<30]`.

        The verbs are exact complements, so the two spellings name the same ages to adopt: 30 days and older.
        """
        marker = parse_marker(line("humanize==4.15.0  # update-time: allow[cooldown>=30]"))
        self.assertEqual(marker, Marker(cooldown=DayCount(days=30)))

    def test_inverted_directions_set_no_cooldown(self):
        """Test that `allow[cooldown<30]` and `ignore[cooldown>=30]` set no cooldown."""
        allowed = parse_marker(line("humanize==4.15.0  # update-time: allow[cooldown<30]"))
        self.assertEqual(allowed, Marker(cooldown=DayCount(inverted_item="cooldown<30")))
        ignored = parse_marker(line("humanize==4.15.0  # update-time: ignore[cooldown>=30]"))
        self.assertEqual(ignored, Marker(cooldown=DayCount(inverted_item="cooldown>=30")))

    def test_a_bare_cooldown_item_is_rejected(self):
        """Test that `cooldown` without a day count sets no cooldown, whichever verb it follows.

        Read as English, "ignore the cooldown" asks to adopt at once; read through the grammar, where `ignore` drops
        what it names, the directive drops every candidate, which is a freeze. The two readings are opposites, so
        rather than guess at one, Update-time reports the item and leaves the reference unchanged.
        """
        for verb in Verb:
            with self.subTest(verb=verb):
                marker = parse_marker(line(f"humanize==4.15.0  # update-time: {verb}[cooldown]"))
                self.assertEqual(marker, Marker(invalid_specifier="cooldown"))


class ParseMarkerDayCountTest(unittest.TestCase):
    """Unit tests for the day count both the `stale` and the `cooldown` bracket items take."""

    def test_malformed_day_count_is_rejected(self):
        """Test that a day count that is not a whole number of days is reported, for either item and either operator.

        The count is judged before the direction, so `stale>=1.5` and `cooldown>=1.5` are reported as unreadable
        counts rather than as inverted comparisons.
        """
        for keyword in ("stale", "cooldown"):
            for operator in ("<", ">="):
                for days in ("-5", "1.5", "10,<30"):
                    item = f"{keyword}{operator}{days}"
                    with self.subTest(item=item):
                        marker = parse_marker(line(f"humanize==4.15.0  # update-time: ignore[{item}]"))
                        self.assertEqual(marker, Marker(invalid_specifier=item))


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
            ("ignore[stale<30]", "ignore[stale<90]", Marker(stale=DayCount(days=30))),
            ("ignore[cooldown<30]", "ignore[cooldown<90]", Marker(cooldown=DayCount(days=30))),
            ("ignore[stale>=30]", "ignore[stale>=90]", Marker(stale=DayCount(inverted_item="stale>=30"))),
            ("ignore[cooldown>=30]", "ignore[cooldown>=90]", Marker(cooldown=DayCount(inverted_item="cooldown>=30"))),
            ("ignore[stlae]", "ignore[updaet]", Marker(invalid_specifier="stlae")),
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
