"""The package's source code as the architecture and visibility tests read it, and a project on disk to scan."""

import ast
import contextlib
import pathlib
import tempfile
from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator


@cache
def syntax_nodes(source: str) -> tuple[ast.AST, ...]:
    """Return every node of the source's syntax tree, walking each source once."""
    return tuple(ast.walk(syntax_tree(source)))


@cache
def syntax_tree(source: str) -> ast.Module:
    """Return the syntax tree of the source, parsing each source once."""
    return ast.parse(source)


def module_level_assignments(tree: ast.Module) -> Iterator[tuple[list[ast.expr], ast.expr | None]]:
    """Yield the targets and assigned value of each module-level assignment, annotated or not.

    An annotation without a value (`NAME: int`) assigns nothing, so its value is None.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            yield node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            yield [node.target], node.value


@contextlib.contextmanager
def project(files: dict[str, str]) -> Generator[str]:
    """Yield the path of a directory holding the given files, so a rule or a check has a project to scan."""
    with tempfile.TemporaryDirectory() as directory:
        for name, source in files.items():
            path = pathlib.Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
        yield directory
