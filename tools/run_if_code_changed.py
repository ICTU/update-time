"""Run a command where Python code changed since it last passed: docstrings, comments, and formatting do not count.

A coverage pragma is a comment that changes what the tests report, so a pragma does count.

The tool compares the code with the fingerprint in `build/`, and runs the command where the two differ. A command that
passes leaves the code's fingerprint there, so the next run skips the command until the code changes again.

Usage: `uv run python -m tools.run_if_code_changed --skipped-status STATUS -- COMMAND`, which exits with STATUS
where the Python code did not change, and with the command's exit status where it did.
"""

import argparse
import ast
import hashlib
import io
import json
import subprocess  # nosec
import sys
import tokenize
from pathlib import Path

# The git command that lists the Python files the repository holds, tracked or new, leaving out the ones it ignores.
# A NUL ends each entry.
_PYTHON_FILES = ("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "*.py")

# Where a passing command leaves the fingerprint of the code it ran on, per file.
_RECORD = "build/py-code-fingerprints.json"

# The definitions that can open with a docstring.
_DOCUMENTED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def main() -> int:
    """Run the command where Python code changed, and return its exit status, or the skip status where none changed."""
    arguments = _arguments()
    fingerprints = _fingerprints()
    if fingerprints is not None and fingerprints == _recorded():
        return arguments.skipped_status
    status = subprocess.run(arguments.command, check=False).returncode  # noqa: S603 # nosec
    if not status and fingerprints is not None:
        _record(fingerprints)
    return status


def _arguments() -> argparse.Namespace:
    """Return the arguments the command line gives: the skip status, and the command to run."""
    parser = argparse.ArgumentParser(description="Run the command where Python code changed since it last passed.")
    parser.add_argument(
        "--skipped-status", type=int, required=True, help="the exit status to give where the code did not change"
    )
    parser.add_argument("command", nargs="+", help="the command to run, after --")
    return parser.parse_args()


def _fingerprints() -> dict[str, str] | None:
    """Return the fingerprint of the Python code per file, or None where it cannot be taken."""
    try:
        result = subprocess.run(_PYTHON_FILES, check=False, capture_output=True, text=True)  # noqa: S603 # nosec
    except OSError as error:
        _report_changed(f"git ls-files failed: {error}")
        return None
    if result.returncode:
        _report_changed(f"git ls-files failed: {result.stderr.strip()}")
        return None
    paths = [path for path in result.stdout.split("\0") if path]
    try:
        return {path: _fingerprint(path, source) for path in paths if (source := _source(path)) is not None}
    except SyntaxError as error:
        _report_changed(f"{error.filename} does not parse: {error}")
        return None


def _source(path: str) -> str | None:
    """Return the file's text, or None where git lists a file the working tree no longer holds."""
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return None


def _recorded() -> dict[str, str] | None:
    """Return the fingerprint the last passing command left, or None where there is none or it does not parse."""
    try:
        return json.loads(Path(_RECORD).read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as error:
        _report_changed(f"{_RECORD} does not parse: {error}")
        return None


def _record(fingerprints: dict[str, str]) -> None:
    """Write the fingerprint for the next run to compare the code with."""
    record = Path(_RECORD)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(fingerprints, indent=2))


def _fingerprint(path: str, source: str) -> str:
    """Return a hash of the source's syntax tree without its docstrings, and of its coverage pragmas."""
    tree = ast.parse(source, filename=path)
    for node in ast.walk(tree):
        if isinstance(node, _DOCUMENTED) and ast.get_docstring(node, clean=False) is not None:
            node.body = node.body[1:] or [ast.Pass()]
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    pragmas = [token.string for token in tokens if token.type == tokenize.COMMENT and "pragma" in token.string]
    return hashlib.sha256(repr((ast.dump(tree), pragmas)).encode()).hexdigest()


def _report_changed(reason: str) -> None:
    """Write why the code is reported as changed without a comparison."""
    sys.stderr.write(f"Reporting Python code as changed, since {reason}\n")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
