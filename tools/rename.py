"""Rename a name and every reference to it in the files named, and fail when a file is left holding the old one."""

import ast
import re
import subprocess  # nosec
import sys
from fnmatch import fnmatch
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import libcst
from libcst.codemod import CodemodContext
from libcst.codemod.commands.rename import RenameCommand
from libcst.helpers import get_full_name_for_node
from libcst.metadata import QualifiedNameProvider

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

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
    return [f"{path}:{line}" for path in files for line in _mentioned_lines(name, _prose(path))]


def _prose(path: Path) -> str:
    """Return the file's text, or none where git lists a file the working tree no longer holds."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def _mentioned_lines(name: str, text: str) -> list[int]:
    """Return the lines of the text that mention the name in backticks, qualified or bare."""
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if any(mention.rsplit(".", 1)[-1] == name for mention in _MENTION.findall(line))
    ]


def _prose_files() -> list[Path]:
    """Return every version-controlled file whose prose can mention a name."""
    command = ["git", "ls-files", "-z", *_PROSE_ROOTS]
    listed = subprocess.run(command, check=False, capture_output=True, text=True).stdout  # noqa: S603 # nosec
    tracked = (Path(entry) for entry in listed.split("\0") if entry)
    return [path for path in tracked if any(fnmatch(path.name, pattern) for pattern in _PROSE_FILES)]


def _sources(paths: list[str]) -> dict[str, str]:
    """Return what each path holds, read once so the same text is renamed and searched."""
    return {path: Path(path).read_text() for path in paths}


class _InPlaceRenameCommand(RenameCommand):
    """Rename a name as `RenameCommand` does, but in place where a line imports it alone or its module reaches it.

    `RenameCommand` deletes such a line and imports the new name at the top of the module instead, which moves an
    import out of the `if TYPE_CHECKING:` block it sat in.
    """

    def leave_ImportFrom(  # noqa: N802
        self, original_node: libcst.ImportFrom, updated_node: libcst.ImportFrom
    ) -> libcst.Import | libcst.ImportFrom:
        """Return the import with the old name renamed in place when it imports that name alone."""
        names = updated_node.names
        if updated_node.module is None or isinstance(names, libcst.ImportStar) or len(names) != 1 or names[0].asname:
            return super().leave_ImportFrom(original_node, updated_node)
        imported = f"{get_full_name_for_node(updated_node.module)}.{get_full_name_for_node(names[0].name)}"
        if imported != self.old_name:
            return super().leave_ImportFrom(original_node, updated_node)
        self.bypass_import = True  # The import stands renamed, so `leave_Module` does not import the new name again
        new_name = libcst.Name(self.new_name.rpartition(".")[-1])
        return updated_node.with_changes(names=[names[0].with_changes(name=new_name)])

    def leave_Attribute(  # noqa: N802
        self, original_node: libcst.Attribute, updated_node: libcst.Attribute
    ) -> libcst.Name | libcst.Attribute:
        """Return the attribute with the old name renamed where the code reads it through its module's name.

        `RenameCommand` imports the new name directly instead, which a local of that name can then shadow. A node
        without a qualified name sits inside an import statement, which `RenameCommand` renames as it does.
        """
        qualified = self.get_metadata(QualifiedNameProvider, original_node, set())
        if qualified and QualifiedNameProvider.has_name(self, original_node, self.old_name):
            self.bypass_import = True  # The module's name still reaches the new name, so `leave_Module` imports nothing
            return updated_node.with_changes(attr=libcst.Name(self.new_name.rpartition(".")[-1]))
        return super().leave_Attribute(original_node, updated_node)


def _renamed(old: str, new: str, source: str) -> str:
    """Return the source with the old name renamed to the new one, resolved against the source's own scopes.

    A source that never uses the old name stays as it is. `RenameCommand` would import the new name into it when the
    source imports the old name's module.
    """
    renamed = _InPlaceRenameCommand(CodemodContext(), old, new).transform_module(libcst.parse_module(source)).code
    return renamed if surviving_occurrences(old.rpartition(".")[-1], source) else source


class _AttributeRenamer(libcst.CSTTransformer):
    """Rename each attribute, keyword argument, and method of one name, whatever it belongs to, and leave the rest."""

    def __init__(self, old: str, new: str) -> None:
        """Remember the name to rename, and what to rename it to."""
        super().__init__()
        self._old, self._new = old, new

    def leave_Attribute(self, original_node: libcst.Attribute, updated_node: libcst.Attribute) -> libcst.Attribute:  # noqa: N802
        """Return the attribute renamed when it carries the old name, or as it is otherwise."""
        return updated_node.with_changes(attr=self._renamed(original_node.attr))

    def leave_Arg(self, original_node: libcst.Arg, updated_node: libcst.Arg) -> libcst.Arg:  # noqa: N802
        """Return the argument renamed when its keyword is the old name, or as it is otherwise."""
        if original_node.keyword is None:
            return updated_node
        return updated_node.with_changes(keyword=self._renamed(original_node.keyword))

    def leave_ClassDef(self, original_node: libcst.ClassDef, updated_node: libcst.ClassDef) -> libcst.ClassDef:  # noqa: N802, ARG002
        """Return the class with its members of the old name renamed."""
        body = [self._renamed_member(statement) for statement in updated_node.body.body]
        return updated_node.with_changes(body=updated_node.body.with_changes(body=body))

    def _renamed_member(
        self, statement: libcst.BaseStatement | libcst.BaseSmallStatement
    ) -> libcst.BaseStatement | libcst.BaseSmallStatement:
        """Return the class body's statement with the method it defines or the attributes it sets renamed."""
        if isinstance(statement, libcst.FunctionDef):
            decorators = [self._renamed_decorator(decorator) for decorator in statement.decorators]
            return statement.with_changes(name=self._renamed(statement.name), decorators=decorators)
        if isinstance(statement, libcst.SimpleStatementLine):
            return statement.with_changes(body=[self._renamed_assignment(small) for small in statement.body])
        return statement

    def _renamed_decorator(self, decorator: libcst.Decorator) -> libcst.Decorator:
        """Return the decorator with the property it extends renamed, as `@old.setter` extends `old`."""
        if isinstance(expression := decorator.decorator, libcst.Attribute):
            return decorator.with_changes(decorator=expression.with_changes(value=self._renamed(expression.value)))
        return decorator

    def _renamed_assignment(self, statement: libcst.BaseSmallStatement) -> libcst.BaseSmallStatement:
        """Return the statement with the attribute it sets renamed, when it is an assignment."""
        if isinstance(statement, libcst.AnnAssign):
            return statement.with_changes(target=self._renamed(statement.target))
        if isinstance(statement, libcst.Assign):
            targets = [target.with_changes(target=self._renamed(target.target)) for target in statement.targets]
            return statement.with_changes(targets=targets)
        return statement

    def _renamed(self, node: libcst.BaseExpression) -> libcst.BaseExpression:
        """Return the new name when the node is the old name, or the node as it is otherwise."""
        return libcst.Name(self._new) if isinstance(node, libcst.Name) and node.value == self._old else node


