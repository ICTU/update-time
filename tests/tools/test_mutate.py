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

    def probe(
        self,
        path: Mock,
        returncode: int = 1,
        text: str = _INPUT,
        argv: list[str] | None = None,
        outputs: tuple[str, ...] = ("",),
    ) -> int:
        """Run the probe over the path with the given command result and outputs, and return its exit code."""
        mutated = Mock(returncode=returncode, stdout=outputs[0], stderr="")
        baselines = [Mock(returncode=0, stdout=output, stderr="") for output in outputs[1:]]
        run = Mock(side_effect=[mutated, *baselines])
        self.reported = io.StringIO()
        with (
            patch.object(sys, "argv", argv or ["mutate.py", "file.py"]),
            patch("sys.stdin", io.StringIO(text)),
            patch("tools.mutate.Path", Mock(return_value=path)),
            patch("tools.mutate.subprocess.run", run),
            redirect_stdout(self.reported),
            redirect_stderr(io.StringIO()),
        ):
            self.run_command = run
            return main()

    def test_the_file_is_mutated_and_restored(self):
        """Test that the snippet is replaced, the command run, and the file put back as it was."""
        path = mock_path(_ORIGINAL)
        self.probe(path)
        self.assertEqual(path.write_text.call_args_list, [call("before\nnew\nafter\n"), call(_ORIGINAL)])
        self.run_command.assert_called_once_with(["just", "test"], check=False, capture_output=True, text=True)

    def test_a_command_of_its_own(self):
        """Test that a command given after the file is run instead of the default."""
        self.probe(mock_path(_ORIGINAL), argv=["mutate.py", "file.py", "just", "check"])
        self.run_command.assert_called_once_with(["just", "check"], check=False, capture_output=True, text=True)

    def test_a_failing_command_caught_the_mutation(self):
        """Test that a command that fails means the mutation was caught, which is what a guarding test does."""
        self.assertEqual(self.probe(mock_path(_ORIGINAL), returncode=1), 0)

    def test_a_command_that_errored_without_reporting_a_test_count(self):
        """Test that a run reporting errors but no test count is hedged about, there being nothing to compare.

        A command that reports no `Ran N tests` line — `just check`, say — leaves the errors to speak for
        themselves, so the probe says the stub may have broken the file rather than that it did.
        """
        self.assertEqual(self.probe(mock_path(_ORIGINAL), outputs=("FAILED (errors=16)\n", "")), 3)
        self.assertIn("The run reported 16 errors", self.reported.getvalue())

    def test_a_stub_that_kept_tests_from_running(self):
        """Test that a run reporting errors and fewer tests than the restored file runs is reported as broken."""
        outputs = ("Ran 260 tests\nFAILED (errors=38)\n", "test \x1b[32mPASS\x1b[0m (935 tests)\n")
        self.assertEqual(self.probe(mock_path(_ORIGINAL), outputs=outputs), 3)
        self.assertIn("675 of 935 tests never ran", self.reported.getvalue())

    def test_a_stub_that_left_every_test_running(self):
        """Test that a run reporting errors is a catch when it reached every test the restored file runs."""
        outputs = ("Ran 935 tests\nFAILED (errors=1)\n", "Ran 935 tests\n")
        self.assertEqual(self.probe(mock_path(_ORIGINAL), outputs=outputs), 0)
        self.assertNotIn("never ran", self.reported.getvalue())

    def test_a_command_that_only_failed(self):
        """Test that a run reporting failures alone is left to speak for itself, and is caught."""
        self.assertEqual(self.probe(mock_path(_ORIGINAL), outputs=("FAILED (failures=1)\n",)), 0)
        self.assertNotIn("errors", self.reported.getvalue())

    def test_the_commands_output_is_written_through(self):
        """Test that what the command wrote reaches the reader, which capturing it would otherwise swallow."""
        self.probe(mock_path(_ORIGINAL), outputs=("Ran 928 tests\n",))
        self.assertIn("Ran 928 tests", self.reported.getvalue())

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
