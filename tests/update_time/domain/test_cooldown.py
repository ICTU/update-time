"""Unit tests for the cooldown module."""

import unittest
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from update_time.domain import cooldown
from update_time.domain.cooldown import (
    COOLDOWN,
    CooldownWalk,
    cooldown_cutoff,
    first_eligible,
    within_cooldown,
)
from update_time.domain.dependency import DependencyVersion, Release

from tests.mutation import Mutation, kills
from tests.update_time.helpers import days_ago

if TYPE_CHECKING:
    from collections.abc import Callable


class WithinCooldownTest(unittest.TestCase):
    """Unit tests for the within_cooldown helper."""

    def test_no_timestamp(self):
        """Test that a missing timestamp is not within the cooldown period."""
        self.assertFalse(within_cooldown(None, COOLDOWN.default))

    def test_cooldown_argument(self):
        """Test that within_cooldown honours the cooldown period passed to it."""
        timestamp = days_ago(10)
        self.assertTrue(within_cooldown(timestamp, 30))
        self.assertFalse(within_cooldown(timestamp, 5))

    def test_cooldown_longer_than_a_date_interval_can_hold(self):
        """Test that a cooldown of more days than a `timedelta` holds is honoured rather than aborting the run."""
        self.assertTrue(within_cooldown(days_ago(1), 10**12))

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
    """Unit tests for the cooldown expressed as a cutoff instant."""

    def test_cutoff_is_the_cooldown_ago(self):
        """Test that the cutoff is the instant the cooldown reaches back to."""
        cutoff = datetime.fromisoformat(cooldown_cutoff(30))
        self.assertEqual((datetime.now(UTC) - cutoff).days, 30)

    def test_cutoff_reaching_past_the_earliest_date(self):
        """Test that a cooldown reaching further back than a date can express yields the earliest instant."""
        self.assertEqual(cooldown_cutoff(10**12), datetime.min.replace(tzinfo=UTC).isoformat())


class CooldownWalkTest(unittest.TestCase):
    """Unit tests for the cooldown a walk applies to the candidates it is asked about."""

    def test_a_zero_day_cooldown_holds_back_nothing(self):
        """Test that a zero-day cooldown does not hold back a candidate dated in the future."""
        walk = CooldownWalk(0)
        self.assertFalse(walk.holds_back("2.0", datetime.now(UTC) + timedelta(days=2)))
        self.assertIsNone(walk.fresh_release)


class FirstEligibleTest(unittest.TestCase):
    """Unit tests for walking the candidates newest-first."""

    @staticmethod
    def walked_without_cooldown(
        candidates: list[str], resolve: Callable[[str], DependencyVersion | None]
    ) -> DependencyVersion:
        """Return what a walk without a cooldown resolves the candidates to, for a reference on version 1.0."""
        return first_eligible(candidates, lambda candidate, _: resolve(candidate), lambda _: False, "1.0", 0)

    def test_no_candidates(self):
        """Test that the current version is returned unchanged when there are no candidates."""
        self.assertEqual(DependencyVersion(version="1.0"), self.walked_without_cooldown([], DependencyVersion))

    def test_first_candidate_eligible(self):
        """Test that the first eligible candidate is returned."""
        self.assertEqual(DependencyVersion("3.0"), self.walked_without_cooldown(["3.0", "2.0"], DependencyVersion))

    def test_skips_ineligible_candidates(self):
        """Test that candidates that resolve to None are skipped in favour of the next one."""

        def resolve(candidate: str) -> DependencyVersion | None:
            """Resolve only the "2.0" candidate, skipping the rest."""
            return DependencyVersion(candidate) if candidate == "2.0" else None

        self.assertEqual(DependencyVersion("2.0"), self.walked_without_cooldown(["3.0", "2.0"], resolve))

    def test_no_eligible_candidate(self):
        """Test that the current version is returned unchanged when no candidate is eligible."""
        self.assertEqual(DependencyVersion(version="1.0"), self.walked_without_cooldown(["3.0", "2.0"], lambda _: None))

    @kills(
        Mutation(
            cooldown.CooldownWalk._remember,
            "if self.fresh_release is None:",
            "if True:",
            "the line names the oldest version the cooldown holds back rather than the newest",
        )
    )
    def test_the_newest_candidate_inside_the_cooldown_is_carried(self):
        """Test that of several candidates inside the cooldown, the version carries the newest one alone."""
        published = days_ago(1)

        def resolve(candidate: str, walk: CooldownWalk) -> DependencyVersion | None:
            """Hold back every candidate, all of them published a day ago."""
            return None if walk.holds_back(candidate, published) else DependencyVersion(candidate)

        latest = first_eligible(["2.0", "3.0"], resolve, lambda _: False, "1.0", COOLDOWN.default)
        self.assertEqual(DependencyVersion("1.0", fresh_release=Release("3.0", published)), latest)
