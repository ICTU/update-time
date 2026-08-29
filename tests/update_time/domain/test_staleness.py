"""Unit tests for the staleness module."""

import unittest
from datetime import UTC, datetime, timedelta, timezone

from update_time.domain.staleness import STALE_AFTER, is_stale


class IsStaleTest(unittest.TestCase):
    """Unit tests for the is_stale helper."""

    def test_disabled(self):
        """Test that a threshold of 0 disables the check, so nothing is stale."""
        old = datetime.now(UTC) - timedelta(days=STALE_AFTER.default * 10)
        self.assertFalse(is_stale(old, 0))

    def test_old_timestamp(self):
        """Test that a timestamp older than the threshold is stale."""
        old = datetime.now(UTC) - timedelta(days=STALE_AFTER.default + 1)
        self.assertTrue(is_stale(old, STALE_AFTER.default))

    def test_recent_timestamp(self):
        """Test that a timestamp newer than the threshold is not stale."""
        self.assertFalse(is_stale(datetime.now(UTC) - timedelta(days=1), STALE_AFTER.default))

    def test_boundary_compares_whole_days(self):
        """Test that the threshold is compared in whole days, so a fractional day over it is not yet stale."""
        boundary = datetime.now(UTC) - timedelta(days=STALE_AFTER.default, hours=12)
        self.assertFalse(is_stale(boundary, STALE_AFTER.default))

    def test_judges_against_the_given_threshold(self):
        """Test that is_stale judges the same timestamp differently against two different thresholds."""
        timestamp = datetime.now(UTC) - timedelta(days=STALE_AFTER.default + 1)
        self.assertTrue(is_stale(timestamp, STALE_AFTER.default))
        self.assertFalse(is_stale(timestamp, STALE_AFTER.default * 2))

    def test_future_timestamp(self):
        """Test that a timestamp in the future is not stale."""
        self.assertFalse(is_stale(datetime.now(UTC) + timedelta(days=1), STALE_AFTER.default))

    def test_non_utc_timestamp(self):
        """Test that an old timestamp in a non-UTC timezone is stale."""
        old = datetime.now(timezone(timedelta(hours=5))) - timedelta(days=STALE_AFTER.default + 1)
        self.assertTrue(is_stale(old, STALE_AFTER.default))
