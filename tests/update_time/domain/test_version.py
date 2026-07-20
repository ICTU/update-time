"""Unit tests for the version module."""

import unittest

from update_time.domain.version import DependencyVersion, first_eligible, is_valid


class IsValidTest(unittest.TestCase):
    """Unit tests for the is_valid version checker."""

    def test_is_valid(self):
        """Test that a valid version is reported as valid."""
        self.assertTrue(is_valid("1.0"))

    def test_is_invalid(self):
        """Test that a invalid version is reported as invalid."""
        self.assertFalse(is_valid("nope-1.0"))

    def test_v_prefix_is_allowed(self):
        """Test that a version with a v-prefix is valid."""
        self.assertTrue(is_valid("v1.0"))


class FirstEligibleTest(unittest.TestCase):
    """Unit tests for the first_eligible walk."""

    def test_no_candidates(self):
        """Test that the current version is returned unchanged when there are no candidates."""
        self.assertEqual(DependencyVersion(version="1.0"), first_eligible([], DependencyVersion, "1.0"))

    def test_first_candidate_eligible(self):
        """Test that the first eligible candidate is returned."""
        self.assertEqual(DependencyVersion("3.0"), first_eligible(["3.0", "2.0"], DependencyVersion, "1.0"))

    def test_skips_ineligible_candidates(self):
        """Test that candidates that resolve to None are skipped in favour of the next one."""

        def resolve(candidate: str) -> DependencyVersion | None:
            """Resolve only the "2.0" candidate, skipping the rest."""
            return DependencyVersion(candidate) if candidate == "2.0" else None

        self.assertEqual(DependencyVersion("2.0"), first_eligible(["3.0", "2.0"], resolve, "1.0"))

    def test_no_eligible_candidate(self):
        """Test that the current version is returned unchanged when no candidate is eligible."""
        self.assertEqual(DependencyVersion(version="1.0"), first_eligible(["3.0", "2.0"], lambda _: None, "1.0"))
