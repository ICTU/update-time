"""Unit tests for running a command where its inputs changed since it last passed."""

import contextlib
import io
import json
import subprocess  # nosec
import sys
import unittest
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Final
from unittest.mock import Mock, patch

from tools.run_if_changed import _fingerprint, _fingerprints, _paths, _recorded, _source, main

from mutation_time.mutation import kills

from tests.file_mocks import FakeFiles

if TYPE_CHECKING:
    from collections.abc import Callable, Generator


# A module with a docstring of its own and one in its function.
_MODULE: Final[str] = '''"""Answer questions."""


def answer() -> int:
    """Return the answer."""
    return 42
'''


# The tests ask the tool for this exit status where it skips the command.
_SKIPPED: Final[int] = 3
# The tests give the tool this command to run.
_COMMAND: Final[tuple[str, ...]] = ("just", "test")
# Every test gives the tool these inputs and this record, unless it names its own.
_SHARED_OPTIONS: Final[tuple[str, ...]] = ("--inputs", "*.py", "--record", "py-code")
# The file that keeps the fingerprints of that record.
_RECORD_FILE: Final[Path] = Path("build/py-code-fingerprints.json")
# The Python files most tests change.
_ANSWER: Final[Path] = Path("src/answer.py")
_OTHER: Final[Path] = Path("src/other.py")
# The input other than Python that tests change.
_UV_LOCK: Final[Path] = Path("uv.lock")


def _argv(*options: str) -> list[str]:
    """Return the command line that runs the tool with the options given, which win over the tests' inputs."""
    return ["run_if_changed.py", "--skipped-status", str(_SKIPPED), *_SHARED_OPTIONS, *options, "--", *_COMMAND]


