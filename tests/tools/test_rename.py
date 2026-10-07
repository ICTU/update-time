"""Unit tests for the check that a rename left no occurrence of the old name behind."""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch

import libcst
from tools import rename as rename_module
from tools.rename import _PROSE_ROOTS, _prose_files, main, stale_mentions, surviving_occurrences

from tests.helpers import mock_path
from tests.mutation import Mutation, kills

# A source the rename reaches, since it imports the name from the module the old name qualifies it with, and one
# it leaves alone, since a definition is resolved against the module it sits in rather than against that one.
_IMPORTS_AND_CALLS = "from m import old\n\nold()\n"
_DEFINES = "def old():\n    pass\n"


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
                self.assertEqual(surviving_occurrences("old", source), [1])

    def test_the_name_as_text(self):
        """Test that a docstring mentioning the name is no occurrence, where a grep would report one."""
        self.assertEqual(surviving_occurrences("old", '"""Hands the line to `old`."""\nnew()\n'), [])

    def test_a_line_holding_the_name_twice(self):
        """Test that a line is reported once however often the name occurs on it."""
        self.assertEqual(surviving_occurrences("old", "old(old())\nnew()\nold()\n"), [1, 3])

    def test_a_name_the_source_does_not_hold(self):
        """Test that a source the rename reached leaves nothing to report."""
        self.assertEqual(surviving_occurrences("old", "new()\nother.new\n"), [])


class StaleMentionsTest(unittest.TestCase):
    """Unit tests for finding the prose that still mentions a renamed name."""

    def mentions(self, name: str, *texts: str) -> list[str]:
        """Return where files holding the given texts mention the name."""
        return stale_mentions(name, [mock_path(text) for text in texts])

    def test_a_name_in_backticks(self):
        """Test that a mention is found wherever backticks quote the name, bare or qualified."""
        self.assertEqual(self.mentions("old", "Hands it to `old`.\n"), ["f.py:1"])
        self.assertEqual(self.mentions("old", "See `module.old` for the rest.\n"), ["f.py:1"])

    def test_a_name_without_backticks(self):
        """Test that prose naming the name without backticks is no mention, since it may be an English word."""
        self.assertEqual(self.mentions("old", "The old version is kept.\n"), [])

    def test_a_line_mentioning_the_name_twice(self):
        """Test that a line is reported once however often it mentions the name."""
        self.assertEqual(self.mentions("old", "`old` calls `old`.\n"), ["f.py:1"])

    def test_every_file_that_mentions_it(self):
        """Test that each file is searched, not only the first."""
        self.assertEqual(len(self.mentions("old", "`old`\n", "nothing\n", "line\n`old`\n")), 2)


class ProseFilesTest(unittest.TestCase):
    """Unit tests for the files searched for prose mentioning a name."""

    def test_only_the_tracked_prose_files_are_searched(self):
        """Test that git lists the files, so an untracked virtualenv below a root is searched not at all."""
        listed = "src/update_time/oci.py\0docs/README.md.in\0src/update_time/oci.pyc\0"
        with patch("tools.rename.subprocess.run", Mock(return_value=Mock(stdout=listed))) as run:
            found = _prose_files()
        self.assertEqual(found, [Path("src/update_time/oci.py"), Path("docs/README.md.in")])
        self.assertEqual(run.call_args.args[0], ["git", "ls-files", "-z", *_PROSE_ROOTS])


