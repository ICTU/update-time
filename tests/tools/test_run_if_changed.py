"""Unit tests for running a command where its inputs changed since it last passed."""

import contextlib
import io
import json
import subprocess  # nosec
import sys
import unittest
from fnmatch import fnmatch
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from tools import run_if_changed

from tests.helpers import FakeFiles
from tests.mutation import Mutation, kills

if TYPE_CHECKING:
    from collections.abc import Iterator

# A module with a docstring of its own and one in its function.
_MODULE = '''"""Answer questions."""


def answer() -> int:
    """Return the answer."""
    return 42
'''


def _files_listed(paths: list[str], command_exit_status: int = 0) -> Mock:
    """Return a stub for `subprocess.run` whose `git ls-files` lists the files, reading the list at each call.

    It lists the files that match a pathspec the command ends on, as git does. Any other command exits with the given
    status.
    """

    def run(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            pathspecs = [argument for argument in command[2:] if not argument.startswith("-")]
            listed = [path for path in paths if any(fnmatch(path, pathspec) for pathspec in pathspecs)]
            return subprocess.CompletedProcess(command, 0, stdout="".join(f"{path}\0" for path in listed), stderr="")
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
def _patched(run: Mock, files: FakeFiles) -> Iterator[None]:
    """Patch the tool's `subprocess.run` with the stub, and `Path` with the files."""
    with patch("tools.run_if_changed.subprocess.run", run), files.patched():
        yield


# The tests ask the tool for this exit status where it skips the command.
_SKIPPED = 3
# The tests give the tool this command to run.
_COMMAND = ("just", "test")
# Every test gives the tool these inputs and this record, unless it names its own.
_SHARED_OPTIONS = ("--inputs", "*.py", "--record", "py-code")
# The file that keeps the fingerprints of that record.
_RECORD_FILE = "build/py-code-fingerprints.json"


def _argv(*options: str) -> list[str]:
    """Return the command line that runs the tool with the options given, which win over the tests' inputs."""
    return ["run_if_changed.py", "--skipped-status", str(_SKIPPED), *_SHARED_OPTIONS, *options, "--", *_COMMAND]


def _commands_run(run: Mock) -> list[tuple[str, ...]]:
    """Return the commands the stub for `subprocess.run` was asked to run, git's own commands left out."""
    return [tuple(call.args[0]) for call in run.call_args_list if call.args[0][0] != "git"]


@patch.object(sys, "argv", _argv())
class MainTest(unittest.TestCase):
    """Unit tests for running the command where its inputs changed, and skipping it where none did."""

    def test_without_a_fingerprint_the_code_is_fingerprinted_and_the_command_run(self):
        """Test that, where no fingerprint exists, the tool runs the command and fingerprints the code per file."""
        files = FakeFiles({"src/answer.py": _MODULE})
        with _patched(_files_listed(["src/answer.py"]), files):
            self.assertEqual(run_if_changed.main(), 0)
        self.assertEqual(list(json.loads(files.files[_RECORD_FILE])), ["src/answer.py"])

    def test_a_change_to_an_input_runs_the_command_and_a_change_elsewhere_skips_it(self):
        """Test that the files `--inputs` names decide whether the command runs, and the other files do not."""
        argv = _argv("--inputs", "tools/*.py")
        files = FakeFiles({"src/answer.py": _MODULE, "tools/answer.py": _MODULE})
        run = _files_listed(["src/answer.py", "tools/answer.py"])
        with patch.object(sys, "argv", argv), _patched(run, files):
            run_if_changed.main()
            files.files["src/answer.py"] = _MODULE.replace("42", "43")
            self.assertEqual(run_if_changed.main(), _SKIPPED)
            files.files["tools/answer.py"] = _MODULE.replace("42", "43")
            self.assertEqual(run_if_changed.main(), 0)
        self.assertEqual(_commands_run(run), [_COMMAND, _COMMAND])

    def test_an_input_other_than_python_counts_as_changed_by_any_edit(self):
        """Test that an input other than a Python file is fingerprinted by its content, parsing as Python or not."""
        argv = _argv("--inputs", "uv.lock")
        for case, lock in (("parses as Python", "version = 1\n"), ("does not parse as Python", "lock-version = 1\n")):
            files = FakeFiles({"uv.lock": lock})
            with (
                self.subTest(case),
                patch.object(sys, "argv", argv),
                _patched(_files_listed(["uv.lock"]), files),
            ):
                run_if_changed.main()
                self.assertEqual(run_if_changed.main(), _SKIPPED)
                files.files["uv.lock"] = f"{lock}# A comment.\n"
                self.assertEqual(run_if_changed.main(), 0)

    def test_an_input_git_ignores_counts_where_named_by_its_path(self):
        """Test that deleting a file git ignores, which the inputs name by its path, runs the command."""
        files = FakeFiles({"uv.lock": "version = 1\n", ".venv/pyvenv.cfg": "home = /usr/bin\n"})
        run = _files_listed(["uv.lock"])  # Git leaves the ignored environment out of its listing.
        with patch.object(sys, "argv", _argv("--inputs", "uv.lock", ".venv/pyvenv.cfg")), _patched(run, files):
            run_if_changed.main()
            self.assertEqual(run_if_changed.main(), _SKIPPED)
            del files.files[".venv/pyvenv.cfg"]
            self.assertEqual(run_if_changed.main(), 0)

    def test_each_record_keeps_its_own_fingerprint(self):
        """Test that a command passing under one record leaves the command of another record to run."""
        files = FakeFiles({"src/answer.py": _MODULE})
        run = _files_listed(["src/answer.py"])
        with _patched(run, files):
            with patch.object(sys, "argv", _argv("--record", "lint")):
                self.assertEqual(run_if_changed.main(), 0)
            with patch.object(sys, "argv", _argv("--record", "audit")):
                self.assertEqual(run_if_changed.main(), 0)
        self.assertEqual(_commands_run(run), [_COMMAND, _COMMAND])
        self.assertEqual(
            sorted(files.files),
            ["build/audit-fingerprints.json", "build/lint-fingerprints.json", "src/answer.py"],
        )

    def test_the_command_is_passed_the_files_that_changed_among_those_it_lints(self):
        """Test that `--pass-changed` hands the command the files it names that changed, or all without a record."""
        files = FakeFiles({"src/answer.py": _MODULE, "src/other.py": _MODULE})
        run = _files_listed(["src/answer.py", "src/other.py"])
        with (
            patch.object(sys, "argv", _argv("--inputs", "uv.lock", "--pass-changed", "src/*.py")),
            _patched(run, files),
        ):
            run_if_changed.main()
            files.files["src/other.py"] = _MODULE.replace("42", "43")
            self.assertEqual(run_if_changed.main(), 0)
            self.assertEqual(run_if_changed.main(), _SKIPPED)
        self.assertEqual(
            _commands_run(run), [(*_COMMAND, "src/answer.py", "src/other.py"), (*_COMMAND, "src/other.py")]
        )

    def test_with_whole_files_an_edit_to_a_comment_alone_runs_the_command(self):
        """Test that `--whole-files` fingerprints a Python file whole, so an edit to a comment runs the command."""
        files = FakeFiles({"src/answer.py": _MODULE})
        with (
            patch.object(sys, "argv", _argv("--whole-files")),
            _patched(_files_listed(["src/answer.py"]), files),
        ):
            run_if_changed.main()
            files.files["src/answer.py"] = _MODULE.replace("return 42", "return 42  # nosec")
            self.assertEqual(run_if_changed.main(), 0)

    def test_a_file_to_pass_deleted_alone_is_recorded_without_running_the_command(self):
        """Test that deleting one of the files `--pass-changed` names, and nothing else, skips the command."""
        files = FakeFiles({"src/answer.py": _MODULE, "src/other.py": _MODULE})
        # Git keeps listing a tracked file removed from the working tree until the removal is committed.
        run = _files_listed(["src/answer.py", "src/other.py"])
        with (
            patch.object(sys, "argv", _argv("--inputs", "uv.lock", "--pass-changed", "src/*.py")),
            _patched(run, files),
        ):
            run_if_changed.main()
            del files.files["src/other.py"]
            self.assertEqual(run_if_changed.main(), _SKIPPED)
        self.assertEqual(len(_commands_run(run)), 1)
        self.assertEqual(list(json.loads(files.files[_RECORD_FILE])), ["src/answer.py"])

    def test_a_changed_input_hands_the_command_every_file_to_pass(self):
        """Test that editing or deleting an input `--inputs` names hands the command every file `--pass-changed` names.

        That holds for an input `--pass-changed` names as well.
        """
        for case, path, content in (
            ("an input edited", "uv.lock", "version = 2\n"),
            ("an input deleted", "uv.lock", None),
            ("a file to pass that is an input as well, edited", "src/answer.py", _MODULE.replace("42", "43")),
        ):
            files = FakeFiles({"uv.lock": "version = 1\n", "src/answer.py": _MODULE, "src/other.py": _MODULE})
            run = _files_listed(["uv.lock", "src/answer.py", "src/other.py"])
            argv = _argv("--inputs", "uv.lock", "src/answer.py", "--pass-changed", "src/*.py")
            with self.subTest(case), patch.object(sys, "argv", argv), _patched(run, files):
                run_if_changed.main()
                if content is None:
                    del files.files[path]
                else:
                    files.files[path] = content
                self.assertEqual(run_if_changed.main(), 0)
                self.assertEqual(_commands_run(run)[1:], [(*_COMMAND, "src/answer.py", "src/other.py")])

    @kills(
        Mutation(
            run_if_changed.main,
            "fingerprints == recorded",
            "False",
            "code unchanged since the command last passed reads as changed, so the tests run every time",
        )
    )
    def test_code_equal_to_the_fingerprint_is_skipped_and_keeps_the_fingerprint(self):
        """Test that a second run over unchanged code exits with the skip status and does not run the command."""
        files = FakeFiles({"src/answer.py": _MODULE})
        run = _files_listed(["src/answer.py"])
        with _patched(run, files):
            run_if_changed.main()
            fingerprint = files.files[_RECORD_FILE]
            run.reset_mock()
            self.assertEqual(run_if_changed.main(), _SKIPPED)
        self.assertEqual(_commands_run(run), [])
        self.assertEqual(files.files[_RECORD_FILE], fingerprint)

    @kills(
        Mutation(
            run_if_changed.main,
            "if not status and fingerprints is not None:",
            "if fingerprints is not None:",
            "a failing run leaves its fingerprint, so the next run skips the command although it still fails",
        )
    )
    def test_a_command_that_fails_leaves_no_fingerprint_and_gives_a_failing_exit_status(self):
        """Test that a failing command does not leave a fingerprint, and gives its status, or 1 for the skip status."""
        for case, command_exit_status, exit_status in (("another status", 2, 2), ("the skip status", _SKIPPED, 1)):
            files = FakeFiles({"src/answer.py": _MODULE})
            run = _files_listed(["src/answer.py"], command_exit_status=command_exit_status)
            with self.subTest(case), _patched(run, files):
                self.assertEqual(run_if_changed.main(), exit_status)
                self.assertEqual(_commands_run(run), [_COMMAND])
                self.assertNotIn(_RECORD_FILE, files.files)

    @kills(
        Mutation(
            run_if_changed._fingerprint,
            "node.body = node.body[1:] or [ast.Pass()]",
            "pass",
            "an edit to a docstring alone reads as a change to the code, so the tests run all the same",
        ),
        Mutation(
            run_if_changed._fingerprint,
            "ast.dump(tree)",
            "ast.dump(tree, include_attributes=True)",
            "code that moves to another line reads as changed, so a comment above it runs the tests all the same",
        ),
        Mutation(
            run_if_changed._fingerprint,
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
            files = FakeFiles({"src/answer.py": _MODULE})
            with (
                self.subTest(edit=edit),
                _patched(_files_listed(["src/answer.py"]), files),
            ):
                run_if_changed.main()
                fingerprint = files.files[_RECORD_FILE]
                files.files["src/answer.py"] = edited
                self.assertEqual(run_if_changed.main(), exit_status)
                self.assertEqual(files.files[_RECORD_FILE] != fingerprint, rewritten)

    @kills(
        Mutation(
            run_if_changed._source,
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
            files = FakeFiles({"src/answer.py": _MODULE, "src/other.py": _MODULE})
            listed = ["src/answer.py", "src/other.py"]
            with (
                self.subTest(edit=edit),
                _patched(_files_listed(listed), files),
            ):
                run_if_changed.main()
                for path in removed:
                    del files.files[path]
                files.files.update(added)
                listed.extend(added)
                self.assertEqual(run_if_changed.main(), 0)

    @kills(
        Mutation(
            run_if_changed._fingerprints,
            "except SyntaxError as error:",
            "except ValueError as error:",
            "a file that does not parse ends the run with a traceback rather than running the tests",
            raises="SyntaxError: invalid syntax",
        )
    )
    def test_a_python_file_that_does_not_parse_is_a_code_change_and_says_so(self):
        """Test that a file that does not parse is reported as a code change, and that stderr names it."""
        files = FakeFiles({"src/answer.py": "def answer(:\n"})
        with (
            _patched(_files_listed(["src/answer.py"]), files),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(run_if_changed.main(), 0)
        self.assertIn("src/answer.py", stderr.getvalue())
        self.assertNotIn(_RECORD_FILE, files.files)

    @kills(
        Mutation(
            run_if_changed._recorded,
            "except json.JSONDecodeError as error:",
            "except KeyError as error:",
            "a fingerprint left half written ends every run with a traceback until someone removes it",
            raises="json.decoder.JSONDecodeError: "
            "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
        )
    )
    def test_a_fingerprint_that_does_not_parse_runs_the_command_and_says_so(self):
        """Test that a fingerprint that is not JSON runs the command, and that stderr names the fingerprint."""
        files = FakeFiles({"src/answer.py": _MODULE, _RECORD_FILE: "{"})
        with (
            _patched(_files_listed(["src/answer.py"]), files),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(run_if_changed.main(), 0)
        self.assertIn(_RECORD_FILE, stderr.getvalue())

    @kills(
        Mutation(
            run_if_changed._paths,
            "if result.returncode:",
            "if False:",
            "a git that exits with an error lists nothing, which reads as an unchanged tree",
        ),
        Mutation(
            run_if_changed._paths,
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
                _patched(_git_failing(failure), FakeFiles({})),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                self.assertEqual(run_if_changed.main(), 0)
                self.assertIn(reason, stderr.getvalue())
