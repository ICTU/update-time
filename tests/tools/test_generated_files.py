"""Unit tests for writing generated files, and for naming the ones that are out of date."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.generated_files import write_or_check


class WriteOrCheckTest(unittest.TestCase):
    """Unit tests for writing the generated files, and for naming the ones that are out of date."""

    CONTENT = "the generated content"

    def generated_file(self, on_disk: str | None) -> Mock:
        """Return a mock generated file that holds the given content, or that raises when read if there is none."""
        exists = on_disk is not None
        read_text = Mock(return_value=on_disk) if exists else Mock(side_effect=FileNotFoundError)
        path = Mock(is_file=Mock(return_value=exists), read_text=read_text)
        path.relative_to.return_value = "GENERATED.md"
        return path

    def check(self, path: Mock) -> None:
        """Run the check over the one file."""
        with patch.object(sys, "argv", ["generator", "--check"]):
            write_or_check({path: self.CONTENT}, Path("/"), "just generate")

    def test_files_are_written(self):
        """Test that each generated file is written when the generator is not checking."""
        path = self.generated_file("what it held before")
        with patch.object(sys, "argv", ["generator"]):
            write_or_check({path: self.CONTENT}, Path("/"), "just generate")
        path.write_text.assert_called_once_with(self.CONTENT)

    def test_current_file_passes_the_check(self):
        """Test that a file already holding the generated content is not reported, and is not written either."""
        path = self.generated_file(self.CONTENT)
        self.check(path)
        path.write_text.assert_not_called()

    def test_stale_file_is_named(self):
        """Test that a file whose content has drifted exits the run, naming it and how to regenerate it."""
        path = self.generated_file("what it held before")
        with self.assertRaises(SystemExit) as raised:
            self.check(path)
        self.assertIn("GENERATED.md out of date, run `just generate` to regenerate", str(raised.exception))
        path.write_text.assert_not_called()

    def test_missing_file_is_named(self):
        """Test that a generated file that does not exist yet counts as out of date."""
        path = self.generated_file(None)
        with self.assertRaises(SystemExit):
            self.check(path)
