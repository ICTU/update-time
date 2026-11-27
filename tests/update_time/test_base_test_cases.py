"""Unit tests for the test cases the tests build on."""

import unittest
from functools import cache
from typing import Final
from unittest.mock import patch

import update_time
from update_time.domain.reference import Reference
from update_time.io.log import Logger
from update_time.package_managers.requirements_compiler import RequirementsCompiler
from update_time.sources import pypi

from .base_test_cases import CacheClearingTestCase, LoggingTestCase, all_loggers
from .fixtures import VULNERABILITY_LEVEL_NONE
from .io.log_fields import create_location
from .io.log_messages import message_of


class CacheClearingTest(unittest.TestCase):
    """Unit tests for the base test case that clears the caches."""

    def test_a_cache_no_list_names_is_cleared(self):
        """Test that a cached function added to a module is cleared, so adding one calls for no bookkeeping."""
        cached = cache(int)
        with patch.object(pypi, "newly_cached", cached, create=True):
            cached("1")
            self.assertEqual(cached.cache_info().currsize, 1)
            CacheClearingTestCase().setUp()
            self.assertEqual(cached.cache_info().currsize, 0)


class AllLoggersTest(unittest.TestCase):
    """Unit tests for discovering the loggers a package holds."""

    def test_the_loggers_of_the_package_are_found(self):
        """Test that walking the package reaches the loggers a module nested in it holds, or a class in it holds."""
        loggers = all_loggers(update_time)
        self.assertIn(pypi._LOG, loggers)
        self.assertIn(RequirementsCompiler.log, loggers)


# The tables below log these fields. They name a commit SHA that could not be fetched.
_NO_COMMIT_SHA: Final[dict[str, str]] = {
    "dependency": "owner/repository",
    "version": "1.1",
    "reason": "HTTP 404",
    "url": "https://github.com/owner/repository/releases/tag/1.1",
}


class LoggingTestCaseTest(unittest.TestCase):
    """Unit tests for the base test case that fails a test on an error it did not expect."""

    def test_an_error_an_earlier_case_of_a_table_logs_fails_the_test(self):
        """Test that an error the first case of a table logs fails the test, although the last case logs none."""

        class Table(LoggingTestCase):
            """A table whose first case logs an error without asserting it."""

            def test_table(self) -> None:
                """Log an error in the first case only."""
                for case, log in (
                    ("logs an error", lambda: Logger("test").no_commit_sha(**_NO_COMMIT_SHA)),
                    ("logs nothing", lambda: None),
                ):
                    with self.subTest(case):
                        log()

        self.assert_fails_once_on_the_unexpected_error(Table)

    def assert_fails_once_on_the_unexpected_error(self, table: type[LoggingTestCase]) -> None:
        """Assert that running the table's test reports one failure, naming the error it logged unexpectedly."""
        result = unittest.TestResult()
        table("test_table").run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("Could not fetch the commit of the release", result.failures[0][1])

    def test_an_error_a_later_case_of_a_table_logs_fails_the_test_although_an_earlier_case_expected_one(self):
        """Test that an error a later case of a table logs fails the test, although an earlier case asserted one."""

        class Table(LoggingTestCase):
            """A table whose first case asserts the error it logs, and whose second case does not."""

            def test_table(self) -> None:
                """Log an error in both cases, asserting it in the first only."""
                message = message_of(Logger.no_commit_sha)
                for case, handle_error in (
                    ("expects an error", lambda: self.assert_error_logged(message, **_NO_COMMIT_SHA)),
                    ("logs an error", lambda: None),
                ):
                    with self.subTest(case):
                        Logger("test").no_commit_sha(**_NO_COMMIT_SHA)
                        handle_error()

        self.assert_fails_once_on_the_unexpected_error(Table)


class AssertNoRedundantSuppressionLoggedTest(unittest.TestCase):
    """Unit tests for the assertion that no vulnerability suppression was reported as silencing nothing."""

    def test_a_reported_suppression_fails_the_assertion(self):
        """Test that the assertion fails on a suppression that was reported, so it cannot pass on records of none."""
        case = LoggingTestCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        pin = Reference("django", "3.2.0", create_location("requirements.txt", 1), marker=VULNERABILITY_LEVEL_NONE)
        Logger("test").redundant_vulnerable_level(pin, "low")
        self.assertRaises(AssertionError, case.assert_no_redundant_suppression_logged)
