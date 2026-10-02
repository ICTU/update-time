"""Unit tests for making a test kill the mutations it is meant to kill."""

import contextlib
import importlib
import importlib.machinery
import os
import pathlib
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import Mock, call, patch

from update_time.primitives import timestamp

from tests import helpers, mutation_subject
from tests import mutation as checker
from tests.helpers import patch_environ
from tests.mutation import CHECKED_TEST, CHECKS_OFF, Mutation, Outcome, Result, _failure, _record_survival, kills
from tests.mutation_subject import Doubler, is_even, is_multiple_of_three, is_positive_even

if TYPE_CHECKING:
    from collections.abc import Iterator

_EVEN = "number % 2 == 0"
_ODD = "number % 2 != 0"
# The subject's two predicates end on this snippet. So the file holds it twice, and each function holds it once.
_NO_REMAINDER = "== 0"
_A_REMAINDER = "!= 0"
# The doubler's two methods end on this snippet. So its class holds it twice, and each method holds it once.
_DOUBLING = "value * 2"
_TRIPLING = "value * 3"
_SUBJECT_TEST_NAME = "tests.test_mutation.IsEvenTest.test_an_even_number"
_MULTIPLE_TEST_NAME = "tests.test_mutation.IsMultipleOfThreeTest.test_a_multiple_of_three"
_DOUBLER_TEST_NAME = "tests.test_mutation.DoublerTest.test_a_doubled_value"
_ONE_DOUBLED_TEST_NAME = "tests.test_mutation.DoublerTest.test_one_doubled"
_TWO_DOUBLED_TEST_NAME = "tests.test_mutation.DoublerTest.test_two_doubled"
_THREE_DOUBLED_TEST_NAME = "tests.test_mutation.DoublerTest.test_three_doubled"
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
_ODD_REPORTED_AS_EVEN = Mutation(mutation_subject.is_even, _EVEN, _ODD, _REGRESSION)
_ONLY_THREE_REPORTED_AS_EVEN = Mutation(mutation_subject.is_even, _EVEN, _ONLY_THREE, _ONLY_THREE_REGRESSION)
# The regressions of the walk over a source's definitions that both an anchor and the anchor check rely on.
_CLASSES_SKIPPED = Mutation(
    checker._named,
    'yield from _named(node.body, f"{prefix}{node.name}.")',
    "pass",
    "the walk skips classes, so a method is neither found for its anchor nor seen by the check",
)
_SPAN_STOPS_BEFORE_THE_NEWLINE = Mutation(
    checker._node_span,
    '    if source.startswith("\\n", end):\n        end += 1\n',
    "",
    "a span stops before the newline ending a definition's last line, so a snippet ending in it escapes both",
)


class IsEvenTest(unittest.TestCase):
    """Unit tests for deciding whether a number is even, which the tests below use as their targets."""

    def test_an_even_number(self):
        """Test that an even number is even.

        Left undecorated, since checking a mutation runs this test and a decorated one would check a mutation of
        its own while doing so.
        """
        self.assertTrue(is_even(2))

    def test_a_positive_even_number(self):
        """Test that two is positive and even."""
        self.assertTrue(is_positive_even(2))

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
        _ONLY_THREE_REPORTED_AS_EVEN,
    )
    def test_an_odd_number_against_several_mutations(self):
        """Test that an odd number is not even, killing each of the mutations its registration holds."""
        self.assertFalse(is_even(3))

    @kills(Mutation(mutation_subject.is_even, _EVEN, _ERRORING, _RAISING_REGRESSION, raises=_NAME_ERROR))
    def test_an_odd_number_against_a_mutation_that_raises(self):
        """Test that an odd number is not even, killing a mutation by raising the error the mutation declares."""
        self.assertFalse(is_even(3))


class IsMultipleOfThreeTest(unittest.TestCase):
    """Unit tests for deciding whether a value is a multiple of three, which an anchored mutation targets."""

    def test_a_multiple_of_three(self):
        """Test that three is a multiple of three."""
        self.assertTrue(is_multiple_of_three(3))


class DoublerTest(unittest.TestCase):
    """Unit tests for the doubler's methods, which an anchored mutation targets."""

    def test_a_doubled_value(self):
        """Test that two doubled is four."""
        self.assertEqual(Doubler().doubled(2), 4)

    def test_a_quadrupled_value(self):
        """Test that two quadrupled is eight."""
        self.assertEqual(Doubler().quadrupled(2), 8)

    def test_one_doubled(self):
        """Test that one doubled is two."""
        self.assertEqual(Doubler().one_doubled, 2)

    def test_two_doubled(self):
        """Test that two doubled is four."""
        self.assertEqual(Doubler.two_doubled(), 4)

    def test_three_doubled(self):
        """Test that three doubled is six."""
        self.assertEqual(Doubler().three_doubled, 6)


