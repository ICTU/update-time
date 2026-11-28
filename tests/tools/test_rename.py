"""Unit tests for the check that a rename left no occurrence of the old name behind."""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Final
from unittest.mock import Mock, call, patch

import libcst
from tools.rename import (
    _PROSE_ROOTS,
    _AttributeRenamer,
    _InPlaceRenameCommand,
    _is_module,
    _prose,
    _prose_files,
    _ReferenceFinder,
    _relative_names,
    _renamed,
    _renamed_module_level,
    _renamed_sources,
    _stale_mentions,
    _surviving_occurrences,
    _survivors_message,
    main,
)

from mutation_time.mutation import kills

from tests.file_mocks import mock_path

# A source the rename reaches, since it imports the name from the module the old name qualifies it with, and one
# it leaves alone, since a definition is resolved against the module it sits in rather than against that one.
_IMPORTS_AND_CALLS: Final[str] = "from m import old\n\nold()\n"
_DEFINES: Final[str] = "def old():\n    pass\n"


class SurvivingOccurrencesTest(unittest.TestCase):
    """Unit tests for reading a name back out of a module's source."""

    def test_every_kind_of_identifier(self):
        """Test that the name is found wherever it is an identifier, whichever syntax puts it there."""
        cases = {
            "a reference": "old()\n",
            "an attribute": "module.old\n",
            "an import": "from module import old\n",
            "a dotted import": "import package.old\n",
            "an alias": "from module import name as old\n",
            "a function": "def old():\n    pass\n",
            "an async function": "async def old():\n    pass\n",
            "a class": "class old:\n    pass\n",
        }
        for case, source in cases.items():
            with self.subTest(case=case):
                self.assertEqual(_surviving_occurrences("old", source), [1])

    def test_the_name_as_text(self):
        """Test that a docstring mentioning the name is no occurrence, where a grep would report one."""
        self.assertEqual(_surviving_occurrences("old", '"""Hands the line to `old`."""\nnew()\n'), [])

    def test_a_line_holding_the_name_twice(self):
        """Test that a line is reported once however often the name occurs on it."""
        self.assertEqual(_surviving_occurrences("old", "old(old())\nnew()\nold()\n"), [1, 3])

    def test_a_name_the_source_does_not_hold(self):
        """Test that a source the rename reached leaves nothing to report."""
        self.assertEqual(_surviving_occurrences("old", "new()\nother.new\n"), [])


class StaleMentionsTest(unittest.TestCase):
    """Unit tests for finding the prose that still mentions a renamed name."""

    def test_a_name_in_backticks(self):
        """Test that a mention is found wherever backticks quote the name, bare or qualified."""
        self.assertEqual(self.mentions("old", "Hands it to `old`.\n"), ["f.py:1"])
        self.assertEqual(self.mentions("old", "See `module.old` for the rest.\n"), ["f.py:1"])

    def mentions(self, name: str, *texts: str) -> list[str]:
        """Return where files holding the given texts mention the name."""
        return _stale_mentions(name, [mock_path(text) for text in texts])

    def test_a_name_without_backticks(self):
        """Test that prose naming the name without backticks is no mention, since it may be an English word."""
        self.assertEqual(self.mentions("old", "The old version is kept.\n"), [])

    def test_a_line_mentioning_the_name_twice(self):
        """Test that a line is reported once however often it mentions the name."""
        self.assertEqual(self.mentions("old", "`old` calls `old`.\n"), ["f.py:1"])

    def test_every_file_that_mentions_it(self):
        """Test that each file is searched, not only the first."""
        self.assertEqual(len(self.mentions("old", "`old`\n", "nothing\n", "line\n`old`\n")), 2)

    @kills.replace(
        _prose,
        "except FileNotFoundError:",
        "except ValueError:",
        "a file removed but not yet committed ends a rename that landed with a traceback",
        raises="FileNotFoundError: [Errno 2] No such file or directory: 'gone.py'",
    )
    def test_a_file_the_working_tree_no_longer_holds(self):
        """Test that a file git lists but the working tree no longer holds is skipped, and the others searched."""
        gone = mock_path("", name="gone.py")
        gone.read_text.side_effect = FileNotFoundError(2, "No such file or directory", "gone.py")
        self.assertEqual(_stale_mentions("old", [gone, mock_path("`old`\n")]), ["f.py:1"])


