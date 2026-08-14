"""Rename a name and every reference to it in the files named, and fail when a file is left holding the old one.

LibCST renames the references it resolves and reports success either way, so two rewrites it did not make look
like one that landed: a misspelled name reaches nothing, and a bare name reaches the definition alone when the
references live in another module. Both are read back off the files rather than out of the report — the first as
files that came back unchanged, the second as the old name surviving as an identifier, which leaves the
docstrings that mention it alone, unlike a grep.

Those docstrings are then reported, since a rename leaves the prose about a name as it found it, and the prose
mostly lives in files the rename was never given. They are reported rather than rewritten because the same word
is a parameter or a local elsewhere, where it means something else.

Usage: `uv run python tools/rename.py OLD NEW FILE ...`, with OLD qualified for a name defined in another module.
"""

import ast
import hashlib
import re
import subprocess  # nosec
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# What the rename exits with when it did not land, so the recipe that runs it stops.
_FAILED = 1

# A name as prose refers to one, in the backticks the docstrings and the documentation quote it in.
_MENTION = re.compile(r"`([\w.]+)`")

# Where the prose that can mention a renamed name lives, which is rarely the files the rename was given.
_PROSE_ROOTS = ("src", "tests", "tools", "docs", ".claude")
_PROSE_FILES = ("*.py", "*.md", "*.in")

# The codemod that does the renaming, and the flags that keep it from reformatting or drawing a progress bar.
_CODEMOD = ("-m", "libcst.tool", "codemod", "rename.RenameCommand")
_FLAGS = ("--no-format", "--hide-progress")


def surviving_occurrences(name: str, source: str) -> list[int]:
    """Return the lines of the source where the name is an identifier, each line once however often it occurs."""
    lines = (_occurrence_line(node, name) for node in ast.walk(ast.parse(source)))
    return sorted({line for line in lines if line is not None})


def _occurrence_line(node: ast.AST, name: str) -> int | None:
    """Return the line where the node uses the name as an identifier, or None where it does not use it.

    A reference and an attribute carry the name directly, an import carries it as the name imported or the name it
    is bound to, and a definition carries it as the name defined. Anywhere else it is text, which a rename leaves.
    """
    if isinstance(node, ast.Name):
        return node.lineno if node.id == name else None
    if isinstance(node, ast.Attribute):
        return node.lineno if node.attr == name else None
    if isinstance(node, ast.alias):
        return node.lineno if name in (node.name.rsplit(".", 1)[-1], node.asname) else None
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return node.lineno if node.name == name else None
    return None


def stale_mentions(name: str, files: Iterable[Path]) -> list[str]:
    """Return where the files mention the name in backticks, which a rename leaves as it found them.

    A rename resolves the name against each module's scopes, so prose about it is left alone — rightly, since the
    same word is a parameter or a local elsewhere and means something else there. The mentions are reported rather
    than rewritten, for a reader who can tell the two apart.
    """
    return [f"{path}:{line}" for path in files for line in _mentioned_lines(name, path.read_text())]


def _mentioned_lines(name: str, text: str) -> list[int]:
    """Return the lines of the text that mention the name in backticks, qualified or bare."""
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if any(mention.rsplit(".", 1)[-1] == name for mention in _MENTION.findall(line))
    ]


def _prose_files() -> list[Path]:
    """Return every file whose prose can mention a name, which is far more than the rename was given."""
    return [path for root in _PROSE_ROOTS for pattern in _PROSE_FILES for path in Path(root).rglob(pattern)]


def _sources(paths: list[str]) -> dict[str, str]:
    """Return what each path holds, read once so the same text is checksummed and searched."""
    return {path: Path(path).read_text() for path in paths}


def _checksums(sources: dict[str, str]) -> list[str]:
    """Return a checksum per source, so a file the codemod rewrote can be told from one it left as it was."""
    return [hashlib.sha256(source.encode()).hexdigest() for source in sources.values()]


def main() -> int:
    """Rename the name over the files named on the command line, and report a rename that did not land."""
    old, new, *paths = sys.argv[1:]
    before = _checksums(_sources(paths))
    command = [sys.executable, *_CODEMOD, f"--old_name={old}", f"--new_name={new}", *_FLAGS, *paths]
    if (codemod := subprocess.run(command, check=False).returncode) != 0:  # noqa: S603 # nosec
        return codemod
    sources = _sources(paths)
    if _checksums(sources) == before:
        sys.stderr.write(f"Error: nothing was renamed; check the spelling of {old}\n")
        return _FAILED
    name = old.rsplit(".", 1)[-1]
    left = [f"{path}:{line}" for path, source in sources.items() for line in surviving_occurrences(name, source)]
    if left:
        sys.stderr.write(
            f"Error: {name} survives at {', '.join(left)}; name every file that uses it, and use the fully "
            "qualified name for a name defined in another module\n"
        )
        return _FAILED
    if stale := stale_mentions(name, _prose_files()):
        sys.stdout.write(f"Note: prose still mentions {name} at {', '.join(stale)}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