class CheckTest(unittest.TestCase):
    """Unit tests for checking a test against the mutation it is meant to kill."""

    @kills(_SPAN_STOPS_BEFORE_THE_NEWLINE)
    def test_a_mutation_anchored_to_a_function_is_applied_inside_it(self):
        """Test that a mutation anchored to a function is applied to it, up to the newline after its last line."""
        self.assertEqual(Mutation(is_even, _EVEN, _ODD).check(_SUBJECT_TEST_NAME), Result(Outcome.KILLED))
        self.assertEqual(Mutation(is_even, f"{_EVEN}\n", f"{_ODD}\n").check(_SUBJECT_TEST_NAME), Result(Outcome.KILLED))

    @kills(
        _CLASSES_SKIPPED,
        Mutation(
            checker._span,
            "(qualified_name)",
            '(qualified_name.split(".")[-1])',
            "the lookup uses the bare name, so a method is searched for at the top of the module and never found",
        ),
    )
    def test_a_mutation_anchored_to_a_method_is_applied_inside_it(self):
        """Test that a mutation anchored to a method is applied to it, though its class holds the snippet twice."""
        ambiguous = Result(Outcome.STALE, "the snippet occurs 2 times rather than once")
        whole_file = Mutation(mutation_subject, _DOUBLING, _TRIPLING)
        self.assertEqual(whole_file.check(_DOUBLER_TEST_NAME), ambiguous)
        anchored = Mutation(Doubler.doubled, _DOUBLING, _TRIPLING)
        self.assertEqual(anchored.check(_DOUBLER_TEST_NAME), Result(Outcome.KILLED))

    @kills(
        Mutation(
            checker.Mutation._unwrapped,
            'cast("_Function", self.anchor.fget) if isinstance(self.anchor, property) else self.anchor',
            "self.anchor",
            "a property anchor is passed on as it is, so the run ends with a traceback",
            raises="AttributeError: 'property' object has no attribute '__module__'. Did you mean: '__reduce__'?",
        )
    )
    def test_a_mutation_anchored_to_a_property_is_applied_inside_its_getter(self):
        """Test that a mutation anchored to a property reaches the getter, which is what carries its names."""
        mutation = Mutation(Doubler.one_doubled, "self.doubled(1)", "self.doubled(2)")
        self.assertEqual(mutation.check(_ONE_DOUBLED_TEST_NAME), Result(Outcome.KILLED))

    @kills(
        Mutation(
            checker.Mutation._unwrapped,
            'return cast("_Function", self.anchor.func)',
            "return self.anchor",
            "a cached property anchor is passed on as it is, so the run ends with a traceback",
            raises=(
                "AttributeError: 'cached_property' object has no attribute '__qualname__'. "
                "Did you mean: '__set_name__'?"
            ),
        )
    )
    def test_a_mutation_anchored_to_a_cached_property_is_applied_inside_its_function(self):
        """Test that a mutation anchored to a cached property reaches its function, which is what carries its names."""
        mutation = Mutation(Doubler.three_doubled, "self.doubled(3)", "self.doubled(4)")
        self.assertEqual(mutation.check(_THREE_DOUBLED_TEST_NAME), Result(Outcome.KILLED))

    @kills(
        Mutation(
            checker._named,
            "if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):",
            "if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):",
            "a class is left out of the definitions, so a mutation anchored to one is reported stale",
        )
    )
    def test_a_mutation_anchored_to_a_class_is_applied_inside_it(self):
        """Test that a mutation anchored to a class is applied to a snippet in its body, outside its methods."""
        mutation = Mutation(Doubler, "A value doubler", "A doubler")
        self.assertEqual(mutation.check(_DOUBLER_TEST_NAME), Result(Outcome.SURVIVED))

    def test_a_mutation_anchored_to_a_classmethod_is_applied_inside_it(self):
        """Test that a mutation anchored to a classmethod is applied to it, so its test is reported as killed."""
        mutation = Mutation(Doubler.two_doubled, "cls().doubled(2)", "cls().doubled(3)")
        self.assertEqual(mutation.check(_TWO_DOUBLED_TEST_NAME), Result(Outcome.KILLED))

    def test_a_snippet_the_anchored_function_holds_once_and_the_file_twice(self):
        """Test that a snippet the anchored function holds once is applied there, and nowhere else in the file."""
        ambiguous = Result(Outcome.STALE, "the snippet occurs 2 times rather than once")
        whole_file = Mutation(mutation_subject, _NO_REMAINDER, _A_REMAINDER)
        self.assertEqual(whole_file.check(_MULTIPLE_TEST_NAME), ambiguous)
        anchored = Mutation(is_multiple_of_three, _NO_REMAINDER, _A_REMAINDER)
        self.assertEqual(anchored.check(_MULTIPLE_TEST_NAME), Result(Outcome.KILLED))
        # The other predicate ends on the same snippet, so its own test survives only while the change stays put.
        self.assertEqual(anchored.check(_SUBJECT_TEST_NAME), Result(Outcome.SURVIVED))

    @kills(
        Mutation(
            checker.Mutation.check,
            "return Result(Outcome.KILLED if test_result.failures else Outcome.SURVIVED)",
            "return Result(Outcome.KILLED if test_result.failures or self.raises else Outcome.SURVIVED)",
            "a declaration alone counts as a kill, so a mutation the test passes against is reported as killed",
        )
    )
    def test_a_test_that_passes_against_the_mutation(self):
        """Test that a mutation the registered test passes against is reported as survived, declared error or not."""
        for case, declared in (("nothing declared", ""), ("an error declared", _NAME_ERROR)):
            with self.subTest(case=case):
                mutation = Mutation(mutation_subject.is_even, _EVEN, _SURVIVING, raises=declared)
                self.assertEqual(mutation.check(_SUBJECT_TEST_NAME), Result(Outcome.SURVIVED))

    def test_a_mutation_whose_file_and_test_are_in_different_packages(self):
        """Test that the package of the mutated file is purged as well as the package of its test.

        The module between the two is imported first, being the one that holds a binding to the mutated function.
        Without it in `sys.modules`, purging either package alone would do just as well and this would pass.
        """
        importlib.import_module("update_time.domain.staleness")
        mutation = Mutation(
            timestamp.days_since,
            "return (datetime.now(UTC) - timestamp).days",
            "return (datetime.now(UTC) - timestamp).days + 1",
        )
        staleness_test_name = "tests.update_time.domain.test_staleness.IsStaleTest.test_boundary_compares_whole_days"
        self.assertEqual(mutation.check(staleness_test_name), Result(Outcome.KILLED))

    @kills(
        Mutation(
            checker._loaded_test,
            "return unittest.TestLoader().loadTestsFromName(test_name)\n    except Exception as error:",
            "return unittest.TestLoader().loadTestsFromName(test_name)\n    except SyntaxError as error:",
            "a mutation the test module cannot import escapes the check rather than being reported as broken",
            raises="AttributeError: 'function' object has no attribute '_registered_mutations'",
        ),
        Mutation(
            checker._loaded_test,
            "return unittest.TestLoader().loadTestsFromName(test_name)\n"
            "    except Exception as error:\n"
            "        raise _SourceError(_reason(error)) from error",
            "return unittest.TestLoader().loadTestsFromName(test_name)\n"
            "    except Exception as error:\n"
            "        raise _SourceError() from error",
            "a mutation the test module cannot import is reported as broken without naming the error",
        ),
    )
    def test_a_mutation_that_cannot_be_judged_is_reported_as_broken(self):
        """Test that a mutation the check could not judge is reported as broken rather than as survived."""
        deletes_the_registration = Mutation(
            checker.kills,
            "setattr(wrapper, _REGISTERED, mutations)",
            "delattr(wrapper, _REGISTERED)",
        )
        for case, mutation, error in (
            (
                "the mutated source does not import",
                Mutation(mutation_subject.is_even, _EVEN, _UNPARSABLE),
                "SyntaxError",
            ),
            ("the test errors", Mutation(mutation_subject.is_even, _EVEN, _ERRORING), "NameError"),
            ("the test module raises when imported", deletes_the_registration, "AttributeError"),
        ):
            with self.subTest(case=case):
                result = mutation.check(_SUBJECT_TEST_NAME)
                self.assertEqual(result.outcome, Outcome.BROKEN)
                self.assertIn(error, result.reason)

    @kills(
        Mutation(
            checker.Mutation.check,
            "return Result(Outcome.BROKEN, str(error))",
            "return Result(Outcome.KILLED if str(error) == self.raises else Outcome.BROKEN, str(error))",
            "a mutation whose source does not import counts as killed where the declaration names the import's error",
        )
    )
    def test_a_source_that_does_not_import_though_its_error_is_declared(self):
        """Test that a mutation whose source does not import is reported as broken though it declares that error."""
        reported = Mutation(mutation_subject.is_even, _EVEN, _UNPARSABLE).check(_SUBJECT_TEST_NAME).reason
        declaring = Mutation(mutation_subject.is_even, _EVEN, _UNPARSABLE, raises=reported)
        self.assertEqual(declaring.check(_SUBJECT_TEST_NAME), Result(Outcome.BROKEN, reported))

    @kills(
        Mutation(
            checker.Mutation.check,
            "return Result(Outcome.KILLED) if raised == self.raises else Result(Outcome.BROKEN, raised)",
            "return Result(Outcome.BROKEN, raised)",
            "a test that kills a mutation by raising the error the mutation declares is reported as broken",
        )
    )
    def test_a_test_that_raises_the_declared_error(self):
        """Test that a mutation whose test raises the declared error is reported as killed."""
        mutation = Mutation(mutation_subject.is_even, _EVEN, _ERRORING, raises=_NAME_ERROR)
        self.assertEqual(mutation.check(_SUBJECT_TEST_NAME), Result(Outcome.KILLED))

    @kills(
        Mutation(
            checker.Mutation.check,
            "Result(Outcome.BROKEN, raised)",
            "Result(Outcome.BROKEN)",
            "a mutation reported as broken does not name the error the test raised, which is the line to declare",
        )
    )
    def test_a_test_that_raises_an_undeclared_error(self):
        """Test that a mutation whose test raises an undeclared error is reported as broken, naming the error."""
        declared = "TypeError: an error the test does not raise"
        self.assertEqual(
            Mutation(mutation_subject.is_even, _EVEN, _ERRORING, raises=declared).check(_SUBJECT_TEST_NAME),
            Result(Outcome.BROKEN, _NAME_ERROR),
        )

    @kills(
        Mutation(
            checker.Mutation.check,
            "except _SourceError as error:",
            "except Exception as error:",
            "an error in the checker itself is caught and misreported as a broken mutation",
        )
    )
    def test_a_defect_in_the_checker_itself(self):
        """Test that an error from the checker is raised, rather than reported as the mutation being broken."""
        with patch.object(Mutation, "_purge", Mock(side_effect=RuntimeError("the checker is broken"))):
            self.assertRaises(RuntimeError, Mutation(mutation_subject.is_even, _EVEN, _ODD).check, _SUBJECT_TEST_NAME)

    def test_the_modules_are_left_as_they_were(self):
        """Test that a check restores sys.modules, whether the mutated source ran or raised instead.

        The outcome is asserted as well, since a check that returned before touching `sys.modules` would leave it
        alone too, and pass on that alone.
        """
        for case, new, outcome in (("ran", _ODD, Outcome.KILLED), ("raised", "number %", Outcome.BROKEN)):
            with self.subTest(case=case):
                imported = dict(sys.modules)
                result = Mutation(mutation_subject.is_even, _EVEN, new).check(_SUBJECT_TEST_NAME)
                self.assertEqual(result.outcome, outcome)
                names = sys.modules.keys() | imported.keys()
                changed = [name for name in names if sys.modules.get(name) is not imported.get(name)]
                self.assertEqual(changed, [])

    @kills(
        Mutation(
            checker.Mutation._mutated,
            "raise StaleError(_reason(error)) from error",
            "raise StaleError from error",
            "a mutation naming an unreadable file is reported as stale without saying why",
        )
    )
    def test_a_mutation_whose_file_cannot_be_read(self):
        """Test that a mutation naming a file that cannot be read is reported as stale, and says so."""
        unreadable = Mock(side_effect=FileNotFoundError(2, "No such file or directory"))
        with patch("pathlib.Path.read_text", unreadable):
            result = Mutation(mutation_subject.is_even, _EVEN, _ODD).check(_SUBJECT_TEST_NAME)
        self.assertEqual(result.outcome, Outcome.STALE)
        self.assertIn("FileNotFoundError: [Errno 2] No such file or directory", result.reason)

    @kills(
        Mutation(
            checker._definitions,
            "raise StaleError(_reason(error)) from error",
            "raise error",
            "the run ends with a traceback where a source no longer parses, rather than reporting it stale",
            raises="SyntaxError: '(' was never closed",
        )
    )
    def test_a_mutation_whose_source_does_not_parse(self):
        """Test that a mutation whose file no longer parses is reported as stale, and says so, whatever its anchor."""
        for case, mutation in (
            ("function anchor", Mutation(Doubler.doubled, _DOUBLING, _TRIPLING)),
            ("module anchor", Mutation(mutation_subject, "def broken(", "def mended(")),
        ):
            with self.subTest(case=case), patch("pathlib.Path.read_text", Mock(return_value="def broken(")):
                result = mutation.check(_DOUBLER_TEST_NAME)
                self.assertEqual(result.outcome, Outcome.STALE)
                self.assertIn("SyntaxError", result.reason)

    def test_a_module_anchor_on_the_last_function_of_a_file_without_a_final_newline(self):
        """Test that a module anchor on the last function's snippet is stale, though the file lacks a final newline."""
        with patch("pathlib.Path.read_text", Mock(return_value="def last():\n    return 1")):
            result = Mutation(mutation_subject, "return 1", "return 2").check(_SUBJECT_TEST_NAME)
        self.assertEqual(result, Result(Outcome.STALE, "the snippet lies inside last, so anchor the mutation on last"))

    @kills(
        Mutation(
            checker._definitions,
            "definitions.setdefault(name, node)",
            "definitions[name] = node",
            "a setter hides its getter, so a module anchor is accepted around a snippet the getter holds",
        )
    )
    def test_a_module_anchor_on_a_getter_whose_property_has_a_setter(self):
        """Test that a module anchor on a getter's snippet is stale, though the setter shares the getter's name."""
        source = (
            "class A:\n    @property\n    def x(self):\n        return self._x + 1\n\n"
            "    @x.setter\n    def x(self, value):\n        self._x = value\n"
        )
        with patch("pathlib.Path.read_text", Mock(return_value=source)):
            result = Mutation(mutation_subject, "self._x + 1", "self._x + 2").check(_DOUBLER_TEST_NAME)
        self.assertEqual(result, Result(Outcome.STALE, "the snippet lies inside A.x, so anchor the mutation on A.x"))

    @kills(
        Mutation(
            checker.mutated_source,
            "if new == old:",
            "if False:",
            "a mutation that changes nothing is reported as survived, blaming the test rather than the registration",
        )
    )
    def test_a_mutation_that_would_change_nothing(self):
        """Test that a replacement equal to the snippet is reported as stale, rather than as the test's failing."""
        result = Mutation(mutation_subject.is_even, _EVEN, _EVEN).check(_SUBJECT_TEST_NAME)
        self.assertEqual(result.outcome, Outcome.STALE)
        self.assertEqual(result.reason, "the snippet and its replacement are the same, so nothing changes")

    @kills(
        Mutation(
            checker.mutated_source,
            "if old[:1] in",
            "if False and old[:1] in",
            "a registration pads its snippet with indentation, hiding the token the mutation changes",
        ),
        Mutation(
            checker.mutated_source,
            'in (" ", "\\t")',
            'in (" ",)',
            "a registration pads its snippet with tabs, hiding the token the mutation changes",
        ),
        Mutation(
            checker.mutated_source,
            "new[:1] == old[:1]:",
            "True:",
            "a mutation deleting a whole line is rejected, though the line's indentation is part of what it deletes",
        ),
    )
    def test_a_snippet_and_replacement_opening_on_the_same_indentation(self):
        """Test that a snippet and a replacement opening on the same indentation are reported as stale."""
        stale = Result(
            Outcome.STALE,
            "the snippet and its replacement open on the same indentation, so drop that indentation from both",
        )
        for case, indentation in (("spaces", "    "), ("a tab", "\t")):
            with self.subTest(case=case):
                old, new = f"{indentation}return {_EVEN}", f"{indentation}return {_ODD}"
                self.assertEqual(Mutation(is_even, old, new).check(_SUBJECT_TEST_NAME), stale)
        deleted_line = Mutation(is_even, f"    return {_EVEN}\n", "")
        self.assertEqual(deleted_line.check(_SUBJECT_TEST_NAME), Result(Outcome.KILLED))

    @kills(
        Mutation(
            checker._function_holding,
            "first <= start and end <= last",
            "False",
            "a module anchor is accepted around a snippet one function holds, so the anchor claims the whole file",
        ),
        _CLASSES_SKIPPED,
        Mutation(
            checker._named,
            'f"{prefix}{node.name}."',
            "prefix",
            "the message names a method without its class, and an anchor cannot reach the method by that name",
        ),
        Mutation(
            checker._named,
            'yield f"{prefix}{node.name}", node',
            'yield from _named(node.body, f"{prefix}{node.name}.")\n            yield f"{prefix}{node.name}", node',
            "the message names a nested function instead of the function around it, and an anchor cannot reach it",
        ),
        _SPAN_STOPS_BEFORE_THE_NEWLINE,
    )
    def test_a_module_anchor_on_a_snippet_one_function_holds(self):
        """Test that a module anchor on a snippet one function or method holds is reported as stale, naming it."""
        for case, old, new, test_name, function in (
            ("function", _EVEN, _ODD, _SUBJECT_TEST_NAME, "is_even"),
            ("method", "cls().doubled(2)", "cls().doubled(3)", _TWO_DOUBLED_TEST_NAME, "Doubler.two_doubled"),
            ("nested function", "count > 0", "count > 1", _SUBJECT_TEST_NAME, "is_positive_even"),
            ("ending in the newline after the last line", f"{_EVEN}\n", f"{_ODD}\n", _SUBJECT_TEST_NAME, "is_even"),
        ):
            with self.subTest(case=case):
                result = Mutation(mutation_subject, old, new).check(test_name)
                self.assertEqual(result.outcome, Outcome.STALE)
                self.assertEqual(
                    result.reason, f"the snippet lies inside {function}, so anchor the mutation on {function}"
                )

    @kills(
        Mutation(
            checker._function_holding,
            "first <= start and end <= last",
            "True",
            "a module anchor is rejected around a snippet outside every function, though a function cannot anchor it",
        ),
        Mutation(
            checker._node_span,
            "_offset(source, node.lineno, node.col_offset)",
            "_offset(source, min([node.lineno, "
            '*(decorator.lineno for decorator in getattr(node, "decorator_list", []))]), 0)',
            "a span starts at the decorators, so a snippet starting at one is rejected, though its function lacks it",
        ),
        Mutation(
            checker._function_holding,
            "first <= start and end <= last",
            "first <= start <= last",
            "a snippet starting in one function and ending in the next is rejected as if the first held it all",
        ),
    )
    def test_a_module_anchor_on_a_snippet_no_function_holds_on_its_own(self):
        """Test that a module anchor on a snippet no function or method holds on its own is applied."""
        for case, old, new in (
            (
                "decorator",
                "@property\n    def one_doubled(self)",
                "@property  # applied\n    def one_doubled(self)",
            ),
            ("two functions", "== 0\n\n\ndef is_positive_even", "== 0\n\n\n\ndef is_positive_even"),
        ):
            with self.subTest(case=case):
                result = Mutation(mutation_subject, old, new).check(_SUBJECT_TEST_NAME)
                self.assertEqual(result, Result(Outcome.SURVIVED))

    @kills(
        Mutation(
            checker.mutated_source,
            "if (occurrences := source[start:end].count(old)) != 1:",
            "if not source[start:end].count(old):\n"
            "        start, end = 0, len(source)\n"
            "    if (occurrences := source[start:end].count(old)) != 1:",
            "the checker falls back to the whole file, so a mutation lands outside the anchor that named it",
        )
    )
    def test_a_snippet_the_anchor_does_not_hold_exactly_once(self):
        """Test that a snippet the anchor holds never, or more than once, is reported as stale, saying how often."""
        for case, anchor, old, occurrences in (
            ("absent from the file", mutation_subject, "number % 3 == 0", 0),
            ("repeated in the file", mutation_subject, "number", 3),
            ("in the file but outside the anchored function", is_multiple_of_three, _EVEN, 0),
        ):
            with self.subTest(case=case):
                result = Mutation(anchor, old, "count").check(_SUBJECT_TEST_NAME)
                self.assertEqual(result.outcome, Outcome.STALE)
                self.assertEqual(result.reason, f"the snippet occurs {occurrences} times rather than once")

    @kills(
        Mutation(
            checker._span,
            "if definition is None:",
            "if False:",
            "the checker reads the offsets of a definition it did not find, so the run ends with a traceback",
            raises="AttributeError: 'NoneType' object has no attribute 'lineno'",
        )
    )
    def test_an_anchor_the_source_does_not_define(self):
        """Test that an anchor the source does not define is reported as stale, naming the anchor."""
        moved_on = Mock(return_value='"""A source defining nothing that an anchor names."""\n')
        with patch("pathlib.Path.read_text", moved_on):
            result = Mutation(Doubler.doubled, _DOUBLING, _TRIPLING).check(_DOUBLER_TEST_NAME)
        self.assertEqual(result.outcome, Outcome.STALE)
        self.assertEqual(result.reason, "the source does not define Doubler.doubled")


