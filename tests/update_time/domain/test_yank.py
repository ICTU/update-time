"""Unit tests for the yank module."""

import unittest
from unittest.mock import Mock

from update_time.domain.dependency import DependencyVersion, Yank
from update_time.domain.yank import with_yank_state


class WithYankStateTest(unittest.TestCase):
    """Unit tests for attaching the withdrawal state of the version the run leaves the reference on."""

    def test_unchanged_version_carries_the_yank_state_looked_up_for_it(self):
        """Test that a version the run left the reference on comes back carrying its withdrawal state."""
        yank = Yank(yanked=True, reason="broke Python 3.10 support")
        latest = with_yank_state(DependencyVersion("1.0"), "1.0", Mock(return_value=yank))
        self.assertEqual(latest, DependencyVersion("1.0", yank=yank))

    def test_moved_version_is_returned_without_a_yank_lookup(self):
        """Test that a version the run moved to comes back as it was, and that its yank state is never looked up."""
        yank_state = Mock()
        latest = DependencyVersion("1.1")
        self.assertEqual(with_yank_state(latest, "1.0", yank_state), latest)
        yank_state.assert_not_called()
