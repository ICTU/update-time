"""Unit tests for the directives a reference's source may be unable to apply."""

import unittest

from update_time.markers.directive import DIRECTIVES
from update_time.markers.marker import _IGNORABLE_SCOPES, Scope


class DirectivesTest(unittest.TestCase):
    """Unit tests for the table naming those directives."""

    def test_every_scope_a_marker_holds_back_is_judged_against_the_source(self):
        """Test that every scope a marker can hold back has a row, so a scope added later is judged as the rest are.

        `update` is the one scope without a row: every source resolves a version, so there is no capability it
        could lack. The scopes a comparison item sets, such as `cooldown`, have a row without being ignorable.
        """
        judged = Scope(0)
        for directive in DIRECTIVES:
            judged |= directive.scope
        self.assertEqual(_IGNORABLE_SCOPES & ~judged, Scope.UPDATE)