def _renamed_attributes(old: str, new: str, _path: str, source: str) -> str:
    """Return the source with each attribute, keyword argument, and method of the old name renamed to the new one."""
    bare_old, bare_new = old.rpartition(".")[-1], new.rpartition(".")[-1]
    return libcst.parse_module(source).visit(_AttributeRenamer(bare_old, bare_new)).code


def _renamed_module_level(old: str, new: str, path: str, source: str) -> str:
    """Return the source with the module-level name renamed, spelled the way the file at the path reaches it."""
    return _renamed(*_names_for(path, old, new), source)


def _names_for(path: str, old: str, new: str) -> tuple[str, str]:
    """Return the pair of names to rename the file at the path with, both spelled the way it reaches them.

    LibCST reads the new name the way it reads the old, taking the module to import from out of it, so the two are
    spelled alike: a new name given bare beside a qualified old one leaves that module empty, which LibCST fails to
    parse.
    """
    bare_new = new.rpartition(".")[-1]
    if (inside := _inside_module(path, old)) is not None:
        return inside, bare_new
    module = old.rpartition(".")[0]
    return old, f"{module}.{bare_new}" if module else bare_new


def _names_its_module(old: str, paths: Iterable[str]) -> bool:
    """Return whether one of the files is a module the old name names, as a bare name always does."""
    return "." not in old or any(_inside_module(path, old) is not None for path in paths)


def _names_a_member(old: str, paths: Iterable[str]) -> bool:
    """Return whether the old name names a member of a class, in a module that one of the files is."""
    return any((name := _inside_module(path, old)) is not None and "." in name for path in paths)


def _inside_module(path: str, old: str) -> str | None:
    """Return the name inside the module the file at the path is, such as `Class.method`, or None for another file."""
    parts = old.split(".")
    for end in range(len(parts) - 1, 0, -1):
        if _is_module(path, ".".join(parts[:end])):
            return ".".join(parts[end:])
    return None


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


def _renamed_sources(sources: dict[str, str], rename: Callable[[str, str], str]) -> dict[str, str] | None:
    """Return each source renamed, or None where one of them could not be, which is reported.

    A rename is turned down for a source LibCST cannot parse, and for an old name holding a colon.
    """
    renamed = {}
    for path, source in sources.items():
        try:
            renamed[path] = rename(path, source)
        except (libcst.ParserSyntaxError, ValueError) as reason:
            # Rendering a parse error's context can itself fail, so the report gives the message alone. A parser
            # error's message names the position. A tokenizer error's message names none.
            described = reason.message if isinstance(reason, libcst.ParserSyntaxError) else str(reason)
            _report(f"{path} could not be renamed: {described}")
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
    if not _names_its_module(old, paths):
        _report(f"none of the files given is a module that {old} names; add the file that defines it")
        return _FAILED
    sources = _sources(paths)
    member = _names_a_member(old, paths)
    # A member is renamed by its bare name in every source, a module-level name as each file reaches it.
    rename = partial(_renamed_attributes if member else _renamed_module_level, old, new)
    if (renamed := _renamed_sources(sources, rename)) is None:
        return _FAILED
    changed = {path: source for path, source in renamed.items() if source != sources[path]}
    if not changed:
        _report(f"nothing was renamed; check the spelling of {old}")
        return _FAILED
    name = old.rsplit(".", 1)[-1]
    left = (
        []
        if member
        else [f"{path}:{line}" for path, source in renamed.items() for line in surviving_occurrences(name, source)]
    )
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
