"""Unit tests for the timestamp module."""

import unittest
from datetime import UTC, datetime, timedelta, timezone

from update_time.primitives.timestamp import days_since, newest_timestamp, parse_timestamp


class DaysSinceTest(unittest.TestCase):
    """Unit tests for the days_since helper."""

    def test_days_ago(self):
        """Test that the number of whole days since the timestamp is returned, the part day dropped."""
        self.assertEqual(days_since(datetime.now(UTC) - timedelta(days=10, hours=1)), 10)

    def test_future_timestamp(self):
        """Test that a timestamp in the future counts as a negative number of days."""
        self.assertEqual(days_since(datetime.now(UTC) + timedelta(days=1)), -1)

    def test_non_utc_timestamp(self):
        """Test that a timestamp in another timezone is measured against the same instant, not the same clock time."""
        self.assertEqual(days_since(datetime.now(timezone(timedelta(hours=5))) - timedelta(days=1)), 1)

    def test_oldest_representable_timestamp(self):
        """Test that the earliest date there is yields a count rather than an error.

        The count is an `int`, so it compares against a day count of any size. It is the `timedelta` such a count
        would otherwise have to become that caps, at 999999999 days.
        """
        self.assertGreater(days_since(datetime.min.replace(tzinfo=UTC)), 700_000)


class ParseTimestampTest(unittest.TestCase):
    """Unit tests for the parse_timestamp helper."""

    def test_timestamp(self):
        """Test that an ISO-8601 timestamp is parsed."""
        self.assertEqual(parse_timestamp("2024-06-01T12:00:00Z"), datetime(2024, 6, 1, 12, tzinfo=UTC))

    def test_no_timestamp(self):
        """Test that a registry reporting no timestamp yields None rather than raising."""
        for missing in (None, ""):
            with self.subTest(missing=missing):
                self.assertIsNone(parse_timestamp(missing))


class NewestTimestampTest(unittest.TestCase):
    """Unit tests for the newest_timestamp helper."""

    def test_empty(self):
        """Test that no timestamps yields None."""
        self.assertIsNone(newest_timestamp([]))

    def test_newest(self):
        """Test that the most recent of several ISO-8601 timestamps is returned, whatever their order."""
        timestamps = ["2020-01-01T00:00:00Z", "2024-06-01T12:00:00Z", "2022-03-03T03:03:03Z"]
        self.assertEqual(datetime(2024, 6, 1, 12, tzinfo=UTC), newest_timestamp(timestamps))

    def test_absent_timestamp_is_skipped(self):
        """Test that a timestamp that isn't there is skipped, so the ones that are still yield the newest."""
        for missing in (None, ""):
            with self.subTest(missing=missing):
                timestamps = ["2024-06-01T12:00:00Z", missing]
                self.assertEqual(datetime(2024, 6, 1, 12, tzinfo=UTC), newest_timestamp(timestamps))

    def test_all_timestamps_absent(self):
        """Test that timestamps that are all absent yield None, as no timestamps at all do."""
        self.assertIsNone(newest_timestamp([None, ""]))
