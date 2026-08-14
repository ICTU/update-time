"""Unit tests for the check that a rename left no occurrence of the old name behind."""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, call, patch

from tools.rename import _PROSE_FILES, _PROSE_ROOTS, _prose_files, main, stale_mentions, surviving_occurrences


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

    def rename(self, *files: tuple[str, str], old: str = "old", codemod: int = 0, prose: str = "") -> int:
        """Rename over files given as the source before and after the codemod, and return the exit code.

        `prose` is what the one file searched for mentions of the name holds, empty for a repository mentioning it
        nowhere.
        """
        self.reported, self.noted = io.StringIO(), io.StringIO()
        paths = {
            f"file{index}.py": Mock(read_text=Mock(side_effect=[before, after]))
            for index, (before, after) in enumerate(files)
        }
        self.run_codemod = Mock(return_value=Mock(returncode=codemod))
        mentioning = Mock(read_text=Mock(return_value=prose), __str__=lambda _: "prose.py")
        with (
            patch.object(sys, "argv", ["rename.py", old, "new", *paths]),
            patch("tools.rename.Path", Mock(side_effect=lambda path: paths[path])),
            patch("tools.rename.subprocess.run", self.run_codemod),
            patch("tools.rename._prose_files", Mock(return_value=[mentioning])),
            redirect_stdout(self.noted),
            redirect_stderr(self.reported),
        ):
            return main()

    def test_a_rename_that_reached_every_file(self):
        """Test that files the codemod rewrote, none of them left holding the name, pass without a report."""
        self.assertEqual(self.rename(("old()\n", "new()\n"), ("from module import old\n", "from m import new\n")), 0)
        self.assertEqual(self.reported.getvalue(), "")

    def test_the_codemod_is_given_the_names_and_the_files(self):
        """Test that the codemod is run over the files named, with the old and new name it is to rewrite."""
        self.rename(("old()\n", "new()\n"))
        command = self.run_codemod.call_args.args[0]
        self.assertIn("--old_name=old", command)
        self.assertIn("--new_name=new", command)
        self.assertEqual(command[-1], "file0.py")

    def test_a_codemod_that_failed(self):
        """Test that a codemod exiting non-zero is reported by exiting with the same code, checking nothing."""
        self.assertEqual(self.rename(("old()\n", "new()\n"), codemod=2), 2)
        self.assertEqual(self.reported.getvalue(), "")

    def test_a_rename_that_changed_nothing(self):
        """Test that files the codemod left as they were are reported as a name it never found."""
        self.assertEqual(self.rename(("old()\n", "old()\n")), 1)
        self.assertIn("nothing was renamed", self.reported.getvalue())

    def test_a_rename_that_reached_the_definition_alone(self):
        """Test that a file still holding the name fails the rename, which names that file and line."""
        both = (("def old():\n    pass\n", "def new():\n    pass\n"), ("from m import old\n", "from m import old\n"))
        self.assertEqual(self.rename(*both), 1)
        self.assertIn("file1.py:1", self.reported.getvalue())

    def test_every_file_that_still_holds_the_name(self):
        """Test that the report names each surviving occurrence, rather than stopping at the first file."""
        self.rename(("old()\n", "new()\n"), ("old()\n", "old()\n"), ("old()\n", "old()\n"))
        self.assertIn("file1.py:1", self.reported.getvalue())
        self.assertIn("file2.py:1", self.reported.getvalue())

    def test_a_qualified_name(self):
        """Test that a name given as `module.name` is looked for as the identifier alone."""
        both = (("old()\n", "new()\n"), ("old()\n", "old()\n"))
        self.assertEqual(self.rename(*both, old="update_time.module.old"), 1)

    def test_prose_that_still_mentions_the_name(self):
        """Test that a rename that landed reports the prose mentioning the old name, without failing over it."""
        self.assertEqual(self.rename(("old()\n", "new()\n"), prose="Hands it to `old`.\n"), 0)
        self.assertIn("prose.py:1", self.noted.getvalue())

    def test_prose_that_mentions_the_name_nowhere(self):
        """Test that a rename no prose mentions the old name after reports nothing."""
        self.assertEqual(self.rename(("old()\n", "new()\n")), 0)
        self.assertEqual(self.noted.getvalue(), "")
