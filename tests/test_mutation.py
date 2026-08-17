"""Unit tests for making a test kill the mutation it is meant to kill."""

import importlib
import os
import sys
import unittest
from unittest.mock import Mock, patch

from tests.mutation import _CHECKING, Mutation, Outcome, Result, check, kills
from tests.mutation_subject import is_even

_SUBJECT = "tests/mutation_subject.py"
_EVEN = "number % 2 == 0"
_ODD = "number % 2 != 0"
_REGISTERING_TEST = "tests.test_mutation.IsEvenTest.test_an_odd_number"
_SUBJECT_TEST = "tests.test_mutation.IsEvenTest.test_an_even_number"
_REGRESSION = "an odd number is reported as even"


class IsEvenTest(unittest.TestCase):
    """Unit tests for deciding whether a number is even, which the tests below use as their targets."""

    def test_an_even_number(self):
        """Test that an even number is even.

        Left undecorated, since checking a mutation runs this test and a decorated one would check a mutation of
        its own while doing so.
        """
        self.assertTrue(is_even(2))

    @kills(_SUBJECT, _EVEN, _ODD, _REGRESSION)
    def test_an_odd_number(self):
        """Test that an odd number is not even."""
        self.assertFalse(is_even(3))

    @kills(_SUBJECT, _EVEN, _ODD, _REGRESSION)
    @patch("tests.mutation_subject.is_even")
    def test_an_odd_number_patched_beneath_the_decorator(self, patched_subject: Mock):
        """Test that an odd number is not even, with a patch handing this test a mock from beneath the decorator.

        The patch replaces the subject module's attribute, while the binding this module imported is the one the
        assertion reaches and the one the mutation breaks.
        """
        self.assertIsInstance(patched_subject, Mock)
        self.assertFalse(is_even(3))

    @patch("tests.mutation_subject.is_even")
    @kills(_SUBJECT, _EVEN, _ODD, _REGRESSION)
    def test_an_odd_number_patched_above_the_decorator(self, patched_subject: Mock):
        """Test that an odd number is not even, with a patch above the decorator handing this test a mock.

        The patch calls the decorator's wrapper with the mock, so the wrapper has to hand on what it was called
        with rather than only the test case.
        """
        self.assertIsInstance(patched_subject, Mock)
        self.assertFalse(is_even(3))