class MainTest(unittest.TestCase):
    """Unit tests for renaming over the files named and reporting a rename that did not land."""

    def rename(
        self, *sources: str, old: str = "m.old", new: str = "m.new", module: str = _DEFINES, prose: str = ""
    ) -> int:
        """Rename over files holding the given sources, and return the exit code.

        `module` is what the module `m` holds, given as a file of its own when the old name names that module.

        `prose` is what the one file searched for mentions of the name holds, empty for a repository mentioning it
        nowhere.
        """
        self.reported, self.noted = io.StringIO(), io.StringIO()
        self.paths = {f"file{index}.py": mock_path(source) for index, source in enumerate(sources)}
        if old.startswith("m."):
            self.paths["m.py"] = mock_path(module)
        mentioning = Mock(read_text=Mock(return_value=prose), __str__=lambda _: "prose.py")
        with (
            patch.object(sys, "argv", ["rename.py", old, new, *self.paths]),
            patch("tools.rename.Path", Mock(side_effect=lambda path: self.paths[path])),
            patch("tools.rename._prose_files", Mock(return_value=[mentioning])),
            redirect_stdout(self.noted),
            redirect_stderr(self.reported),
        ):
            return main()

    def test_a_rename_that_reached_every_file(self):
        """Test that files the codemod rewrote, none of them left holding the name, pass without a report."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, "from m import old as alias\n\nalias()\n"), 0)
        self.assertEqual(self.reported.getvalue(), "")

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

    def test_the_module_the_qualified_name_names(self):
        """Test that the file the old name qualifies is renamed too, since the definition sits in it."""
        # The helper writes the first source to `file0.py`, which is the module the name `file0.old` qualifies.
        self.assertEqual(self.rename(_DEFINES, old="file0.old", new="new"), 0)
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call("def new():\n    pass\n")])

    def test_a_method_named_by_its_class_is_renamed_at_its_definition_and_calls(self):
        """Test that a name given as `module.Class.method` renames the method's definition and every call to it."""
        defines = "class Helper:\n    def old(self):\n        return self.old()\n"
        calls = "def use(helper):\n    return helper.{}()\n"
        self.assertEqual(self.rename(defines, calls.format("old"), old="file0.Helper.old", new="new"), 0)
        renamed = "class Helper:\n    def new(self):\n        return self.new()\n"
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call(renamed)])
        self.assertEqual(self.paths["file1.py"].write_text.call_args_list, [call(calls.format("new"))])

    def test_a_property_rename_renames_the_decorator_of_its_setter(self):
        """Test that a property named by its class is renamed at its getter and at its setter's decorator."""
        defines = (
            "class Shape:\n    @property\n    def {0}(self):\n        return 1\n\n"
            "    @{0}.setter\n    def {0}(self, value):\n        pass\n"
        )
        self.assertEqual(self.rename(defines.format("old"), old="file0.Shape.old", new="new"), 0)
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call(defines.format("new"))])

    def test_a_method_rename_renames_another_class_s_method_of_that_name_in_the_defining_file(self):
        """Test that a method rename renames the method of that name another class in the file defines, as elsewhere."""
        defines = (
            "class Helper:\n    def {0}(self):\n        return 1\n\n\n"
            "class Other:\n    def {0}(self):\n        return 2\n\n\n"
            "def use(helper, other):\n    return helper.{0}() + other.{0}()\n"
        )
        self.assertEqual(self.rename(defines.format("old"), old="file0.Helper.old", new="new"), 0)
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call(defines.format("new"))])

    def test_a_method_rename_leaves_a_function_of_that_name_in_another_file(self):
        """Test that a method rename renames its calls in another file, not a function or another method there."""
        defines = "class Helper:\n    def old(self):\n        return 1\n"
        uses = "def old():\n    return 2\n\n\ndef use(helper):\n    return old() + helper.{}() + helper.other()\n"
        self.assertEqual(self.rename(defines, uses.format("old"), old="file0.Helper.old", new="new"), 0)
        self.assertEqual(self.paths["file1.py"].write_text.call_args_list, [call(uses.format("new"))])

    def test_a_method_rename_renames_an_override_in_another_file(self):
        """Test that a method rename renames the override of the method a subclass in another file defines."""
        defines = "class Helper:\n    def old(self):\n        return 1\n"
        overrides = (
            "class Sub(Helper):\n    def {}(self):\n        return 2\n\n    def other(self):\n        return 3\n\n"
            "    class Meta:\n        pass\n"
        )
        self.assertEqual(self.rename(defines, overrides.format("old"), old="file0.Helper.old", new="new"), 0)
        self.assertEqual(self.paths["file1.py"].write_text.call_args_list, [call(overrides.format("new"))])

    def test_an_attribute_rename_renames_its_keyword_arguments_in_another_file(self):
        """Test that an attribute rename renames the keyword arguments of that name in a file not defining it."""
        defines = "class Point:\n    old: int\n"
        uses = "def use(point):\n    return Point(1, {0}=1, other=2), point.{0}\n"
        self.assertEqual(self.rename(defines, uses.format("old"), old="file0.Point.old", new="new"), 0)
        self.assertEqual(self.paths["file1.py"].write_text.call_args_list, [call(uses.format("new"))])

    def test_an_attribute_rename_renames_the_class_attribute_a_subclass_in_another_file_sets(self):
        """Test that an attribute rename renames the class attribute of that name a subclass in another file sets."""
        defines = "class Point:\n    old: int\n"
        cases = {
            "an assignment": "class Origin(Point):\n    {} = 0\n    other = 1\n",
            "an annotated assignment": "class Origin(Point):\n    {}: int = 0\n    other: int = 1\n",
        }
        for case, sets in cases.items():
            with self.subTest(case=case):
                self.assertEqual(self.rename(defines, sets.format("old"), old="file0.Point.old", new="new"), 0)
                self.assertEqual(self.paths["file1.py"].write_text.call_args_list, [call(sets.format("new"))])

    def test_the_files_the_rename_changed_are_written(self):
        """Test that a file the rename changed is written, and one it left as it was is not."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, "print(1)\n"), 0)
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call("from m import new\n\nnew()\n")])
        self.assertEqual(self.paths["file1.py"].write_text.call_args_list, [])

    @kills(
        Mutation(
            rename_module.main,
            "return _FAILED\n    for path, source in changed.items():",
            "for path, source in changed.items():\n            Path(path).write_text(source)\n"
            "        return _FAILED\n    for path, source in changed.items():",
            "a rename that left the old name behind still writes the files it changed to disk",
        )
    )
    def test_a_rename_that_left_the_name_behind_writes_nothing(self):
        """Test that a file left holding the name leaves every file unwritten, the ones the rename changed too."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, _DEFINES), 1)
        self.assertEqual([path.write_text.call_args_list for path in self.paths.values()], [[], [], []])

    @kills(
        Mutation(
            rename_module._renamed_sources,
            '_report(f"{path} could not be renamed: {described}")',
            '_report(f"{path} could not be renamed: {described}")\n'
            "            for written, renamed_source in renamed.items():\n"
            "                Path(written).write_text(renamed_source)",
            "a file the codemod cannot parse still writes the renames already made to the other files",
        )
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

    @kills(
        Mutation(
            rename_module._survivors_message,
            "', '.join(left)",
            "' '.join(left)",
            "the surviving occurrences are run together without the comma between them",
        )
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
