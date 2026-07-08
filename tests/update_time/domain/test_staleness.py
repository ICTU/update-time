"""Unit tests for the staleness module."""

import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from update_time.domain.staleness import (
    STALE_AFTER_DAYS,
    STALE_AFTER_DAYS_ENV_VAR,
    is_stale,
    newest_datetime,
    stale_after_days,
    staleness_days,
    warn_about_stale_dependencies,
)
from update_time.domain.version import DependencyVersion


class StaleAfterDaysTest(unittest.TestCase):
    """Unit tests for the stale_after_days helper."""

    def test_default(self):
        """Test that the threshold defaults to the default staleness period when the env var is not set."""
        with patch.dict("os.environ", clear=True):
            self.assertEqual(STALE_AFTER_DAYS, stale_after_days())

    def test_env_var(self):
        """Test that the threshold is read from the env var when set."""
        with patch.dict("os.environ", {STALE_AFTER_DAYS_ENV_VAR: "30"}):
            self.assertEqual(30, stale_after_days())


class IsStaleTest(unittest.TestCase):
    """Unit tests for the is_stale helper."""

    def test_no_timestamp(self):
        """Test that a missing timestamp is never stale."""
        self.assertFalse(is_stale(None))

    def test_disabled(self):
        """Test that a threshold of 0 disables the check, so nothing is stale."""
        old = datetime.now(UTC) - timedelta(days=STALE_AFTER_DAYS * 10)
        with patch.dict("os.environ", {STALE_AFTER_DAYS_ENV_VAR: "0"}):
            self.assertFalse(is_stale(old))

    def test_old_timestamp(self):
        """Test that a timestamp older than the threshold is stale."""
        self.assertTrue(is_stale(datetime.now(UTC) - timedelta(days=STALE_AFTER_DAYS + 1)))

    def test_recent_timestamp(self):
        """Test that a timestamp newer than the threshold is not stale."""
        self.assertFalse(is_stale(datetime.now(UTC) - timedelta(days=1)))

    def test_boundary_compares_whole_days(self):
        """Test that the threshold is compared in whole days, so a fractional day over it is not yet stale.

        This keeps the decision in step with the reported day count, so a "N days ago (> N)" message never appears.
        """
        self.assertFalse(is_stale(datetime.now(UTC) - timedelta(days=STALE_AFTER_DAYS, hours=12)))

    def test_configured_threshold(self):
        """Test that is_stale honours the threshold from the env var."""
        timestamp = datetime.now(UTC) - timedelta(days=STALE_AFTER_DAYS + 1)
        self.assertTrue(is_stale(timestamp))
        with patch.dict("os.environ", {STALE_AFTER_DAYS_ENV_VAR: str(STALE_AFTER_DAYS * 2)}):
            self.assertFalse(is_stale(timestamp))

    def test_future_timestamp(self):
        """Test that a timestamp in the future is not stale."""
        self.assertFalse(is_stale(datetime.now(UTC) + timedelta(days=1)))

    def test_non_utc_timestamp(self):
        """Test that an old timestamp in a non-UTC timezone is stale."""
        old = datetime.now(timezone(timedelta(hours=5))) - timedelta(days=STALE_AFTER_DAYS + 1)
        self.assertTrue(is_stale(old))


class StalenessDaysTest(unittest.TestCase):
    """Unit tests for the staleness_days helper."""

    def test_days_ago(self):
        """Test that the number of whole days since publication is returned."""
        self.assertEqual(10, staleness_days(datetime.now(UTC) - timedelta(days=10, hours=1)))


class NewestDatetimeTest(unittest.TestCase):
    """Unit tests for the newest_datetime helper."""

    def test_empty(self):
        """Test that no timestamps yields None."""
        self.assertIsNone(newest_datetime([]))

    def test_newest(self):
        """Test that the most recent of several ISO-8601 timestamps is returned, whatever their order."""
        timestamps = ["2020-01-01T00:00:00Z", "2024-06-01T12:00:00Z", "2022-03-03T03:03:03Z"]
        self.assertEqual(datetime(2024, 6, 1, 12, tzinfo=UTC), newest_datetime(timestamps))


class WarnAboutStaleDependenciesTest(unittest.TestCase):
    """Unit tests for the warn_about_stale_dependencies helper the manifest updaters share."""

    def setUp(self):
        """Create a file, a resolved release, and a mock warning callback for the tests to share."""
        self.file = Path("pyproject.toml")
        self.release = DependencyVersion(version="1.0.0")
        self.warn = Mock()

    def test_warns_for_each_resolved_release(self):
        """Test that the warn callback is called with the name, release, and file for every resolved dependency."""
        newest_releases = Mock(return_value=[("humanize", self.release)])
        warn_about_stale_dependencies([self.file], newest_releases, self.warn)
        newest_releases.assert_called_once_with(self.file)
        self.warn.assert_called_once_with("humanize", self.release, self.file)

    def test_skips_unresolved_releases(self):
        """Test that a dependency whose newest release can't be resolved (None) is skipped, not warned about."""
        newest_releases = Mock(return_value=[("humanize", None), ("rich", self.release)])
        warn_about_stale_dependencies([self.file], newest_releases, self.warn)
        self.warn.assert_called_once_with("rich", self.release, self.file)

    def test_disabled(self):
        """Test that a threshold of 0 skips the pass entirely, so the resolver never runs and makes no request."""
        newest_releases = Mock(return_value=[("humanize", self.release)])
        with patch.dict("os.environ", {STALE_AFTER_DAYS_ENV_VAR: "0"}):
            warn_about_stale_dependencies([self.file], newest_releases, self.warn)
        newest_releases.assert_not_called()
        self.warn.assert_not_called()
