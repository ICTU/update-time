"""Unit tests for the domain objects the tests build."""

import unittest

from update_time.domain.bound import Verb

from .domain_objects import bound


class BoundTest(unittest.TestCase):
    """Unit tests for the bound helper."""

    def test_item_that_is_no_bound(self):
        """Test that the helper fails on an item that is not a bound, so a typo can't silently weaken a test."""
        self.assertRaises(ValueError, bound, Verb.ALLOW, "not-a-bound")