@patch.object(sys, "argv", _argv())
class MainTest(unittest.TestCase):
    """Unit tests for running the command where its inputs changed, and skipping it where none did."""

    def test_without_a_fingerprint_the_code_is_fingerprinted_and_the_command_run(self):
        """Test that, where no fingerprint exists, the tool runs the command and fingerprints the code per file."""
        files = FakeFiles({_ANSWER: _MODULE})
        with _patched(_files_listed([_ANSWER]), files):
            self.assertEqual(main(), 0)
        self.assertEqual(list(json.loads(files[_RECORD_FILE])), [str(_ANSWER)])

    def test_a_change_to_an_input_runs_the_command_and_a_change_elsewhere_skips_it(self):
        """Test that the files `--inputs` names decide whether the command runs, and the other files do not."""
        argv = _argv("--inputs", "tools/*.py")
        files = FakeFiles({_ANSWER: _MODULE, Path("tools/answer.py"): _MODULE})
        run = _files_listed([_ANSWER, Path("tools/answer.py")])
        with patch.object(sys, "argv", argv), _patched(run, files):
            main()
            files[_ANSWER] = _MODULE.replace("42", "43")
            self.assertEqual(main(), _SKIPPED)
            files[Path("tools/answer.py")] = _MODULE.replace("42", "43")
            self.assertEqual(main(), 0)
        self.assertEqual(_commands_run(run), [_COMMAND, _COMMAND])

    def test_an_input_other_than_python_counts_as_changed_by_any_edit(self):
        """Test that an input other than a Python file is fingerprinted by its content, parsing as Python or not."""
        argv = _argv("--inputs", str(_UV_LOCK))
        for case, lock in (("parses as Python", "version = 1\n"), ("does not parse as Python", "lock-version = 1\n")):
            files = FakeFiles({_UV_LOCK: lock})
            with self.subTest(case), patch.object(sys, "argv", argv), _patched(_files_listed([_UV_LOCK]), files):
                main()
                self.assertEqual(main(), _SKIPPED)
                files[_UV_LOCK] = f"{lock}# A comment.\n"
                self.assertEqual(main(), 0)

    def test_an_input_git_ignores_counts_where_named_by_its_path(self):
        """Test that deleting a file git ignores, which the inputs name by its path, runs the command."""
        files = FakeFiles({_UV_LOCK: "version = 1\n", Path(".venv/pyvenv.cfg"): "home = /usr/bin\n"})
        run = _files_listed([_UV_LOCK])  # Git leaves the ignored environment out of its listing.
        with patch.object(sys, "argv", _argv("--inputs", str(_UV_LOCK), ".venv/pyvenv.cfg")), _patched(run, files):
            main()
            self.assertEqual(main(), _SKIPPED)
            del files[Path(".venv/pyvenv.cfg")]
            self.assertEqual(main(), 0)

    def test_each_record_keeps_its_own_fingerprint(self):
        """Test that a command passing under one record leaves the command of another record to run."""
        files = FakeFiles({_ANSWER: _MODULE})
        run = _files_listed([_ANSWER])
        with _patched(run, files):
            with patch.object(sys, "argv", _argv("--record", "lint")):
                self.assertEqual(main(), 0)
            with patch.object(sys, "argv", _argv("--record", "audit")):
                self.assertEqual(main(), 0)
        self.assertEqual(_commands_run(run), [_COMMAND, _COMMAND])
        self.assertEqual(
            sorted(files), [Path("build/audit-fingerprints.json"), Path("build/lint-fingerprints.json"), _ANSWER]
        )

    def test_the_command_is_passed_the_files_that_changed_among_those_it_lints(self):
        """Test that `--pass-changed` hands the command the files it names that changed, or all without a record."""
        files = FakeFiles({_ANSWER: _MODULE, _OTHER: _MODULE})
        run = _files_listed([_ANSWER, _OTHER])
        with (
            patch.object(sys, "argv", _argv("--inputs", str(_UV_LOCK), "--pass-changed", "src/*.py")),
            _patched(run, files),
        ):
            main()
            files[_OTHER] = _MODULE.replace("42", "43")
            self.assertEqual(main(), 0)
            self.assertEqual(main(), _SKIPPED)
        self.assertEqual(_commands_run(run), [(*_COMMAND, str(_ANSWER), str(_OTHER)), (*_COMMAND, str(_OTHER))])

    def test_with_whole_files_an_edit_to_a_comment_alone_runs_the_command(self):
        """Test that `--whole-files` fingerprints a Python file whole, so an edit to a comment runs the command."""
        files = FakeFiles({_ANSWER: _MODULE})
        with patch.object(sys, "argv", _argv("--whole-files")), _patched(_files_listed([_ANSWER]), files):
            main()
            files[_ANSWER] = _MODULE.replace("return 42", "return 42  # nosec")
            self.assertEqual(main(), 0)

    def test_a_file_to_pass_deleted_alone_is_recorded_without_running_the_command(self):
        """Test that deleting one of the files `--pass-changed` names, and nothing else, skips the command."""
        files = FakeFiles({_ANSWER: _MODULE, _OTHER: _MODULE})
        # Git keeps listing a tracked file removed from the working tree until the removal is committed.
        run = _files_listed([_ANSWER, _OTHER])
        with (
            patch.object(sys, "argv", _argv("--inputs", str(_UV_LOCK), "--pass-changed", "src/*.py")),
            _patched(run, files),
        ):
            main()
            del files[_OTHER]
            self.assertEqual(main(), _SKIPPED)
        self.assertEqual(len(_commands_run(run)), 1)
        self.assertEqual(list(json.loads(files[_RECORD_FILE])), [str(_ANSWER)])

    def test_a_changed_input_hands_the_command_every_file_to_pass(self):
        """Test that editing or deleting an input `--inputs` names hands the command every file `--pass-changed` names.

        That holds for an input `--pass-changed` names as well.
        """
        changes: dict[str, Callable[[FakeFiles], object]] = {
            "an input edited": lambda files: files.update({_UV_LOCK: "version = 2\n"}),
            "an input deleted": lambda files: files.pop(_UV_LOCK),
            "a file to pass that is an input as well, edited": lambda files: files.update(
                {_ANSWER: _MODULE.replace("42", "43")}
            ),
        }
        for case, change in changes.items():
            files = FakeFiles({_UV_LOCK: "version = 1\n", _ANSWER: _MODULE, _OTHER: _MODULE})
            run = _files_listed([_UV_LOCK, _ANSWER, _OTHER])
            argv = _argv("--inputs", str(_UV_LOCK), str(_ANSWER), "--pass-changed", "src/*.py")
            with self.subTest(case), patch.object(sys, "argv", argv), _patched(run, files):
                main()
                change(files)
                self.assertEqual(main(), 0)
                self.assertEqual(_commands_run(run)[1:], [(*_COMMAND, str(_ANSWER), str(_OTHER))])

    def test_code_equal_to_the_fingerprint_is_skipped(self):
        """Test that a second run over unchanged code exits with the skip status and does not run the command."""
        run = _files_listed([_ANSWER])
        with _patched(run, FakeFiles({_ANSWER: _MODULE})):
            main()
            run.reset_mock()
            self.assertEqual(main(), _SKIPPED)
        self.assertEqual(_commands_run(run), [])

    @kills.delete(
        main,
        "not status and ",
        "a failing run leaves its fingerprint, so the next run skips the command although it still fails",
        case="another status",
    )
    @kills.delete(
        main,
        "1 if status == arguments.skipped_status else ",
        "a command exiting with the skip status reads as skipped, so its failure passes unseen",
        case="the skip status",
    )
    def test_a_command_that_fails_leaves_no_fingerprint_and_gives_a_failing_exit_status(self):
        """Test that a failing command does not leave a fingerprint, and gives its status, or 1 for the skip status."""
        for case, command_exit_status, exit_status in (("another status", 2, 2), ("the skip status", _SKIPPED, 1)):
            files = FakeFiles({_ANSWER: _MODULE})
            run = _files_listed([_ANSWER], command_exit_status=command_exit_status)
            with self.subTest(case), _patched(run, files):
                self.assertEqual(main(), exit_status)
                self.assertEqual(_commands_run(run), [_COMMAND])
                self.assertNotIn(_RECORD_FILE, files)

    @kills.delete_statement(
        _fingerprint,
        "node.body = node.body[1:] or [ast.Pass()]",
        "an edit to a docstring alone reads as a change to the code, so the tests run all the same",
        case="its docstrings",
    )
    @kills.append(
        _fingerprint,
        "ast.dump(tree",
        ", include_attributes=True",
        "code that moves to another line reads as changed, so a comment above it runs the tests all the same",
        case="a comment",
    )
    @kills.replace(
        _fingerprint,
        '"pragma" in token.string]',
        '"never" in token.string]',
        "a coverage pragma removed alone reads as unchanged, so the coverage gate never sees the line it exposed",
        case="a coverage pragma",
    )
    def test_an_edit_to_docstrings_or_comments_alone_skips_the_command(self):
        """Test that editing docstrings or comments alone skips the command, and editing a coverage pragma does not."""
        # An edit to the code runs the command, so a fingerprint that never changes fails the test
        self.assertEqual(_rerun_after(_MODULE.replace("return 42", "return 43")), (0, True))
        edits = {
            "its docstrings": _MODULE.replace("Answer questions.", "Answer every question.").replace(
                "Return the answer.", "Return the answer to everything."
            ),
            "a comment": _MODULE.replace("    return 42", "    # The answer to everything.\n    return 42"),
        }
        for edit, edited in edits.items():
            with self.subTest(edit=edit):
                self.assertEqual(_rerun_after(edited), (_SKIPPED, False))
        with self.subTest(edit="a coverage pragma"):
            self.assertEqual(_rerun_after(_MODULE.replace("-> int:", "-> int:  # pragma: no cover")), (0, True))

    @kills.replace(
        _source,
        "except FileNotFoundError:",
        "except ValueError:",
        "a file removed but not yet committed ends the run with a traceback, which reads as unchanged code",
        raises="FileNotFoundError: [Errno 2] No such file or directory: 'src/other.py'",
    )
    def test_a_python_file_removed_since_the_fingerprint_is_a_code_change(self):
        """Test that, after a first run, a file removed is reported as a code change."""
        files = FakeFiles({_ANSWER: _MODULE, _OTHER: _MODULE})
        # Git keeps listing a tracked file removed from the working tree until the removal is committed, so the
        # listing keeps it.
        with _patched(_files_listed([_ANSWER, _OTHER]), files):
            main()
            del files[_OTHER]
            self.assertEqual(main(), 0)

    @kills.replace(
        _fingerprints,
        "except SyntaxError as error:",
        "except ValueError as error:",
        "a file that does not parse ends the run with a traceback rather than running the tests",
        raises="SyntaxError: invalid syntax",
    )
    def test_a_python_file_that_does_not_parse_is_a_code_change_and_says_so(self):
        """Test that a file that does not parse is reported as a code change, and that stderr names it."""
        files = FakeFiles({_ANSWER: "def answer(:\n"})
        with _patched(_files_listed([_ANSWER]), files), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(), 0)
        self.assertIn(str(_ANSWER), stderr.getvalue())
        self.assertNotIn(_RECORD_FILE, files)

    @kills.replace(
        _recorded,
        "except json.JSONDecodeError as error:",
        "except KeyError as error:",
        "a fingerprint left half written ends every run with a traceback until someone removes it",
        raises="json.decoder.JSONDecodeError: "
        "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
    )
    def test_a_fingerprint_that_does_not_parse_runs_the_command_and_says_so(self):
        """Test that a fingerprint that is not JSON runs the command, and that stderr names the fingerprint."""
        files = FakeFiles({_ANSWER: _MODULE, _RECORD_FILE: "{"})
        with _patched(_files_listed([_ANSWER]), files), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(), 0)
        self.assertIn(str(_RECORD_FILE), stderr.getvalue())

    @kills.force_false(
        _paths,
        "result.returncode",
        "a git that exits with an error lists nothing, which reads as an unchanged tree",
        case="git exits with an error",
    )
    @kills.replace(
        _paths,
        "except OSError as error:",
        "except ValueError as error:",
        "a missing git ends the run with a traceback rather than running the tests",
        raises="FileNotFoundError: [Errno 2] No such file or directory: 'git'",
        case="git is not installed",
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
                _patched(_git_failing(failure), FakeFiles({})),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                self.assertEqual(main(), 0)
                self.assertIn(reason, stderr.getvalue())


@contextlib.contextmanager
def _patched(run: Mock, files: FakeFiles) -> Generator[None]:
    """Patch the tool's `subprocess.run` with the stub, and `Path` with the files."""
    with patch("tools.run_if_changed.subprocess.run", run), files.patched():
        yield


def _files_listed(paths: list[Path], command_exit_status: int = 0) -> Mock:
    """Return a stub for `subprocess.run` whose `git ls-files` lists the files, reading the list at each call.

    It lists the files that match a pathspec the command ends on, as git does. Any other command exits with the given
    status.
    """

    def run(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            pathspecs = [argument for argument in command[2:] if not argument.startswith("-")]
            listed = [path for path in paths if any(fnmatch(path.as_posix(), pathspec) for pathspec in pathspecs)]
            return subprocess.CompletedProcess(command, 0, stdout="".join(f"{path}\0" for path in listed), stderr="")
        return subprocess.CompletedProcess(command, command_exit_status)

    return Mock(side_effect=run)


def _commands_run(run: Mock) -> list[tuple[str, ...]]:
    """Return the commands the stub for `subprocess.run` was asked to run, git's own commands left out."""
    return [tuple(call.args[0]) for call in run.call_args_list if call.args[0][0] != "git"]


def _rerun_after(edited: str) -> tuple[int, bool]:
    """Return the exit status of a run after the module was edited, and whether that run rewrote the fingerprint."""
    files = FakeFiles({_ANSWER: _MODULE})
    with _patched(_files_listed([_ANSWER]), files):
        main()
        fingerprint = files[_RECORD_FILE]
        files[_ANSWER] = edited
        return main(), files[_RECORD_FILE] != fingerprint


def _git_failing(failure: subprocess.CompletedProcess[str] | OSError) -> Mock:
    """Return a stub for `subprocess.run` whose git fails as given, while any other command succeeds."""

    def run(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] != "git":
            return subprocess.CompletedProcess(command, 0)
        if isinstance(failure, OSError):
            raise failure
        return failure

    return Mock(side_effect=run)
