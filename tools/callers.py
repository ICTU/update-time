"""Report where a name is called, so the size of a change to it is counted rather than guessed.

A grep counts the lines that hold a name, which is a different number. A call whose arguments sit on their own
lines spreads one call site over several lines, and the line defining the name holds no call at all.

Usage: `uv run python tools/callers.py NAME`, which reports every call this repository makes to the name.
"""

import ast
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# The roots holding this repository's Python, which is where a call to one of its own names is made.
_ROOTS = ("src", "tests", "tools")

# The definitions a call reaches: a function, a method, and a class.
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

# What the report exits with when it could not be made, so the recipe that runs it stops.
_FAILED = 1


def call_sites(name: str, files: Iterable[Path]) -> list[str]:
    """Return where the files call the name, at the line each call starts on, as the source makes it."""
    return [f"{path}:{call.lineno}: {ast.unparse(call)}" for path in files for call in _calls(name, path)]


def _calls(name: str, path: Path) -> list[ast.Call]:
    """Return the calls the file makes to the name."""
    return [node for node in ast.walk(_parsed(path)) if isinstance(node, ast.Call) and _calls_name(node, name)]


def _calls_name(call: ast.Call, name: str) -> bool:
    """Return whether the call names the name, looking past any object the call is made on.

    A call is written bare, on an object, or on a chain of them, and the name is the last part in each case.
    """
    return ast.unparse(call.func).rsplit(".", 1)[-1] == name


def _defined(name: str, files: Iterable[Path]) -> bool:
    """Return whether the files define a callable of the name: a function, a method, or a class."""
    nodes = (node for path in files for node in ast.walk(_parsed(path)))
    return any(isinstance(node, _DEFINITIONS) and node.name == name for node in nodes)


def _parsed(path: Path) -> ast.Module:
    """Return the file's syntax tree, parsed under its path, so a syntax error names the file it sits in."""
    return ast.parse(path.read_text(), filename=str(path))


def _python_files() -> list[Path]:
    """Return every Python file the repository holds, so a call site outside the package is counted too."""
    return [path for root in _ROOTS for path in Path(root).rglob("*.py")]


def _report(message: str) -> None:
    """Write to standard error what stopped the report."""
    sys.stderr.write(f"Error: {message}\n")


def main() -> int:
    """Report where the name given on the command line is called, and how many places call it.

    A name nothing defines fails the run, so no call sites means the name exists and nothing calls it.
    """
    name, files = sys.argv[1], _python_files()
    try:
        sites = call_sites(name, files)
        undefined = not sites and not _defined(name, files)
    except SyntaxError as reason:
        _report(f"{reason.filename} could not be parsed: {reason.msg}")
        return _FAILED
    if undefined:
        _report(f"nothing defines {name}; check the spelling")
        return _FAILED
    sys.stdout.write("\n".join([*sites, f"call sites: {len(sites)}"]) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
