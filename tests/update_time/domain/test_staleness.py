"""Unit tests for the staleness module."""

import unittest
from datetime import UTC, datetime, timedelta, timezone

from update_time.domain.dependency import DependencyVersion, Project, Release
from update_time.domain.staleness import STALE_AFTER, is_stale, release_judged_alike

from tests.mutation import Mutation, kills
from tests.update_time.helpers import days_ago


class IsStaleTest(unittest.TestCase):
    """Unit tests for the is_stale helper."""

    def test_disabled(self):
        """Test that a threshold of 0 disables the check, so nothing is stale."""
        old = days_ago(STALE_AFTER.default * 10)
        self.assertFalse(is_stale(old, 0))

    def test_old_timestamp(self):
        """Test that a timestamp older than the threshold is stale."""
        old = days_ago(STALE_AFTER.default + 1)
        self.assertTrue(is_stale(old, STALE_AFTER.default))

    def test_recent_timestamp(self):
        """Test that a timestamp newer than the threshold is not stale."""
        self.assertFalse(is_stale(days_ago(1), STALE_AFTER.default))

    def test_boundary_compares_whole_days(self):
        """Test that the threshold is compared in whole days, so a fractional day over it is not yet stale."""
        boundary = datetime.now(UTC) - timedelta(days=STALE_AFTER.default, hours=12)
        self.assertFalse(is_stale(boundary, STALE_AFTER.default))

    def test_judges_against_the_given_threshold(self):
        """Test that is_stale judges the same timestamp differently against two different thresholds."""
        timestamp = days_ago(STALE_AFTER.default + 1)
        self.assertTrue(is_stale(timestamp, STALE_AFTER.default))
        self.assertFalse(is_stale(timestamp, STALE_AFTER.default * 2))

    def test_future_timestamp(self):
        """Test that a timestamp in the future is not stale."""
        self.assertFalse(is_stale(datetime.now(UTC) + timedelta(days=1), STALE_AFTER.default))

    def test_non_utc_timestamp(self):
        """Test that an old timestamp in a non-UTC timezone is stale."""
        old = datetime.now(timezone(timedelta(hours=5))) - timedelta(days=STALE_AFTER.default + 1)
        self.assertTrue(is_stale(old, STALE_AFTER.default))


class ReleaseJudgedAlikeTest(unittest.TestCase):
    """Unit tests for telling whether two staleness thresholds judge a dependency's newest release alike."""

    @kills(
        Mutation(
            release_judged_alike,
            ") != is_stale(",
            ") or is_stale(",
            "a threshold that warns about the release, as the run does, is never reported as redundant",
        )
    )
    def test_a_release_both_thresholds_judge_alike(self):
        """Test that a release is returned where neither threshold warns about it, and where both do."""
        for case, age in {"neither warns": 30, "both warn": 500}.items():
            with self.subTest(case=case):
                newest = Release("4.15.0", days_ago(age))
                version = DependencyVersion("4.15.0", project=Project(newest=newest))
                self.assertEqual(release_judged_alike(version, 90, STALE_AFTER.default), newest)