def _stand_in(_test_case: unittest.TestCase, *_args: object) -> None:
    """Stand in for the test that a registration under test decorates."""


class CapturedMethodsTest(unittest.TestCase):
    """Unit tests for the `Path` methods the module holds when it is imported."""

    @staticmethod
    def import_copy() -> None:
        """Import a copy of the mutation module under a name of its own, leaving the one the suite runs on alone."""
        loader = importlib.machinery.SourceFileLoader("mutation_imported_afresh", checker.__file__)
        loader.exec_module(types.ModuleType(loader.name))

    def test_importing_while_a_path_method_is_patched_fails(self):
        """Test that importing fails while a `Path` method is patched, since the module would hold the patch."""
        for method in ("exists", "read_text", "write_text"):
            with self.subTest(patched=method), patch(f"pathlib.Path.{method}", Mock()):
                self.assertRaises(TypeError, self.import_copy)

    def test_importing_succeeds_while_nothing_is_patched(self):
        """Test that importing a copy succeeds while nothing is patched, so a refusal names the patch, not the copy."""
        self.import_copy()

    def test_importing_succeeds_inside_a_mutation_check(self):
        """Test that importing succeeds inside a mutation check, whose test may have patched a `Path` method."""
        with patch("pathlib.Path.exists", Mock()), patch_environ({CHECKED_TEST: _SUBJECT_TEST_NAME}):
            self.import_copy()


