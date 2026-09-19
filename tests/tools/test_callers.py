"""Unit tests for the report of where a name is called."""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, call, patch

from tools import callers as callers_module
from tools.callers import _ROOTS, _python_files, call_sites, main

from tests.helpers import mock_path
from tests.mutation import Mutation, kills


class CallSitesTest(unittest.TestCase):
    """Unit tests for finding the calls a name is made by."""

    def sites(self, name: str, *texts: str) -> list[str]:
        """Return where files holding the given texts call the name."""
        return call_sites(name, [mock_path(text) for text in texts])

    @kills(
        Mutation(
            callers_module,
            '    return ast.unparse(call.func).rsplit(".", 1)[-1] == name',
            "    return ast.unparse(call.func) == name",
            "a call made on an object is missed, so the count comes out short",
        )
    )
    def test_every_kind_of_call(self):
        """Test that a call is found whichever syntax makes it, bare or through an object."""
        cases = {
            "a bare call": "target(1)\n",
            "a call on an object": "self.target(1)\n",
            "a call on a chain of objects": "module.helpers.target(1)\n",
        }
        for case, source in cases.items():
            with self.subTest(case=case):
                self.assertEqual(self.sites("target", source), [f"f.py:1: {source.strip()}"])

    @kills(
        Mutation(
            callers_module,
            '    return [f"{path}:{call.lineno}: {ast.unparse(call)}"',
            '    return [f"{path}:{call.end_lineno}: {ast.unparse(call)}"',
            "a call is reported at the line its arguments end on, so the report points past the call it names",
        )
    )
    def test_a_call_spanning_several_lines(self):
        """Test that a call whose arguments sit on their own lines is reported once, at the line it starts on."""
        self.assertEqual(self.sites("target", 'target(\n    "a",\n    "b",\n)\n'), ["f.py:1: target('a', 'b')"])

    @kills(
        Mutation(
            callers_module,
            "    return [node for node in ast.walk(_parsed(path)) "
            "if isinstance(node, ast.Call) and _calls_name(node, name)]",
            '    return [node for node in ast.walk(_parsed(path)) if getattr(node, "name", None) == name '
            "or (isinstance(node, ast.Call) and _calls_name(node, name))]",
            "the line defining the name counts as a call site, which is the miscount a grep makes",
            expected_killers=2,
        ),
        Mutation(
            callers_module,
            '    return ast.unparse(call.func).rsplit(".", 1)[-1] == name',
            '    return name in ast.unparse(call.func).split(".")',
            "a call made on the name counts as a call to it, so a method call inflates the count",
        ),
    )
    def test_a_name_that_is_not_called_is_not_reported(self):
        """Test that the call on the first line is all a source holding the name in other ways reports."""
        cases = {
            "its definition": "def target():\n    pass\n",
            "a method called on it": "target.method(1)\n",
        }
        for case, rest in cases.items():
            with self.subTest(case=case):
                self.assertEqual(self.sites("target", f"target(1)\n{rest}"), ["f.py:1: target(1)"])


class PythonFilesTest(unittest.TestCase):
    """Unit tests for the files searched for calls."""

    def test_every_root_is_searched(self):
        """Test that every root holding Python is searched, for the Python files under it."""
        root = Mock(rglob=Mock(return_value=[Mock()]))
        with patch("tools.callers.Path", Mock(return_value=root)):
            found = _python_files()
        self.assertEqual(len(found), len(_ROOTS))
        self.assertEqual(root.rglob.call_args_list, [call("*.py")] * len(_ROOTS))


class MainTest(unittest.TestCase):
    """Unit tests for the report the command line writes."""

    def report(self, name: str, *sources: str) -> int:
        """Report where the name is called over files holding the sources, and return the exit code."""
        self.written, self.reported = io.StringIO(), io.StringIO()
        files = [mock_path(source, name=f"file{index}.py") for index, source in enumerate(sources)]
        with (
            patch.object(sys, "argv", ["callers.py", name]),
            patch("tools.callers._python_files", Mock(return_value=files)),
            redirect_stdout(self.written),
            redirect_stderr(self.reported),
        ):
            return main()

    def test_the_number_of_call_sites(self):
        """Test that the report ends with the number of call sites."""
        self.assertEqual(self.report("target", "target(1)\n"), 0)
        self.assertEqual(self.written.getvalue(), "file0.py:1: target(1)\ncall sites: 1\n")

    @kills(
        Mutation(
            callers_module,
            "        undefined = not sites and not _defined(name, files)",
            "        undefined = not sites",
            "a name nothing calls fails the run, so a name nobody uses cannot be told from a misspelled one",
        )
    )
    def test_a_name_nothing_defines(self):
        """Test that a name no file defines fails the run, where a name nothing calls reports no call site."""
        self.assertEqual(self.report("target", "other(1)\n"), 1)
        self.assertIn("nothing defines target", self.reported.getvalue())
        self.assertEqual(self.report("target", "def target():\n    pass\n"), 0)
        self.assertEqual(self.written.getvalue(), "call sites: 0\n")

    def test_a_file_that_cannot_be_parsed(self):
        """Test that a file whose Python does not parse fails the run by name, rather than counting the rest."""
        self.assertEqual(self.report("target", "target(1)\n", "this is not python(\n"), 1)
        self.assertIn("file1.py", self.reported.getvalue())
