"""Unit tests for running a command where Python code changed since it last passed."""

import contextlib
import io
import json
import subprocess  # nosec
import sys
import unittest
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from tools import run_if_code_changed

from tests.mutation import Mutation, kills

if TYPE_CHECKING:
    from collections.abc import Iterator

# A module with a docstring of its own and one in its function.
_MODULE = '''"""Answer questions."""


def answer() -> int:
    """Return the answer."""
    return 42
'''


class _Files:
    """Hold the text of each file the tool reads and writes, under the file's path."""

    def __init__(self, files: dict[str, str]) -> None:
        """Hold the given files."""
        self.files = dict(files)

    def path(self, name: object) -> Mock:
        """Return a stand-in for the path, reading and writing the text held under it."""
        key = str(name)

        def read_text() -> str:
            if key not in self.files:
                raise FileNotFoundError(2, "No such file or directory", key)
            return self.files[key]

        def write_text(text: str) -> None:
            self.files[key] = text

        return Mock(
            read_text=Mock(side_effect=read_text),
            write_text=Mock(side_effect=write_text),
        )


def _python_files_listed(paths: list[str], command_exit_status: int = 0) -> Mock:
    """Return a stub for `subprocess.run` whose `git ls-files` lists the Python files, reading the list at each call.

    Any other command exits with the given status.
    """

    def run(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout="".join(f"{path}\0" for path in paths), stderr="")
        return subprocess.CompletedProcess(command, command_exit_status)

    return Mock(side_effect=run)


def _git_failing(failure: subprocess.CompletedProcess[str] | OSError) -> Mock:
    """Return a stub for `subprocess.run` whose git fails as given, while any other command succeeds."""

    def run(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] != "git":
            return subprocess.CompletedProcess(command, 0)
        if isinstance(failure, OSError):
            raise failure
        return failure

    return Mock(side_effect=run)


@contextlib.contextmanager
def _patched(run: Mock, files: _Files) -> Iterator[None]:
    """Patch the tool's `subprocess.run` with the stub, and its `Path` with the files."""
    with (
        patch("tools.run_if_code_changed.subprocess.run", run),
        patch("tools.run_if_code_changed.Path", Mock(side_effect=files.path)),
    ):
        yield


# The tests ask the tool for this exit status where it skips the command.
_SKIPPED = 3
# The tests give the tool this command to run.
_COMMAND = ("just", "test")


def _commands_run(run: Mock) -> list[tuple[str, ...]]:
    """Return the commands the stub for `subprocess.run` was asked to run, git's own commands left out."""
    return [tuple(call.args[0]) for call in run.call_args_list if call.args[0][0] != "git"]


