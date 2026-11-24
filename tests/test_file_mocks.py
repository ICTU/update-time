"""Unit tests for the mocks of the files a run reads and writes."""

import unittest
from pathlib import Path

from .file_mocks import FakeFiles


class FakeFilesTest(unittest.TestCase):
    """Unit tests for the file system in memory."""

    def test_removing_a_file_that_does_not_exist_raises_unless_that_is_allowed(self):
        """Test that removing a missing file raises, as `Path.unlink` does, unless `missing_ok` allows it."""
        path = Path("/missing.txt")
        with FakeFiles().patched():
            path.unlink(missing_ok=True)
            with self.assertRaises(FileNotFoundError):
                path.unlink()
