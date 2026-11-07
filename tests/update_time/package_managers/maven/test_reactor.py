"""Unit tests for running Maven over the poms of a reactor."""

import unittest

from update_time.package_managers.maven import reactor as reactor_module


class EffectivePomFileTest(unittest.TestCase):
    """Unit tests for the file Maven writes the effective pom to."""

    def test_the_file_is_writable_until_it_is_removed(self):
        """Test that the file takes what Maven writes while the context is open, and is removed once it closes."""
        with reactor_module._effective_pom_file() as path:
            path.write_text("<project/>")
            self.assertEqual(path.read_text(), "<project/>")
        self.assertFalse(path.exists())
