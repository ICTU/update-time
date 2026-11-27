"""Test that the names the modules make public have callers outside the module defining them.

Also test that no private class takes a public class's name with an underscore added.
"""

import pathlib
import unittest
from typing import TYPE_CHECKING

from mutation_time.mutation import kills

from .source_code import project
from .visibility import (
    imported_module_file,
    module_local_constants,
    module_local_definitions,
    private_classes_named_like_public_ones,
    privates_imported_elsewhere,
    python_files,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class VisibilityTest(unittest.TestCase):
    """Test that a public module-level name has callers outside the module defining it.

    Constants, functions, and classes are all checked, bar the names reached without a call: a class a framework
    discovers, and the entry point a module runs when run as a script.
    """

    def test_private_names_have_no_importer_outside_their_module(self):
        """Test that a name kept private is imported nowhere else, so its spelling matches its callers."""
        self.assertEqual(privates_imported_elsewhere(python_files()), [])

    @kills.append(
        privates_imported_elsewhere,
        "or not node.module",
        " or node.level",
        "a relative import is passed over, so a private name a sibling module imports goes unreported",
    )
    def test_a_private_name_imported_by_another_module_is_reported(self):
        """Test that a private name another module imports is reported, unless a test reaches into the code it tests."""
        files = {
            "src/pkg/mod.py": "_PRIVATE = 1\n",
            "tests/test_other.py": "from pkg.mod import _PRIVATE\n",  # a test reaching into the code it covers
            "tests/harness.py": "_HARNESS = 2\n",
            "tests/test_harness.py": "from tests.harness import _HARNESS\n",  # a test of that very module
            "tests/outsider.py": "from unittest.mock import _Call\n",  # a private of another project
            "tests/helpers.py": "_HELPER = 3\n",
            "tests/test_architecture.py": "from tests.helpers import _HELPER\n",  # neither of those
            "tests/support/helper.py": "_SUPPORT = 4\n",
            "tests/support/user.py": "from .helper import _SUPPORT\n",  # neither of those, imported relatively
        }
        self.assertEqual(self.reported(privates_imported_elsewhere, files), ["_SUPPORT", "_HELPER"])

    @staticmethod
    def reported(check: Callable[[list[pathlib.Path]], list[str]], files: dict[str, str]) -> list[str]:
        """Return the names the check reports for a project of the given files, without their paths."""
        with project(files) as directory:
            reported = check(sorted(pathlib.Path(directory).rglob("*.py")))
        return [entry.rsplit(":", maxsplit=1)[-1] for entry in reported]

    def test_no_private_class_is_named_like_a_public_class(self):
        """Test that no private class shares its name, underscore aside, with a public class elsewhere."""
        self.assertEqual(private_classes_named_like_public_ones(python_files()), [])

    def test_a_private_class_named_like_a_public_class_is_reported(self):
        """Test that a private class is reported where a public class elsewhere has its name without the underscore."""
        files = {
            "pkg/domain.py": "class ResolvedReference: ...\n",
            "pkg/updater.py": "class _ResolvedReference: ...\nclass _SubstitutedLine: ...\n",
        }
        self.assertEqual(self.reported(private_classes_named_like_public_ones, files), ["_ResolvedReference"])

    def test_constants_without_outside_callers_are_private(self):
        """Test that every public module-level constant is imported from its module or read as an attribute."""
        self.assertEqual(module_local_constants(python_files()), [])

    def test_definitions_without_outside_callers_are_private(self):
        """Test that every public module-level definition is imported from its module or read as an attribute."""
        self.assertEqual(module_local_definitions(python_files()), [])

    def test_constants_have_a_caller_outside_the_tests(self):
        """Test that no constant is public only so that a test can reach it.

        The `tools` scripts count as callers, so what they import stays public.
        """
        self.assertEqual(module_local_constants([f for f in python_files() if f.parts[0] != "tests"]), [])

    def test_definitions_have_a_caller_outside_the_tests(self):
        """Test that no function, class, or type alias is public only so that a test can reach it.

        The `tools` scripts count as callers, so what they import stays public.
        """
        self.assertEqual(module_local_definitions([f for f in python_files() if f.parts[0] != "tests"]), [])

    def test_a_constant_imported_from_another_root_is_not_reported(self):
        """Test that an import is matched to the module assigning it although the two sit under different roots."""
        files = {
            "one/pkg/mod.py": "SHARED = 1\nLOCAL = 2\n\nLOCAL\n",
            "two/importer.py": "from pkg.mod import SHARED\n\nSHARED\n",
        }
        self.assertEqual(self.reported(module_local_constants, files), ["LOCAL"])

    @kills.delete(
        imported_module_file,
        "(importer.parents[level - 1] / module_file).as_posix() if level else ",
        "a relative import is matched by its module's name alone, so another module of that name counts as imported",
        case="sibling",
    )
    @kills.replace(
        imported_module_file,
        "level - 1",
        "0",
        "a relative import is resolved from the importer's own package whatever its level",
        case="subpackage",
    )
    def test_a_constant_imported_by_a_relative_import_is_not_reported(self):
        """Test that a relative import is matched to the module it names, rather than to another module of its name."""
        for case, importer in {
            "sibling": {"pkg/importer.py": "from .mod import SHARED\n\nSHARED\n"},
            "subpackage": {"pkg/sub/importer.py": "from ..mod import SHARED\n\nSHARED\n"},
        }.items():
            with self.subTest(case=case):
                files = {"pkg/mod.py": "SHARED = 1\n", "other/mod.py": "SHARED = 2\n", **importer}
                self.assertEqual(self.reported(module_local_constants, files), ["SHARED"])

    def test_a_constant_named_in_a_string_is_not_reported(self):
        """Test that a constant another module names in a string is not reported, since it is reached that way."""
        self.assert_only_the_unreached_constant_is_reported('import pkg.mod\n\ngetattr(pkg.mod, "REACHED")\n')

    def assert_only_the_unreached_constant_is_reported(self, reader: str) -> None:
        """Assert that of the two constants a module assigns, only the one the reader leaves alone is reported."""
        files = {"pkg/mod.py": "REACHED = 1\nUNREACHED = 2\n\nUNREACHED\n", "reader.py": reader}
        self.assertEqual(self.reported(module_local_constants, files), ["UNREACHED"])

    def test_a_constant_read_as_an_attribute_is_not_reported(self):
        """Test that a constant another module reads through the module object is not reported."""
        self.assert_only_the_unreached_constant_is_reported("import pkg.mod\n\npkg.mod.REACHED\n")

    def test_names_that_are_not_public_constants_are_not_reported(self):
        """Test that a function, a class, a lower-case variable, and an already-private constant are not reported."""
        source = "def function(): ...\nclass Klass: ...\nvariable = 1\n_PRIVATE = 2\nPUBLIC = 3\n"
        self.assertEqual(self.reported(module_local_constants, {"mod.py": source}), ["PUBLIC"])

    def test_a_function_without_outside_callers_is_reported(self):
        """Test that a public module-level function no module other than the one defining it refers to is reported.

        An async function is a function here, so it is reported the same way.
        """
        files = {
            "pkg/mod.py": "def reached(): ...\ndef local(): ...\nasync def waited(): ...\n\nlocal()\n",
            "reader.py": "from pkg.mod import reached\n\nreached()\n",
        }
        self.assertEqual(self.reported(module_local_definitions, files), ["local", "waited"])

    def test_a_test_case_is_not_reported(self):
        """Test that a class the test runner discovers is not reported, however far it sits from `TestCase`.

        A subscripted base names no class the scan can resolve, so a class carrying one is no test case.
        """
        direct = "import unittest\n\n\nclass Discovered(unittest.TestCase): ...\n"
        inherited = "import unittest\n\n\nclass Base(unittest.TestCase): ...\nclass Discovered(Base): ...\n"
        for case, source in {
            "direct": f"{direct}class Klass: ...\n",
            "inherited": f"{inherited}class Klass: ...\n",
            "subscripted base": f"{direct}class Klass(tuple[str, ...]): ...\n",
        }.items():
            with self.subTest(case=case):
                self.assertEqual(self.reported(module_local_definitions, {"mod.py": source}), ["Klass"])

    def test_a_lint_rule_is_not_reported(self):
        """Test that a class the linter discovers is not reported."""
        source = "from fixit import LintRule\n\n\nclass Rule(LintRule): ...\nclass Klass: ...\n"
        self.assertEqual(self.reported(module_local_definitions, {"mod.py": source}), ["Klass"])

    def test_an_entry_point_is_not_reported(self):
        """Test that the function a module runs when run as a script is not reported, unlike one merely named so.

        The name alone says nothing, since a module that never runs as a script exposes no entry point.
        """
        defined = "def main(): ...\ndef local(): ...\n"
        for case, source in {
            "entry point": f'{defined}\n\nif __name__ == "__main__":\n    main()\n',
            "named main only": defined,
        }.items():
            with self.subTest(case=case):
                expected = ["local"] if case == "entry point" else ["local", "main"]
                self.assertEqual(self.reported(module_local_definitions, {"mod.py": source}), expected)