class ProseFilesTest(unittest.TestCase):
    """Unit tests for the files searched for prose mentioning a name."""

    def test_only_the_tracked_prose_files_are_searched(self):
        """Test that git lists the files, so an untracked virtualenv below a root is searched not at all."""
        listed = "src/update_time/oci.py\0docs/README.md.in\0src/update_time/oci.pyc\0"
        with patch("tools.rename.subprocess.run", Mock(return_value=Mock(stdout=listed))) as run:
            found = _prose_files()
        self.assertEqual(found, [Path("src/update_time/oci.py"), Path("docs/README.md.in")])
        self.assertEqual(run.call_args.args[0], ["git", "ls-files", "-z", *_PROSE_ROOTS])


class RenamedTest(unittest.TestCase):
    """Unit tests for renaming a name in one source."""

    def test_a_source_reaching_the_module_but_not_the_name_is_left_as_it_is(self):
        """Test that a source importing the name's module, but never using the name, does not gain an import of it."""
        source = "from pkg import m\n\nm.other()\n"
        self.assertEqual(_renamed("pkg.m.old", "pkg.m.new", source), source)

    def test_a_reference_through_the_module_keeps_naming_the_module(self):
        """Test that a reference written through the module's name is renamed there, rather than imported directly."""
        for case, (imports, name) in {
            "the module": ("from pkg import m", "m"),
            "an alias": ("import pkg.m as alias", "alias"),
        }.items():
            with self.subTest(case=case):
                source = f"{imports}\n\n{name}.old()\n"
                self.assertEqual(_renamed("pkg.m.old", "pkg.m.new", source), source.replace(".old()", ".new()"))

    @kills.replace(
        _InPlaceRenameCommand.leave_ImportFrom,
        "if not any(matches):",
        "if matches != [True]:",
        "an import of several names loses its trailing comma, since LibCST rebuilds it",
    )
    def test_an_import_of_several_names_keeps_its_layout(self):
        """Test that an import of the name among others is renamed in place, its lines and trailing comma kept."""
        source = "from m import (\n    alpha,\n    {0},\n)\n\nalpha()\n{0}()\n"
        self.assertEqual(_renamed("m.old", "m.new", source.format("old")), source.format("new"))

    @kills.delete(
        _InPlaceRenameCommand.leave_ImportFrom,
        " or isinstance(names, libcst.ImportStar)",
        "a star import of the name's module ends the rename with a traceback",
        raises="TypeError: 'ImportStar' object is not iterable",
    )
    def test_a_star_import_of_the_module_is_left_as_it_is(self):
        """Test that a source importing every name of the name's module, naming none of them, is left as it is."""
        source = "from m import *\n\nold()\n"
        self.assertEqual(_renamed("m.old", "m.new", source), source)


class RelativeNamesTest(unittest.TestCase):
    """Unit tests for spelling a name as a relative import from a file reaches it."""

    @kills.delete(
        _relative_names,
        " * level",
        "a relative import from a subpackage is spelled as one from the package itself",
        case="parent package",
    )
    @kills.force_true(
        _relative_names,
        "end < len(parts)",
        "the relative spelling of a name in the package's `__init__.py` gets a dot too many",
        case="the package itself",
    )
    @kills.replace(
        _relative_names,
        '_is_module(f"{package.as_posix()}/__init__.py", ".".join(parts[:end]))',
        'package.as_posix().replace("/", ".") == ".".join(parts[:end])',
        "a package under a root such as `src` is not recognised as the package the name's module sits in",
        case="under a root",
    )
    @kills.replace(
        _relative_names,
        "return None",
        "return old, new",
        "a file outside the name's packages is given the qualified names as its relative spelling",
        case="another package",
    )
    def test_the_relative_spelling_climbs_to_the_nearest_package_holding_the_module(self):
        """Test that the names climb to the nearest package holding the old name's module."""
        for case, (path, old, expected) in {
            "parent package": ("pkg/sub/user.py", "pkg.m.old", ("..m.old", "..m.new")),
            "the package itself": ("pkg/user.py", "pkg.old", (".old", ".new")),
            "under a root": ("src/pkg/user.py", "pkg.m.old", (".m.old", ".m.new")),
            "another package": ("other/user.py", "pkg.m.old", None),
        }.items():
            with self.subTest(case=case):
                self.assertEqual(_relative_names(path, old, "new"), expected)


