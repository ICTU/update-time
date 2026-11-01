"""Unit tests for the version scheme module."""

import unittest

from update_time.domain import version_scheme
from update_time.domain.version_scheme import SEMVER

from tests.mutation import Mutation, kills


class SemverTest(unittest.TestCase):
    """Unit tests for the semver version scheme."""

    @kills(
        Mutation(
            version_scheme._semver_order,
            "int(number) for number",
            "number for number",
            "semver compares release numbers as text, so 1.10 orders below 1.2",
        ),
        Mutation(
            version_scheme._semver_order,
            "return release, (1,)",
            "return release, (0,)",
            "semver orders a release below its own pre-releases",
        ),
        Mutation(
            version_scheme._semver_order,
            '(0, int(part), "") if part.isdigit()',
            "(1, 0, part) if part.isdigit()",
            "semver compares numeric pre-release identifiers as text, so beta.11 orders below beta.2",
        ),
        Mutation(
            version_scheme._semver_order,
            "else (1, 0, part)",
            "else (-1, 0, part)",
            "semver orders an alphanumeric pre-release identifier below a numeric one",
        ),
        Mutation(
            version_scheme._semver_order,
            "is None:\n        return None",
            "is None:\n        return (999,), (1,)",
            "semver orders a version it cannot read above every other version",
        ),
        Mutation(
            version_scheme._semver_order,
            "is None:\n        return None",
            "is None:\n        return (), (1,)",
            "semver orders a version it cannot read below every other version",
        ),
    )
    def test_a_downgrade_is_a_move_to_a_version_semver_orders_lower(self):
        """Test that a move is a downgrade where semver orders the new version below the version."""
        cases = {
            "to a higher release": ("1.2", "1.10", False),
            "from a release to its own pre-release": ("1.0.0", "1.0.0-alpha.beta", True),
            "to a pre-release with a lower number": ("1.0.0-beta.11", "1.0.0-beta.2", True),
            "to a numeric identifier from an alphanumeric one": ("1.0.0-alpha.beta", "1.0.0-alpha.1", True),
            "from a version that is not semver": ("next", "1.0.0", False),
            "to a version that is not semver": ("1.0.0", "latest", False),
        }
        for case, (version, new_version, downgrade) in cases.items():
            with self.subTest(case):
                self.assertEqual(SEMVER.is_downgrade(version, new_version), downgrade)
