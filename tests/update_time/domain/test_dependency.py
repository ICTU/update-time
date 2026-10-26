"""Unit tests for the dependency module."""

import unittest
from datetime import UTC, datetime

from update_time.domain import dependency
from update_time.domain.dependency import DependencyVersion, Release, Yank, is_valid
from update_time.primitives.lookup import LookedUp
from update_time.primitives.timestamp import utc_date_and_time

from tests.mutation import Mutation, kills


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


class ReleaseTest(unittest.TestCase):
    """Unit tests for the release a dependency's source published."""

    @kills(
        Mutation(
            dependency.Release._sortable_version,
            "return Version(self.version) if is_valid(self.version) else LOWEST_VERSION",
            "return LOWEST_VERSION",
            "releases published at one moment order by the order the source listed them in",
        )
    )
    def test_a_tie_is_broken_by_version(self):
        """Test that two releases published at the same moment are ordered by version, in either listing order.

        The versions compare as versions rather than as strings, so `10.0.0` is the newer of the two.
        """
        moment = datetime(2026, 8, 24, tzinfo=UTC)
        older, newer = Release("9.9.4", moment), Release("10.0.0", moment)
        for case, releases in (("newest listed last", [older, newer]), ("newest listed first", [newer, older])):
            with self.subTest(case=case):
                self.assertEqual(Release.newest(releases), newer)


class DependencyVersionTest(unittest.TestCase):
    """Unit tests for the DependencyVersion data carrier."""

    def test_str_is_the_version_when_the_publication_date_is_unknown(self):
        """Test that a version whose publication date is unknown renders as its version alone."""
        self.assertEqual(str(DependencyVersion("4.15.0")), "4.15.0")

    def test_str_appends_the_publication_date(self):
        """Test that a known publication date is appended to the version."""
        published = datetime(2026, 5, 29, 13, 54, tzinfo=UTC)
        self.assertEqual(
            str(DependencyVersion("1.0", publication=LookedUp(published))),
            f"1.0, published: {utc_date_and_time(published)}",
        )

    def test_yank_state_unknown_by_default(self):
        """Test that a version's yank state defaults to unknown."""
        self.assertIsNone(DependencyVersion(version="1.0").yank)

    def test_carries_yank_state(self):
        """Test that a version can carry a yank state."""
        yank = Yank(yanked=True, reason="broke Python 3.10 support")
        self.assertEqual(DependencyVersion(version="1.0", yank=yank).yank, yank)
