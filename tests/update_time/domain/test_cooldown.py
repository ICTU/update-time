"""Unit tests for the cooldown module."""

import unittest
from datetime import UTC, datetime, timedelta, timezone

from update_time.domain.cooldown import COOLDOWN_DAYS, COOLDOWN_DAYS_ENV_VAR, cooldown_days, within_cooldown

from tests.update_time.helpers import patch_environ


class CooldownDaysTest(unittest.TestCase):
    """Unit tests for the cooldown_days helper."""

    def test_default(self):
        """Test that the cooldown defaults to the default cooldown period when the env var is not set."""
        with patch_environ():
            self.assertEqual(COOLDOWN_DAYS, cooldown_days())

    def test_env_var(self):
        """Test that the cooldown is read from the env var when set."""
        with patch_environ({COOLDOWN_DAYS_ENV_VAR: "14"}):
            self.assertEqual(14, cooldown_days())


class WithinCooldownTest(unittest.TestCase):
    """Unit tests for the within_cooldown helper."""

    def test_no_timestamp(self):
        """Test that a missing timestamp is not within the cooldown period."""
        self.assertFalse(within_cooldown(None))

    def test_configured_cooldown(self):
        """Test that within_cooldown honours the cooldown period from the env var."""
        timestamp = datetime.now(UTC) - timedelta(days=COOLDOWN_DAYS + 1)
        self.assertFalse(within_cooldown(timestamp))
        with patch_environ({COOLDOWN_DAYS_ENV_VAR: str(COOLDOWN_DAYS + 2)}):
            self.assertTrue(within_cooldown(timestamp))

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
