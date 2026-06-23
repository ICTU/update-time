"""Unit tests for the cooldown module."""

import unittest
from datetime import UTC, datetime, timedelta, timezone

from update_time.domain.cooldown import COOLDOWN_DAYS, within_cooldown


class WithinCooldownTest(unittest.TestCase):
    """Unit tests for the within_cooldown helper."""

    def test_no_timestamp(self):
        """Test that a missing timestamp is not within the cooldown period."""
        self.assertFalse(within_cooldown(None))

    def test_recent_timestamp(self):
        """Test that a timestamp from within the cooldown period is reported as within cooldown."""
        self.assertTrue(within_cooldown(datetime.now(UTC) - timedelta(days=1)))

    def test_just_within_cooldown(self):
        """Test that a timestamp just inside the cooldown period is reported as within cooldown."""
        self.assertTrue(within_cooldown(datetime.now(UTC) - timedelta(days=COOLDOWN_DAYS, hours=-1)))

    def test_old_timestamp(self):
        """Test that a timestamp from before the cooldown period is not within cooldown."""
        self.assertFalse(within_cooldown(datetime.now(UTC) - timedelta(days=COOLDOWN_DAYS, hours=1)))

    def test_future_timestamp(self):
        """Test that a timestamp in the future is within the cooldown period."""
        self.assertTrue(within_cooldown(datetime.now(UTC) + timedelta(days=1)))

    def test_non_utc_timestamp(self):
        """Test that a recent timestamp in a non-UTC timezone is within the cooldown period."""
        recent = datetime.now(timezone(timedelta(hours=5))) - timedelta(days=1)
        self.assertTrue(within_cooldown(recent))
