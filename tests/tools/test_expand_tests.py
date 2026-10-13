"""Unit tests for expanding the test packages named on the command line into their test modules."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from tools.expand_tests import expanded_test_names, main


def _package(*modules: str) -> Mock:
    """Return a directory holding the test modules at the given paths, relative to the repository root."""
    return Mock(is_dir=Mock(return_value=True), rglob=Mock(return_value=[Path(module) for module in modules]))


class ExpandedTestNamesTest(unittest.TestCase):
    """Unit tests for the names `unittest` is handed."""

    def test_a_package_expands_into_its_test_modules(self):
        """Test that a package is replaced by the dotted names of the test modules under it, sorted."""
        package = _package("tests/pkg/sub/test_b.py", "tests/pkg/test_a.py")
        with patch("tools.expand_tests.Path", Mock(return_value=package)):
            self.assertEqual(expanded_test_names(["tests.pkg"]), ["tests.pkg.sub.test_b", "tests.pkg.test_a"])
        package.rglob.assert_called_once_with("test_*.py")

    def test_a_name_that_is_no_package_is_kept(self):
        """Test that a module, a class, or a method is passed on as it was named."""
        names = ["tests.pkg.test_a", "tests.pkg.test_a.SomeTest", "tests.pkg.test_a.SomeTest.test_it"]
        with patch("tools.expand_tests.Path", Mock(return_value=Mock(is_dir=Mock(return_value=False)))):
            self.assertEqual(expanded_test_names(names), names)


class MainTest(unittest.TestCase):
    """Unit tests for what the command line writes."""

    def test_the_names_are_written_on_one_line(self):
        """Test that the names are written separated by spaces, as a shell splits them into arguments."""
        written = io.StringIO()
        with (
            patch.object(sys, "argv", ["expand_tests.py", "tests.pkg", "tests.other.test_c"]),
            patch(
                "tools.expand_tests.expanded_test_names", Mock(return_value=["tests.pkg.test_a", "tests.other.test_c"])
            ),
            redirect_stdout(written),
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(written.getvalue(), "tests.pkg.test_a tests.other.test_c\n")
