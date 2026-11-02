"""Unit tests for the requirements.txt file format."""

import re
import unittest

from update_time.manifests import requirements_txt

from tests.mutation import Mutation, kills


class RequirementPatternTest(unittest.TestCase):
    """Unit tests for the patterns that tell an exact pin from a requirement pinning no version."""

    @kills(
        Mutation(
            requirements_txt,
            r'_EXACT_PIN = rf"^\s*{_NAME}{_EXTRAS}\s*==\s*{_VERSION}{_VERSION_ENDS}"',
            r'_EXACT_PIN = rf"^\s*{_NAME}{_EXTRAS}\s*==\s*[A-Za-z0-9][A-Za-z0-9._+-]*{_VERSION_ENDS}"',
            "the lookahead's copy of the pin drifts from the pin itself, so a spelling only one of them "
            "accepts matches both patterns and is read twice",
        )
    )
    def test_a_requirement_matches_one_pattern(self):
        """Test that a requirement is read as an exact pin or as pinning none, and never as both or as neither."""
        for line in (
            "humanize==4.15.0",
            "humanize[fast] == 4.15.0",
            "humanize==4.15.0  # keep",
            "humanize==4.15.0; python_version<'3.13'",
            "humanize==1.0+local",
            "humanize==1!2.0",
            "humanize==4.15.*",
            "humanize == 4.15.*",
            "humanize==4.15.0,!=4.15.1",
            "humanize==4.15.0,!=4.14.*",
            "humanize===nightly",
            "humanize>=4",
            "humanize",
        ):
            with self.subTest(line=line):
                pinned = re.search(requirements_txt.EXACT_PIN_REFERENCE, line) is not None
                pins_nothing = re.search(requirements_txt.LOOSE_REQUIREMENT_REFERENCE, line) is not None
                self.assertNotEqual(pinned, pins_nothing)
