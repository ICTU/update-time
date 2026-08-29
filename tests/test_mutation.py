"""Unit tests for making a test kill the mutations it is meant to kill."""

import importlib
import os
import sys
import unittest
from unittest.mock import Mock, call, patch

from update_time.primitives import timestamp

from tests import helpers, mutation_subject
from tests import mutation as checker
from tests.helpers import patch_environ
from tests.mutation import CHECKED_TEST, CHECKS_OFF, Mutation, Outcome, Result, kills
from tests.mutation_subject import is_even

_EVEN = "number % 2 == 0"
_ODD = "number % 2 != 0"
_SUBJECT_TEST_NAME = "tests.test_mutation.IsEvenTest.test_an_even_number"
# The `IsEvenTest` tests `KillsTest` runs to exercise the decorator: one registers a single mutation, one several.
_DECORATED_TEST = "test_an_odd_number"
_SEVERAL_MUTATIONS_TEST = "test_an_odd_number_against_several_mutations"
_REGRESSION = "an odd number is reported as even"
_RAISING_REGRESSION = "deciding whether a number is even raises instead of answering"
_ONLY_THREE = "number == 3"
_ONLY_THREE_REGRESSION = "only the number three is reported as even"
# A replacement that leaves the subject importable but raises when the test calls it, and the error it raises.
_ERRORING = "nonexistent"
_NAME_ERROR = "NameError: name 'nonexistent' is not defined"
_UNPARSABLE = "number %"  # A replacement that leaves the subject unparsable, so it does not import at all.
_SURVIVING = "number == 2"  # A replacement the test passes against, so nothing it asserts breaks.
_ODD_REPORTED_AS_EVEN = Mutation(mutation_subject, _EVEN, _ODD, _REGRESSION)


class IsEvenTest(unittest.TestCase):
    """Unit tests for deciding whether a number is even, which the tests below use as their targets."""

    def test_an_even_number(self):
        """Test that an even number is even.

        Left undecorated, since checking a mutation runs this test and a decorated one would check a mutation of
        its own while doing so.
        """
        self.assertTrue(is_even(2))

    @kills(_ODD_REPORTED_AS_EVEN)
    def test_an_odd_number(self):
        """Test that an odd number is not even."""
        self.assertFalse(is_even(3))

    @kills(_ODD_REPORTED_AS_EVEN)
    @patch("tests.mutation_subject.is_even")
    def test_an_odd_number_patched_beneath_the_decorator(self, patched_subject: Mock):
        """Test that an odd number is not even, with a patch handing this test a mock from beneath the decorator.

        The patch replaces the subject module's attribute, while the binding this module imported is the one the
        assertion reaches and the one the mutation breaks.
        """
        self.assertIsInstance(patched_subject, Mock)
        self.assertFalse(is_even(3))

    @patch("tests.mutation_subject.is_even")
    @kills(_ODD_REPORTED_AS_EVEN)
    def test_an_odd_number_patched_above_the_decorator(self, patched_subject: Mock):
        """Test that an odd number is not even, with a patch above the decorator handing this test a mock.

        The patch calls the decorator's wrapper with the mock, so the wrapper has to hand on what it was called
        with rather than only the test case.
        """
        self.assertIsInstance(patched_subject, Mock)
        self.assertFalse(is_even(3))

    @kills(
        _ODD_REPORTED_AS_EVEN,
        Mutation(mutation_subject, _EVEN, _ONLY_THREE, _ONLY_THREE_REGRESSION),
    )
    def test_an_odd_number_against_several_mutations(self):
        """Test that an odd number is not even, killing each of the mutations its registration holds."""
        self.assertFalse(is_even(3))

    @kills(Mutation(mutation_subject, _EVEN, _ERRORING, _RAISING_REGRESSION, raises=_NAME_ERROR))
    def test_an_odd_number_against_a_mutation_that_raises(self):
        """Test that an odd number is not even, killing a mutation by raising the error the mutation declares."""
        self.assertFalse(is_even(3))


