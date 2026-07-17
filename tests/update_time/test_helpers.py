"""Unit tests for the shared test helpers."""

import unittest

from update_time.domain.version import Verb

from tests.update_time.helpers import bound


class BoundTest(unittest.TestCase):
    """Unit tests for the bound helper."""

    def test_item_that_is_no_bound(self):
        """Test that the helper fails on an item that is not a bound, so a typo can't silently weaken a test."""
        self.assertRaises(ValueError, bound, Verb.ALLOW, "not-a-bound")