@patch.object(sys, "argv", ["run_if_code_changed.py", "--skipped-status", str(_SKIPPED), "--", *_COMMAND])
class MainTest(unittest.TestCase):
    """Unit tests for running the command where Python code changed, and skipping it where none did."""

    def test_without_a_fingerprint_the_code_is_fingerprinted_and_the_command_run(self):
        """Test that, where no fingerprint exists, the tool runs the command and fingerprints the code per file."""
        files = _Files({"src/answer.py": _MODULE})
        with _patched(_python_files_listed(["src/answer.py"]), files):
            self.assertEqual(run_if_code_changed.main(), 0)
        self.assertEqual(list(json.loads(files.files[run_if_code_changed._RECORD])), ["src/answer.py"])

    @kills(
        Mutation(
            run_if_code_changed.main,
            "fingerprints == _recorded()",
            "False",
            "code unchanged since the command last passed reads as changed, so the tests run every time",
        )
    )
    def test_code_equal_to_the_fingerprint_is_skipped_and_keeps_the_fingerprint(self):
        """Test that a second run over unchanged code exits with the skip status and does not run the command."""
        files = _Files({"src/answer.py": _MODULE})
        run = _python_files_listed(["src/answer.py"])
        with _patched(run, files):
            run_if_code_changed.main()
            fingerprint = files.files[run_if_code_changed._RECORD]
            run.reset_mock()
            self.assertEqual(run_if_code_changed.main(), _SKIPPED)
        self.assertEqual(_commands_run(run), [])
        self.assertEqual(files.files[run_if_code_changed._RECORD], fingerprint)

    @kills(
        Mutation(
            run_if_code_changed.main,
            "if not status and fingerprints is not None:",
            "if fingerprints is not None:",
            "a failing run leaves its fingerprint, so the next run skips the command although it still fails",
        )
    )
    def test_a_command_that_fails_leaves_no_fingerprint_and_gives_its_exit_status(self):
        """Test that a failing command gives the tool its exit status, and does not leave a fingerprint."""
        files = _Files({"src/answer.py": _MODULE})
        run = _python_files_listed(["src/answer.py"], command_exit_status=2)
        with _patched(run, files):
            self.assertEqual(run_if_code_changed.main(), 2)
        self.assertEqual(_commands_run(run), [_COMMAND])
        self.assertNotIn(run_if_code_changed._RECORD, files.files)

    @kills(
        Mutation(
            run_if_code_changed._fingerprint,
            "node.body = node.body[1:] or [ast.Pass()]",
            "pass",
            "an edit to a docstring alone reads as a change to the code, so the tests run all the same",
        ),
        Mutation(
            run_if_code_changed._fingerprint,
            "ast.dump(tree)",
            "ast.dump(tree, include_attributes=True)",
            "code that moves to another line reads as changed, so a comment above it runs the tests all the same",
        ),
        Mutation(
            run_if_code_changed._fingerprint,
            '"pragma" in token.string]',
            '"never" in token.string]',
            "a coverage pragma removed alone reads as unchanged, so the coverage gate never sees the line it exposed",
        ),
    )
    def test_an_edit_to_docstrings_or_comments_alone_skips_the_command(self):
        """Test that editing docstrings or comments alone skips the command, and editing a coverage pragma does not."""
        edits = {
            "its docstrings": (
                _MODULE.replace("Answer questions.", "Answer every question.").replace(
                    "Return the answer.", "Return the answer to everything."
                ),
                _SKIPPED,
                False,
            ),
            "a comment": (
                _MODULE.replace("    return 42", "    # The answer to everything.\n    return 42"),
                _SKIPPED,
                False,
            ),
            "a coverage pragma": (_MODULE.replace("-> int:", "-> int:  # pragma: no cover"), 0, True),
            "its code": (_MODULE.replace("return 42", "return 43"), 0, True),
        }
        for edit, (edited, exit_status, rewritten) in edits.items():
            files = _Files({"src/answer.py": _MODULE})
            with (
                self.subTest(edit=edit),
                _patched(_python_files_listed(["src/answer.py"]), files),
            ):
                run_if_code_changed.main()
                fingerprint = files.files[run_if_code_changed._RECORD]
                files.files["src/answer.py"] = edited
                self.assertEqual(run_if_code_changed.main(), exit_status)
                self.assertEqual(files.files[run_if_code_changed._RECORD] != fingerprint, rewritten)

    @kills(
        Mutation(
            run_if_code_changed._source,
            "except FileNotFoundError:",
            "except ValueError:",
            "a file removed but not yet committed ends the run with a traceback, which reads as unchanged code",
            raises="FileNotFoundError: [Errno 2] No such file or directory: 'src/other.py'",
        )
    )
    def test_a_python_file_added_or_removed_since_the_fingerprint_is_a_code_change(self):
        """Test that, after a first run, a file added or removed is reported as a code change."""
        # Git keeps listing a tracked file removed from the working tree until the removal is committed, so the
        # listing keeps it.
        edits = {"a file removed": ({"src/other.py"}, {}), "a file added": (set(), {"src/new.py": _MODULE})}
        for edit, (removed, added) in edits.items():
            files = _Files({"src/answer.py": _MODULE, "src/other.py": _MODULE})
            listed = ["src/answer.py", "src/other.py"]
            with (
                self.subTest(edit=edit),
                _patched(_python_files_listed(listed), files),
            ):
                run_if_code_changed.main()
                for path in removed:
                    del files.files[path]
                files.files.update(added)
                listed.extend(added)
                self.assertEqual(run_if_code_changed.main(), 0)

    @kills(
        Mutation(
            run_if_code_changed._fingerprints,
            "except SyntaxError as error:",
            "except ValueError as error:",
            "a file that does not parse ends the run with a traceback rather than running the tests",
            raises="SyntaxError: invalid syntax",
        )
    )
    def test_a_python_file_that_does_not_parse_is_a_code_change_and_says_so(self):
        """Test that a file that does not parse is reported as a code change, and that stderr names it."""
        files = _Files({"src/answer.py": "def answer(:\n"})
        with (
            _patched(_python_files_listed(["src/answer.py"]), files),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(run_if_code_changed.main(), 0)
        self.assertIn("src/answer.py", stderr.getvalue())
        self.assertNotIn(run_if_code_changed._RECORD, files.files)

    @kills(
        Mutation(
            run_if_code_changed._recorded,
            "except json.JSONDecodeError as error:",
            "except KeyError as error:",
            "a fingerprint left half written ends every run with a traceback until someone removes it",
            raises="json.decoder.JSONDecodeError: "
            "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
        )
    )
    def test_a_fingerprint_that_does_not_parse_runs_the_command_and_says_so(self):
        """Test that a fingerprint that is not JSON runs the command, and that stderr names the fingerprint."""
        files = _Files({"src/answer.py": _MODULE, run_if_code_changed._RECORD: "{"})
        with (
            _patched(_python_files_listed(["src/answer.py"]), files),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(run_if_code_changed.main(), 0)
        self.assertIn(run_if_code_changed._RECORD, stderr.getvalue())

    @kills(
        Mutation(
            run_if_code_changed._fingerprints,
            "if result.returncode:",
            "if False:",
            "a git that exits with an error lists nothing, which reads as an unchanged tree",
        ),
        Mutation(
            run_if_code_changed._fingerprints,
            "except OSError as error:",
            "except ValueError as error:",
            "a missing git ends the run with a traceback rather than running the tests",
            raises="FileNotFoundError: [Errno 2] No such file or directory: 'git'",
        ),
    )
    def test_a_git_that_fails_is_reported_as_a_change_and_says_why(self):
        """Test that a git that fails or is missing is reported as a change, and that stderr says why."""
        cases = {
            "git exits with an error": (
                subprocess.CompletedProcess(
                    ("git", "ls-files"), 128, stdout="", stderr="fatal: not a git repository\n"
                ),
                "fatal: not a git repository",
            ),
            "git is not installed": (
                FileNotFoundError(2, "No such file or directory", "git"),
                "No such file or directory",
            ),
        }
        for case, (failure, reason) in cases.items():
            with (
                self.subTest(case=case),
                _patched(_git_failing(failure), _Files({})),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                self.assertEqual(run_if_code_changed.main(), 0)
                self.assertIn(reason, stderr.getvalue())
