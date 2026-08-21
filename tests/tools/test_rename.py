"""Unit tests for the check that a rename left no occurrence of the old name behind."""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, call, patch

from tools import rename as rename_module
from tools.rename import _PROSE_FILES, _PROSE_ROOTS, _prose_files, main, stale_mentions, surviving_occurrences

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
        return stale_mentions(
            name, [Mock(read_text=Mock(return_value=text), __str__=lambda _: "f.py") for text in texts]
        )

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

    def test_the_roots_and_patterns_searched(self):
        """Test that every pattern is searched under every root, so the files a rename was given are not the limit."""
        root = Mock(rglob=Mock(return_value=[Mock()]))
        with patch("tools.rename.Path", Mock(return_value=root)):
            found = _prose_files()
        self.assertEqual(len(found), len(_PROSE_ROOTS) * len(_PROSE_FILES))
        self.assertEqual(root.rglob.call_args_list[:2], [call(_PROSE_FILES[0]), call(_PROSE_FILES[1])])


class MainTest(unittest.TestCase):
    """Unit tests for renaming over the files named and reporting a rename that did not land."""

    def rename(self, *sources: str, old: str = "m.old", new: str = "m.new", prose: str = "") -> int:
        """Rename over files holding the given sources, and return the exit code.

        `prose` is what the one file searched for mentions of the name holds, empty for a repository mentioning it
        nowhere.
        """
        self.reported, self.noted = io.StringIO(), io.StringIO()
        self.paths = {f"file{index}.py": mock_path(source) for index, source in enumerate(sources)}
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

    def test_a_new_name_given_bare(self):
        """Test that a new name given bare renames a qualified old one, as the usage spells a rename."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, old="m.old", new="new"), 0)
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call("from m import new\n\nnew()\n")])

    def test_the_module_the_qualified_name_names(self):
        """Test that the file the old name qualifies is renamed too, since the definition sits in it."""
        # The helper writes the first source to `file0.py`, which is the module the name `file0.old` qualifies.
        self.assertEqual(self.rename(_DEFINES, old="file0.old", new="new"), 0)
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call("def new():\n    pass\n")])

    def test_the_files_the_rename_changed_are_written(self):
        """Test that a file the rename changed is written, and one it left as it was is not."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, "print(1)\n"), 0)
        self.assertEqual(self.paths["file0.py"].write_text.call_args_list, [call("from m import new\n\nnew()\n")])
        self.assertEqual(self.paths["file1.py"].write_text.call_args_list, [])

    @kills(
        Mutation(
            rename_module,
            "        return _FAILED\n    for path, source in changed.items():",
            "        for path, source in changed.items():\n            Path(path).write_text(source)\n"
            "        return _FAILED\n    for path, source in changed.items():",
            "a rename that left the old name behind still writes the files it changed to disk",
        )
    )
    def test_a_rename_that_left_the_name_behind_writes_nothing(self):
        """Test that a file left holding the name leaves every file unwritten, the ones the rename changed too."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, _DEFINES), 1)
        self.assertEqual([path.write_text.call_args_list for path in self.paths.values()], [[], []])

    @kills(
        Mutation(
            rename_module,
            '            _report(f"{path} could not be renamed: {reason}")',
            '            _report(f"{path} could not be renamed: {reason}")\n'
            "            for written, renamed_source in renamed.items():\n"
            "                Path(written).write_text(renamed_source)",
            "a file the codemod cannot parse still writes the renames already made to the other files",
        )
    )
    def test_a_file_the_codemod_cannot_parse(self):
        """Test that a source the codemod cannot parse is reported by name, and writes none of the files."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, "this is not python(\n"), 1)
        self.assertIn("file1.py", self.reported.getvalue())
        self.assertEqual([path.write_text.call_args_list for path in self.paths.values()], [[], []])

    def test_an_argument_libcst_rejects(self):
        """Test that an argument LibCST rejects is reported by name, rather than raised at the reader."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, old="m:old"), 1)
        self.assertIn("file0.py could not be renamed", self.reported.getvalue())

    def test_a_rename_that_changed_nothing(self):
        """Test that files the codemod left as they were are reported as a name it never found."""
        self.assertEqual(self.rename("print(1)\n"), 1)
        self.assertIn("nothing was renamed", self.reported.getvalue())

    @kills(
        Mutation(
            rename_module,
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
        self.assertIn("No file was written; a rename that lands writes file0.py\n", self.reported.getvalue())

    def test_every_file_that_still_holds_the_name(self):
        """Test that the report names each surviving occurrence, rather than stopping at the first file."""
        self.rename(_IMPORTS_AND_CALLS, _DEFINES, _DEFINES)
        self.assertIn("file1.py:1", self.reported.getvalue())
        self.assertIn("file2.py:1", self.reported.getvalue())

    def test_a_qualified_name(self):
        """Test that a name given as `module.name` is looked for as the identifier alone."""
        sources = ("from update_time.module import old\n\nold()\n", _DEFINES)
        self.assertEqual(self.rename(*sources, old="update_time.module.old", new="update_time.module.new"), 1)

    def test_prose_that_still_mentions_the_name(self):
        """Test that a rename that landed reports the prose mentioning the old name, without failing over it."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS, prose="Hands it to `old`.\n"), 0)
        self.assertIn("prose.py:1", self.noted.getvalue())

    def test_prose_that_mentions_the_name_nowhere(self):
        """Test that a rename no prose mentions the old name after reports nothing."""
        self.assertEqual(self.rename(_IMPORTS_AND_CALLS), 0)
        self.assertEqual(self.noted.getvalue(), "")