class CheckTest(unittest.TestCase):
    """Unit tests for checking a test against the mutation it is meant to kill."""

    def test_a_test_that_fails_against_the_mutation(self):
        """Test that a mutation the registered test fails against is reported as killed."""
        self.assertEqual(check(Mutation(_SUBJECT, _EVEN, _ODD, _SUBJECT_TEST)), Result(Outcome.KILLED))

    def test_a_test_that_passes_against_the_mutation(self):
        """Test that a mutation the registered test passes against is reported as survived."""
        self.assertEqual(check(Mutation(_SUBJECT, _EVEN, "number == 2", _SUBJECT_TEST)), Result(Outcome.SURVIVED))

    def test_a_mutation_whose_file_and_test_are_in_different_packages(self):
        """Test that the package of the mutated file is purged as well as the package of its test.

        The module between the two is imported first, being the one that holds a binding to the mutated function.
        Without it in `sys.modules`, purging either package alone would do just as well and this would pass.
        """
        importlib.import_module("update_time.domain.staleness")
        mutation = Mutation(
            "src/update_time/primitives/timestamp.py",
            "return (datetime.now(UTC) - timestamp).days",
            "return (datetime.now(UTC) - timestamp).days + 1",
            "tests.update_time.domain.test_staleness.IsStaleTest.test_boundary_compares_whole_days",
        )
        self.assertEqual(check(mutation), Result(Outcome.KILLED))

    def test_a_source_that_does_not_import_or_a_test_that_errors(self):
        """Test that a mutation the test could not judge is reported as broken rather than as survived."""
        for case, new in (("does not import", "number %"), ("errors", "nonexistent")):
            with self.subTest(case=case):
                self.assertEqual(check(Mutation(_SUBJECT, _EVEN, new, _SUBJECT_TEST)).outcome, Outcome.BROKEN)

    @kills(
        "tests/mutation.py",
        "except _SourceError as error:",
        "except Exception as error:",
        "an error in the checker itself is caught and misreported as a broken mutation",
    )
    def test_a_defect_in_the_checker_itself(self):
        """Test that an error from the checker is raised, rather than reported as the mutation being broken."""
        with patch("tests.mutation._purge", Mock(side_effect=RuntimeError("the checker is broken"))):
            self.assertRaises(RuntimeError, check, Mutation(_SUBJECT, _EVEN, _ODD, _SUBJECT_TEST))

    def test_the_modules_are_left_as_they_were(self):
        """Test that a check restores sys.modules, whether the mutated source ran or raised instead.

        The outcome is asserted as well, since a check that returned before touching `sys.modules` would leave it
        alone too, and pass on that alone.
        """
        for case, new, outcome in (("ran", _ODD, Outcome.KILLED), ("raised", "number %", Outcome.BROKEN)):
            with self.subTest(case=case):
                imported = dict(sys.modules)
                result = check(Mutation(_SUBJECT, _EVEN, new, _SUBJECT_TEST))
                self.assertEqual(result.outcome, outcome)
                names = sys.modules.keys() | imported.keys()
                changed = [name for name in names if sys.modules.get(name) is not imported.get(name)]
                self.assertEqual(changed, [])

    @kills(
        "tests/mutation.py",
        "return Result(Outcome.STALE, _reason(error))",
        "return Result(Outcome.STALE)",
        "a mutation naming an unreadable file is reported as stale without saying why",
    )
    def test_a_mutation_whose_file_cannot_be_read(self):
        """Test that a mutation naming a file that cannot be read is reported as stale, and says so."""
        unreadable = Mock(side_effect=FileNotFoundError(2, "No such file or directory"))
        with patch("pathlib.Path.read_text", unreadable):
            result = check(Mutation("tests/gone.py", _EVEN, _ODD, _SUBJECT_TEST))
        self.assertEqual(result.outcome, Outcome.STALE)
        self.assertIn("FileNotFoundError: [Errno 2] No such file or directory", result.reason)

    @kills(
        "tests/mutation.py",
        'return Result(Outcome.STALE, "the snippet and its replacement are the same, so nothing changes")',
        "return Result(Outcome.SURVIVED)",
        "a mutation that changes nothing is reported as survived, blaming the test rather than the registration",
    )
    def test_a_mutation_that_would_change_nothing(self):
        """Test that a replacement equal to the snippet is reported as stale, rather than as the test's failing."""
        result = check(Mutation(_SUBJECT, _EVEN, _EVEN, _SUBJECT_TEST))
        self.assertEqual(result.outcome, Outcome.STALE)
        self.assertEqual(result.reason, "the snippet and its replacement are the same, so nothing changes")

    def test_a_snippet_the_file_does_not_hold_exactly_once(self):
        """Test that a snippet the file holds never, or more than once, is reported as stale, saying how often."""
        for case, old, occurrences in (("absent", "number % 3 == 0", 0), ("repeated", "number", 3)):
            with self.subTest(case=case):
                result = check(Mutation(_SUBJECT, old, "count", _SUBJECT_TEST))
                self.assertEqual(result.outcome, Outcome.STALE)
                self.assertEqual(result.reason, f"the snippet occurs {occurrences} times rather than once")


