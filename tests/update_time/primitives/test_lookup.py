"""Unit tests for the outcome of a lookup."""

import unittest

from mutation_time.mutation import kills
from update_time.primitives.lookup import LookedUp


class LookedUpTest(unittest.TestCase):
    """Unit tests for a lookup made at once."""

    @kills.delete(
        LookedUp.failure,
        ", absent=self.absent",
        "a passed-on lookup of an absent resource reads as a failed request",
        case="absent",
    )
    @kills.replace(
        LookedUp.failure,
        "absent=self.absent",
        "absent=True",
        "a passed-on lookup of a failed request reads as an absent resource",
        case="failed",
    )
    def test_failure_keeps_the_reason_and_whether_the_source_answered_absent(self):
        """Test that a failed lookup, passed on as another type, keeps why it failed and whether it was absent."""
        cases = {
            "absent": LookedUp[str](None, "HTTP 404", absent=True),
            "failed": LookedUp[str](None, "the request failed"),
        }
        for case, found in cases.items():
            with self.subTest(case=case):
                failure: LookedUp[int] = found.failure()
                self.assertEqual(failure, found)
