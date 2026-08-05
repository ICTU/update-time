"""Unit tests for the cooldown module."""

import unittest
from datetime import UTC, datetime, timedelta

from update_time.domain.cooldown import COOLDOWN, cooldown_cutoff, within_cooldown

from tests.helpers import patch_environ


class WithinCooldownTest(unittest.TestCase):
    """Unit tests for the within_cooldown helper."""

    def test_no_timestamp(self):
        """Test that a missing timestamp is not within the cooldown period."""
        self.assertFalse(within_cooldown(None, COOLDOWN.default))

    def test_cooldown_argument(self):
        """Test that within_cooldown honours the cooldown period passed to it."""
        timestamp = datetime.now(UTC) - timedelta(days=10)
        self.assertTrue(within_cooldown(timestamp, 30))
        self.assertFalse(within_cooldown(timestamp, 5))

    def test_cooldown_longer_than_a_date_interval_can_hold(self):
        """Test that a cooldown of more days than a `timedelta` holds is honoured rather than aborting the run.

        A marker names its own cooldown, so the number comes from a file rather than from an operator, and a
        `timedelta` caps at 999999999 days. Whole days are compared, so no interval is built from the count.
        """
        self.assertTrue(within_cooldown(datetime.now(UTC) - timedelta(days=1), 10**12))

    def test_just_within_cooldown(self):
        """Test that a timestamp just inside the cooldown period is reported as within cooldown."""
        timestamp = datetime.now(UTC) - timedelta(days=COOLDOWN.default, hours=-1)
        self.assertTrue(within_cooldown(timestamp, COOLDOWN.default))

    def test_old_timestamp(self):
        """Test that a timestamp from before the cooldown period is not within cooldown."""
        timestamp = datetime.now(UTC) - timedelta(days=COOLDOWN.default, hours=1)
        self.assertFalse(within_cooldown(timestamp, COOLDOWN.default))

    def test_future_timestamp(self):
        """Test that a timestamp in the future is within the cooldown period."""
        self.assertTrue(within_cooldown(datetime.now(UTC) + timedelta(days=1), COOLDOWN.default))


class CooldownCutoffTest(unittest.TestCase):
    """Unit tests for the cooldown expressed as the cutoff instant uv's `--exclude-newer` wants."""

    def test_cutoff_is_the_cooldown_ago(self):
        """Test that the cutoff is the instant the cooldown reaches back to."""
        with patch_environ({COOLDOWN.name: "30"}):
            cutoff = datetime.fromisoformat(cooldown_cutoff())
        self.assertEqual((datetime.now(UTC) - cutoff).days, 30)

    def test_cutoff_reaching_past_the_earliest_date(self):
        """Test that a cooldown reaching further back than a date can express yields the earliest instant.

        A cooldown that long excludes every release anyway, which is what the earliest instant tells uv, so it is
        clamped rather than aborting the run.
        """
        with patch_environ({COOLDOWN.name: str(10**12)}):
            self.assertEqual(cooldown_cutoff(), datetime.min.replace(tzinfo=UTC).isoformat())