class CheckTest(unittest.TestCase):
    """Unit tests for checking a test against the mutation it is meant to kill."""

    def test_a_test_that_fails_against_the_mutation(self):
        """Test that a mutation the registered test fails against is reported as killed."""
        self.assertEqual(Mutation(mutation_subject, _EVEN, _ODD).check(_SUBJECT_TEST_NAME), Result(Outcome.KILLED))

    @kills(
        Mutation(
            checker,
            "        return Result(Outcome.KILLED if test_result.failures else Outcome.SURVIVED)",
            "        return Result(Outcome.KILLED if test_result.failures or self.raises else Outcome.SURVIVED)",
            "a declaration alone counts as a kill, so a mutation the test passes against is reported as killed",
        )
    )
    def test_a_test_that_passes_against_the_mutation(self):
        """Test that a mutation the registered test passes against is reported as survived, declared error or not."""
        for case, declared in (("nothing declared", ""), ("an error declared", _NAME_ERROR)):
            with self.subTest(case=case):
                mutation = Mutation(mutation_subject, _EVEN, _SURVIVING, raises=declared)
                self.assertEqual(mutation.check(_SUBJECT_TEST_NAME), Result(Outcome.SURVIVED))

    def test_a_mutation_whose_file_and_test_are_in_different_packages(self):
        """Test that the package of the mutated file is purged as well as the package of its test.

        The module between the two is imported first, being the one that holds a binding to the mutated function.
        Without it in `sys.modules`, purging either package alone would do just as well and this would pass.
        """
        importlib.import_module("update_time.domain.staleness")
        mutation = Mutation(
            timestamp,
            "return (datetime.now(UTC) - timestamp).days",
            "return (datetime.now(UTC) - timestamp).days + 1",
        )
        staleness_test_name = "tests.update_time.domain.test_staleness.IsStaleTest.test_boundary_compares_whole_days"
        self.assertEqual(mutation.check(staleness_test_name), Result(Outcome.KILLED))

    @kills(
        Mutation(
            checker,
            "        return unittest.TestLoader().loadTestsFromName(test_name)\n    except Exception as error:",
            "        return unittest.TestLoader().loadTestsFromName(test_name)\n    except SyntaxError as error:",
            "a mutation the test module cannot import escapes the check rather than being reported as broken",
            raises="AttributeError: 'function' object has no attribute '_registered_mutations'",
        ),
        Mutation(
            checker,
            "        return unittest.TestLoader().loadTestsFromName(test_name)\n"
            "    except Exception as error:\n"
            "        raise _SourceError(_reason(error)) from error",
            "        return unittest.TestLoader().loadTestsFromName(test_name)\n"
            "    except Exception as error:\n"
            "        raise _SourceError() from error",
            "a mutation the test module cannot import is reported as broken without naming the error",
        ),
    )
    def test_a_mutation_that_cannot_be_judged_is_reported_as_broken(self):
        """Test that a mutation the check could not judge is reported as broken rather than as survived."""
        deletes_the_registration = Mutation(
            checker,
            "        setattr(wrapper, _REGISTERED, mutations)",
            "        delattr(wrapper, _REGISTERED)",
        )
        for case, mutation, error in (
            ("the mutated source does not import", Mutation(mutation_subject, _EVEN, _UNPARSABLE), "SyntaxError"),
            ("the test errors", Mutation(mutation_subject, _EVEN, _ERRORING), "NameError"),
            ("the test module raises when imported", deletes_the_registration, "AttributeError"),
        ):
            with self.subTest(case=case):
                result = mutation.check(_SUBJECT_TEST_NAME)
                self.assertEqual(result.outcome, Outcome.BROKEN)
                self.assertIn(error, result.reason)

    @kills(
        Mutation(
            checker,
            "            return Result(Outcome.BROKEN, str(error))",
            "            return Result(Outcome.KILLED if str(error) == self.raises else Outcome.BROKEN, str(error))",
            "a mutation whose source does not import counts as killed where the declaration names the import's error",
        )
    )
    def test_a_source_that_does_not_import_though_its_error_is_declared(self):
        """Test that a mutation whose source does not import is reported as broken though it declares that error."""
        reported = Mutation(mutation_subject, _EVEN, _UNPARSABLE).check(_SUBJECT_TEST_NAME).reason
        declaring = Mutation(mutation_subject, _EVEN, _UNPARSABLE, raises=reported)
        self.assertEqual(declaring.check(_SUBJECT_TEST_NAME), Result(Outcome.BROKEN, reported))

    @kills(
        Mutation(
            checker,
            "return Result(Outcome.KILLED) if raised == self.raises else Result(Outcome.BROKEN, raised)",
            "return Result(Outcome.BROKEN, raised)",
            "a test that kills a mutation by raising the error the mutation declares is reported as broken",
        )
    )
    def test_a_test_that_raises_the_declared_error(self):
        """Test that a mutation whose test raises the declared error is reported as killed."""
        mutation = Mutation(mutation_subject, _EVEN, _ERRORING, raises=_NAME_ERROR)
        self.assertEqual(mutation.check(_SUBJECT_TEST_NAME), Result(Outcome.KILLED))

    @kills(
        Mutation(
            checker,
            "Result(Outcome.BROKEN, raised)",
            "Result(Outcome.BROKEN)",
            "a mutation reported as broken does not name the error the test raised, which is the line to declare",
        )
    )
    def test_a_test_that_raises_an_undeclared_error(self):
        """Test that a mutation whose test raises an undeclared error is reported as broken, naming the error."""
        declared = "TypeError: an error the test does not raise"
        self.assertEqual(
            Mutation(mutation_subject, _EVEN, _ERRORING, raises=declared).check(_SUBJECT_TEST_NAME),
            Result(Outcome.BROKEN, _NAME_ERROR),
        )

    @kills(
        Mutation(
            checker,
            "except _SourceError as error:",
            "except Exception as error:",
            "an error in the checker itself is caught and misreported as a broken mutation",
        )
    )
    def test_a_defect_in_the_checker_itself(self):
        """Test that an error from the checker is raised, rather than reported as the mutation being broken."""
        with patch.object(Mutation, "_purge", Mock(side_effect=RuntimeError("the checker is broken"))):
            self.assertRaises(RuntimeError, Mutation(mutation_subject, _EVEN, _ODD).check, _SUBJECT_TEST_NAME)

    def test_the_modules_are_left_as_they_were(self):
        """Test that a check restores sys.modules, whether the mutated source ran or raised instead.

        The outcome is asserted as well, since a check that returned before touching `sys.modules` would leave it
        alone too, and pass on that alone.
        """
        for case, new, outcome in (("ran", _ODD, Outcome.KILLED), ("raised", "number %", Outcome.BROKEN)):
            with self.subTest(case=case):
                imported = dict(sys.modules)
                result = Mutation(mutation_subject, _EVEN, new).check(_SUBJECT_TEST_NAME)
                self.assertEqual(result.outcome, outcome)
                names = sys.modules.keys() | imported.keys()
                changed = [name for name in names if sys.modules.get(name) is not imported.get(name)]
                self.assertEqual(changed, [])

    @kills(
        Mutation(
            checker,
            "return Result(Outcome.STALE, _reason(error))",
            "return Result(Outcome.STALE)",
            "a mutation naming an unreadable file is reported as stale without saying why",
        )
    )
    def test_a_mutation_whose_file_cannot_be_read(self):
        """Test that a mutation naming a file that cannot be read is reported as stale, and says so."""
        unreadable = Mock(side_effect=FileNotFoundError(2, "No such file or directory"))
        with patch("pathlib.Path.read_text", unreadable):
            result = Mutation(mutation_subject, _EVEN, _ODD).check(_SUBJECT_TEST_NAME)
        self.assertEqual(result.outcome, Outcome.STALE)
        self.assertIn("FileNotFoundError: [Errno 2] No such file or directory", result.reason)

    @kills(
        Mutation(
            checker,
            'return Result(Outcome.STALE, "the snippet and its replacement are the same, so nothing changes")',
            "return Result(Outcome.SURVIVED)",
            "a mutation that changes nothing is reported as survived, blaming the test rather than the registration",
        )
    )
    def test_a_mutation_that_would_change_nothing(self):
        """Test that a replacement equal to the snippet is reported as stale, rather than as the test's failing."""
        result = Mutation(mutation_subject, _EVEN, _EVEN).check(_SUBJECT_TEST_NAME)
        self.assertEqual(result.outcome, Outcome.STALE)
        self.assertEqual(result.reason, "the snippet and its replacement are the same, so nothing changes")

    def test_a_snippet_the_file_does_not_hold_exactly_once(self):
        """Test that a snippet the file holds never, or more than once, is reported as stale, saying how often."""
        for case, old, occurrences in (("absent", "number % 3 == 0", 0), ("repeated", "number", 3)):
            with self.subTest(case=case):
                result = Mutation(mutation_subject, old, "count").check(_SUBJECT_TEST_NAME)
                self.assertEqual(result.outcome, Outcome.STALE)
                self.assertEqual(result.reason, f"the snippet occurs {occurrences} times rather than once")


