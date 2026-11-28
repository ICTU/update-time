"""Unit tests for the requirements.txt file format."""

import re
import unittest

from mutation_time.mutation import kills
from update_time.manifests import requirements_txt


class RequirementPatternTest(unittest.TestCase):
    """Unit tests for the patterns that tell an exact pin from a requirement pinning no version."""

    @kills.replace(
        requirements_txt,
        r'_EXACT_PIN: Final[str] = rf"^\s*{_NAME}{_EXTRAS}\s*==\s*{_VERSION}{_VERSION_ENDS}"',
        r'_EXACT_PIN: Final[str] = rf"^\s*{_NAME}{_EXTRAS}\s*==\s*[A-Za-z0-9][A-Za-z0-9._+-]*{_VERSION_ENDS}"',
        "the lookahead's copy of the pin refuses an epoch, so a pin on one matches both patterns and is read twice",
        case="humanize==1!2.0",
    )
    @kills.replace(
        requirements_txt,
        r'_EXACT_PIN: Final[str] = rf"^\s*{_NAME}{_EXTRAS}',
        r'_EXACT_PIN: Final[str] = rf"^\s*{_NAME}',
        "the lookahead's copy of the pin leaves out the extras, so a pin with extras matches both patterns and is "
        "read twice",
        case="humanize[fast] == 4.15.0",
    )
    @kills.replace(
        requirements_txt,
        r'{_VERSION}{_VERSION_ENDS}"',
        r'[A-Za-z0-9][A-Za-z0-9._!-]*{_VERSION_ENDS}"',
        "the lookahead's copy of the pin refuses a local version, so a pin on one matches both patterns and is "
        "read twice",
        case="humanize==1.0+local",
    )
    @kills.append(
        requirements_txt,
        r'_EXACT_PIN: Final[str] = rf"^\s*{_NAME}{_EXTRAS}\s*==',
        "=?",
        "the lookahead's copy of the pin reads `===` as an exact pin, so an arbitrary equality matches neither pattern",
        case="humanize===nightly",
    )
    @kills.replace(
        requirements_txt,
        r'{_VERSION}{_VERSION_ENDS}"',
        r'{_VERSION}(?![A-Za-z0-9._!+,-])(?!\s*,)"',
        "the lookahead's copy of the pin accepts a `*` after the version, so a wildcard matches neither pattern",
        case="humanize==4.15.*",
    )
    @kills.replace(
        requirements_txt,
        r'{_VERSION}{_VERSION_ENDS}"',
        r'{_VERSION}(?![A-Za-z0-9._!+*-])"',
        "the lookahead's copy of the pin accepts a comma after the version, so a pin with an exclusion matches neither",
        case="humanize==4.15.0,!=4.15.1",
    )
    @kills.replace(
        requirements_txt,
        r'\s*==\s*{_VERSION}{_VERSION_ENDS}"',
        r'\s*==\s*{_VERSION}$"',
        "the lookahead's copy of the pin needs the version to end the line, so a pin with a marker matches both",
        case="humanize==4.15.0; python_version<'3.13'",
    )
    @kills.replace(
        requirements_txt,
        "[<>!~]=|[<>]",
        "[<!~]=|<",
        "Update-time does not read a requirement with a lower bound, since it matches neither pattern",
        case="humanize>=4",
    )
    @kills.delete(
        requirements_txt,
        "|$",
        "Update-time does not read a requirement without a version, since it matches neither pattern",
        case="humanize",
    )
    def test_a_requirement_matches_one_pattern(self):
        """Test that a requirement is read as an exact pin or as pinning none, and never as both or as neither."""
        for line in (
            "humanize[fast] == 4.15.0",
            "humanize==4.15.0; python_version<'3.13'",
            "humanize==1.0+local",
            "humanize==1!2.0",
            "humanize==4.15.*",
            "humanize==4.15.0,!=4.15.1",
            "humanize===nightly",
            "humanize>=4",
            "humanize",
        ):
            with self.subTest(line=line):
                pinned = re.search(requirements_txt.EXACT_PIN_REFERENCE, line) is not None
                self.assertNotEqual(pinned, (re.search(requirements_txt.LOOSE_REQUIREMENT_REFERENCE, line) is not None))
