"""Check that a test really guards a behaviour: replace a snippet in a file, run a command, and restore the file.

A test's name is not evidence of what it guards, and neither is a green run. Breaking the code the test names is:
a command that then fails a test has killed the mutation, one that still passes has not, so nothing guards that
code. A command that fails some other way has killed nothing either, since a gate it applies beyond the tests is
what failed. The file is restored whether the command passes, fails, or raises, so a probe never leaves the tree
changed.

Usage: `just mutate FILE[:ANCHOR] [COMMAND ...]`, with the snippet to replace and its replacement read from standard
input, separated by a line holding only the separator. COMMAND defaults to `just test`. Naming a definition after
the file looks for the snippet inside that definition, so the snippet has to be unique within that definition rather
than across the whole file. The recipe unsets FORCE_COLOR, so the names of the tests that failed can be read back.
"""

import os
import re
import subprocess  # nosec
import sys
from pathlib import Path

from tests.mutation import CHECKS_OFF, StaleError, mutated_source

# The line separating the snippet to replace from its replacement on standard input.
_SEPARATOR = "@@"

# What separates a file from the definition inside it to change, as in `tests/mutation.py:Mutation._mutated`. The
# snippet then has to be unique within that definition rather than across the whole file.
_ANCHOR = ":"

# What unittest reports when a run broke rather than failed: a test that raised before it could assert. A stub
# naming something the file cannot resolve breaks every test that reaches it, and exits non-zero exactly as a
# guarding test does, so the count is read back out and reported.
_ERRORS = re.compile(r"^FAILED \(.*\berrors=(?P<errors>\d+)", re.MULTILINE)

# How many tests a run reported running, which a stub that broke the file cuts short. A failing run replays
# unittest's own `Ran N tests`, while a passing one is summarised by the recipe as `PASS (N tests)`, so a comparison
# between the two has to read both spellings.
_TESTS_RUN = re.compile(r"(?:^Ran |PASS \()(?P<tests>\d+) tests?\b", re.MULTILINE)

# A test the run reported as failing, as unittest names it: the method, where it lives, and a `subTest` case's
# parameters where it has them. Those parameters are what tells one case of a table from another.
_KILLED_BY = re.compile(r"^(?:FAIL|ERROR): (?P<test>.+)$", re.MULTILINE)

# The exit code for a probe that never ran: the file could not be read, the anchor did not hold the snippet exactly
# once, the file does not define the anchor, or the input could not be read.
_NOT_RUN = 2

# The exit code for a run that was killed and reported errors: the stub may have broken the file rather than the
# behaviour, so the kill is the run's to explain. A guard firing through an exception is an error too, which is why
# this is neither a plain kill nor a probe that told nothing.
_UNCERTAIN = 3

# The exit code for a run that failed although its tests passed: a gate the command applies beyond the tests is
# what failed. So this says as little about the guard as a survival does.
_UNGUARDED = 4

# unittest and pytest exit with this status when a test failed. A command failing with any other status failed a
# gate it applies beyond the tests, so its tests passed.
_TESTS_FAILED = 1

_DEFAULT_COMMAND = ("just", "test")

# The command runs in this environment, which has the `@kills` checks switched off. A test whose own registered mutation
# names a line this probe rewrote would report that mutation as stale or survived, which says nothing about the
# probe, so the kill list holds the tests that failed on this mutation and no others.
_ENVIRONMENT = os.environ | {CHECKS_OFF: "1"}


def snippets(text: str) -> tuple[str, str]:
    """Return the snippet to replace and its replacement, split on the separator line.

    The replacement drops the trailing newline the input ends with, so that both snippets are the text between the
    separators and nothing more.
    """
    before, separator, after = text.partition(f"\n{_SEPARATOR}\n")
    if not separator:
        message = f"standard input holds no {_SEPARATOR!r} line separating the snippet from its replacement"
        raise ValueError(message)
    return before, after.removesuffix("\n")


def main() -> int:
    """Mutate the file named on the command line, run the command, and report whether the mutation was killed."""
    if not sys.argv[1:]:
        sys.stderr.write(
            f"usage: mutate.py FILE[:ANCHOR] [COMMAND ...], with the snippets on stdin around a {_SEPARATOR!r}\n"
        )
        return _NOT_RUN
    file_name, _, anchor = sys.argv[1].partition(_ANCHOR)
    path, command = Path(file_name), sys.argv[2:] or list(_DEFAULT_COMMAND)
    try:
        old, new = snippets(sys.stdin.read())
    except ValueError as reason:
        sys.stderr.write(f"{reason}\n")
        return _NOT_RUN
    try:
        original = path.read_text()
        path.write_text(mutated_source(original, anchor, old, new))
    except (OSError, StaleError) as reason:
        sys.stderr.write(f"{path}: {reason}; nothing was changed\n")
        return _NOT_RUN
    try:
        # Captured rather than streamed, so the run can be read back for the errors below, and written through.
        result = subprocess.run(  # noqa: S603 # nosec
            command, check=False, capture_output=True, text=True, env=_ENVIRONMENT
        )
    finally:
        path.write_text(original)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    killed = result.returncode != 0
    spelled = " ".join(command)
    output = result.stdout + result.stderr
    if killed and result.returncode != _TESTS_FAILED:
        sys.stdout.write(
            f"{spelled} exited {result.returncode} rather than {_TESTS_FAILED}: a gate failed, not a test\n"
        )
        return _UNGUARDED
    sys.stdout.write(f"The mutation was killed: {spelled} failed\n" if killed else f"The mutation survived {spelled}\n")
    for test in _killed_by(output):
        sys.stdout.write(f"  killed by {test}\n")
    if killed and (errors := _ERRORS.search(output)):
        return _report_errors(errors.group("errors"), output, command)
    return 0 if killed else 1


def _report_errors(errors: str, output: str, command: list[str]) -> int:
    """Report what a killed run's errors mean, and return the corresponding exit code.

    Errors leave two readings open: a guard that fired by raising, or a stub that broke the file so the tests never
    ran. A clean run's test count tells them apart, and the file is restored by now, so running the command again
    is where that count comes from.
    """
    sys.stdout.write("The command runs again on the restored file, to count the tests a clean run reaches\n")
    baseline = subprocess.run(  # noqa: S603 # nosec
        command, check=False, capture_output=True, text=True, env=_ENVIRONMENT
    )
    mutated_tests = _tests_run(output)
    baseline_tests = _tests_run(baseline.stdout + baseline.stderr)
    if mutated_tests is None or baseline_tests is None:
        sys.stdout.write(
            f"The run reported {errors} errors rather than failures, so the stub may have broken the file rather "
            "than the behaviour a test guards\n"
        )
        return _UNCERTAIN
    if mutated_tests < baseline_tests:
        sys.stdout.write(
            f"{baseline_tests - mutated_tests} of {baseline_tests} tests never ran, so the stub broke the file "
            "rather than the behaviour a test guards\n"
        )
        return _UNCERTAIN
    return 0


def _killed_by(output: str) -> list[str]:
    """Return each test the run reported as failing, once, in the order reported.

    Naming them is what shows a guard that kills nothing: a table's case that never appears here, or a mutation
    of one line that a second test kills as well, is a test earning less than it looks to.
    """
    return list(dict.fromkeys(_KILLED_BY.findall(output)))


def _tests_run(output: str) -> int | None:
    """Return how many tests the run reported running, or None where it reported no count."""
    match = _TESTS_RUN.search(output)
    return int(match.group("tests")) if match else None


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
