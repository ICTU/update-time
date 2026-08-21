"""Rename a name and every reference to it in the files named, and fail when a file is left holding the old one.

LibCST resolves a name against the module a file sits in, so the names are spelled per file: bare for the file
holding the definition, qualified for the files importing it (see `_names_for`). It renames the references it
resolves and hands back the source either way, so two rewrites it did not make look like one that landed: a
misspelled name reaches nothing, and a file none of the spellings reach comes back as it was. Both are read off
the source it hands back — the first as every source coming back unchanged, the second as the old name surviving
as an identifier, which leaves the docstrings that mention it alone, unlike a grep.

The sources are written once every one of them has come back clean, so a rename that fails leaves the files as
it found them rather than a tree holding half a rename. A source LibCST cannot parse fails the run the same way,
before anything has been written.

Those docstrings are then reported, since a rename leaves the prose about a name as it found it, and the prose
mostly lives in files the rename was never given. They are reported rather than rewritten because the same word
is a parameter or a local elsewhere, where it means something else.

Usage: `uv run python tools/rename.py OLD NEW FILE ...`, with OLD qualified for a name defined in another module.
"""

import ast
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import libcst
from libcst.codemod import CodemodContext
from libcst.codemod.commands.rename import RenameCommand

if TYPE_CHECKING:
    from collections.abc import Iterable

# What the rename exits with when it did not land, so the recipe that runs it stops.
_FAILED = 1

# A name as prose refers to one, in the backticks the docstrings and the documentation quote it in.
_MENTION = re.compile(r"`([\w.]+)`")

# Where the prose that can mention a renamed name lives, which is rarely the files the rename was given.
_PROSE_ROOTS = ("src", "tests", "tools", "docs", ".claude")
_PROSE_FILES = ("*.py", "*.md", "*.in")


def surviving_occurrences(name: str, source: str) -> list[int]:
    """Return the lines of the source where the name is an identifier, each line once however often it occurs."""
    lines = (_occurrence_line(node, name) for node in ast.walk(ast.parse(source)))
    return sorted({line for line in lines if line is not None})


def _occurrence_line(node: ast.AST, name: str) -> int | None:
    """Return the line where the node uses the name as an identifier, or None where it does not use it.

    A reference, an attribute, and a definition carry the name directly, while an import carries it as the name
    imported or as the name it is bound to. Anywhere else it is text, which a rename leaves.
    """
    match node:
        case (
            ast.Name(id=carried)
            | ast.Attribute(attr=carried)
            | ast.FunctionDef(name=carried)
            | ast.AsyncFunctionDef(name=carried)
            | ast.ClassDef(name=carried)
        ):
            return node.lineno if carried == name else None
        case ast.alias(name=imported, asname=bound):
            return node.lineno if name in (imported.rsplit(".", 1)[-1], bound) else None
        case _:
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
    """Return what each path holds, read once so the same text is renamed and searched."""
    return {path: Path(path).read_text() for path in paths}


def _renamed(old: str, new: str, source: str) -> str:
    """Return the source with the old name renamed to the new one, resolved against the source's own scopes."""
    return RenameCommand(CodemodContext(), old, new).transform_module(libcst.parse_module(source)).code


def _names_for(path: str, old: str, new: str) -> tuple[str, str]:
    """Return the pair of names to rename the file at the path with, both spelled the way it reaches them.

    LibCST resolves a definition against the module it sits in rather than against the module a qualified name
    names, so the file holding the definition is reached by the bare name alone, and every other file by the
    qualified one. It reads the new name the same way it reads the old, taking the module to import from out of
    it, so the two are spelled alike: a new name given bare beside a qualified old one leaves that module empty,
    which LibCST fails to parse.
    """
    module, _, bare_old = old.rpartition(".")
    bare_new = new.rpartition(".")[-1]
    if _is_module(path, module):
        return bare_old, bare_new
    return old, f"{module}.{bare_new}" if module else bare_new


def _is_module(path: str, module: str) -> bool:
    """Return whether the file at the path is the module, so the definitions the module holds sit in it.

    The path is read as the dotted module it spells, which the module ends it with wherever the package root sits:
    `src/update_time/io/log.py` is the module `update_time.io.log`, and `tools/rename.py` is `tools.rename`.
    """
    dotted = path.removesuffix(".py").replace("/", ".")
    return bool(module) and (dotted == module or dotted.endswith(f".{module}"))


def _report(message: str) -> None:
    """Write the message to standard error, where what stops a rename is reported."""
    sys.stderr.write(f"Error: {message}\n")


def _renamed_sources(old: str, new: str, sources: dict[str, str]) -> dict[str, str] | None:
    """Return each source renamed, or None where one of them could not be, which is reported.

    A rename is turned down for a source LibCST cannot parse, and for an old name holding a colon.
    """
    renamed = {}
    for path, source in sources.items():
        try:
            renamed[path] = _renamed(*_names_for(path, old, new), source)
        except (libcst.ParserSyntaxError, ValueError) as reason:
            _report(f"{path} could not be renamed: {reason}")
            return None
    return renamed


def _survivors_message(name: str, left: list[str], changed: dict[str, str]) -> str:
    """Return what a rename a file is left holding the name after reports: where it survives, and what it left.

    Naming the files a rename that lands writes tells the reader what the run they are about to repeat touches.
    """
    return (
        f"{name} survives at {', '.join(left)}; name every file that uses it, and use the fully qualified name "
        f"for a name defined in another module\nNo file was written; a rename that lands writes {', '.join(changed)}"
    )


def main() -> int:
    """Rename the name over the files named on the command line, and report a rename that did not land."""
    old, new, *paths = sys.argv[1:]
    sources = _sources(paths)
    if (renamed := _renamed_sources(old, new, sources)) is None:
        return _FAILED
    changed = {path: source for path, source in renamed.items() if source != sources[path]}
    if not changed:
        _report(f"nothing was renamed; check the spelling of {old}")
        return _FAILED
    name = old.rsplit(".", 1)[-1]
    left = [f"{path}:{line}" for path, source in renamed.items() for line in surviving_occurrences(name, source)]
    if left:
        _report(_survivors_message(name, left, changed))
        return _FAILED
    for path, source in changed.items():
        Path(path).write_text(source)
    if stale := stale_mentions(name, _prose_files()):
        sys.stdout.write(f"Note: prose still mentions {name} at {', '.join(stale)}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