class KillsTest(unittest.TestCase):
    """Unit tests for the decorator that makes a test check its mutation."""

    def run_decorated_test(self, checked: Result) -> unittest.TestResult:
        """Run the decorated test with the check answering as given, and return what unittest recorded.

        The check is stood in for, so that a test of the decorator does not pay for a real one; the decorated test
        the suite runs of its own accord is what exercises the real check.
        """
        self.checked = Mock(return_value=checked)
        result = unittest.TestResult()
        with patch("tests.mutation.check", self.checked):
            IsEvenTest("test_an_odd_number").run(result)
        return result

    @kills(
        "tests/mutation.py",
        '        mutation = Mutation(path, old, new, f"{function.__module__}.{function.__qualname__}", regression)',
        '        mutation = Mutation(path, old, new, f"{function.__module__}.{function.__qualname__}")',
        "the regression never reaches the mutation, so a failure cannot name it",
    )
    def test_a_decorated_test_checks_its_mutation(self):
        """Test that running a decorated test checks its mutation, and passes when the mutation is killed."""
        result = self.run_decorated_test(Result(Outcome.KILLED))
        self.assertEqual((result.failures, result.errors), ([], []))
        self.checked.assert_called_once_with(Mutation(_SUBJECT, _EVEN, _ODD, _REGISTERING_TEST, _REGRESSION))

    @kills(
        "tests/mutation.py",
        '        return f"{mutation.regression} — the test did not kill this mutation of {mutation.path}"',
        '        return f"the test did not kill this mutation of {mutation.path}"',
        "the survivor message drops the regression, so it no longer says what went wrong",
    )
    def test_a_surviving_mutation_fails_the_test(self):
        """Test that a survivor fails the test, leading with the regression rather than the snippets."""
        result = self.run_decorated_test(Result(Outcome.SURVIVED))
        self.assertEqual(len(result.failures), 1)
        message = result.failures[0][1]
        self.assertIn(f"{_REGRESSION} — the test did not kill this mutation of {_SUBJECT}", message)
        self.assertNotIn(_ODD, message)
        self.assertNotIn(_EVEN, message)

    @kills(
        "tests/mutation.py",
        '    return f"{mutation.regression} — this mutation of {mutation.path} is {result.outcome}: {result.reason}"',
        '    return f"this mutation of {mutation.path} is {result.outcome}: {result.reason}"',
        "the stale-or-broken message drops the regression, so it no longer says what went wrong",
    )
    def test_a_mutation_the_test_could_not_judge_fails_it_with_the_reason(self):
        """Test that a stale or broken mutation fails the test, leading with the regression and then the reason."""
        for case, outcome, reason in (
            ("stale", Outcome.STALE, "FileNotFoundError: no such file"),
            ("broken", Outcome.BROKEN, "SyntaxError: invalid syntax"),
        ):
            with self.subTest(case=case):
                result = self.run_decorated_test(Result(outcome, reason))
                self.assertEqual(len(result.failures), 1)
                message = result.failures[0][1]
                self.assertIn(f"{_REGRESSION} — this mutation of {_SUBJECT} is {outcome}: {reason}", message)

    def test_a_test_failing_of_its_own_accord_is_not_checked(self):
        """Test that a test already failing fails on that, rather than its mutation being reported as killed."""
        with patch("tests.test_mutation.is_even", Mock(return_value=True)):
            result = self.run_decorated_test(Result(Outcome.KILLED))
        self.assertEqual(len(result.failures), 1)
        self.checked.assert_not_called()

    def test_a_test_re_run_against_its_mutation_checks_nothing(self):
        """Test that a test re-run against its own mutation runs its body and checks nothing further."""
        is_even_stub = Mock(return_value=False)
        with patch.dict(os.environ, {_CHECKING: "1"}), patch("tests.test_mutation.is_even", is_even_stub):
            result = self.run_decorated_test(Result(Outcome.KILLED))
        self.assertEqual((result.failures, result.errors), ([], []))
        is_even_stub.assert_called_once_with(3)
        self.checked.assert_not_called()

    def test_the_decorated_test_reports_as_the_test_it_decorates(self):
        """Test that the wrapper carries the name and the docstring unittest prints for the test it decorates."""
        method = IsEvenTest.test_an_odd_number
        wrapped = kills(_SUBJECT, _EVEN, _ODD, _REGRESSION)(method)
        self.assertEqual((wrapped.__qualname__, wrapped.__doc__), (method.__qualname__, method.__doc__))
