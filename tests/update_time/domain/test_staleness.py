"""Unit tests for the staleness module."""

import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from update_time.domain.dependency import DependencyVersion
from update_time.domain.staleness import STALE_AFTER, is_stale, warn_about_stale_dependencies
from update_time.primitives.location import Location

from tests.update_time.helpers import resolved_reference, staleness_disabled


class IsStaleTest(unittest.TestCase):
    """Unit tests for the is_stale helper."""

    def test_no_timestamp(self):
        """Test that a missing timestamp is never stale."""
        self.assertFalse(is_stale(None, STALE_AFTER.default))

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


class WarnAboutStaleDependenciesTest(unittest.TestCase):
    """Unit tests for the warn_about_stale_dependencies helper the delegating updaters share."""

    def setUp(self):
        """Create a file, a location in it, a resolved release, and a mock warning callback for the tests to share."""
        self.file = Path("pyproject.toml")
        self.location = Location(self.file, 4)
        self.release = DependencyVersion(version="1.0.0")
        self.warn = Mock()

    def test_warns_for_each_resolved_release(self):
        """Test that the warn callback is passed the resolved reference the resolver reported, and the threshold."""
        resolved = resolved_reference("humanize", self.location, self.release)
        newest_releases = Mock(return_value=[resolved])
        warn_about_stale_dependencies([self.file], newest_releases, self.warn)
        newest_releases.assert_called_once_with(self.file)
        self.warn.assert_called_once_with(resolved, STALE_AFTER.default)

    def test_disabled(self):
        """Test that a threshold of 0 skips the pass entirely, so the resolver never runs and makes no request."""
        newest_releases = Mock(return_value=[resolved_reference("humanize", self.location, self.release)])
        with staleness_disabled:
            warn_about_stale_dependencies([self.file], newest_releases, self.warn)
        newest_releases.assert_not_called()
        self.warn.assert_not_called()
