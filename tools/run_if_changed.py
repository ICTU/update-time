"""Run a command where its inputs changed since it last passed.

In a Python input, docstrings, comments, and formatting do not count. A coverage pragma is a comment that changes what
the tests report, so a pragma does count. With `--whole-files`, every edit to a Python input counts, as it does for a
linter, which reads the comments and docstrings too.

The tool compares the inputs with their fingerprints in `build/`, and runs the command where the two differ. A command
that passes leaves the fingerprints there, so the next run skips the command until an input changes again.

Usage: `uv run --no-project python -m tools.run_if_changed --skipped-status STATUS --record NAME --inputs PATHSPEC ...
[--pass-changed PATHSPEC ...] [--whole-files] -- COMMAND`, which exits with STATUS where every input is unchanged, and
with the command's exit status where one changed. A command exiting with STATUS makes the tool exit with 1, so a failure
never reads as a skip. The inputs are the files the pathspecs match. A pathspec naming one file counts that file even
where git ignores it. The files `--pass-changed` names count as inputs too, and the command gets the ones that changed
since it last passed after its own arguments. A change to a file `--inputs` names hands the command every one of them.
"""

import argparse
import ast
import hashlib
import io
import json
import re
import subprocess  # nosec
import sys
import tokenize
from pathlib import Path

# The git command that lists the files the repository holds, tracked or new, leaving out the ones it ignores. The
# pathspecs naming the inputs go after it. A NUL ends each entry.
_LIST_FILES = ("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--")

# The characters that make a pathspec a pattern rather than the path of one file.
_GLOB = re.compile(r"[*?\[]")

# Where a passing command leaves the fingerprint of its inputs, per file, under the name of its record.
_RECORD = "build/{}-fingerprints.json"

# The definitions that can open with a docstring.
_DOCUMENTED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def main() -> int:
    """Run the command where an input changed, and return its exit status, or the skip status where none changed."""
    arguments = _arguments()
    record = _RECORD.format(arguments.record)
    candidates, inputs, fingerprints = _fingerprints_of(arguments)
    recorded = _recorded(record)
    if fingerprints is not None and fingerprints == recorded:
        return arguments.skipped_status
    files = _files_to_pass(candidates, inputs, fingerprints or {}, recorded or {})
    # A deleted file left the command nothing to check.
    if arguments.pass_changed and not files and fingerprints is not None:
        _record(record, fingerprints)
        return arguments.skipped_status
    status = subprocess.run([*arguments.command, *files], check=False).returncode  # noqa: S603 # nosec
    if not status and fingerprints is not None:
        _record(record, fingerprints)
    return 1 if status == arguments.skipped_status else status


def _arguments() -> argparse.Namespace:
    """Return the arguments the command line gives."""
    parser = argparse.ArgumentParser(description="Run the command where an input changed since it last passed.")
    parser.add_argument(
        "--skipped-status", type=int, required=True, help="the exit status to give where every input is unchanged"
    )
    parser.add_argument(
        "--inputs", nargs="+", required=True, help="the pathspecs naming the files the command depends on"
    )
    parser.add_argument("--record", required=True, help="the name to keep the fingerprints of the inputs under")
    parser.add_argument(
        "--pass-changed",
        nargs="+",
        default=[],
        help="the pathspecs naming the files to pass to the command where they changed, on top of the inputs",
    )
    parser.add_argument(
        "--whole-files", action="store_true", help="fingerprint Python files whole, their comments and docstrings too"
    )
    parser.add_argument("command", nargs="+", help="the command to run, after --")
    return parser.parse_args()


def _fingerprints_of(arguments: argparse.Namespace) -> tuple[set[str], set[str], dict[str, str] | None]:
    """Return the files to pass, the inputs, and the fingerprint per file, or None where it cannot be taken."""
    inputs = _paths(arguments.inputs)
    candidates = _paths(arguments.pass_changed) if arguments.pass_changed else set()
    if inputs is None or candidates is None:
        return set(), set(), None
    return candidates, inputs, _fingerprints(inputs | candidates, whole_files=arguments.whole_files)


def _paths(pathspecs: list[str]) -> set[str] | None:
    """Return the files the pathspecs match, or None where git cannot list them."""
    try:
        result = subprocess.run((*_LIST_FILES, *pathspecs), check=False, capture_output=True, text=True)  # noqa: S603 # nosec
    except OSError as error:
        _report_changed(f"git ls-files failed: {error}")
        return None
    if result.returncode:
        _report_changed(f"git ls-files failed: {result.stderr.strip()}")
        return None
    listed = {path for path in result.stdout.split("\0") if path}
    # Git leaves out the files it ignores, so a file named by its path counts whether git lists it or not.
    return listed | {pathspec for pathspec in pathspecs if not _GLOB.search(pathspec)}


def _fingerprints(paths: set[str], *, whole_files: bool) -> dict[str, str] | None:
    """Return the fingerprint per file, or None where it cannot be taken."""
    try:
        return {
            path: _fingerprint(path, source, whole_file=whole_files)
            for path in sorted(paths)
            if (source := _source(path)) is not None
        }
    except SyntaxError as error:
        _report_changed(f"{error.filename} does not parse: {error}")
        return None


def _files_to_pass(
    candidates: set[str], inputs: set[str], fingerprints: dict[str, str], recorded: dict[str, str]
) -> list[str]:
    """Return the candidates the tree holds: all of them where an input changed, or else the ones that changed."""
    files_in_tree = [path for path in sorted(candidates) if path in fingerprints]
    # The record also names the inputs deleted since, which git may have stopped listing.
    if any(fingerprints.get(path) != recorded.get(path) for path in inputs | (recorded.keys() - candidates)):
        return files_in_tree
    return [path for path in files_in_tree if fingerprints[path] != recorded.get(path)]


def _source(path: str) -> str | None:
    """Return the file's text, or None where the working tree does not hold the file."""
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return None


def _recorded(record: str) -> dict[str, str] | None:
    """Return the fingerprint the last passing command left, or None where there is none or it does not parse."""
    try:
        return json.loads(Path(record).read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as error:
        _report_changed(f"{record} does not parse: {error}")
        return None


def _record(record: str, fingerprints: dict[str, str]) -> None:
    """Write the fingerprint for the next run to compare the inputs with."""
    path = Path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fingerprints, indent=2))


def _fingerprint(path: str, source: str, *, whole_file: bool) -> str:
    """Return a hash of the source's syntax tree without its docstrings, and of its coverage pragmas.

    A file other than a Python file is hashed whole, since every edit to it may change what the command does. So is a
    Python file the command reads whole, as a linter does.
    """
    if whole_file or not path.endswith(".py"):
        return hashlib.sha256(source.encode()).hexdigest()
    tree = ast.parse(source, filename=path)
    for node in ast.walk(tree):
        if isinstance(node, _DOCUMENTED) and ast.get_docstring(node, clean=False) is not None:
            node.body = node.body[1:] or [ast.Pass()]
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    pragmas = [token.string for token in tokens if token.type == tokenize.COMMENT and "pragma" in token.string]
    return hashlib.sha256(repr((ast.dump(tree), pragmas)).encode()).hexdigest()


def _report_changed(reason: str) -> None:
    """Write why the inputs are reported as changed without a comparison."""
    sys.stderr.write(f"Reporting the inputs as changed, since {reason}\n")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
