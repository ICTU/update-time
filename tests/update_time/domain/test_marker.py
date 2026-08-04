"""Unit tests for the marker domain object."""

import unittest
from pathlib import Path

from update_time.domain.bound import Verb
from update_time.domain.line import Line
from update_time.domain.marker import Marker, parse_marker
from update_time.primitives.location import Location


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
        self.assertEqual(marker, Marker(stale_after_days=90))

    def test_the_complement_verb_spells_the_same_threshold(self):
        """Test that `allow[stale>=90]` sets the same 90-day threshold as `ignore[stale<90]`.

        The verbs are exact complements, so the two spellings name the same ages to warn about: 90 days and older.
        """
        marker = parse_marker(line("humanize==4.15.0  # update-time: allow[stale>=90]"))
        self.assertEqual(marker, Marker(stale_after_days=90))

    def test_inverted_directions_set_no_threshold(self):
        """Test that `allow[stale<90]` and `ignore[stale>=90]` set no threshold."""
        allowed = parse_marker(line("humanize==4.15.0  # update-time: allow[stale<90]"))
        self.assertEqual(allowed, Marker(inverted_stale_item="stale<90"))
        ignored = parse_marker(line("humanize==4.15.0  # update-time: ignore[stale>=90]"))
        self.assertEqual(ignored, Marker(inverted_stale_item="stale>=90"))

    def test_malformed_day_count_is_rejected(self):
        """Test that a day count that is not a whole number of days is reported, whichever way the comparison runs.

        The count is judged before the direction, so `stale>=1.5` is reported as an unreadable count rather than as
        an inverted comparison.
        """
        for item in ("stale<-5", "stale<1.5", "stale<10,<30", "stale>=-5", "stale>=1.5"):
            with self.subTest(item=item):
                marker = parse_marker(line(f"humanize==4.15.0  # update-time: ignore[{item}]"))
                self.assertEqual(marker, Marker(invalid_specifier=item))


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
