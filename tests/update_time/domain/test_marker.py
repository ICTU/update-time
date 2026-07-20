"""Unit tests for the marker domain object."""

import unittest

from update_time.domain.bound import Verb
from update_time.domain.marker import Marker, parse_marker


class MergeTest(unittest.TestCase):
    """Unit tests for folding two markers into one."""

    def test_raw_texts_concatenate_in_order(self):
        """Test that merging keeps both verbatim directive runs, this marker's first, so the echo reads in order."""
        merged = Marker(raw="allow[update<3.13]").merge(Marker(raw="ignore[stale]"))
        self.assertEqual(merged.raw, "allow[update<3.13] ignore[stale]")

    def test_raw_is_excluded_from_equality(self):
        """Test that two markers meaning the same thing compare equal however their directives were spelled."""
        self.assertEqual(Marker(ignore_update=True, raw="ignore[update]"), Marker(ignore_update=True))


class RawMarkerTest(unittest.TestCase):
    """Unit tests for reading a marker's verbatim directive text back out of its `raw` text."""

    def test_whole_marker_without_a_verb(self):
        """Test that without a verb the whole `raw` text is returned unchanged."""
        marker = Marker(raw="ignore[update] allow[digest-drift]")
        self.assertEqual(marker.raw_marker(), "ignore[update] allow[digest-drift]")

    def test_keeps_only_the_given_verbs_directives(self):
        """Test that with a verb only that verb's directives are kept, the other verb's directive left out."""
        marker = Marker(raw="ignore[update] allow[digest-drift]")
        self.assertEqual(marker.raw_marker(Verb.IGNORE), "ignore[update]")
        self.assertEqual(marker.raw_marker(Verb.ALLOW), "allow[digest-drift]")

    def test_combined_scopes_stay_as_written(self):
        """Test that combined scopes are kept verbatim, not collapsed to a bare `ignore`."""
        marker = Marker(raw="ignore[update] ignore[stale]")
        self.assertEqual(marker.raw_marker(Verb.IGNORE), "ignore[update] ignore[stale]")

    def test_empty_without_a_matching_directive(self):
        """Test that a marker whose `raw` holds no directive of the verb yields an empty text."""
        self.assertEqual(Marker(raw="allow[update<3.13]").raw_marker(Verb.IGNORE), "")


class ParseMarkerRawTest(unittest.TestCase):
    """Unit tests that `parse_marker` captures the directive text verbatim."""

    def test_verbatim_directives(self):
        """Test that the parsed marker keeps the directives exactly as written."""
        marker = parse_marker("image: python:3.12  # update-time: ignore[update] ignore[stale]", "")
        self.assertEqual(marker.raw, "ignore[update] ignore[stale]")

    def test_comma_combined_items_kept_together(self):
        """Test that comma-combined bracket items are kept as one directive, not split apart."""
        marker = parse_marker("image: python:3.14  # update-time: allow[update<3.15, digest-drift]", "")
        self.assertEqual(marker.raw, "allow[update<3.15, digest-drift]")

    def test_unrecognised_scope_kept_as_written(self):
        """Test that a typo'd scope is kept as written even though it parses as a bare `ignore`."""
        marker = parse_marker("image: python:3.12  # update-time: ignore[updaet]", "")
        self.assertEqual(marker.raw, "ignore[updaet]")

    def test_trailing_reason_left_out(self):
        """Test that free text after the last directive (a reason) is not part of the captured text."""
        marker = parse_marker("image: python:3.12  # update-time: ignore[stale] (until the migration)", "")
        self.assertEqual(marker.raw, "ignore[stale]")

    def test_directives_across_two_lines_are_ordered_inline_first(self):
        """Test that a marker split over a line and the comment above it captures both, inline directives first."""
        marker = parse_marker("image: python:3.14  # update-time: allow[update<3.15]", "# update-time: ignore[stale]")
        self.assertEqual(marker.raw, "allow[update<3.15] ignore[stale]")

    def test_no_marker_has_empty_raw(self):
        """Test that a line without a marker parses to an empty verbatim text, so nothing is echoed for it."""
        self.assertEqual(parse_marker("image: python:3.12", "").raw, "")
