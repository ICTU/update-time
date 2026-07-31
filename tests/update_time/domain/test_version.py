"""Unit tests for the version module."""

import unittest
from datetime import datetime, timedelta, timezone

from update_time.domain.version import DependencyVersion, Yank, first_eligible, is_valid


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


class YankTest(unittest.TestCase):
    """Unit tests for rendering a version's withdrawal state."""

    def test_str_quotes_the_reason(self):
        """Test that a yank renders as the maintainer's reason, in double quotes."""
        self.assertEqual(str(Yank(yanked=True, reason="broke Python 3.10 support")), '"broke Python 3.10 support"')

    def test_str_reports_an_unspecified_reason(self):
        """Test that a yank the maintainer gave no reason for renders as `reason not specified`."""
        self.assertEqual(str(Yank(yanked=True)), "reason not specified")


class DependencyVersionTest(unittest.TestCase):
    """Unit tests for the DependencyVersion data carrier."""

    def test_str_is_the_version_when_the_publication_date_is_unknown(self):
        """Test that a version whose publication date is unknown renders as its version alone."""
        self.assertEqual(str(DependencyVersion("4.15.0")), "4.15.0")

    def test_str_appends_the_publication_date_in_utc(self):
        """Test that a known publication date is appended, converted to UTC whatever timezone it was given in."""
        published = datetime(2026, 5, 29, 15, 54, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(str(DependencyVersion("1.0", published=published)), "1.0, published: 2026-05-29 13:54")

    def test_not_yanked_by_default(self):
        """Test that a version's yank state defaults to not yanked."""
        self.assertEqual(DependencyVersion(version="1.0").yank, Yank())

    def test_carries_yank_state(self):
        """Test that a version can carry a yank state."""
        yank = Yank(yanked=True, reason="broke Python 3.10 support")
        self.assertEqual(DependencyVersion(version="1.0", yank=yank).yank, yank)


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
