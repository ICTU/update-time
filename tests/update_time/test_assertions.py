"""Unit tests for the shared test assertions."""

import unittest

from .assertions import assert_success


class AssertSuccessTest(unittest.TestCase):
    """Unit tests for the assert_success helper."""

    def test_success(self):
        """Test that the success exit code passes."""
        assert_success(0)

    def test_failure(self):
        """Test that a non-zero exit code raises an assertion error mentioning the exit code."""
        with self.assertRaises(AssertionError) as context:
            assert_success(1)
        self.assertIn("1", str(context.exception))