class RecordSurvivalTest(unittest.TestCase):
    """Unit tests for the record kept of registrations that survived."""

    _HEADING = "# Registrations that survived\n\n"

    @contextlib.contextmanager
    def survivals(self, recorded: str | None = None) -> Iterator[pathlib.Path]:
        """Yield the file `_record_survival` writes to, holding `recorded`, or standing for one that is not there.

        A real file, since a stand-in answers with its own methods rather than the patched ones to get past.
        """
        with tempfile.TemporaryDirectory() as directory:
            survivals = pathlib.Path(directory) / "mutation-survivals.md"
            if recorded is not None:
                survivals.write_text(recorded)
            with patch.object(checker, "_SURVIVALS", survivals):
                yield survivals

    def record(self, mutation: Mutation, recorded: str | None = None) -> str:
        """Return what the file holds after recording the mutation's survival."""
        with self.survivals(recorded) as survivals:
            _record_survival("tests.test_module.Case.test_name", mutation)
            return survivals.read_text()

    def written(self, recorded: str | None = None) -> str:
        """Return what recording a survived registration of a production module leaves the file holding."""
        return self.record(Mutation(timestamp, "old", "new", _REGRESSION), recorded)

    def test_the_entry_names_the_day_the_test_and_the_regression(self):
        """Test that the entry dates the survival and names the test that stopped guarding, and against what."""
        written = self.written(self._HEADING)
        self.assertIn("`tests.test_module.Case.test_name`", written)
        self.assertIn(_REGRESSION, written)
        self.assertIn(f"{datetime.now(UTC):%Y-%m-%d}", written)

    def test_a_file_holding_nothing_yet_is_given_its_heading(self):
        """Test that the first entry carries the heading, and that a file that exists keeps what it holds."""
        self.assertTrue(self.written().startswith(self._HEADING))
        earlier = f"{self._HEADING}- an earlier entry\n"
        self.assertTrue(self.written(earlier).startswith(earlier))

    def test_an_entry_the_file_already_holds_is_not_repeated(self):
        """Test that re-running the suite while the test is fixed adds no second entry for the same day."""
        already = self._HEADING + self.written(self._HEADING).removeprefix(self._HEADING)
        self.assertEqual(self.record(Mutation(timestamp, "old", "new", _REGRESSION), already), already)

    def test_the_entries_survive_a_test_that_patches_pathlib(self):
        """Test that recording keeps what the file holds, though the test being checked patches a `Path` method."""
        earlier = f"{self._HEADING}- an earlier entry\n"
        fixture = "a fixture the test being checked reads"
        for target, replacement in (
            ("pathlib.Path.exists", Mock(return_value=False)),
            ("pathlib.Path.read_text", Mock(return_value=fixture)),
            ("pathlib.Path.write_text", Mock()),
        ):
            with self.subTest(patched=target), self.survivals(earlier) as survivals:
                mutation = Mutation(timestamp, "old", "new", _REGRESSION)
                with patch(target, replacement):
                    _record_survival("tests.test_module.Case.test_name", mutation)
                recorded = survivals.read_text()
                self.assertTrue(recorded.startswith(earlier))  # What the file held is still there ...
                self.assertIn(_REGRESSION, recorded)  # ... and the entry reached the file rather than the patch.

    def test_a_mutation_of_a_test_module_is_passed_over(self):
        """Test that the framework's own targets, which survive by design, are recorded not at all."""
        self.assertEqual(self.record(_ODD_REPORTED_AS_EVEN, self._HEADING), self._HEADING)


