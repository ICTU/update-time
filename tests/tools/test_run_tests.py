"""Unit tests for running the tests in shards, each in a process of its own."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from tools.run_tests import _expanded_test_names, _run_shard, _ShardResult, main

from tests.mutation import KillsTestLoader


def _passes() -> None:
    """Pass, as a test that finds nothing wrong does."""


def _fails() -> None:
    """Fail, as a test whose assertion does not hold does."""
    message = "the assertion does not hold"
    raise AssertionError(message)


def _errs() -> None:
    """Raise an error other than a failed assertion, as a test that breaks does."""
    message = "the test breaks"
    raise ValueError(message)


def _package(*modules: str) -> Mock:
    """Return a directory holding the test modules at the given paths, relative to the repository root."""
    return Mock(is_dir=Mock(return_value=True), rglob=Mock(return_value=[Path(module) for module in modules]))


class ExpandedTestNamesTest(unittest.TestCase):
    """Unit tests for the names `unittest` is handed."""

    def test_a_package_expands_into_its_test_modules(self):
        """Test that a package is replaced by the dotted names of the test modules under it, sorted."""
        package = _package("tests/pkg/sub/test_b.py", "tests/pkg/test_a.py")
        with patch("tools.run_tests.Path", Mock(return_value=package)):
            self.assertEqual(_expanded_test_names(["tests.pkg"]), ["tests.pkg.sub.test_b", "tests.pkg.test_a"])
        package.rglob.assert_called_once_with("test_*.py")

    def test_a_name_that_is_no_package_is_kept(self):
        """Test that a module, a class, or a method is passed on as it was named."""
        names = ["tests.pkg.test_a", "tests.pkg.test_a.SomeTest", "tests.pkg.test_a.SomeTest.test_it"]
        with patch("tools.run_tests.Path", Mock(return_value=Mock(is_dir=Mock(return_value=False)))):
            self.assertEqual(_expanded_test_names(names), names)


class RunShardTest(unittest.TestCase):
    """Unit tests for running one shard, which happens in a process of its own."""

    def test_a_shard_returns_what_its_tests_showed(self):
        """Test that a shard counts the tests it ran, its failures, and its errors, and keeps the report."""
        suite = unittest.TestSuite(unittest.FunctionTestCase(test) for test in (_passes, _fails, _errs))
        loader = Mock(loadTestsFromName=Mock(return_value=suite))
        result = _run_shard("tests.a.test_x", loader)
        loader.loadTestsFromName.assert_called_once_with("tests.a.test_x")
        self.assertEqual((result.tests_run, result.failures, result.errors), (3, 1, 1))
        self.assertIn("AssertionError: the assertion does not hold", result.report)
        self.assertIn("ValueError: the test breaks", result.report)
        self.assertTrue(result.report.startswith("=" * 70), "the report writes a progress line before the failures")


class MainTest(unittest.TestCase):
    """Unit tests for what the command line runs and writes."""

    def setUp(self):
        """Give the pool an executor that runs nothing, and keep it to inspect."""
        self.executor = MagicMock()
        patcher = patch("tools.run_tests.ProcessPoolExecutor", self.executor)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, results: list[_ShardResult], *argv: str) -> tuple[int, str, MagicMock]:
        """Run the command line with the shards showing the results, and return its exit code, output, and pool."""
        pool = self.executor.return_value.__enter__.return_value
        pool.map.return_value = results
        written = io.StringIO()
        with patch.object(sys, "argv", ["run_tests.py", *argv]), redirect_stdout(written):
            exit_code = main()
        return exit_code, written.getvalue(), pool

    def test_the_pool_gets_half_the_cores(self):
        """Test that the pool gets half the cores, since the coverage pass and the mutations pass run side by side."""
        for case, cores, workers in (
            ("seven cores", 7, 3),
            ("one core", 1, 1),
            ("unknown", None, 1),
        ):
            with self.subTest(case), patch("os.process_cpu_count", Mock(return_value=cores)):
                self.run_main([_ShardResult(1, 0, 0, "")], "tests.a.test_x")
                self.assertEqual(self.executor.call_args.kwargs["max_workers"], workers)

    def test_the_pool_spawns_its_workers(self):
        """Test that the pool spawns its workers, which reach them over pipes rather than the sockets tests refuse."""
        self.run_main([_ShardResult(1, 0, 0, "")], "tests.a.test_x")
        self.assertEqual(self.executor.call_args.kwargs["mp_context"].get_start_method(), "spawn")

    def test_a_run_whose_shards_pass_writes_the_number_of_tests_they_ran(self):
        """Test that each test name runs as a shard of its own, and that the run adds up the tests the shards ran."""
        results = [_ShardResult(3, 0, 0, "report a"), _ShardResult(2, 0, 0, "report b")]
        exit_code, written, pool = self.run_main(results, "tests.a.test_x", "tests.b.test_y")
        self.assertEqual(exit_code, 0)
        self.assertEqual(pool.map.call_args.args[:2], (_run_shard, ["tests.a.test_x", "tests.b.test_y"]))
        self.assertEqual(written, "Ran 5 tests\n\nOK\n")

    def test_each_test_module_under_the_named_packages_runs_as_a_shard(self):
        """Test that a package named, or `tests` where the run does not name a test, expands into its test modules."""
        for case, argv, packages in (("no name", [], ["tests"]), ("a package", ["tests.a"], ["tests.a"])):
            with self.subTest(case):
                modules = Mock(return_value=["tests.a.test_x", "tests.a.test_y"])
                with patch("tools.run_tests._expanded_test_names", modules):
                    _exit_code, _written, pool = self.run_main([_ShardResult(1, 0, 0, "")] * 2, *argv)
                modules.assert_called_once_with(packages)
                self.assertEqual(pool.map.call_args.args[:2], (_run_shard, ["tests.a.test_x", "tests.a.test_y"]))

    def test_each_shard_loads_its_tests_with_the_loader_the_command_line_picks(self):
        """Test that each shard loads the tests carrying a kills decorator alone where the run says so, else all."""
        for case, argv, loader_class in (("all", [], unittest.TestLoader), ("kills", ["--kills"], KillsTestLoader)):
            with self.subTest(case):
                _exit_code, _written, pool = self.run_main([_ShardResult(1, 0, 0, "")], *argv, "tests.a.test_x")
                _run, names, loaders = pool.map.call_args.args
                self.assertEqual(list(names), ["tests.a.test_x"])
                self.assertIs(type(next(iter(loaders))), loader_class)

    def test_a_run_with_a_failing_shard_writes_its_report_and_fails(self):
        """Test that the report of a failing shard is written, and the run counts the failures and errors of all."""
        for case, failures, errors, verdict in (
            ("failure", 1, 0, "FAILED (failures=1)"),
            ("error", 0, 1, "FAILED (errors=1)"),
            ("both", 1, 2, "FAILED (failures=1, errors=2)"),
        ):
            with self.subTest(case):
                results = [_ShardResult(3, 0, 0, "report a\n"), _ShardResult(2, failures, errors, "report b\n")]
                exit_code, written, _pool = self.run_main(results, "tests.a.test_x", "tests.b.test_y")
                self.assertEqual(exit_code, 1)
                self.assertEqual(written, f"report b\nRan 5 tests\n\n{verdict}\n")
