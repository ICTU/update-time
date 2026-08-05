"""Unit tests for the mutation probe."""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, call, patch

from tools.mutate import _NOT_RUN, _SEPARATOR, main, snippets

from tests.helpers import mock_path

_ORIGINAL = "before\nold\nafter\n"
_INPUT = f"old\n{_SEPARATOR}\nnew\n"


class SnippetsTest(unittest.TestCase):
    """Unit tests for splitting the input into the snippet to replace and its replacement."""

    def test_split_on_the_separator(self):
        """Test that the text before the separator is the snippet and the text after it the replacement."""
        self.assertEqual(snippets(f"one\ntwo\n{_SEPARATOR}\nthree\n"), ("one\ntwo", "three"))

    def test_input_without_a_separator(self):
        """Test that input holding no separator line is reported, rather than read as one snippet."""
        self.assertRaises(ValueError, snippets, "one\ntwo\n")


class MainTest(unittest.TestCase):
    """Unit tests for running a command against a mutated file."""

    def probe(self, path: Mock, returncode: int = 1, text: str = _INPUT, argv: list[str] | None = None) -> int:
        """Run the probe over the path with the given command result, and return its exit code."""
        run = Mock(return_value=Mock(returncode=returncode))
        with (
            patch.object(sys, "argv", argv or ["mutate.py", "file.py"]),
            patch("sys.stdin", io.StringIO(text)),
            patch("tools.mutate.Path", Mock(return_value=path)),
            patch("tools.mutate.subprocess.run", run),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.run_command = run
            return main()

    def test_the_file_is_mutated_and_restored(self):
        """Test that the snippet is replaced, the command run, and the file put back as it was."""
        path = mock_path(_ORIGINAL)
        self.probe(path)
        self.assertEqual(path.write_text.call_args_list, [call("before\nnew\nafter\n"), call(_ORIGINAL)])
        self.run_command.assert_called_once_with(["just", "test"], check=False)

    def test_a_command_of_its_own(self):
        """Test that a command given after the file is run instead of the default."""
        self.probe(mock_path(_ORIGINAL), argv=["mutate.py", "file.py", "just", "check"])
        self.run_command.assert_called_once_with(["just", "check"], check=False)

    def test_a_failing_command_caught_the_mutation(self):
        """Test that a command that fails means the mutation was caught, which is what a guarding test does."""
        self.assertEqual(self.probe(mock_path(_ORIGINAL), returncode=1), 0)

    def test_a_passing_command_means_the_mutation_survived(self):
        """Test that a command that passes means nothing guards the mutated code."""
        self.assertEqual(self.probe(mock_path(_ORIGINAL), returncode=0), 1)

    def test_a_snippet_that_is_not_there(self):
        """Test that a snippet the file doesn't hold leaves the file alone and runs no command."""
        path = mock_path("nothing to replace\n")
        self.assertEqual(self.probe(path), _NOT_RUN)
        path.write_text.assert_not_called()
        self.run_command.assert_not_called()

    def test_a_snippet_that_occurs_twice(self):
        """Test that an ambiguous snippet leaves the file alone, since which occurrence to mutate is unknown."""
        path = mock_path("old\nold\n")
        self.assertEqual(self.probe(path), _NOT_RUN)
        path.write_text.assert_not_called()

    def test_input_without_a_separator(self):
        """Test that unreadable input leaves the file alone and runs no command."""
        path = mock_path(_ORIGINAL)
        self.assertEqual(self.probe(path, text="old\n"), _NOT_RUN)
        path.write_text.assert_not_called()

    def test_no_file_named(self):
        """Test that the usage is reported when no file is named."""
        self.assertEqual(self.probe(mock_path(_ORIGINAL), argv=["mutate.py"]), _NOT_RUN)
        self.run_command.assert_not_called()

    def test_the_file_is_restored_when_the_command_raises(self):
        """Test that a command that cannot be run leaves the file as it was, so a probe never changes the tree."""
        path = mock_path(_ORIGINAL)
        run = Mock(side_effect=OSError("no such command"))
        with (
            patch.object(sys, "argv", ["mutate.py", "file.py"]),
            patch("sys.stdin", io.StringIO(_INPUT)),
            patch("tools.mutate.Path", Mock(return_value=path)),
            patch("tools.mutate.subprocess.run", run),
            self.assertRaises(OSError),
        ):
            main()
        self.assertEqual(path.write_text.call_args_list[-1], call(_ORIGINAL))