class FailureMessageTest(unittest.TestCase):
    """Unit tests for the message that reports a mutation the test did not kill."""

    @kills(
        Mutation(
            checker.Mutation._anchor_name,
            '".".join(filter(None, (self._module.__name__, self._qualified_name)))',
            "self._module.__name__",
            "a report names the module alone, so it does not say which of its anchors went stale",
        ),
        Mutation(
            checker.Mutation._anchor_name,
            "filter(None, (self._module.__name__, self._qualified_name))",
            "(self._module.__name__, self._qualified_name)",
            "a module anchor is reported with a trailing dot, as though a definition were missing from the name",
        ),
    )
    def test_the_message_names_the_anchor(self):
        """Test that the message names the definition the mutation is anchored to, and the module where none is."""
        for anchor, name in (
            (mutation_subject, "tests.mutation_subject"),
            (is_even, "tests.mutation_subject.is_even"),
            (Doubler.doubled, "tests.mutation_subject.Doubler.doubled"),
        ):
            with self.subTest(name=name):
                mutation = Mutation(anchor, _EVEN, _ODD, _REGRESSION)
                survived, stale = Result(Outcome.SURVIVED), Result(Outcome.STALE, "why")
                self.assertEqual(
                    _failure(mutation, survived), f"{_REGRESSION} — the test did not kill this mutation of {name}"
                )
                self.assertEqual(_failure(mutation, stale), f"{_REGRESSION} — this mutation of {name} is stale: why")


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
            checker._fail_unless_killed,
            "for mutation in mutations:",
            "for mutation in mutations[:1]:",
            "only the first of the mutations a registration holds is checked",
        ),
        Mutation(
            checker._fail_unless_killed,
            "result = mutation.check(test_name)",
            'result = mutation.check(test_name.split(".")[-1])',
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
                call(_ONLY_THREE_REPORTED_AS_EVEN, self.decorated_id),
            ],
        )

    @kills(
        Mutation(
            checker._failure,
            "{mutation.regression} — the test did not kill",
            "the test did not kill",
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
            checker._fail_unless_killed,
            "with test_case.subTest(regression=mutation.regression):",
            "if True:",
            "a test stops at the first mutation it did not kill, leaving every mutation after it unreported",
        ),
        Mutation(
            checker._fail_unless_killed,
            "with test_case.subTest(regression=mutation.regression):",
            "with test_case.subTest():",
            "a mutation the test did not kill is reported without naming which mutation it was",
        ),
        Mutation(
            checker._fail_unless_killed,
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
            checker._failure,
            "{mutation.regression} — this mutation of",
            "this mutation of",
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
                    f"{_REGRESSION} — this mutation of {is_even.__module__}.{is_even.__name__} is {outcome}: {reason}",
                    message,
                )

    def test_a_test_failing_of_its_own_accord_is_not_checked(self):
        """Test that a test already failing fails on that, rather than its mutation being reported as killed."""
        with patch("tests.test_mutation.is_even", Mock(return_value=True)):
            result = self.run_decorated_test(Result(Outcome.KILLED))
        self.assertEqual(len(result.failures), 1)
        self.checked.assert_not_called()

    @kills(
        Mutation(
            checker.kills,
            'if os.environ.get(CHECKED_TEST) == self.id() or os.environ.get(CHECKS_OFF) == "1":',
            "if os.environ.get(CHECKS_OFF):",
            "a test re-run against its own mutation checks its mutations again, so a run never ends",
        ),
        Mutation(
            checker.kills,
            'if os.environ.get(CHECKED_TEST) == self.id() or os.environ.get(CHECKS_OFF) == "1":',
            "if os.environ.get(CHECKED_TEST) == self.id():",
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
            helpers.patch_environ,
            "for sentinel in (CHECKS_OFF, CHECKED_TEST):",
            "for sentinel in (CHECKED_TEST,):",
            "a test whose class clears the environment loses the sentinel, so `just mutate` checks it after all",
        ),
        Mutation(
            helpers.patch_environ,
            "for sentinel in (CHECKS_OFF, CHECKED_TEST):",
            "for sentinel in (CHECKS_OFF,):",
            "a rerun that clears the environment checks its registrations again, so a survivor passes as killed",
        ),
    )
    def test_the_sentinels_survive_a_cleared_environment(self):
        """Test that patching the environment keeps both mutation-check sentinels, whatever else it clears."""
        for sentinel, value in ((CHECKS_OFF, "1"), (CHECKED_TEST, _SUBJECT_TEST_NAME)):
            with self.subTest(sentinel), patch.dict(os.environ, {sentinel: value}, clear=True), patch_environ():
                self.assertEqual(os.environ.get(sentinel), value)

    @kills(
        Mutation(
            checker.kills,
            'if os.environ.get(CHECKED_TEST) == self.id() or os.environ.get(CHECKS_OFF) == "1":',
            "if os.environ.get(CHECKED_TEST) or os.environ.get(CHECKS_OFF):",
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
            checker.kills,
            "setattr(wrapper, _REGISTERED, mutations)",
            "pass",
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
            checker.kills,
            "if not mutations:",
            "if False:",
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
            checker.kills,
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