class MainTest(unittest.TestCase):
    """Unit tests for renaming over the files named and reporting a rename that did not land."""

    def test_a_rename_that_reached_every_file(self):
        """Test that files the codemod rewrote, none of them left holding the name, pass without a report."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, "from m import old as alias\n\nalias()\n"), 0)
        self.assertEqual(self.reported.getvalue(), "")
        self.assertEqual(self.paths["file1.py"].write_text.call_args_list, [call("from m import new\n\nnew()\n")])

    def rename(
        self,
        *sources: str,
        old: str = "m.old",
        new: str = "m.new",
        module: str = _DEFINES,
        module_file: str = "m.py",
        prose: str = "",
    ) -> int:
        """Rename over files holding the given sources, and return the exit code."""
        self.reported, self.noted = io.StringIO(), io.StringIO()
        dotted = module_file.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
        package = dotted.rpartition(".")[0]
        folder = f"{package.replace('.', '/')}/" if package else ""
        self.paths = {f"{folder}file{index}.py": mock_path(source) for index, source in enumerate(sources)}
        if old.startswith(f"{dotted}."):
            self.paths[module_file] = mock_path(module)
        mentioning = Mock(read_text=Mock(return_value=prose), __str__=lambda _: "prose.py")
        with (
            patch.object(sys, "argv", ["rename.py", old, new, *self.paths]),
            patch("tools.rename.Path", Mock(side_effect=lambda path: self.paths[path])),
            patch("tools.rename._prose_files", Mock(return_value=[mentioning])),
            redirect_stdout(self.noted),
            redirect_stderr(self.reported),
        ):
            return main()

    def test_a_qualified_name_none_of_the_files_defines(self):
        """Test that a qualified name whose module none of the files is fails before anything is renamed."""
        cases = {
            "a module-level name": ("from absent import old\n\nold()\n", "absent.old"),
            "a member": ("def show(shape):\n    return shape.area\n", "shapes.Shape.area"),
        }
        for case, (uses, old) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(self.rename(uses, old=old, new="new"), 1)
                self.assertIn("add the file that defines it", self.reported.getvalue())
                self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [])

    def test_a_new_name_given_bare(self):
        """Test that a new name given bare renames a qualified old one, as the usage spells a rename."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, old="m.old", new="new"), 0)
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call("from m import new\n\nnew()\n")])

    @kills.delete(
        _InPlaceRenameCommand.leave_ImportFrom,
        '"." * len(updated_node.relative) + ',
        "a relative import of the name loses its dots, so it imports from a module of that name at the top",
        case="the name",
    )
    @kills.delete(
        _renamed_module_level,
        ", context",
        "the relative import cannot be resolved, so the rename of the name it imports stops",
        case="the name",
    )
    @kills.force_none(
        _renamed_module_level,
        "_relative_names(path, old, new)",
        "a name reached by a relative import is left behind, so the rename stops",
        case="its module",
    )
    @kills.prepend(
        _InPlaceRenameCommand.leave_ImportFrom,
        'f"{module}.',
        "not alias.asname and ",
        "an aliased name is left to `RenameCommand`, which imports the new name from the module without its dots",
        case="an alias",
    )
    def test_a_name_a_relative_import_reaches_is_renamed(self):
        """Test that a name is renamed where a sibling module imports it, aliased or not, or its module, relatively."""
        for case, (uses, renamed) in {
            "the name": ("from .m import old\n\nold()\n", "from .m import new\n\nnew()\n"),
            "its module": ("from . import m\n\nm.old()\n", "from . import m\n\nm.new()\n"),
            "an alias": ("from .m import old as alias\n\nalias()\n", "from .m import new\n\nnew()\n"),
        }.items():
            with self.subTest(case=case):
                self.assertEqual(self.rename(uses, old="pkg.m.old", new="new", module_file="pkg/m.py"), 0)
                self.assertEqual(self.paths["pkg/file0.py"].write_text.call_args_list, [call(renamed)])

    def test_a_name_imported_alone_for_type_checking_is_renamed_where_it_is_imported(self):
        """Test that the import of a name a `TYPE_CHECKING` block imports alone stays in that block, renamed."""
        uses = (
            "from typing import TYPE_CHECKING\n\nfrom m import other\n\nif TYPE_CHECKING:\n    from m import {0}\n\n\n"
            "def use(value: {0}) -> None:\n    other(value)\n"
        )
        self.assertEqual(self.rename(uses.format("old")), 0)
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call(uses.format("new"))])

    def test_the_module_the_qualified_name_names(self):
        """Test that the file the old name qualifies is renamed too, since the definition sits in it."""
        # The helper writes the first source to `file0.py`, which is the module the name `file0.old` qualifies.
        self.assertEqual(self.rename(_DEFINES, old="file0.old", new="new"), 0)
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call("def new():\n    pass\n")])

    @kills.delete(
        _is_module,
        '.removesuffix("/__init__")',
        "the rename reports that none of the files given defines a name from a package's `__init__.py`",
    )
    def test_a_name_the_init_of_a_package_defines_is_renamed_there(self):
        """Test that a method the `__init__.py` of a package defines is renamed there."""
        defines = "class Helper:\n    def {}(self):\n        pass\n"
        uses = "def use(helper):\n    return helper.old()\n"
        module_file = "m/__init__.py"
        self.assertEqual(
            self.rename(uses, old="m.Helper.old", module=defines.format("old"), module_file=module_file), 0
        )
        self.assertEqual(self.paths[module_file].write_text.call_args_list, [call(defines.format("new"))])

    @kills.replace(
        _ReferenceFinder._bound_locally,
        "return True",
        "return False",
        "a parameter or a local of the renamed name reads as the module-level name left behind",
    )
    @kills.replace(
        _ReferenceFinder._bound_locally,
        "scope = scope.parent",
        "break",
        "the rename stops where a nested function reads a local of the function around it",
    )
    @kills.replace(
        main,
        "for line in _surviving_references(name, source)]",
        "for line in _surviving_occurrences(name, source)]",
        "a rename stops wherever a file spells the renamed name, whatever that name refers to there",
    )
    def test_a_parameter_and_a_local_of_the_name_do_not_stop_the_rename(self):
        """Test that a module-level name is renamed where a function's parameter and local share its name."""
        defines = (
            "def {}():\n    pass\n\n\ndef use(old):\n    return old\n\n\n"
            "def count():\n    old = 1\n\n    def nested():\n        return old\n\n    return nested\n"
        )
        self.assertEqual(self.rename(defines.format("old"), old="file0.old", new="new"), 0)
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call(defines.format("new"))])

    @kills.force_true(
        _ReferenceFinder.visit_Name,
        "self._read_through_an_import(parent)",
        "a method of the renamed name on another object reads as the module-level name left behind",
        case="a parameter",
    )
    @kills.replace(
        _ReferenceFinder._read_through_an_import,
        "any(isinstance(assignment, ImportAssignment) for assignment in scope[root.value])",
        "bool(scope[root.value])",
        "a method of the renamed name on a parameter reads as the module-level name left behind",
        case="a parameter",
    )
    @kills.replace(
        _ReferenceFinder._read_through_an_import,
        "return False",
        "return True",
        "a method of the renamed name on what a call returns reads as the module-level name left behind",
        case="a call's result",
    )
    def test_a_method_of_the_name_on_another_object_does_not_stop_the_rename(self):
        """Test that a module-level name is renamed where a call on another object reads a method of that name."""
        for case, other in {"a parameter": "item", "a call's result": "item()"}.items():
            with self.subTest(case):
                uses = f"from m import {{0}}\n\n\ndef use(item):\n    {{0}}()\n    return {other}.old()\n"
                self.assertEqual(self.rename(uses.format("old")), 0)
                self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call(uses.format("new"))])

    @kills.force_false(
        _ReferenceFinder._read_through_an_import,
        "any(isinstance(assignment, ImportAssignment) for assignment in scope[root.value])",
        "a reference read through an imported module goes unnoticed, so a rename that missed it is written",
    )
    @kills.replace(
        _ReferenceFinder._read_through_an_import,
        "root = root.value",
        "break",
        "a reference read through an imported package goes unnoticed, so a rename that missed it is written",
    )
    def test_a_reference_through_a_package_the_rename_cannot_reach_stops_the_rename(self):
        """Test that a reference reading the old name through an imported package stops the rename."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, "import pkg\n\npkg.m.old()\n"), 1)
        self.assertIn("file1.py:3", self.reported.getvalue())

    def test_a_member_rename_renames_the_member_wherever_a_file_names_it(self):
        """Test that a member rename renames the member's definitions and uses, and leaves a function of its name."""
        helper = "class Helper:\n    def {0}(self):\n        return 1\n"
        point = "class Point:\n    {0}: int\n"
        shape = (
            "class Shape:\n    @property\n    def {0}(self):\n        return 1\n\n"
            "    @{0}.setter\n    def {0}(self, value):\n        pass\n"
        )
        two_classes = (
            "class Helper:\n    def {0}(self):\n        return 1\n\n\n"
            "class Other:\n    def {0}(self):\n        return 2\n\n\n"
            "def use(helper, other):\n    return helper.{0}() + other.{0}()\n"
        )
        subclass = (
            "class Sub(Helper):\n    def {0}(self):\n        return 2\n\n    def other(self):\n        return 3\n\n"
            "    class Meta:\n        pass\n"
        )
        cases = {
            "a method and its calls": (
                "file0.Helper.old",
                "class Helper:\n    def {0}(self):\n        return self.{0}()\n",
                "def use(helper):\n    return helper.{0}()\n",
            ),
            "a property and its setter's decorator": ("file0.Shape.old", shape),
            "another class's method in the defining file": ("file0.Helper.old", two_classes),
            "a call beside a function of that name in another file": (
                "file0.Helper.old",
                helper,
                "def old():\n    return 2\n\n\ndef use(helper):\n    return old() + helper.{0}() + helper.other()\n",
            ),
            "an override in another file": ("file0.Helper.old", helper, subclass),
            "keyword arguments in another file": (
                "file0.Point.old",
                point,
                "def use(point):\n    return Point(1, {0}=1, other=2), point.{0}\n",
            ),
            "an assigned class attribute of a subclass in another file": (
                "file0.Point.old",
                point,
                "class Origin(Point):\n    {0} = 0\n    other = 1\n",
            ),
            "an annotated class attribute of a subclass in another file": (
                "file0.Point.old",
                point,
                "class Origin(Point):\n    {0}: int = 0\n    other: int = 1\n",
            ),
        }
        for case, (old, *sources) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(self.rename(*[source.format("old") for source in sources], old=old, new="new"), 0)
                written = {path: mock.write_text.call_args_list for path, mock in self.paths.items()}
                self.assertEqual(
                    written, {f"file{i}.py": [call(source.format("new"))] for i, source in enumerate(sources)}
                )

    @kills.replace(
        _AttributeRenamer.visit_ImportFrom,
        "return False",
        "return True",
        "an attribute rename renames a module of that name that a `from` import names",
        case="a from import",
    )
    @kills.replace(
        _AttributeRenamer.visit_Import,
        "return False",
        "return True",
        "an attribute rename renames a module of that name that an import names",
        case="an import",
    )
    def test_an_attribute_rename_leaves_the_module_an_import_names_alone(self):
        """Test that an attribute rename leaves the module an import names as it is, though a part of it is the name."""
        defines = "class Point:\n    old: int\n"
        cases = {
            "a from import": "from pkg.old import Shape\n\n\ndef use(point):\n    return point.{}, Shape\n",
            "an import": "import pkg.old\n\n\ndef use(point):\n    return point.{}\n",
        }
        for case, uses in cases.items():
            with self.subTest(case=case):
                self.assertEqual(self.rename(defines, uses.format("old"), old="file0.Point.old", new="new"), 0)
                self.assertEqual(self.paths["file1.py"].write_text.call_args_list, [call(uses.format("new"))])

    def test_the_files_the_rename_changed_are_written(self):
        """Test that a file the rename changed is written, and one it left as it was is not."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, "print(1)\n"), 0)
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call("from m import new\n\nnew()\n")])
        self.assertEqual(self.paths["file1.py"].write_text.call_args_list, [])

    @kills.prepend(
        main,
        "return _FAILED\n    for path, source in changed.items():",
        "for path, source in changed.items():\n            Path(path).write_text(source)\n        ",
        "a rename that left the old name behind still writes the files it changed to disk",
    )
    def test_a_rename_that_left_the_name_behind_writes_nothing(self):
        """Test that a file left holding the name leaves every file unwritten, the ones the rename changed too."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, _DEFINES), 1)
        self.assertEqual([path.write_text.call_args_list for path in self.paths.values()], [[], [], []])

    @kills.append(
        _renamed_sources,
        '_report(f"{path} could not be renamed: {described}")\n',
        "            for written, renamed_source in renamed.items():\n"
        "                Path(written).write_text(renamed_source)\n",
        "a file the codemod cannot parse still writes the renames already made to the other files",
    )
    def test_a_file_the_codemod_cannot_parse(self):
        """Test that a source the codemod cannot parse is reported by name, and writes none of the files."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, "this is not python(\n"), 1)
        self.assertIn("file1.py", self.reported.getvalue())
        self.assertEqual([path.write_text.call_args_list for path in self.paths.values()], [[], [], []])

    def test_a_parse_error_that_cannot_render_its_context_is_reported_by_its_message(self):
        """Test that a LibCST error that cannot render its own context is reported by name and message."""
        unrenderable = libcst.ParserSyntaxError("parser error", lines=["\n"], raw_line=2, raw_column=0)
        with patch("tools.rename._renamed", Mock(side_effect=unrenderable)):
            self.assertEqual(self.rename(_IMPORTS_AND_CALLS), 1)
        self.assertIn("file0.py could not be renamed: parser error", self.reported.getvalue())
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [])

    def test_an_argument_libcst_rejects(self):
        """Test that an argument LibCST rejects is reported by name, rather than raised at the reader."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, old="m:old"), 1)
        self.assertIn("file0.py could not be renamed", self.reported.getvalue())

    def test_a_rename_that_changed_nothing(self):
        """Test that files the codemod left as they were are reported as a name it never found."""
        self.assertEqual(self.rename("print(1)\n", module="print(2)\n"), 1)
        self.assertIn("nothing was renamed", self.reported.getvalue())

    @kills.replace(
        _survivors_message,
        "', '.join(left)",
        "' '.join(left)",
        "the surviving occurrences are run together without the comma between them",
    )
    def test_a_bare_name_that_reached_the_definition_alone(self):
        """Test that a bare name renames the definition alone, so the references importing it survive."""
        self.assertEqual(self.rename(_DEFINES, _IMPORTS_AND_CALLS, old="old", new="new"), 1)
        self.assertIn("old survives at file1.py:1, file1.py:3", self.reported.getvalue())

    def test_the_files_a_failed_rename_would_have_written(self):
        """Test that a rename left holding the name names the files a run that lands writes, and none besides."""
        self.rename(_IMPORTS_AND_CALLS, _DEFINES)
        self.assertIn("No file was written; a rename that lands writes file0.py, m.py\n", self.reported.getvalue())

    def test_every_file_that_still_holds_the_name(self):
        """Test that the report names each surviving occurrence, rather than stopping at the first file."""
        self.rename(_IMPORTS_AND_CALLS, _DEFINES, _DEFINES)
        self.assertIn("file1.py:1", self.reported.getvalue())
        self.assertIn("file2.py:1", self.reported.getvalue())

    def test_prose_that_still_mentions_the_name(self):
        """Test that a rename that landed reports the prose mentioning the old name, without failing over it."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, prose="Hands it to `old`.\n"), 0)
        self.assertIn("prose.py:1", self.noted.getvalue())

    def test_prose_that_mentions_the_name_nowhere(self):
        """Test that a rename no prose mentions the old name after reports nothing."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS), 0)
        self.assertEqual(self.noted.getvalue(), "")
