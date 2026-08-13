"""Check that a test really guards a behaviour: replace a snippet in a file, run a command, and restore the file.

A test's name is not evidence of what it guards, and neither is a green run. Breaking the code the test names is:
a command that then fails a test has caught the mutation, one that still passes has not, so nothing guards that
code. A command that fails with every test passing has caught nothing either, since a gate it applies beyond the
tests is what failed. The file is restored whether the command passes, fails, or raises, so a probe never leaves
the tree changed.

Usage: `uv run python tools/mutate.py FILE [COMMAND ...]`, with the snippet to replace and its replacement read from
standard input, separated by a line holding only the separator. COMMAND defaults to `just test`.
"""

import re
import subprocess  # nosec
import sys
from pathlib import Path

# The line separating the snippet to replace from its replacement on standard input.
_SEPARATOR = "@@"

# What unittest reports when a run broke rather than failed: a test that raised before it could assert. A stub
# naming something the file cannot resolve breaks every test that reaches it, and exits non-zero exactly as a
# guarding test does, so the count is read back out and reported.
_ERRORS = re.compile(r"^FAILED \(.*\berrors=(?P<errors>\d+)", re.MULTILINE)

# How many tests a run reported running, which a stub that broke the file cuts short. A failing run replays
# unittest's own `Ran N tests`, while a passing one is summarised by the recipe as `PASS (N tests)`, so a comparison
# between the two has to read both spellings.
_TESTS_RUN = re.compile(r"(?:^Ran |PASS \()(?P<tests>\d+) tests?\b", re.MULTILINE)

# The colour codes the recipe wraps its own words in, which would otherwise stand between `PASS` and the count.
_COLOURS = re.compile(r"\x1b\[[0-9;]*m")

# A test the run reported as failing, as unittest names it: the method, where it lives, and a `subTest` case's
# parameters where it has them. Those parameters are what tells one case of a table from another.
_CAUGHT_BY = re.compile(r"^(?:FAIL|ERROR): (?P<test>.+)$", re.MULTILINE)

# The exit code for a probe that never ran: the snippet was not found exactly once, or the input could not be read.
_NOT_RUN = 2

# The exit code for a run that was caught and reported errors: the stub may have broken the file rather than the
# behaviour, so the catch is the run's to explain. A guard firing through an exception is an error too, which is why
# this is neither a plain catch nor a probe that told nothing.
_UNCERTAIN = 3

# The exit code for a run that failed with every test passing: a gate the command applies beyond the tests, such as
# coverage, is what failed. No test caught the mutation, so this says as little about the guard as a survival does.
_UNGUARDED = 4

# What unittest reports when every test it ran passed, whatever a gate the command applies afterwards then says.
_ALL_TESTS_PASSED = re.compile(r"^OK\b", re.MULTILINE)

_DEFAULT_COMMAND = ("just", "test")


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
    """Mutate the file named on the command line, run the command, and report whether the mutation was caught."""
    if not sys.argv[1:]:
        sys.stderr.write(f"usage: mutate.py FILE [COMMAND ...], with the snippets on stdin around a {_SEPARATOR!r}\n")
        return _NOT_RUN
    path, command = Path(sys.argv[1]), sys.argv[2:] or list(_DEFAULT_COMMAND)
    try:
        old, new = snippets(sys.stdin.read())
    except ValueError as reason:
        sys.stderr.write(f"{reason}\n")
        return _NOT_RUN
    original = path.read_text()
    if (occurrences := original.count(old)) != 1:
        sys.stderr.write(f"{path}: the snippet occurs {occurrences} times rather than once; nothing was changed\n")
        return _NOT_RUN
    path.write_text(original.replace(old, new))
    try:
        # Captured rather than streamed, so the run can be read back for the errors below, and written through.
        result = subprocess.run(command, check=False, capture_output=True, text=True)  # noqa: S603 # nosec
    finally:
        path.write_text(original)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    caught = result.returncode != 0
    spelled = " ".join(command)
    output = result.stdout + result.stderr
    if caught and _ALL_TESTS_PASSED.search(output):
        sys.stdout.write(f"{spelled} failed with every test passing, so no test caught the mutation\n")
        return _UNGUARDED
    sys.stdout.write(f"The mutation was caught: {spelled} failed\n" if caught else f"The mutation survived {spelled}\n")
    for test in _caught_by(output):
        sys.stdout.write(f"  caught by {test}\n")
    if caught and (errors := _ERRORS.search(output)):
        return _report_errors(errors.group("errors"), output, command)
    return 0 if caught else 1


def _report_errors(errors: str, output: str, command: list[str]) -> int:
    """Report what a caught run's errors mean, and return the corresponding exit code."""
    baseline = subprocess.run(command, check=False, capture_output=True, text=True)  # noqa: S603 # nosec
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


def _caught_by(output: str) -> list[str]:
    """Return each test the run reported as failing, once, in the order reported.

    Naming them is what shows a guard that catches nothing: a table's case that never appears here, or a mutation
    of one line that a second test catches as well, is a test earning less than it looks to.
    """
    return list(dict.fromkeys(_CAUGHT_BY.findall(_COLOURS.sub("", output))))


def _tests_run(output: str) -> int | None:
    """Return how many tests the run reported running, or None where it reported no count."""
    match = _TESTS_RUN.search(_COLOURS.sub("", output))
    return int(match.group("tests")) if match else None


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
