"""Check that a test really guards a behaviour: replace a snippet in a file, run a command, and restore the file.

A test's name is not evidence of what it guards, and neither is a green run. Breaking the code the test names is:
a command that then fails has caught the mutation, one that still passes has not, so nothing guards that code. The
file is restored whether the command passes, fails, or raises, so a probe never leaves the tree changed.

Usage: `uv run python tools/mutate.py FILE [COMMAND ...]`, with the snippet to replace and its replacement read from
standard input, separated by a line holding only the separator. COMMAND defaults to `just test`.
"""

import subprocess  # nosec
import sys
from pathlib import Path

# The line separating the snippet to replace from its replacement on standard input.
_SEPARATOR = "@@"

# The exit code for a probe that never ran: the snippet was not found exactly once, or the input could not be read.
_NOT_RUN = 2

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
        caught = subprocess.run(command, check=False).returncode != 0  # noqa: S603 # nosec
    finally:
        path.write_text(original)
    spelled = " ".join(command)
    sys.stdout.write(f"The mutation was caught: {spelled} failed\n" if caught else f"The mutation survived {spelled}\n")
    return 0 if caught else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