def _stand_in(_test_case: unittest.TestCase, *_args: object) -> None:
    """Stand in for the test that a registration under test decorates."""


class KillsTest(unittest.TestCase):
    """Unit tests for the decorator that makes a test check its mutations."""

    def run_decorated_test(
        self, checked: Result, test_name: str = _DECORATED_TEST, sentinels: dict[str, str] | None = None
    ) -> unittest.TestResult:
        """Run the named decorated test with the check answering as given, and return what unittest recorded.

        The check is stood in for, so that a test of the decorator does not pay for a real one; the decorated test
        the suite runs of its own accord is what exercises the real check. The run sees the sentinels named and no
        others, so what holds the decorated test back is what the test asked for rather than what a `just mutate`
        run set.
        """
        result = unittest.TestResult()
        decorated = IsEvenTest(test_name)
        self.decorated_id = decorated.id()
        with patch.dict(os.environ), patch.object(Mutation, "check", autospec=True) as self.checked:
            os.environ.pop(CHECKED_TEST, None)
            os.environ.pop(CHECKS_OFF, None)
            os.environ.update(sentinels or {})
            self.checked.return_value = checked
            decorated.run(result)
        return result

    @kills(
        Mutation(
            checker,
            "        for mutation in mutations:",
            "        for mutation in mutations[:1]:",
            "only the first of the mutations a registration holds is checked",
        ),
        Mutation(
            checker,
            "            result = mutation.check(test_name)",
            '            result = mutation.check(test_name.split(".")[-1])',
            "the test is named without the module it sits in, so the checker cannot load it",
        ),
    )
    def test_a_decorated_test_checks_every_mutation_it_registers(self):
        """Test that a registration holding several mutations checks each of them, in order, naming the test."""
        result = self.run_decorated_test(Result(Outcome.KILLED), _SEVERAL_MUTATIONS_TEST)
        self.assertEqual((result.failures, result.errors), ([], []))
        self.assertEqual(
            self.checked.call_args_list,
            [
                call(_ODD_REPORTED_AS_EVEN, self.decorated_id),
                call(Mutation(mutation_subject, _EVEN, _ONLY_THREE, _ONLY_THREE_REGRESSION), self.decorated_id),
            ],
        )

    @kills(
        Mutation(
            checker,
            '        return f"{mutation.regression} — the test did not kill this mutation of '
            '{mutation.module.__name__}"',
            '        return f"the test did not kill this mutation of {mutation.module.__name__}"',
            "the survivor message drops the regression, so it no longer says what went wrong",
        )
    )
    def test_a_surviving_mutation_fails_the_test(self):
        """Test that a survivor fails the test, leading with the regression rather than the snippets."""
        result = self.run_decorated_test(Result(Outcome.SURVIVED))
        self.assertEqual(len(result.failures), 1)
        message = result.failures[0][1]
        self.assertIn(f"{_REGRESSION} — the test did not kill this mutation of {mutation_subject.__name__}", message)
        self.assertNotIn(_ODD, message)
        self.assertNotIn(_EVEN, message)

    @kills(
        Mutation(
            checker,
            "            with test_case.subTest(regression=mutation.regression):",
            "            if True:",
            "a test stops at the first mutation it did not kill, leaving every mutation after it unreported",
        ),
        Mutation(
            checker,
            "            with test_case.subTest(regression=mutation.regression):",
            "            with test_case.subTest():",
            "a mutation the test did not kill is reported without naming which mutation it was",
        ),
        Mutation(
            checker,
            "_failure(mutation, result))",
            "_failure(mutations[0], result))",
            "every mutation the test did not kill is reported with the first one's regression",
        ),
    )
    def test_every_surviving_mutation_is_reported_separately(self):
        """Test that each mutation a test does not kill fails it as a case of its own, naming its regression."""
        result = self.run_decorated_test(Result(Outcome.SURVIVED), _SEVERAL_MUTATIONS_TEST)
        self.assertEqual(len(result.failures), 2)
        regressions = (_REGRESSION, _ONLY_THREE_REGRESSION)
        for (subtest, message), regression in zip(result.failures, regressions, strict=True):
            with self.subTest(regression=regression):
                self.assertIn(f"(regression={regression!r})", str(subtest))
                self.assertIn(regression, message)

    @kills(
        Mutation(
            checker,
            '    return f"{mutation.regression} — this mutation of {mutation.module.__name__} is '
            '{result.outcome}: {result.reason}"',
            '    return f"this mutation of {mutation.module.__name__} is {result.outcome}: {result.reason}"',
            "the stale-or-broken message drops the regression, so it no longer says what went wrong",
        )
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
                self.assertIn(
                    f"{_REGRESSION} — this mutation of {mutation_subject.__name__} is {outcome}: {reason}", message
                )

    def test_a_test_failing_of_its_own_accord_is_not_checked(self):
        """Test that a test already failing fails on that, rather than its mutation being reported as killed."""
        with patch("tests.test_mutation.is_even", Mock(return_value=True)):
            result = self.run_decorated_test(Result(Outcome.KILLED))
        self.assertEqual(len(result.failures), 1)
        self.checked.assert_not_called()

    @kills(
        Mutation(
            checker,
            "            if os.environ.get(CHECKED_TEST) == self.id() or os.environ.get(CHECKS_OFF):",
            "            if os.environ.get(CHECKS_OFF):",
            "a test re-run against its own mutation checks its mutations again, so a run never ends",
        ),
        Mutation(
            checker,
            "            if os.environ.get(CHECKED_TEST) == self.id() or os.environ.get(CHECKS_OFF):",
            "            if os.environ.get(CHECKED_TEST) == self.id():",
            "a `just mutate` run checks the registered mutations too, so its kill list names tests it never broke",
        ),
    )
    def test_a_test_held_back_by_a_sentinel_checks_nothing(self):
        """Test that a test held back by either sentinel runs its body and checks nothing further."""
        cases = (
            ("re-run against its mutation", {CHECKED_TEST: IsEvenTest(_DECORATED_TEST).id()}),
            ("checks switched off", {CHECKS_OFF: "1"}),
        )
        for case, sentinels in cases:
            with self.subTest(case=case):
                is_even_stub = Mock(return_value=False)
                with patch("tests.test_mutation.is_even", is_even_stub):
                    result = self.run_decorated_test(Result(Outcome.KILLED), sentinels=sentinels)
                self.assertEqual((result.failures, result.errors), ([], []))
                is_even_stub.assert_called_once_with(3)
                self.checked.assert_not_called()

    @kills(
        Mutation(
            helpers,
            "        in_dict[CHECKS_OFF] = checks_off  # Keep the checks-off flag; it's used by tools/mutate.py "
            "to turn off @kills",
            "        pass  # Keep the checks-off flag; it's used by tools/mutate.py to turn off @kills",
            "a test whose class clears the environment loses the sentinel, so `just mutate` checks it after all",
        )
    )
    def test_the_sentinel_survives_a_cleared_environment(self):
        """Test that patching the environment keeps the checks-off sentinel, whatever else it clears."""
        with patch.dict(os.environ, {CHECKS_OFF: "1"}, clear=True), patch_environ():
            self.assertEqual(os.environ.get(CHECKS_OFF), "1")

    @kills(
        Mutation(
            checker,
            "            if os.environ.get(CHECKED_TEST) == self.id() or os.environ.get(CHECKS_OFF):",
            "            if os.environ.get(CHECKED_TEST) or os.environ.get(CHECKS_OFF):",
            "a decorated test the checked test reaches stands aside too, so its mutations go unchecked",
        )
    )
    def test_a_test_the_sentinel_does_not_name_checks_its_mutations(self):
        """Test that a decorated test checks its mutations while another test is the one being checked."""
        result = self.run_decorated_test(Result(Outcome.KILLED), sentinels={CHECKED_TEST: _SUBJECT_TEST_NAME})
        self.assertEqual((result.failures, result.errors), ([], []))
        self.checked.assert_called_once()

    @kills(
        Mutation(
            checker,
            "        setattr(wrapper, _REGISTERED, mutations)",
            "        pass",
            "a decorated test carries no registration, so a second one on it goes undetected",
        )
    )
    def test_a_second_registration_on_one_test_fails_it(self):
        """Test that a second kills decorator on one test fails it, whatever decorator sits between the two."""
        registered = kills(_ODD_REPORTED_AS_EVEN)(_stand_in)
        for case, stacked in (
            ("adjacent", registered),
            ("with a patch between", patch("tests.mutation_subject.is_even")(registered)),
        ):
            with self.subTest(case=case):
                with patch.object(Mutation, "check", autospec=True) as checked:
                    checked.return_value = Result(Outcome.KILLED)
                    twice_registered = kills(_ODD_REPORTED_AS_EVEN)(stacked)
                    with self.assertRaises(self.failureException) as raised:
                        twice_registered(self)
                self.assertIn("more than one kills decorator", str(raised.exception))
                checked.assert_not_called()

    @kills(
        Mutation(
            checker,
            "        if not mutations:",
            "        if False:",
            "a registration naming no mutation is accepted, so the test it decorates checks nothing",
        )
    )
    def test_a_registration_of_no_mutation_fails_the_test(self):
        """Test that a kills decorator given no mutation fails the test, rather than leaving it checking nothing."""
        decorated = kills()(_stand_in)
        with self.assertRaises(self.failureException) as raised:
            decorated(self)
        self.assertIn("registers no mutation", str(raised.exception))

    @kills(
        Mutation(
            checker,
            "        @functools.wraps(method)",
            "",
            "a decorated test reports under the wrapper's name, so a failing run names no test",
        )
    )
    def test_the_decorated_test_reports_as_the_test_it_decorates(self):
        """Test that the wrapper carries the name and the docstring unittest prints for the test it decorates."""
        method = IsEvenTest.test_an_even_number
        wrapped = kills(_ODD_REPORTED_AS_EVEN)(method)
        self.assertEqual((wrapped.__qualname__, wrapped.__doc__), (method.__qualname__, method.__doc__))
