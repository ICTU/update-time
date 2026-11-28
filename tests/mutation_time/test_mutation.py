"""Unit tests for making a test kill the mutations it is meant to kill."""

import functools
import importlib
import importlib.machinery
import os
import sys
import types
import unittest
from typing import TYPE_CHECKING, Final
from unittest.mock import Mock, call, patch

from mutation_time import mutation as checker
from mutation_time.mutation import (
    _CHECKED_TEST,
    _REGISTERED,
    CHECKS_OFF,
    OUTSIDE,
    Case,
    KillsTestLoader,
    Mutation,
    Outcome,
    _failure,
    _MutationResult,
    kills,
    registered_mutations,
)
from update_time.primitives.timestamp import days_since

import tests.mutation_time.mutation_subject_package.limit
from tests.environment import patch_environ

from . import mutation_subject, mutation_subject_package
from .mutation_subject import (
    FIVE,
    Banner,
    DelegatingDoubler,
    Doubler,
    Greeter,
    first,
    is_even,
    is_multiple_of_five,
    is_multiple_of_ten,
    is_multiple_of_three,
    is_odd,
    is_positive_even,
    scaled,
)
from .mutation_subject_package import step, tally

if TYPE_CHECKING:
    from collections.abc import Callable

    from mutation_time.mutation import Registration

_EVEN: Final[str] = "number % 2 == 0"
_ODD: Final[str] = "number % 2 != 0"
# The doubler's two methods end on this snippet. So its class holds it twice, and each method holds it once.
_DOUBLING: Final[str] = "value * 2"
_TRIPLING: Final[str] = "value * 3"
_SUBJECT_TEST_NAME: Final[str] = f"{__name__}.IsEvenTest.test_an_even_number"
_TABLE_TEST_NAME: Final[str] = f"{__name__}.IsEvenTableTest.test_odd_numbers"
_TABLE_THEN_EVEN_TEST_NAME: Final[str] = f"{__name__}.IsEvenTableTest.test_odd_numbers_then_an_even_one"
_PARAMETERS_TABLE_TEST_NAME: Final[str] = f"{__name__}.IsEvenTableTest.test_odd_numbers_by_parameters"
# The cases of the `IsEvenTableTest` tables that name each case by a parameter called `case`.
_TABLE_CASES: Final[tuple[Case, ...]] = tuple(
    Case(message=None, params={"case": name}, description=f"(case={name!r})") for name in ("three", "five")
)
_UNNAMED_CASE: Final[Case] = Case(
    message=None, params={"case": "seven"}, description="(case='seven')"
)  # A case no registration of those tables names.
_EVEN_THEN_TABLE_TEST_NAME: Final[str] = f"{__name__}.IsEvenTableTest.test_two_then_odd_numbers"
_SHARED_MESSAGE_TABLE_TEST_NAME: Final[str] = f"{__name__}.IsEvenTableTest.test_odd_numbers_by_a_shared_message"
_TWICE_TABLE_TEST_NAME: Final[str] = f"{__name__}.IsEvenTableTest.test_odd_numbers_twice"
_NESTED_TABLE_TEST_NAME: Final[str] = f"{__name__}.IsEvenTableTest.test_odd_numbers_in_groups"
_MESSAGE_TABLE_TEST_NAME: Final[str] = f"{__name__}.IsEvenTableTest.test_odd_numbers_by_message"
# The module-level assignment of the subject that the tests of a mutated assignment change.
_FIVE_ASSIGNMENT: Final[str] = "FIVE: Final[int] = 5"
_FIVE_TEST_NAME: Final[str] = f"{__name__}.IsMultipleOfFiveTest.test_a_multiple_of_five"
_MULTIPLES_TEST_NAME: Final[str] = f"{__name__}.IsMultipleOfFiveTest.test_multiples_of_five_and_ten"
_DOUBLER_TEST_NAME: Final[str] = f"{__name__}.DoublerTest.test_a_doubled_value"
_ONE_DOUBLED_TEST_NAME: Final[str] = f"{__name__}.DoublerTest.test_one_doubled"
_TWO_DOUBLED_TEST_NAME: Final[str] = f"{__name__}.DoublerTest.test_two_doubled"
_THREE_DOUBLED_TEST_NAME: Final[str] = f"{__name__}.DoublerTest.test_three_doubled"
_ODD_TEST_NAME: Final[str] = f"{__name__}.IsOddTest.test_an_odd_number"
_DELEGATING_TEST_NAME: Final[str] = f"{__name__}.DelegatingDoublerTest.test_a_doubled_value"
_GREETER_TEST_NAME: Final[str] = f"{__name__}.GreeterTest.test_a_greeting"
_SCALED_TEST_NAME: Final[str] = f"{__name__}.ScaledTest.test_a_value_scaled_by_the_defaults"
_INDENTED_BANNER_TEST_NAME: Final[str] = f"{__name__}.BannerTest.test_an_indented_banner"
_FLUSH_LEFT_BANNER_TEST_NAME: Final[str] = f"{__name__}.BannerTest.test_a_flush_left_banner"
_COUNTER_TEST_NAME: Final[str] = f"{__name__}.CounterTest.test_one_step"
_TALLY_TEST_NAME: Final[str] = f"{__name__}.TallyTest.test_the_summary"
_LIMIT_TEST_NAME: Final[str] = f"{__name__}.LimitTest.test_the_value"
# The `IsEvenTest` tests `KillsTest` runs to exercise the decorator: one registers a single mutation, one several,
# one asserts inside a subTest, one has a patch above its decorator, and two stack their registrations, the second
# with a patch between them. `IsEvenPatchedInSetUpTest` gives its decorated test the name of the first.
_DECORATED_TEST: Final[str] = "test_an_odd_number"
# The `IsEvenTableTest` tests `KillsTest` runs to exercise the checks for what the registrations leave unnamed. The
# suite does not run the first as a test, since the registration it leaves unnamed fails it.
_PARTLY_NAMED_TEST: Final[str] = "odd_numbers_partly_named_by_a_registration"
_OUTSIDE_NAMED_TEST: Final[str] = "test_odd_numbers_and_two_each_named_by_a_registration"
_SEVERAL_MUTATIONS_TEST: Final[str] = "test_an_odd_number_against_several_mutations"
_STACKED_TEST: Final[str] = "test_an_odd_number_against_stacked_registrations"
_STACKED_ACROSS_A_PATCH_TEST: Final[str] = "test_an_odd_number_against_registrations_stacked_across_a_patch"
_SUBTEST_DECORATED_TEST: Final[str] = "test_an_odd_number_in_a_subtest"
_PATCHED_ABOVE_TEST: Final[str] = "test_an_odd_number_patched_above_the_decorator"
_REGRESSION: Final[str] = "an odd number is reported as even"
# A regression for the mutations that serve as test data. No assertion reads it.
_ANY_REGRESSION: Final[str] = "the mutated code misbehaves"
# Make a mutation serving as test data.
_MUTATION: Final[functools.partial[Mutation]] = functools.partial(Mutation, regression=_ANY_REGRESSION)
_KILLED: Final[_MutationResult] = _MutationResult(outcome=Outcome.KILLED)
# Make the result of a mutation that is stale for the reason given.
_STALE: Final[functools.partial[_MutationResult]] = functools.partial(_MutationResult, outcome=Outcome.STALE)
_SURVIVED: Final[_MutationResult] = _MutationResult(outcome=Outcome.SURVIVED)
_NAMED_CASE: Final[str] = "the named case"  # A case for a registration under test to name.
_RAISING_REGRESSION: Final[str] = "deciding whether a number is even raises instead of answering"
_ONLY_THREE: Final[str] = "number == 3"
_ONLY_THREE_REGRESSION: Final[str] = "only the number three is reported as even"
# A replacement that leaves the subject importable but raises when the test calls it, and the error it raises.
_ERRORING: Final[str] = "nonexistent"
_NAME_ERROR: Final[str] = "NameError: name 'nonexistent' is not defined"
_ZERO_DIVISION: Final[str] = "ZeroDivisionError: division by zero"  # What a replacement dividing by zero raises.
# A replacement that makes deciding whether two is even raise `_NAME_ERROR`, and leaves the other numbers alone.
_TWO_RAISES: Final[str] = f"(number != 2 or {_ERRORING}) and {_EVEN}"
_UNPARSABLE: Final[str] = "number %"  # A replacement that leaves the subject unparsable, so it does not import at all.
_SURVIVING: Final[str] = "number == 2"  # A replacement the test passes against, so nothing it asserts breaks.
_ODD_REPORTED_AS_EVEN: Final[Mutation] = Mutation(anchor=is_even, old=_EVEN, new=_ODD, regression=_REGRESSION)
_KILLS_ODD_REPORTED_AS_EVEN: Final[Registration] = kills.replace(
    anchor=is_even, old=_EVEN, new=_ODD, regression=_REGRESSION
)
_ONLY_THREE_REPORTED_AS_EVEN: Final[Mutation] = Mutation(
    anchor=is_even, old=_EVEN, new=_ONLY_THREE, regression=_ONLY_THREE_REGRESSION
)
_KILLS_ONLY_THREE_REPORTED_AS_EVEN: Final[Registration] = kills.replace(
    is_even, _EVEN, _ONLY_THREE, _ONLY_THREE_REGRESSION
)


# The regressions of the walk over a source's definitions that both an anchor and the anchor check rely on.
def _kills_classes_skipped(case: str | None = None) -> Registration:
    """Return a decorator registering the mutation that makes the walk over the definitions skip classes."""
    return kills.delete_statement(
        checker._named,
        'yield from _named(node.body, f"{prefix}{node.name}.")',
        "the walk skips classes, so a method is neither found for its anchor nor seen by the check",
        case=case,
    )


def _kills_span_stopping_before_the_newline(case: str | None = None) -> Registration:
    """Return a decorator registering the mutation that ends a definition's span before its last newline."""
    return kills.delete(
        checker._node_span,
        '    if source.startswith("\\n", end):\n        end += 1\n',
        "a span stops before the newline ending a definition's last line, so a snippet ending in it escapes both",
        case=case,
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

    @_KILLS_ODD_REPORTED_AS_EVEN
    def test_an_odd_number(self):
        """Test that an odd number is not even."""
        self.assertFalse(is_even(3))

    @kills.replace(is_even, _EVEN, _ODD, _REGRESSION, case="(number=3)")
    def test_an_odd_number_in_a_subtest(self):
        """Test that an odd number is not even, asserting inside a subTest."""
        with self.subTest(number=3):
            self.assertFalse(is_even(3))

    @_KILLS_ODD_REPORTED_AS_EVEN
    @patch("tests.mutation_time.mutation_subject.is_even")
    def test_an_odd_number_patched_beneath_the_decorator(self, patched_subject: Mock):
        """Test that an odd number is not even, with a patch handing this test a mock from beneath the decorator.

        The patch replaces the subject module's attribute, while the binding this module imported is the one the
        assertion reaches and the one the mutation breaks.
        """
        self.assertIsInstance(patched_subject, Mock)
        self.assertFalse(is_even(3))

    @patch("tests.mutation_time.mutation_subject.is_even")
    @_KILLS_ODD_REPORTED_AS_EVEN
    def test_an_odd_number_patched_above_the_decorator(self, patched_subject: Mock):
        """Test that an odd number is not even, with a patch above the decorator handing this test a mock.

        The patch calls the decorator's wrapper with the mock, so the wrapper has to hand on what it was called
        with rather than only the test case.
        """
        self.assertIsInstance(patched_subject, Mock)
        self.assertFalse(is_even(3))

    @_KILLS_ODD_REPORTED_AS_EVEN
    @_KILLS_ONLY_THREE_REPORTED_AS_EVEN
    def test_an_odd_number_against_several_mutations(self):
        """Test that an odd number is not even, killing each of the mutations its registration holds."""
        self.assertFalse(is_even(3))

    @_KILLS_ODD_REPORTED_AS_EVEN
    @_KILLS_ONLY_THREE_REPORTED_AS_EVEN
    def test_an_odd_number_against_stacked_registrations(self):
        """Test that an odd number is not even, killing the mutations of two registrations stacked on it."""
        self.assertFalse(is_even(3))

    @_KILLS_ODD_REPORTED_AS_EVEN
    @patch("tests.mutation_time.mutation_subject.is_even")
    @_KILLS_ONLY_THREE_REPORTED_AS_EVEN
    def test_an_odd_number_against_registrations_stacked_across_a_patch(self, patched_subject: Mock):
        """Test that an odd number is not even, killing the mutations of two registrations with a patch between."""
        self.assertIsInstance(patched_subject, Mock)
        self.assertFalse(is_even(3))

    @kills.replace(is_even, _EVEN, _ERRORING, _RAISING_REGRESSION, raises=_NAME_ERROR)
    def test_an_odd_number_against_a_mutation_that_raises(self):
        """Test that an odd number is not even, killing a mutation by raising the error the mutation declares."""
        self.assertFalse(is_even(3))


class IsMultipleOfThreeTest(unittest.TestCase):
    """Unit tests for deciding whether a value is a multiple of three, which an anchored mutation targets."""

    def test_a_multiple_of_three(self):
        """Test that three is a multiple of three."""
        self.assertTrue(is_multiple_of_three(3))


class IsMultipleOfFiveTest(unittest.TestCase):
    """Unit tests for deciding whether a value is a multiple of five, which a mutation of a constant targets."""

    def test_a_multiple_of_five(self):
        """Test that ten is a multiple of five, and seven is not."""
        self.assertTrue(is_multiple_of_five(10))
        self.assertFalse(is_multiple_of_five(7))

    def test_multiples_of_five_and_ten(self):
        """Test that ten is a multiple of five and twenty a multiple of ten, in a case each."""
        for case, is_multiple, value in (("five", is_multiple_of_five, 10), ("ten", is_multiple_of_ten, 20)):
            with self.subTest(case=case):
                self.assertTrue(is_multiple(value))


class ScaledTest(unittest.TestCase):
    """Unit tests for scaling a value, whose defaults a mutation can change."""

    def test_a_value_scaled_by_the_defaults(self):
        """Test that two scaled by the default factor and offset is six."""
        self.assertEqual(scaled(2), 6)


class BannerTest(unittest.TestCase):
    """Unit tests for the banner, whose methods hold a string spanning lines."""

    def test_an_indented_banner(self):
        """Test that the indented banner keeps the indentation of its second line."""
        self.assertEqual(Banner().indented(), "first\n        second")

    def test_a_flush_left_banner(self):
        """Test that the flush-left banner starts its second line at the left margin."""
        self.assertEqual(Banner().flush_left(), "first\nsecond")


class GreeterTest(unittest.TestCase):
    """Unit tests for the greeter, whose method's default names a constant of its class."""

    def test_a_greeting(self):
        """Test that the greeting addresses the name with the class's greeting."""
        self.assertEqual(Greeter().greeting("world"), "hello, world")


class DelegatingDoublerTest(unittest.TestCase):
    """Unit tests for the doubler that delegates to the one it extends, whose method's code needs its class."""

    def test_a_doubled_value(self):
        """Test that two doubled is four."""
        self.assertEqual(DelegatingDoubler().doubled(2), 4)


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


class CounterTest(unittest.TestCase):
    """Unit tests for the counter of a package, which this module reaches as an attribute of the package's parent."""

    def test_one_step(self):
        """Test that the count after one step is one."""
        self.assertEqual(mutation_subject_package.Counter().count(), 1)


class TallyTest(unittest.TestCase):
    """Unit tests for a tally, whose summary a module that imports the tally's module back writes."""

    def test_the_summary(self):
        """Test that the summary of a tally names its start."""
        self.assertEqual(tally.Tally().summary(), "starts at 0")


class LimitTest(unittest.TestCase):
    """Unit tests for a limit, which this module reaches by the dotted name of the limit's module."""

    def test_the_value(self):
        """Test that the limit is ten."""
        self.assertEqual(tests.mutation_time.mutation_subject_package.limit.Limit.value, 10)


class CheckTest(unittest.TestCase):
    """Unit tests for checking a test against the mutation it is meant to kill."""

    @_kills_span_stopping_before_the_newline()
    def test_a_mutation_anchored_to_a_function_is_applied_inside_it(self):
        """Test that a mutation anchored to a function is applied to it, up to the newline after its last line."""
        self.assertEqual(_checked(_MUTATION(anchor=is_even, old=_EVEN, new=_ODD), _SUBJECT_TEST_NAME), (_KILLED, 0))
        self.assertEqual(
            _MUTATION(anchor=is_even, old=f"{_EVEN}\n", new=f"{_ODD}\n").check(_SUBJECT_TEST_NAME), _KILLED
        )

    @_kills_classes_skipped()
    @kills.append(
        checker._span,
        "(qualified_name",
        '.split(".")[-1]',
        "the lookup uses the bare name, so a method is searched for at the top of the module and never found",
    )
    def test_a_mutation_anchored_to_a_method_is_applied_inside_it(self):
        """Test that a mutation anchored to a method is applied to it, though its class holds the snippet twice."""
        ambiguous = _STALE(reason="the snippet occurs 2 times rather than once")
        whole_file = _MUTATION(anchor=mutation_subject, old=_DOUBLING, new=_TRIPLING)
        self.assertEqual(whole_file.check(_DOUBLER_TEST_NAME), ambiguous)
        anchored = _MUTATION(anchor=Doubler.doubled, old=_DOUBLING, new=_TRIPLING)
        self.assertEqual(_checked(anchored, _DOUBLER_TEST_NAME), (_KILLED, 0))

    @kills.delete(
        checker.Mutation._unwrapped,
        'cast("_Function", self.anchor.fget) if isinstance(self.anchor, property) else ',
        "a property anchor is passed on as it is, so the run ends with a traceback",
        raises="AttributeError: 'property' object has no attribute '__module__'. "
        "Did you mean '.__reduce__' instead of '.__module__'?",
    )
    def test_a_mutation_anchored_to_a_property_is_applied_inside_its_getter(self):
        """Test that a mutation anchored to a property reaches the getter, which is what carries its names."""
        mutation = _MUTATION(anchor=Doubler.one_doubled, old="self.doubled(1)", new="self.doubled(2)")
        self.assertEqual(_checked(mutation, _ONE_DOUBLED_TEST_NAME), (_KILLED, 0))

    @kills.replace(
        checker.Mutation._unwrapped,
        'return cast("_Function", self.anchor.func)',
        "return self.anchor",
        "a cached property anchor is passed on as it is, so the run ends with a traceback",
        raises="AttributeError: 'cached_property' object has no attribute '__qualname__'. "
        "Did you mean '.__set_name__' instead of '.__qualname__'?",
    )
    def test_a_mutation_anchored_to_a_cached_property_is_applied_inside_its_function(self):
        """Test that a mutation anchored to a cached property reaches its function, which is what carries its names."""
        mutation = _MUTATION(anchor=Doubler.three_doubled, old="self.doubled(3)", new="self.doubled(4)")
        self.assertEqual(_checked(mutation, _THREE_DOUBLED_TEST_NAME), (_KILLED, 0))

    @kills.delete(
        checker._named,
        "ast.ClassDef, ",
        "a class is left out of the definitions, so a mutation anchored to one is reported stale",
    )
    def test_a_mutation_anchored_to_a_class_is_applied_inside_it(self):
        """Test that a mutation anchored to a class is applied to a snippet in its body, outside its methods."""
        self.assertEqual(_MUTATION(anchor=Greeter, old='"hello"', new='"hi"').check(_DOUBLER_TEST_NAME), _SURVIVED)

    def test_a_mutation_anchored_to_a_function_is_checked_without_executing_its_module_afresh(self):
        """Test that a mutation of a classmethod, cached function, or default kills its test in the loaded module."""
        is_odd(3)  # A check must not serve this cached answer to the test of the mutated function.
        cases = (
            (
                "classmethod",
                _MUTATION(anchor=Doubler.two_doubled, old="cls().doubled(2)", new="cls().doubled(3)"),
                _TWO_DOUBLED_TEST_NAME,
            ),
            ("cached function", _MUTATION(anchor=is_odd, old=_ODD, new=_EVEN), _ODD_TEST_NAME),
            ("a default", _MUTATION(anchor=scaled, old="factor: int = 3", new="factor: int = 4"), _SCALED_TEST_NAME),
            (
                "a keyword-only default",
                _MUTATION(anchor=scaled, old="offset: int = 0", new="offset: int = 1"),
                _SCALED_TEST_NAME,
            ),
        )
        for case, mutation, test_name in cases:
            with self.subTest(case):
                self.assertEqual(_checked(mutation, test_name), (_KILLED, 0))

    @kills.replace(
        checker.Mutation._mutated_assignments,
        '_occurrences(source, "", self.old)[0]',
        "source.index(self.old)",
        "the check takes a docstring repeating a module-level assignment for it, so it executes the module afresh",
    )
    @kills.delete_statement(
        checker.Mutation._run_test_with_names_bound,
        "delattr(module, name)",
        "the check leaves behind the name that the mutated assignment adds to the module",
    )
    def test_a_mutation_of_a_module_level_assignment_is_checked_without_executing_its_module_afresh(self):
        """Test that a mutation of a module-level assignment kills its test in the loaded module, then restores it.

        A docstring above the assignment repeats it. The module gets its name back afterwards, and loses the name only
        the mutated assignment binds.
        """
        mutation = _MUTATION(anchor=mutation_subject, old=_FIVE_ASSIGNMENT, new="FIVE = _SIX = 4")
        self.assertEqual(_checked(mutation, _FIVE_TEST_NAME), (_KILLED, 0))
        self.assertEqual(FIVE, 5)
        self.assertFalse(hasattr(mutation_subject, "_SIX"))

    @kills.force_false(
        checker._names_assigned,
        "isinstance(statement, ast.AnnAssign) and statement.value is not None",
        "a mutation of an annotated module-level assignment executes its module afresh",
    )
    def test_a_mutation_of_an_annotated_assignment_is_checked_without_executing_its_module_afresh(self):
        """Test that a mutation of an annotated module-level assignment kills its test in the loaded module."""
        mutation = _MUTATION(anchor=mutation_subject, old=_FIVE_ASSIGNMENT, new="FIVE: Final[int] = 4")
        self.assertEqual(_checked(mutation, _FIVE_TEST_NAME), (_KILLED, 0))
        self.assertEqual(FIVE, 5)

    @kills.force_true(
        checker.Mutation._run_test,
        "self._failed_in(result)",
        "a mutation of an assignment that survives in place is reported as surviving, though a constant computed "
        "from it still holds the original value",
        case="leaves its test passing",
    )
    @kills.replace(
        checker.Mutation._mutated_assignments,
        "except SyntaxError:",
        "except ValueError:",
        "a mutated assignment that does not parse ends the check with a traceback, rather than being checked afresh",
        raises="SyntaxError: '(' was never closed",
        case="does not parse",
    )
    @kills.replace(
        checker.Mutation._mutated_assignments,
        "except Exception:",
        "except NameError:",
        "a mutated assignment that raises ends the check with a traceback, rather than being checked afresh",
        raises="ZeroDivisionError: division by zero",
        case="raises",
    )
    def test_a_mutation_of_an_assignment_the_loaded_module_cannot_judge_is_checked_afresh(self):
        """Test that a mutated assignment that does not parse, raises, or leaves its test passing is checked afresh."""
        for case, new, outcome in (
            ("does not parse", "FIVE: Final[int] = (5", Outcome.BROKEN),
            ("raises", "FIVE: Final[int] = 5 // 0", Outcome.BROKEN),
            ("leaves its test passing", "FIVE: Final[int] = 4 + 1", Outcome.SURVIVED),
        ):
            with self.subTest(case):
                mutation = _MUTATION(anchor=mutation_subject, old=_FIVE_ASSIGNMENT, new=new)
                result, executed = _checked(mutation, _FIVE_TEST_NAME)
                self.assertEqual((result.outcome, executed), (outcome, 1))

    @kills.replace(
        checker.Mutation._failed_in,
        "return bool(failures)",
        "return bool(run.failures)",
        "a mutated assignment naming a case is judged in place where another case fails, hidden by a derived constant",
        case="a case that passes in place",
    )
    @kills.replace(
        checker.Mutation._failed_in,
        "return False",
        "return True",
        "a mutated assignment naming a case the test lacks is judged in place, without running its module afresh",
        case="a case missing from the test",
    )
    def test_a_mutation_of_an_assignment_naming_a_case_it_passes_in_place_is_checked_afresh(self):
        """Test that a mutated assignment is checked afresh where the case it names passes, or is missing, in place."""
        for case, named, outcome in (
            ("a case that passes in place", "ten", Outcome.KILLED),
            ("a case missing from the test", "eleven", Outcome.STALE),
        ):
            with self.subTest(case):
                mutation = _MUTATION(
                    anchor=mutation_subject, old=_FIVE_ASSIGNMENT, new="FIVE: Final[int] = 4", case=named
                )
                result, executed = _checked(mutation, _MULTIPLES_TEST_NAME)
                self.assertEqual((result.outcome, executed), (outcome, 1))

    def test_a_mutation_leaves_a_string_spanning_the_methods_lines_as_it_is(self):
        """Test that a mutation of a method holding a string spanning lines changes nothing in the string."""
        cases = (
            ("indented", Banner.indented, "indented as the body is", _INDENTED_BANNER_TEST_NAME),
            ("at the left margin", Banner.flush_left, "flush left", _FLUSH_LEFT_BANNER_TEST_NAME),
        )
        for case, method, remark, test_name in cases:
            with self.subTest(case):
                mutation = _MUTATION(anchor=method, old=remark, new=f"{remark}, reworded")
                self.assertEqual(_checked(mutation, test_name), (_SURVIVED, 0))

    def test_a_mutation_of_a_method_whose_definition_needs_its_class_executes_its_module_afresh(self):
        """Test that a mutation of a method whose definition needs its class executes its module afresh."""
        cases = (
            (
                "a method calling super()",
                _MUTATION(
                    anchor=DelegatingDoubler.doubled, old="super().doubled(value * 1)", new="super().doubled(value + 1)"
                ),
                _DELEGATING_TEST_NAME,
            ),
            (
                "a default naming a class constant",
                _MUTATION(anchor=Greeter.greeting, old="{greeting}, {name}", new="{greeting}; {name}"),
                _GREETER_TEST_NAME,
            ),
        )
        for case, mutation, test_name in cases:
            with self.subTest(case):
                self.assertEqual(_checked(mutation, test_name), (_KILLED, 1))

    def test_a_mutation_of_a_package_importing_its_own_submodule_is_killed(self):
        """Test that a mutation of a package whose module imports a submodule of its own reaches the test it breaks."""
        mutation = _MUTATION(anchor=mutation_subject_package, old="start = 0", new="start = 1")
        self.assertEqual(_checked(mutation, _COUNTER_TEST_NAME), (_KILLED, 1))

    @kills.replace(
        checker._executed_module,
        "sys.modules[module.__name__] = executed\n"
        "    try:\n"
        '        exec(compile(source, executed.__file__ or "", "exec"), executed.__dict__)',
        "try:\n"
        '        exec(compile(source, executed.__file__ or "", "exec"), executed.__dict__)\n'
        "        sys.modules[module.__name__] = executed",
        "the mutation survives, since a module importing the mutated one back gets the original",
    )
    def test_a_mutation_of_a_module_that_a_module_it_imports_imports_back_is_killed(self):
        """Test that a mutation reaches the test it breaks through a module that imports the mutated module back."""
        mutation = _MUTATION(anchor=tally, old="start = 0", new="start = 1")
        self.assertEqual(_checked(mutation, _TALLY_TEST_NAME), (_KILLED, 1))

    @kills.delete(
        checker._bind_on_parent,
        "setattr(package, child, module)",
        "a test naming the mutated module by a dotted name does not find it on its parent",
    )
    def test_a_mutation_of_a_module_the_test_reaches_by_its_dotted_name_is_killed(self):
        """Test that a mutation reaches the test it breaks where the test names the mutated module by a dotted name."""
        mutation = _MUTATION(
            anchor=tests.mutation_time.mutation_subject_package.limit, old="value = 10", new="value = 11"
        )
        self.assertEqual(_checked(mutation, _LIMIT_TEST_NAME), (_KILLED, 1))

    def test_a_mutation_of_the_module_holding_the_test_is_killed(self):
        """Test that a mutation reaches the test it breaks where the mutated module holds that test."""
        mutation = _MUTATION(anchor=IsOddTest, old="is_odd(3)", new="is_odd(4)")
        self.assertEqual(_checked(mutation, _ODD_TEST_NAME), (_KILLED, 1))

    @kills.force_false(
        checker._bind_on_parent,
        "not parent",
        "a mutated top-level package is reported as broken, since the check imports an empty parent name",
    )
    def test_a_mutation_of_a_top_level_package_is_checked_afresh(self):
        """Test that a mutation of a package without a parent is checked afresh, with nothing to bind it on."""
        mutation = _MUTATION(anchor=tests, old=" no test may lift the refusal.", new="")
        self.assertEqual(_checked(mutation, _SUBJECT_TEST_NAME), (_SURVIVED, 1))

    @kills.replace(
        checker.Mutation._mutated_function,
        "defined.get(",
        "(vars(self.module) | defined).get(",
        "a mutation renaming its function runs the unmutated function in place, so it survives",
    )
    def test_a_mutation_renaming_its_function_executes_its_module_afresh(self):
        """Test that a mutation renaming the function it is anchored to executes its module afresh, the name gone."""
        result, executed = _checked(
            _MUTATION(anchor=is_even, old="def is_even(", new="def is_even_renamed("), _SUBJECT_TEST_NAME
        )
        self.assertEqual((result.outcome, executed), (Outcome.BROKEN, 1))

    @kills.append(
        checker.Mutation.check,
        "not failures and not errors",
        " and not self.raises",
        "a declaration alone counts as a kill, so a mutation the test passes against is reported as killed",
    )
    def test_a_test_that_passes_against_the_mutation(self):
        """Test that a mutation the registered test passes against is reported as survived, its declared error aside."""
        mutation = _MUTATION(anchor=is_even, old=_EVEN, new=_SURVIVING, raises=_NAME_ERROR)
        self.assertEqual(mutation.check(_SUBJECT_TEST_NAME), _SURVIVED)

    @kills.force_true(
        checker._RecordedRun.reports,
        "name is None",
        "a mutation naming a case is killed where another case fails, so the name shows nothing about that case",
        case="naming a case that passes",
    )
    @kills.force_false(
        checker._reports_of,
        "case.is_named(name)",
        "a mutation naming a case survives where that case fails",
        case="naming the case that fails, beside one raising an undeclared error",
    )
    @kills.delete(
        checker.Mutation.check,
        ", cases=tuple(run.cases)",
        "a killed mutation leaves out the cases of the table, so the check finds none of them unnamed",
        case="naming the case that raises the declared error",
    )
    @kills.replace(
        checker._RecordedRun.reports,
        "_reports_of(self.failures, name)",
        "[]",
        "a mutation naming a case survives where that case fails rather than raises",
        case="naming the case that fails, beside one raising an undeclared error",
    )
    @kills.replace(
        checker._RecordedRun.reports,
        "_reports_of(self.errors, name)",
        "[]",
        "a mutation naming a case survives where that case raises the declared error",
        case="naming the case that raises the declared error",
    )
    @kills.replace(
        checker._reports_of,
        "if (case := _case_of(test)) and case.is_named(name)",
        "if not (case := _case_of(test)) or case.is_named(name)",
        "a mutation naming a case is killed where the test fails after its table, though the case passes",
        case="naming a case that passes",
    )
    @kills.append(
        checker.Mutation.check,
        "!= self.raises",
        " and self.case is None",
        "a named case raising an undeclared error kills the mutation, so the scaffolding it broke goes unreported",
        case="naming a case raising an undeclared error, after one raising the declared error",
    )
    @kills.replace(
        checker.Mutation.check,
        "if errors and",
        "if run.errors and",
        "a mutation naming a case that fails beside another case's error ends the run with a traceback",
        raises="IndexError: list index out of range",
        case="naming the case that fails, beside one raising an undeclared error",
    )
    @kills.delete(
        checker.Mutation.check,
        " and not errors",
        "a mutation killed by the declared error alone is reported as survived",
        case="naming the case that raises the declared error",
    )
    def test_a_mutation_naming_a_case_is_judged_by_that_case_alone(self):
        """Test that a mutation naming a case is judged on that case, whatever the others or the test itself report."""
        three_raises = f"(number != 3 or {_ERRORING}) and {_EVEN}"
        three_raises_and_five_fails = f"(number != 3 or 1 // 0) and (number == 5 or {_EVEN})"
        both_raise = f"(number != 3 or {_ERRORING}) and (number != 5 or 1 // 0) and {_EVEN}"
        for case, named, new, raises, result in (
            ("naming a case that passes", "five", _ONLY_THREE, "", _SURVIVED),
            (
                "naming the case that raises the declared error",
                "three",
                three_raises,
                _NAME_ERROR,
                _MutationResult(outcome=Outcome.KILLED, cases=_TABLE_CASES),
            ),
            (
                "naming the case that fails, beside one raising an undeclared error",
                "five",
                three_raises_and_five_fails,
                "",
                _MutationResult(outcome=Outcome.KILLED, cases=_TABLE_CASES),
            ),
            (
                "naming a case raising an undeclared error, after one raising the declared error",
                "five",
                both_raise,
                _NAME_ERROR,
                _MutationResult(outcome=Outcome.BROKEN, reason=_ZERO_DIVISION),
            ),
        ):
            with self.subTest(case):
                mutation = _MUTATION(anchor=is_even, old=_EVEN, new=new, raises=raises, case=named)
                self.assertEqual(mutation.check(_TABLE_THEN_EVEN_TEST_NAME), result)

    @kills.replace(
        checker.Case.is_named,
        "(self.message, self.description)",
        "(self.description,)",
        "a case name ignores the message of a case, so a mutation naming a case by its message is stale",
    )
    def test_a_mutation_naming_a_case_by_its_message(self):
        """Test that a mutation names a case by its message, as `subTest("five")` gives one, and that case alone."""
        mutation = _MUTATION(anchor=is_even, old=_EVEN, new=_ONLY_THREE, case="five")
        self.assertEqual(mutation.check(_MESSAGE_TABLE_TEST_NAME), _SURVIVED)

    @kills.force_false(
        checker._RecordedRun.addSubTest,
        "any(params.maps[0] is group for group in self._groups)",
        "a group of a nested table counts as a case, so the check for unnamed cases asks a registration to name it",
    )
    def test_a_mutation_naming_a_case_of_a_nested_table(self):
        """Test that a mutation names a case of a nested table, whose cases leave out the groups that hold them."""
        mutation = _MUTATION(anchor=is_even, old=_EVEN, new=_ONLY_THREE, case="(number=3)")
        nested_cases = tuple(
            Case(message=None, params={"number": number}, description=f"(number={number})") for number in (3, 5)
        )
        self.assertEqual(
            mutation.check(_NESTED_TABLE_TEST_NAME), _MutationResult(outcome=Outcome.KILLED, cases=nested_cases)
        )

    @kills.replace(
        checker._RecordedRun.cases_named,
        "len({case.description for case in self.cases if case.is_named(name)})",
        "len([case.description for case in self.cases if case.is_named(name)])",
        "running a case twice makes it count as two cases, so a mutation naming it is stale",
    )
    def test_a_mutation_naming_a_case_the_test_runs_twice(self):
        """Test that a mutation naming a case the test runs twice is judged on that case, as on one it runs once."""
        mutation = _MUTATION(anchor=is_even, old=_EVEN, new=_ONLY_THREE, case="five")
        self.assertEqual(mutation.check(_TWICE_TABLE_TEST_NAME), _SURVIVED)

    @kills.delete(
        checker.Case.is_named,
        " self.description",
        "a case name ignores the description of a case, so a case of several parameters cannot be named",
    )
    def test_a_mutation_naming_a_case_by_its_description(self):
        """Test that a mutation names a case of several parameters by its description, as a report prints it."""
        mutation = _MUTATION(anchor=is_even, old=_EVEN, new=_ONLY_THREE, case="(number=5, parity='odd')")
        self.assertEqual(mutation.check(_PARAMETERS_TABLE_TEST_NAME), _SURVIVED)

    @kills.force_false(
        checker.Mutation._reports,
        "outside_failures or outside_errors",
        "a mutation killed by the declared error before the table is reached is reported as naming a missing case",
        case="raising the declared error before the table",
    )
    @kills.replace(
        checker.Mutation._reports,
        "(outside_failures or outside_errors)",
        "outside_errors",
        "a mutation naming a case the test fails before reaching is reported as naming a missing case",
        case="failing before the table",
    )
    @kills.replace(
        checker.Mutation._reports,
        "(outside_failures or outside_errors)",
        "outside_failures",
        "a mutation naming a case the test raises the declared error before reaching is reported as naming a missing "
        "case",
        case="raising the declared error before the table",
    )
    def test_a_mutation_naming_a_case_the_test_does_not_reach_is_judged_on_what_stopped_the_test(self):
        """Test that a mutation naming a case is judged on what kept the test from reaching the case."""
        for case, new, raises in (
            ("raising the declared error before the table", _TWO_RAISES, _NAME_ERROR),
            ("failing before the table", _ODD, ""),
        ):
            with self.subTest(case):
                mutation = _MUTATION(anchor=is_even, old=_EVEN, new=new, raises=raises, case="five")
                self.assertEqual(mutation.check(_EVEN_THEN_TABLE_TEST_NAME), _KILLED)

    @kills.force_false(
        checker._reports_of,
        "name is OUTSIDE",
        "a mutation naming what the test checks outside its table is read as a case name, so it survives",
        case="failing after the table",
    )
    @kills.force_true(
        checker._reports_of,
        "_case_of(test) is None",
        "a mutation naming what the test checks outside its table is killed by a case that fails",
        case="failing in a case alone",
    )
    def test_a_mutation_naming_what_the_test_checks_outside_its_table(self):
        """Test that a mutation naming what a test checks outside its table is judged on that, whatever its cases do."""
        for case, test_name, result in (
            (
                "failing after the table",
                _TABLE_THEN_EVEN_TEST_NAME,
                _MutationResult(outcome=Outcome.KILLED, cases=_TABLE_CASES),
            ),
            ("failing in a case alone", _TABLE_TEST_NAME, _SURVIVED),
        ):
            with self.subTest(case):
                mutation = _MUTATION(anchor=is_even, old=_EVEN, new=_ONLY_THREE, case=OUTSIDE)
                self.assertEqual(mutation.check(test_name), result)

    @kills.replace(
        checker.Mutation._reports,
        "if count:",
        "if count or run.cases:",
        "a misspelled case name is taken for a case that passes, so the report blames the test, not the registration",
        case="one of the values of a case with two parameters",
    )
    @kills.replace(
        checker.Mutation._reports,
        "if not isinstance(self.case, str):",
        "if not isinstance(self.case, str) or not run.cases:",
        "a case name on a test without a table is taken for a case that passes, so the report blames the test",
        case="a test without a table",
    )
    @kills.force_false(
        checker.Mutation._reports,
        "count > 1",
        "a name two cases share is judged on both of them, so a registration claims more than one case at once",
        case="a name two cases share",
    )
    @kills.delete(
        checker.Mutation._reports,
        "not run.cases and ",
        "a misspelled case name counts as a kill where the test raises the declared error after its table",
        case="a misspelled case, beside an error after the table",
    )
    @kills.replace(
        checker.Case.is_named,
        "list(self.params.values()) == [name]",
        "name in self.params.values()",
        "a name matches any value of a case with several parameters, so it names more than the case meant",
        case="one of the values of a case with two parameters",
    )
    def test_a_mutation_naming_no_single_case_of_the_test_is_stale(self):
        """Test that a mutation naming a case the test does not have, or has twice, is reported as stale."""
        for case, named, test_name, new, raises, reason in (
            (
                "one of the values of a case with two parameters",
                "odd",
                _PARAMETERS_TABLE_TEST_NAME,
                _ODD,
                "",
                "the test has no case named 'odd'",
            ),
            (
                "a test without a table",
                "three",
                _SUBJECT_TEST_NAME,
                _SURVIVING,
                "",
                "the test has no case named 'three'",
            ),
            (
                "a name two cases share",
                "odd",
                _SHARED_MESSAGE_TABLE_TEST_NAME,
                _ODD,
                "",
                "the test has 2 cases named 'odd'",
            ),
            (
                "a misspelled case, beside an error after the table",
                "sevne",
                _TABLE_THEN_EVEN_TEST_NAME,
                _TWO_RAISES,
                _NAME_ERROR,
                "the test has no case named 'sevne'",
            ),
        ):
            with self.subTest(case):
                mutation = _MUTATION(anchor=is_even, old=_EVEN, new=new, raises=raises, case=named)
                self.assertEqual(mutation.check(test_name), _STALE(reason=reason))

    def test_a_mutation_whose_file_and_test_are_in_different_packages(self):
        """Test that the package of the mutated file is purged as well as the package of its test.

        The module between the two is imported first, being the one that holds a binding to the mutated function.
        Without it in `sys.modules`, purging either package alone would do just as well and this would pass.
        """
        importlib.import_module("update_time.domain.staleness")
        mutation = _MUTATION(
            anchor=days_since,
            old="return (datetime.now(UTC) - timestamp).days",
            new="return (datetime.now(UTC) - timestamp).days + 1",
        )
        staleness_test_name = "tests.update_time.domain.test_staleness.IsStaleTest.test_boundary_compares_whole_days"
        self.assertEqual(mutation.check(staleness_test_name), _KILLED)

    @kills.replace(
        checker._loaded_test,
        "Exception",
        "SyntaxError",
        "a mutation the test module cannot import escapes the check rather than being reported as broken",
        raises="AttributeError: 'function' object has no attribute '_registered_mutations'",
        case="the test module raises when imported",
    )
    @kills.delete(
        checker._loaded_test,
        "_reason(error)",
        "a mutation the test module cannot import is reported as broken without naming the error",
        case="the test module raises when imported",
    )
    @kills.delete(
        checker._bind_on_parent,
        "_reason(error)",
        "a mutation that leaves its parent package unimportable is reported as broken without naming the error",
        case="the parent package raises when imported",
    )
    @kills.replace(
        checker._executed_module,
        "except Exception as error:",
        "except ArithmeticError as error:",
        "a mutated source that does not import escapes the check rather than being reported as broken",
        raises="SyntaxError: invalid syntax",
        case="the mutated source does not import",
    )
    @kills.replace(
        checker._executed_module,
        "except Exception as error:",
        "except SyntaxError as error:",
        "a mutated definition that raises when executed escapes the check rather than being reported as broken",
        raises="ZeroDivisionError: division by zero",
        case="the mutated definition raises when executed",
    )
    @kills.force_false(
        checker.Mutation.check,
        "(raised := errors[0][1].strip().splitlines()[-1]) != self.raises",
        "a test raising an error the mutation does not declare counts as killing it",
        case="the test errors",
    )
    def test_a_mutation_that_cannot_be_judged_is_reported_as_broken(self):
        """Test that a mutation the check could not judge is reported as broken rather than as survived."""
        deletes_the_registration = _MUTATION(
            anchor=checker._registration,
            old="setattr(wrapper, _REGISTERED, (mutation,))",
            new="delattr(wrapper, _REGISTERED)",
        )
        for case, mutation, error in (
            (
                "the mutated source does not import",
                _MUTATION(anchor=is_even, old=_EVEN, new=_UNPARSABLE),
                "SyntaxError",
            ),
            ("the test errors", _MUTATION(anchor=is_even, old=_EVEN, new=_ERRORING), "NameError"),
            (
                "the mutated definition raises when executed",
                _MUTATION(anchor=is_even, old="number: int)", new="number: int = 1 // 0)"),
                "ZeroDivisionError",
            ),
            ("the test module raises when imported", deletes_the_registration, "AttributeError"),
            (
                "the parent package raises when imported",
                _MUTATION(anchor=step, old="STEP: Final[int] = 1", new="STEPS: Final[int] = 1"),
                "ImportError",
            ),
        ):
            with self.subTest(case=case):
                result = mutation.check(_SUBJECT_TEST_NAME)
                self.assertEqual(result.outcome, Outcome.BROKEN)
                self.assertIn(error, result.reason)

    @kills.prepend(
        checker.Mutation.check,
        "Outcome.BROKEN, reason=str(error))",
        "Outcome.KILLED if str(error) == self.raises else ",
        "a mutation whose source does not import counts as killed where the declaration names the import's error",
    )
    def test_a_source_that_does_not_import_though_its_error_is_declared(self):
        """Test that a mutation whose source does not import is reported as broken though it declares that error."""
        reported = _MUTATION(anchor=is_even, old=_EVEN, new=_UNPARSABLE).check(_SUBJECT_TEST_NAME).reason
        declaring = _MUTATION(anchor=is_even, old=_EVEN, new=_UNPARSABLE, raises=reported)
        self.assertEqual(declaring.check(_SUBJECT_TEST_NAME), _MutationResult(outcome=Outcome.BROKEN, reason=reported))

    @kills.delete(
        checker.Mutation.check,
        " != self.raises",
        "a test that kills a mutation by raising the error the mutation declares is reported as broken",
    )
    def test_a_test_that_raises_the_declared_error(self):
        """Test that a mutation whose test raises the declared error is reported as killed."""
        mutation = _MUTATION(anchor=is_even, old=_EVEN, new=_ERRORING, raises=_NAME_ERROR)
        self.assertEqual(mutation.check(_SUBJECT_TEST_NAME), _KILLED)

    @kills.delete(
        checker.Mutation.check,
        ", reason=raised",
        "a mutation reported as broken does not name the error the test raised, which is the line to declare",
    )
    def test_a_test_that_raises_an_undeclared_error(self):
        """Test that a mutation whose test raises an undeclared error is reported as broken, naming the error."""
        declared = "TypeError: an error the test does not raise"
        self.assertEqual(
            _MUTATION(anchor=is_even, old=_EVEN, new=_ERRORING, raises=declared).check(_SUBJECT_TEST_NAME),
            _MutationResult(outcome=Outcome.BROKEN, reason=_NAME_ERROR),
        )

    @kills.replace(
        checker.Mutation.check,
        "_SourceError",
        "Exception",
        "an error in the checker itself is caught and misreported as a broken mutation",
    )
    def test_a_defect_in_the_checker_itself(self):
        """Test that an error from the checker is raised, rather than reported as the mutation being broken."""
        with patch.object(Mutation, "_run_test", Mock(side_effect=RuntimeError("the checker is broken"))):
            self.assertRaises(RuntimeError, _MUTATION(anchor=is_even, old=_EVEN, new=_ODD).check, _SUBJECT_TEST_NAME)

    @kills.delete_statement(
        checker.Mutation._run_test_afresh,
        "sys.modules.clear()",
        "the check leaves behind a module that the mutated source adds",
    )
    def test_the_modules_are_left_as_they_were(self):
        """Test that a check restores sys.modules where the mutated source added a module.

        The outcome is asserted as well, since a check that returned before touching `sys.modules` would leave it
        alone too, and pass on that alone.
        """
        functools_import = "from functools import cache, cached_property"
        module_addition = "import sys, types\nsys.modules['added'] = types.ModuleType('added')\n"
        mutation = _MUTATION(anchor=mutation_subject, old=functools_import, new=module_addition + functools_import)
        imported = dict(sys.modules)
        self.assertEqual(mutation.check(_SUBJECT_TEST_NAME).outcome, Outcome.SURVIVED)
        names = sys.modules.keys() | imported.keys()
        self.assertEqual([name for name in names if sys.modules.get(name) is not imported.get(name)], [])

    @kills.delete(
        checker.Mutation._source,
        "_reason(error)",
        "a mutation naming an unreadable file is reported as stale without saying why",
    )
    def test_a_mutation_whose_file_cannot_be_read(self):
        """Test that a mutation naming a file that cannot be read is reported as stale, and says so."""
        with patch.object(checker, "_READ_TEXT", Mock(side_effect=FileNotFoundError(2, "No such file or directory"))):
            result = _MUTATION(anchor=is_even, old=_EVEN, new=_ODD).check(_SUBJECT_TEST_NAME)
        self.assertEqual(result.outcome, Outcome.STALE)
        self.assertIn("FileNotFoundError: [Errno 2] No such file or directory", result.reason)

    def test_a_mutation_checked_while_the_test_patches_reading_a_file_reads_the_real_source(self):
        """Test that a mutation checked while `Path.read_text` is patched is applied to the source on disk."""
        with patch("pathlib.Path.read_text", Mock(return_value="a fixture the test being checked reads")):
            result = _MUTATION(anchor=is_even, old=_EVEN, new=_ODD).check(_SUBJECT_TEST_NAME)
        self.assertEqual(result, _KILLED)

    @kills.delete(
        checker._definitions,
        "StaleError(_reason(error)) from ",
        "the run ends with a traceback where a source no longer parses, rather than reporting it stale",
        raises="SyntaxError: '(' was never closed",
        case="function anchor",
    )
    @kills.force_false(
        checker.Mutation._mutated,
        "not self._qualified_name and (function := _function_holding(source, self.old))",
        "a module anchor on a source that no longer parses is applied rather than reported stale",
        case="module anchor",
    )
    @kills.force_true(
        checker._span,
        "not qualified_name",
        "a function anchor on a source that no longer parses spans the whole source rather than being reported stale",
        case="function anchor",
    )
    def test_a_mutation_whose_source_does_not_parse(self):
        """Test that a mutation whose file no longer parses is reported as stale, and says so, whatever its anchor."""
        for case, mutation in (
            ("function anchor", _MUTATION(anchor=Doubler.doubled, old=_DOUBLING, new=_TRIPLING)),
            ("module anchor", _MUTATION(anchor=mutation_subject, old="def broken(", new="def mended(")),
        ):
            with self.subTest(case=case), patch.object(checker, "_READ_TEXT", Mock(return_value="def broken(")):
                result = mutation.check(_DOUBLER_TEST_NAME)
                self.assertEqual(result.outcome, Outcome.STALE)
                self.assertIn("SyntaxError", result.reason)

    @kills.replace(
        checker._definitions,
        "definitions.setdefault(name, node)",
        "definitions[name] = node",
        "a setter hides its getter, so a module anchor is accepted around a snippet the getter holds",
        case="a getter whose property has a setter",
    )
    @kills.replace(
        checker._function_holding,
        '_occurrences(source, "", snippet)[0]',
        "source.index(snippet)",
        "a module anchor's message names the function whose docstring repeats the snippet, not the one holding it",
        case="a snippet an earlier docstring repeats",
    )
    @kills.replace(
        checker._node_span,
        'source.startswith("\\n", end)',
        'source[end] == "\\n"',
        "a span reads past the end of a file that lacks a final newline, so the run ends with a traceback",
        raises="IndexError: string index out of range",
        case="the last function of a file without a final newline",
    )
    def test_a_module_anchor_on_a_snippet_one_function_of_a_source_holds(self):
        """Test that a module anchor on a snippet one function of the source holds is stale, naming that function."""
        getter_and_setter = (
            "class A:\n    @property\n    def x(self):\n        return self._x + 1\n\n"
            "    @x.setter\n    def x(self, value):\n        self._x = value\n"
        )
        for case, (source, old, new, function) in {
            "the last function of a file without a final newline": (
                "def last():\n    return 1",
                "return 1",
                "return 2",
                "last",
            ),
            "a getter whose property has a setter": (getter_and_setter, "self._x + 1", "self._x + 2", "A.x"),
            "a snippet an earlier docstring repeats": (
                'def f():\n    """Return x or None."""\n\n\ndef g(x):\n    return x or None\n',
                " or None",
                "",
                "g",
            ),
        }.items():
            with self.subTest(case=case), patch.object(checker, "_READ_TEXT", Mock(return_value=source)):
                result = _MUTATION(anchor=mutation_subject, old=old, new=new).check(_SUBJECT_TEST_NAME)
                self.assertEqual(result, _STALE(reason=_lies_inside(function)))

    @kills.force_false(
        checker.mutated_source,
        "new == old",
        "a mutation that changes nothing is reported as survived, blaming the test rather than the registration",
    )
    def test_a_mutation_that_would_change_nothing(self):
        """Test that a replacement equal to the snippet is reported as stale, rather than as the test's failing."""
        result = _MUTATION(anchor=is_even, old=_EVEN, new=_EVEN).check(_SUBJECT_TEST_NAME)
        self.assertEqual(result, _STALE(reason="the snippet and its replacement are the same, so nothing changes"))

    @kills.force_false(
        checker.mutated_source,
        'old[:1] in (" ", "\\t") and new[:1] == old[:1]',
        "a registration pads its snippet with indentation, hiding the token the mutation changes",
        case="spaces",
    )
    @kills.delete(
        checker.mutated_source,
        ' "\\t"',
        "a registration pads its snippet with tabs, hiding the token the mutation changes",
        case="a tab",
    )
    @kills.replace(
        checker.mutated_source,
        '(" ", "\\t")',
        '("\\t",)',
        "a registration pads its snippet with spaces, hiding the token the mutation changes",
        case="spaces",
    )
    @kills.force_true(
        checker.mutated_source,
        "new[:1] == old[:1]",
        "a mutation deleting a whole line is rejected, though the line's indentation is part of what it deletes",
        case=OUTSIDE,
    )
    def test_a_snippet_and_replacement_opening_on_the_same_indentation(self):
        """Test that a snippet and a replacement opening on the same indentation are reported as stale."""
        stale = _STALE(
            reason="the snippet and its replacement open on the same indentation, so drop that indentation from both"
        )
        for case, indentation in (("spaces", "    "), ("a tab", "\t")):
            with self.subTest(case=case):
                old, new = f"{indentation}return {_EVEN}", f"{indentation}return {_ODD}"
                self.assertEqual(_MUTATION(anchor=is_even, old=old, new=new).check(_SUBJECT_TEST_NAME), stale)
        deleted_line = _MUTATION(anchor=is_even, old=f"    return {_EVEN}\n", new="")
        self.assertEqual(deleted_line.check(_SUBJECT_TEST_NAME), _KILLED)

    @kills.force_false(
        checker._function_holding,
        "first <= start and end <= last",
        "a module anchor is accepted around a snippet one function holds, so the anchor claims the whole file",
        case="method",
    )
    @_kills_classes_skipped(case="method")
    @kills.replace(
        checker._named,
        'f"{prefix}{node.name}."',
        "prefix",
        "the message names a method without its class, and an anchor cannot reach the method by that name",
        case="method",
    )
    @kills.prepend(
        checker._named,
        'yield f"{prefix}{node.name}", node',
        'yield from _named(node.body, f"{prefix}{node.name}.")\n            ',
        "the message names a nested function instead of the function around it, and an anchor cannot reach it",
        case="nested function",
    )
    @_kills_span_stopping_before_the_newline(case="ending in the newline after the last line")
    def test_a_module_anchor_on_a_snippet_one_function_holds(self):
        """Test that a module anchor on a snippet one function or method holds is reported as stale, naming it."""
        for case, old, new, test_name, function in (
            ("method", "cls().doubled(2)", "cls().doubled(3)", _TWO_DOUBLED_TEST_NAME, "Doubler.two_doubled"),
            ("nested function", "count > 0", "count > 1", _SUBJECT_TEST_NAME, "is_positive_even"),
            ("ending in the newline after the last line", f"{_EVEN}\n", f"{_ODD}\n", _SUBJECT_TEST_NAME, "is_even"),
        ):
            with self.subTest(case=case):
                result = _MUTATION(anchor=mutation_subject, old=old, new=new).check(test_name)
                self.assertEqual(result, _STALE(reason=_lies_inside(function)))

    @kills.force_true(
        checker._function_holding,
        "first <= start and end <= last",
        "a module anchor is rejected around a snippet outside every function, though a function cannot anchor it",
        case="decorator",
    )
    @kills.replace(
        checker._node_span,
        "_offset(source, node.lineno, node.col_offset)",
        "_offset(source, min([node.lineno, "
        '*(decorator.lineno for decorator in getattr(node, "decorator_list", []))]), 0)',
        "a span starts at the decorators, so a snippet starting at one is rejected, though its function lacks it",
        case="decorator",
    )
    @kills.delete(
        checker._function_holding,
        "and end ",
        "a snippet starting in one function and ending in the next is rejected as if the first held it all",
        case="two functions",
    )
    def test_a_module_anchor_on_a_snippet_no_function_holds_on_its_own(self):
        """Test that a module anchor on a snippet no function or method holds on its own is applied."""
        for case, old, new in (
            ("decorator", "@property\n    def one_doubled(self)", "@property  # applied\n    def one_doubled(self)"),
            ("two functions", "is_even(count)\n\n\ndef is_even", "is_even(count)\n\n\n\ndef is_even"),
        ):
            with self.subTest(case=case):
                result = _MUTATION(anchor=mutation_subject, old=old, new=new).check(_SUBJECT_TEST_NAME)
                self.assertEqual(result, _SURVIVED)

    @kills.append(
        checker._occurrences,
        "start, end = _span(source, qualified_name)",
        "\n    if snippet not in source[start:end]:\n        start, end = 0, len(source)",
        "the checker falls back to the whole file, so a mutation lands outside the anchor that named it",
        case="in the file but outside the anchored function",
    )
    @kills.replace(
        checker.mutated_source,
        ")) != 1:",
        ")) < 1:",
        "the check applies a repeated snippet at its first occurrence",
        case="repeated in the file",
    )
    def test_a_snippet_the_anchor_does_not_hold_exactly_once(self):
        """Test that a snippet the anchor holds never, or more than once, is reported as stale, saying how often."""
        for case, anchor, old, occurrences in (
            ("repeated in the file", mutation_subject, "number", 4),
            ("in the file but outside the anchored function", is_multiple_of_three, _EVEN, 0),
        ):
            with self.subTest(case=case):
                result = _MUTATION(anchor=anchor, old=old, new="count").check(_SUBJECT_TEST_NAME)
                self.assertEqual(result, _STALE(reason=f"the snippet occurs {occurrences} times rather than once"))

    @kills.force_false(
        checker._span,
        "definition is None",
        "the checker reads the offsets of a definition it did not find, so the run ends with a traceback",
        raises="AttributeError: 'NoneType' object has no attribute 'lineno'",
    )
    def test_an_anchor_the_source_does_not_define(self):
        """Test that an anchor the source does not define is reported as stale, naming the anchor."""
        moved_on = Mock(return_value='"""A source defining nothing that an anchor names."""\n')
        with patch.object(checker, "_READ_TEXT", moved_on):
            result = _MUTATION(anchor=Doubler.doubled, old=_DOUBLING, new=_TRIPLING).check(_DOUBLER_TEST_NAME)
        self.assertEqual(result, _STALE(reason="the source does not define Doubler.doubled"))


def _checked(mutation: Mutation, test_name: str) -> tuple[_MutationResult, int]:
    """Return what checking the mutation showed, and how many times the check executed the module afresh."""
    with patch.object(checker, "_executed_module", wraps=checker._executed_module) as executed:
        return mutation.check(test_name), executed.call_count


class IsOddTest(unittest.TestCase):
    """Unit tests for deciding whether a number is odd, whose answers are cached, which a cached anchor targets.

    An anchor on this class targets them too, so that a mutation changes the module holding the test it breaks.
    """

    def test_an_odd_number(self):
        """Test that three is odd."""
        self.assertTrue(is_odd(3))


def _lies_inside(function: str) -> str:
    """Return the reason a module anchor on a snippet the function holds is stale."""
    return f"the snippet lies inside {function}, so anchor the mutation on {function}"


# What a mutation raises where a docstring repeats its snippet and the docstring counts as code.
_SNIPPET_TWICE: Final[str] = "mutation_time.mutation.StaleError: the snippet occurs 2 times rather than once"


class MutatedSourceTest(unittest.TestCase):
    """Unit tests for the source a mutation turns the file into."""

    @kills.replace(
        checker._docstrings,
        "ast.walk(tree)",
        "[tree, *tree.body]",
        "a nested function's docstring counts as code, so a snippet it repeats makes the mutation stale",
        raises=_SNIPPET_TWICE,
        case="a nested function's docstring",
    )
    @kills.delete(
        checker._docstrings,
        "ast.Module, ",
        "the module's docstring counts as code, so a snippet it repeats makes a module anchor's mutation stale",
        raises=_SNIPPET_TWICE,
        case="the module's docstring",
    )
    @kills.delete(
        checker._docstrings,
        "ast.ClassDef, ",
        "a class's docstring counts as code, so a snippet it repeats makes a class anchor's mutation stale",
        raises=_SNIPPET_TWICE,
        case="a class's docstring",
    )
    def test_a_docstring_repeating_the_snippet_is_left_as_written(self):
        """Test that a snippet a docstring repeats is replaced in the code, and left as written in the docstring."""
        for case, anchor, source, mutated in (
            (
                "a nested function's docstring",
                "f",
                'def f(x):\n    def g():\n        """Return x or None."""\n\n    return x or None\n',
                'def f(x):\n    def g():\n        """Return x or None."""\n\n    return x\n',
            ),
            (
                "a class's docstring",
                "C",
                'class C:\n    """Hold x or None."""\n\n    X = x or None\n',
                'class C:\n    """Hold x or None."""\n\n    X = x\n',
            ),
            (
                "the module's docstring",
                "",
                '"""Hold x or None."""\n\nX = x or None\n',
                '"""Hold x or None."""\n\nX = x\n',
            ),
        ):
            with self.subTest(case):
                self.assertEqual(checker.mutated_source(source, anchor, " or None", ""), mutated)


class LinesTest(unittest.TestCase):
    """Unit tests for the lines of its file that a mutation changes."""

    def test_the_lines_the_snippet_covers_inside_its_anchor(self):
        """Test that a mutation's lines are the snippet's, found inside the anchor, however many lines it spans."""
        cases = {
            "one line": (_MUTATION(anchor=is_even, old=_EVEN, new=_ODD), range(18, 19)),
            "a snippet in two methods": (
                _MUTATION(anchor=Doubler.quadrupled, old=_DOUBLING, new=_TRIPLING),
                range(55, 56),
            ),
            "two lines": (_MUTATION(anchor=Banner.indented, old="first\n        second", new="first"), range(69, 71)),
        }
        for case, (mutation, lines) in cases.items():
            with self.subTest(case):
                self.assertEqual(mutation.lines, lines)

    def test_the_lines_of_the_code_rather_than_of_a_docstring_repeating_the_snippet(self):
        """Test that a mutation's lines are the code's, though the docstring above the code repeats the snippet."""
        source = 'def is_even(number):\n    """Return number or None."""\n    return number or None\n'
        with patch.object(checker, "_READ_TEXT", Mock(return_value=source)):
            lines = _MUTATION(anchor=is_even, old=" or None", new="").lines
        self.assertEqual(lines, range(3, 4))


class CapturedMethodsTest(unittest.TestCase):
    """Unit tests for the `Path` methods the module holds when it is imported."""

    def test_importing_while_a_path_method_is_patched_fails(self):
        """Test that importing fails while `Path.read_text` is patched, since the module would hold the patch."""
        with patch("pathlib.Path.read_text", Mock()):
            self.assertRaises(TypeError, self.import_copy)

    @staticmethod
    def import_copy() -> None:
        """Import a copy of the mutation module under a name of its own, leaving the one the suite runs on alone."""
        loader = importlib.machinery.SourceFileLoader("mutation_imported_afresh", checker.__file__)
        loader.exec_module(types.ModuleType(loader.name))

    def test_importing_succeeds_while_nothing_is_patched(self):
        """Test that importing a copy succeeds while nothing is patched, so a refusal names the patch, not the copy."""
        self.import_copy()

    def test_importing_succeeds_inside_a_mutation_check(self):
        """Test that importing succeeds inside a mutation check, whose test may have patched a `Path` method."""
        with patch("pathlib.Path.read_text", Mock()), patch_environ({_CHECKED_TEST: _SUBJECT_TEST_NAME}):
            self.import_copy()


class FailureMessageTest(unittest.TestCase):
    """Unit tests for the message that reports a mutation the test did not kill."""

    @kills.replace(
        checker.Mutation.anchor_name,
        '".".join(filter(None, (self.module.__name__, self._qualified_name)))',
        "self.module.__name__",
        "a report names the module alone, so it does not say which of its anchors went stale",
        case="tests.mutation_time.mutation_subject.Doubler.doubled",
    )
    @kills.replace(
        checker.Mutation.anchor_name,
        "filter(None, (self.module.__name__, self._qualified_name))",
        "(self.module.__name__, self._qualified_name)",
        "a module anchor is reported with a trailing dot, as though a definition were missing from the name",
        case="tests.mutation_time.mutation_subject",
    )
    @kills.replace(
        checker.Mutation._qualified_name,
        "anchor.__qualname__",
        "anchor.__name__",
        "a report names a method without its class, so it does not say which class's method went stale",
        case="tests.mutation_time.mutation_subject.Doubler.doubled",
    )
    def test_the_message_names_the_anchor(self):
        """Test that the message names the definition the mutation is anchored to, and the module where none is."""
        for anchor, name in (
            (mutation_subject, "tests.mutation_time.mutation_subject"),
            (Doubler.doubled, "tests.mutation_time.mutation_subject.Doubler.doubled"),
        ):
            with self.subTest(name=name):
                mutation = Mutation(anchor=anchor, old=_EVEN, new=_ODD, regression=_REGRESSION)
                survived, stale = (_SURVIVED, _STALE(reason="why"))
                self.assertEqual(
                    _failure(mutation, survived), f"{_REGRESSION} — the test did not kill this mutation of {name}"
                )
                self.assertEqual(_failure(mutation, stale), f"{_REGRESSION} — this mutation of {name} is stale: why")

    @kills.force_false(
        checker._killer,
        "mutation.case is OUTSIDE",
        "a survivor naming the checks outside the table is reported as if it named a case",
        case="the checks outside the table",
    )
    @kills.replace(
        checker._killer,
        'f"the case {mutation.case!r}"',
        '"the test"',
        "a survivor naming a case blames the whole test, so the report does not say which case let it through",
        case="a case",
    )
    def test_the_message_of_a_survivor_names_what_was_to_kill_it(self):
        """Test that the message of a survivor naming a case, or the checks outside the table, names that instead."""
        for case, named, killer in (
            ("a case", "five", "the case 'five'"),
            ("the checks outside the table", OUTSIDE, "the checks outside the table"),
        ):
            with self.subTest(case):
                mutation = Mutation(anchor=is_even, old=_EVEN, new=_ODD, regression=_REGRESSION, case=named)
                self.assertEqual(
                    _failure(mutation, _SURVIVED),
                    f"{_REGRESSION} — {killer} did not kill this mutation of {is_even.__module__}.is_even",
                )


class KillsTest(unittest.TestCase):
    """Unit tests for the decorator that makes a test check its mutations."""

    @kills.replace(
        checker._fail_unless_killed,
        "for mutation in mutations:",
        "for mutation in mutations[:1]:",
        "only the first of the mutations a registration holds is checked",
    )
    @kills.append(
        checker._fail_unless_killed,
        "result = mutation.check(test_name",
        '.split(".")[-1]',
        "the test is named without the module it sits in, so the checker cannot load it",
    )
    def test_a_decorated_test_checks_every_mutation_it_registers(self):
        """Test that a registration holding several mutations checks each of them, in order, naming the test."""
        result = self.run_decorated_test(_KILLED, _SEVERAL_MUTATIONS_TEST)
        self.assertEqual((result.failures, result.errors), ([], []))
        self.assertEqual(
            self.checked.call_args_list,
            [call(_ODD_REPORTED_AS_EVEN, self.decorated_id), call(_ONLY_THREE_REPORTED_AS_EVEN, self.decorated_id)],
        )

    def run_decorated_test(
        self,
        checked: _MutationResult | Callable[..., _MutationResult],
        test_name: str = _DECORATED_TEST,
        sentinels: dict[str, str] | None = None,
        test_class: type[unittest.TestCase] = IsEvenTest,
    ) -> unittest.TestResult:
        """Run the class's named decorated test with the check answering as given, and return what unittest recorded.

        The check is stood in for, so that a test of the decorator does not pay for a real one; the decorated test
        the suite runs of its own accord is what exercises the real check. The run sees the sentinels named and no
        others, so what holds the decorated test back is what the test asked for rather than what a `just mutate`
        run set. A function given as the answer runs when the check is called, so a test can look at the state it is
        called in.
        """
        result = unittest.TestResult()
        decorated = test_class(test_name)
        self.decorated_id = decorated.id()
        with patch.dict(os.environ), patch.object(Mutation, "check", autospec=True) as self.checked:
            os.environ.pop(_CHECKED_TEST, None)
            os.environ.pop(CHECKS_OFF, None)
            os.environ.update(sentinels or {})
            if callable(checked):
                self.checked.side_effect = checked
            else:
                self.checked.return_value = checked
            decorated.run(result)
        return result

    @kills.replace(
        checker._Kills.prepend,
        "new + old",
        "old + new",
        "prepending on kills inserts the new code after the snippet rather than before it",
        case="prepend",
    )
    @kills.replace(
        checker._Kills.append,
        "old + new",
        "new + old",
        "appending on kills inserts the new code before the snippet rather than after it",
        case="append",
    )
    @kills.replace(
        checker._Kills.uncache,
        "new=definition, regression=regression,",
        'new=definition, regression="",',
        "uncaching on kills drops the regression, so a survivor's report does not say what went wrong",
        case="uncache",
    )
    @kills.replace(
        checker._Kills.uncache,
        "Mutation(anchor=module,",
        "Mutation(anchor=function,",
        "uncaching anchors on the function, whose span leaves out the decorator, so every uncaching is stale",
        case="uncache",
    )
    @kills.delete(
        checker._Kills.uncache,
        "'[' if type_parameters else ",
        "uncaching a generic function quotes a parenthesis after its name, so the uncaching is stale",
        case="uncache generic",
    )
    @kills.replace(
        checker._Kills.force_true,
        '"True", regression',
        '"False", regression',
        "forcing a condition true on kills forces it false, so the mutation stands for another regression",
        case="force_true",
    )
    @kills.replace(
        checker._Kills.force_false,
        '"False", regression',
        '"True", regression',
        "forcing a condition false on kills forces it true, so the mutation stands for another regression",
        case="force_false",
    )
    @kills.replace(
        checker._Kills.force_none,
        '"None", regression',
        '"False", regression',
        "forcing an expression to None on kills forces it false, so the mutation stands for another regression",
        case="force_none",
    )
    @kills.replace(
        checker._Kills.delete_statement,
        '"pass", regression',
        '"", regression',
        "deleting a statement on kills leaves empty a block that held the statement alone",
        case="delete_statement",
    )
    @kills.replace(
        checker._Kills.delete_statement,
        'new="pass", regression=regression,',
        'new="pass", regression="",',
        "deleting a statement on kills drops the regression, so a survivor's report does not say what went wrong",
        case="delete_statement",
    )
    @kills.each(
        (
            "replace",
            "delete",
            "prepend",
            "append",
            "force_true",
            "force_false",
            "force_none",
            "delete_statement",
            "uncache",
        ),
        lambda verb: kills.replace(
            getattr(checker._Kills, verb),
            ", **kill)",
            ")",
            f"{verb} on kills drops the error it declares and the case it names",
            case=verb,
        ),
    )
    def test_each_verb_on_kills_registers_the_plain_mutation_making_its_change(self):
        """Test that each verb on kills registers the one mutation making its change, such as forcing a condition."""
        kill = {"raises": _NAME_ERROR, "case": _NAMED_CASE}
        cases = {
            "replace": (kills.replace(is_even, _EVEN, _ODD, _REGRESSION, **kill), is_even, _EVEN, _ODD),
            "delete": (kills.delete(is_even, _EVEN, _REGRESSION, **kill), is_even, _EVEN, ""),
            "prepend": (kills.prepend(is_even, _EVEN, "not ", _REGRESSION, **kill), is_even, _EVEN, f"not {_EVEN}"),
            "append": (kills.append(is_even, _EVEN, " + 1", _REGRESSION, **kill), is_even, _EVEN, f"{_EVEN} + 1"),
            "uncache": (
                kills.uncache(is_odd, _REGRESSION, **kill),
                mutation_subject,
                "@cache\ndef is_odd(",
                "def is_odd(",
            ),
            "uncache generic": (
                kills.uncache(first, _REGRESSION, **kill),
                mutation_subject,
                "@cache\ndef first[",
                "def first[",
            ),
            "force_true": (kills.force_true(is_even, _EVEN, _REGRESSION, **kill), is_even, _EVEN, "True"),
            "force_false": (kills.force_false(is_even, _EVEN, _REGRESSION, **kill), is_even, _EVEN, "False"),
            "delete_statement": (
                kills.delete_statement(is_even, f"return {_EVEN}", _REGRESSION, **kill),
                is_even,
                f"return {_EVEN}",
                "pass",
            ),
            "force_none": (kills.force_none(is_even, _EVEN, _REGRESSION, **kill), is_even, _EVEN, "None"),
        }
        for case, (decorator, anchor, old, new) in cases.items():
            with self.subTest(case):
                mutation = Mutation(anchor=anchor, old=old, new=new, regression=_REGRESSION, **kill)
                self.assertEqual(getattr(decorator(_stand_in), _REGISTERED), (mutation,))

    @kills.replace(
        checker._Kills.each,
        "for register in reversed(registrations):",
        "for register in registrations:",
        "each registers the mutations of its values in reverse, so a report lists them out of order",
    )
    @kills.replace(
        checker._Kills.each,
        "reversed(registrations)",
        "reversed(registrations[:1])",
        "each registers the mutation of its first value alone, so the other values go unchecked",
    )
    def test_each_registers_the_mutation_of_every_value_in_their_order(self):
        """Test that `kills.each` registers the mutation each value's registration makes, in the order of the values."""
        names = ("three", "five")
        decorator = kills.each(names, lambda name: kills.replace(is_even, _EVEN, _ODD, _REGRESSION, case=name))
        expected = tuple(
            Mutation(anchor=is_even, old=_EVEN, new=_ODD, regression=_REGRESSION, case=name) for name in names
        )
        self.assertEqual(getattr(decorator(_stand_in), _REGISTERED), expected)

    @kills.replace(
        checker._register_above,
        "(mutation, *getattr(layer, _REGISTERED))",
        "(*getattr(layer, _REGISTERED), mutation)",
        "stacked registrations are checked bottom to top, in the reverse of the order they read in",
    )
    @kills.force_none(
        checker._register_above,
        'getattr(layer, "__wrapped__", None)',
        "a registration stacked above a patch stops at the patch, so the test never checks its mutations",
    )
    @kills.replace(
        checker._registration,
        "getattr(wrapper, _REGISTERED)",
        "[mutation]",
        "a test checks the mutations of its lowest registration alone, leaving the ones stacked above unchecked",
    )
    def test_stacked_registrations_check_their_mutations_top_to_bottom(self):
        """Test that a test with stacked registrations checks each of their mutations, top to bottom, naming the test.

        A patch between the registrations changes nothing.
        """
        expected = (_ODD_REPORTED_AS_EVEN, _ONLY_THREE_REPORTED_AS_EVEN)
        result = self.run_decorated_test(_KILLED, _STACKED_ACROSS_A_PATCH_TEST)
        self.assertEqual((result.failures, result.errors), ([], []))
        self.assertEqual(self.checked.call_args_list, [call(each, self.decorated_id) for each in expected])
        self.assertEqual(registered_mutations(IsEvenTest(_STACKED_ACROSS_A_PATCH_TEST)), expected)

    @kills.replace(
        checker._failure,
        "{mutation.regression} — {_killer(mutation)} did not kill",
        "{_killer(mutation)} did not kill",
        "the survivor message drops the regression, so it no longer says what went wrong",
    )
    def test_a_surviving_mutation_fails_the_test(self):
        """Test that a survivor fails the test, leading with the regression rather than the snippets."""
        result = self.run_decorated_test(_SURVIVED)
        self.assertEqual(len(result.failures), 1)
        message = result.failures[0][1]
        self.assertIn(f"{_REGRESSION} — the test did not kill this mutation of {mutation_subject.__name__}", message)
        self.assertNotIn(_ODD, message)
        self.assertNotIn(_EVEN, message)

    @kills.replace(
        checker._fail_unless_killed,
        "with unittest.TestCase.subTest(test_case, regression=mutation.regression):",
        "if True:",
        "a test stops at the first mutation it did not kill, leaving every mutation after it unreported",
        case=OUTSIDE,
    )
    @kills.delete(
        checker._fail_unless_killed,
        ", regression=mutation.regression",
        "a mutation the test did not kill is reported without naming which mutation it was",
        case=_REGRESSION,
    )
    @kills.replace(
        checker._fail_unless_killed,
        "_failure(mutation, result))",
        "_failure(mutations[0], result))",
        "every mutation the test did not kill is reported with the first one's regression",
        case=_ONLY_THREE_REGRESSION,
    )
    @kills.replace(
        checker._fail_unless_killed,
        "_failure(mutation, result))",
        "_failure(mutations[-1], result))",
        "the report gives each surviving mutation the last one's regression",
        case=_REGRESSION,
    )
    def test_every_surviving_mutation_is_reported_separately(self):
        """Test that each mutation a test does not kill fails it as a case of its own, naming its regression."""
        result = self.run_decorated_test(_SURVIVED, _SEVERAL_MUTATIONS_TEST)
        self.assertEqual(len(result.failures), 2)
        for (subtest, message), regression in zip(result.failures, (_REGRESSION, _ONLY_THREE_REGRESSION), strict=True):
            with self.subTest(regression=regression):
                self.assertIn(f"(regression={regression!r})", str(subtest))
                self.assertIn(regression, message)

    @kills.replace(
        checker._failure,
        "{mutation.regression} — this mutation of",
        "this mutation of",
        "the stale-or-broken message drops the regression, so it no longer says what went wrong",
        case="stale",
    )
    @kills.replace(
        checker._failure,
        "is {result.outcome}:",
        "is stale:",
        "a broken mutation is reported as stale, so the message sends its reader to the code rather than the mutation",
        case="broken",
    )
    @kills.replace(
        checker._failure,
        "is {result.outcome}:",
        "is broken:",
        "a stale mutation is reported as broken, so the message sends its reader to the mutation rather than the code",
        case="stale",
    )
    def test_a_mutation_the_test_could_not_judge_fails_it_with_the_reason(self):
        """Test that a stale or broken mutation fails the test, leading with the regression and then the reason."""
        for case, outcome, reason in (
            ("stale", Outcome.STALE, "FileNotFoundError: no such file"),
            ("broken", Outcome.BROKEN, "SyntaxError: invalid syntax"),
        ):
            with self.subTest(case=case):
                result = self.run_decorated_test(_MutationResult(outcome=outcome, reason=reason))
                self.assertEqual(len(result.failures), 1)
                message = result.failures[0][1]
                self.assertIn(
                    f"{_REGRESSION} — this mutation of {is_even.__module__}.{is_even.__name__} is {outcome}: {reason}",
                    message,
                )

    def test_a_test_checks_its_mutations_once_its_patches_have_stopped(self):
        """Test that a test checks its mutations outside a patch above its decorator and one its set-up entered."""
        cases = {
            "a patch above the decorator": (IsEvenTest, _PATCHED_ABOVE_TEST),
            "a patch in the set-up": (IsEvenPatchedInSetUpTest, _DECORATED_TEST),
        }
        for case, (test_class, test_name) in cases.items():
            with self.subTest(case):
                patched_while_checking: list[bool] = []

                def check(*_args: object, patched: list[bool] = patched_while_checking) -> _MutationResult:
                    patched.append(isinstance(is_even, Mock))
                    return _KILLED

                result = self.run_decorated_test(check, test_name, test_class=test_class)
                self.assertEqual((result.failures, result.errors), ([], []))
                self.assertEqual(patched_while_checking, [False])

    @kills.delete_statement(
        checker._unnamed_cases,
        'yield _NamingFailure(message="no registration names this case", description=(case.description,))',
        "the test passes with a case that its registrations leave unnamed, though nothing shows what that case guards",
        case="one registration names what the test checks outside its table",
    )
    @kills.force_false(
        checker._unnamed_registrations,
        "mutation.case is None",
        "a registration that does not name a case of the test's table passes, though nothing shows what it guards",
        case="one registration leaves its case unnamed",
    )
    @kills.force_true(
        checker._unnamed_cases,
        "not any(case.is_named(name) for name in case_names)",
        "every case of a table fails the test as unnamed, the ones its registrations name included",
        case="one registration names what the test checks outside its table",
    )
    @kills.force_true(
        checker._fail_unless_killed,
        "len(names) == len(mutations)",
        "where one registration leaves its case unnamed, the cases of the table fail the test as unnamed",
        case="one registration leaves its case unnamed",
    )
    @kills.replace(
        checker._fail_unless_killed,
        "if mutation.case is not None]",
        "if isinstance(mutation.case, str)]",
        "a registration naming what the test checks outside its table leaves the test's cases unchecked",
        case="one registration names what the test checks outside its table",
    )
    def test_a_case_no_registration_names_fails_the_test(self):
        """Test that a test fails on a case none of its registrations names, and on one that does not name a case."""
        stubbed_result = _MutationResult(outcome=Outcome.KILLED, cases=(*_TABLE_CASES, _UNNAMED_CASE))
        unnamed_registration = (
            "(regression='only the number five is reported as even')",
            "AssertionError: this registration does not name a case of the test's table",
        )
        unnamed_case = ("[(case='seven')]", "AssertionError: no registration names this case")
        for case, test_name, expected in (
            ("one registration leaves its case unnamed", _PARTLY_NAMED_TEST, [unnamed_registration]),
            ("one registration names what the test checks outside its table", _OUTSIDE_NAMED_TEST, [unnamed_case]),
        ):
            with self.subTest(case):
                result = self.run_decorated_test(stubbed_result, test_name, test_class=IsEvenTableTest)
                failures = [
                    (str(subtest).partition(") ")[2], traceback.strip().splitlines()[-1])
                    for subtest, traceback in result.failures
                ]
                self.assertEqual((failures, result.errors), (expected, []))

    @kills.force_false(
        checker._fail_unless_killed,
        "any(result.outcome is not Outcome.KILLED for result in results)",
        "a test whose mutation survived fails on its unnamed cases too, though the survivor fails it already",
    )
    def test_a_test_with_a_surviving_mutation_reports_no_unnamed_case(self):
        """Test that a test whose mutation survived fails on the survivor alone, rather than on an unnamed case too."""

        def check(mutation: Mutation, _test_name: str) -> _MutationResult:
            if mutation.case == "three":
                return _SURVIVED
            return _MutationResult(outcome=Outcome.KILLED, cases=(*_TABLE_CASES, _UNNAMED_CASE))

        result = self.run_decorated_test(check, _OUTSIDE_NAMED_TEST, test_class=IsEvenTableTest)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("did not kill this mutation", result.failures[0][1])

    def test_a_test_class_overriding_subtest_takes_no_part_in_the_checks(self):
        """Test that the checks run in unittest's own subTests, rather than in the one the test's class overrides."""
        with patch.object(IsEvenTest, "subTest") as overridden:
            result = self.run_decorated_test(_KILLED)
        self.assertEqual((result.failures, result.errors), ([], []))
        self.checked.assert_called_once()
        overridden.assert_not_called()

    def test_a_test_failing_of_its_own_accord_is_not_checked(self):
        """Test that a test already failing fails on that, rather than its mutation being reported as killed."""
        for case, test_name in (("raised", _DECORATED_TEST), ("in a subTest", _SUBTEST_DECORATED_TEST)):
            with self.subTest(case=case):
                with patch("tests.mutation_time.test_mutation.is_even", Mock(return_value=True)):
                    result = self.run_decorated_test(_KILLED, test_name)
                self.assertEqual(len(result.failures), 1)
                self.checked.assert_not_called()

    @kills.replace(
        checker._registration,
        'if os.environ.get(_CHECKED_TEST) == self.id() or os.environ.get(CHECKS_OFF) == "1":',
        "if os.environ.get(CHECKS_OFF):",
        "a test re-run against its own mutation checks its mutations again, so a run never ends",
        case="re-run against its mutation",
    )
    @kills.delete(
        checker._registration,
        ' or os.environ.get(CHECKS_OFF) == "1"',
        "a `just mutate` run checks the registered mutations too, so its kill list names tests it never broke",
        case="checks switched off",
    )
    def test_a_test_held_back_by_a_sentinel_checks_nothing(self):
        """Test that a test held back by either sentinel runs its body and checks nothing further."""
        cases = (
            ("re-run against its mutation", {_CHECKED_TEST: IsEvenTest(_DECORATED_TEST).id()}),
            ("checks switched off", {CHECKS_OFF: "1"}),
        )
        for case, sentinels in cases:
            with self.subTest(case=case):
                is_even_stub = Mock(return_value=False)
                with patch("tests.mutation_time.test_mutation.is_even", is_even_stub):
                    result = self.run_decorated_test(_KILLED, sentinels=sentinels)
                self.assertEqual((result.failures, result.errors), ([], []))
                is_even_stub.assert_called_once_with(3)
                self.checked.assert_not_called()

    @kills.replace(
        checker._sentinels,
        "(CHECKS_OFF, _CHECKED_TEST)",
        "(_CHECKED_TEST,)",
        "a test whose class clears the environment loses the sentinel, so `just mutate` checks it after all",
        case=CHECKS_OFF,
    )
    @kills.delete(
        checker._sentinels,
        " _CHECKED_TEST",
        "a rerun that clears the environment checks its registrations again, so a survivor passes as killed",
        case=_CHECKED_TEST,
    )
    def test_the_sentinels_survive_a_cleared_environment(self):
        """Test that patching the environment keeps both mutation-check sentinels, whatever else it clears."""
        for sentinel, value in ((CHECKS_OFF, "1"), (_CHECKED_TEST, _SUBJECT_TEST_NAME)):
            with self.subTest(sentinel), patch.dict(os.environ, {sentinel: value}, clear=True), patch_environ():
                self.assertEqual(os.environ.get(sentinel), value)

    @kills.replace(
        checker._registration,
        'if os.environ.get(_CHECKED_TEST) == self.id() or os.environ.get(CHECKS_OFF) == "1":',
        "if os.environ.get(_CHECKED_TEST) or os.environ.get(CHECKS_OFF):",
        "a decorated test the checked test reaches stands aside too, so its mutations go unchecked",
    )
    def test_a_test_the_sentinel_does_not_name_checks_its_mutations(self):
        """Test that a decorated test checks its mutations while another test is the one being checked."""
        result = self.run_decorated_test(_KILLED, sentinels={_CHECKED_TEST: _SUBJECT_TEST_NAME})
        self.assertEqual((result.failures, result.errors), ([], []))
        self.checked.assert_called_once()

    @kills.delete(
        checker._registration,
        "@functools.wraps(method)",
        "a decorated test reports under the wrapper's name, so a failing run names no test",
    )
    def test_the_decorated_test_reports_as_the_test_it_decorates(self):
        """Test that the wrapper carries the name and the docstring unittest prints for the test it decorates."""
        method = IsEvenTest.test_an_even_number
        wrapped = checker._registration(_ODD_REPORTED_AS_EVEN)(method)
        self.assertEqual((wrapped.__qualname__, wrapped.__doc__), (method.__qualname__, method.__doc__))


def _stand_in(_test_case: unittest.TestCase, *_args: object) -> None:
    """Stand in for the test that a registration under test decorates."""


class IsEvenPatchedInSetUpTest(unittest.TestCase):
    """Unit tests for deciding whether a number is even, with a patch the set-up enters, which `KillsTest` runs."""

    def setUp(self) -> None:
        """Patch the subject module's attribute until the test's cleanups run."""
        self.enterContext(patch("tests.mutation_time.mutation_subject.is_even"))

    @_KILLS_ODD_REPORTED_AS_EVEN
    def test_an_odd_number(self):
        """Test that an odd number is not even, while the set-up's patch hands the subject module a mock."""
        self.assertFalse(is_even(3))


class IsEvenTableTest(unittest.TestCase):
    """Unit tests for deciding whether a number is even, in tables whose cases a mutation can fail one at a time."""

    def test_odd_numbers(self):
        """Test that three and five are not even."""
        for case, number in (("three", 3), ("five", 5)):
            with self.subTest(case=case):
                self.assertFalse(is_even(number))

    @kills.replace(is_even, _EVEN, _ONLY_THREE, _ONLY_THREE_REGRESSION, case="three")
    @kills.replace(is_even, _EVEN, "number == 5", "only the number five is reported as even")
    def odd_numbers_partly_named_by_a_registration(self):
        """Test that three and five are not even, with one registration naming a case and the other naming none."""
        for case, number in (("three", 3), ("five", 5)):
            with self.subTest(case=case):
                self.assertFalse(is_even(number))

    @kills.replace(is_even, _EVEN, _ONLY_THREE, _ONLY_THREE_REGRESSION, case="three")
    @kills.replace(is_even, _EVEN, "number == 5", "only the number five is reported as even", case="five")
    @kills.replace(is_even, _EVEN, _ODD, _REGRESSION, case=OUTSIDE)
    def test_odd_numbers_and_two_each_named_by_a_registration(self):
        """Test that three and five are not even and two is, each case and the last check named by a registration."""
        for case, number in (("three", 3), ("five", 5)):
            with self.subTest(case=case):
                self.assertFalse(is_even(number))
        self.assertTrue(is_even(2))

    def test_odd_numbers_then_an_even_one(self):
        """Test that three and five are not even, and that two, which the table leaves out, is."""
        for case, number in (("three", 3), ("five", 5)):
            with self.subTest(case=case):
                self.assertFalse(is_even(number))
        self.assertTrue(is_even(2))

    def test_odd_numbers_by_parameters(self):
        """Test that three and five are not even, naming each case by more than one parameter."""
        for number in (3, 5):
            with self.subTest(number=number, parity="odd"):
                self.assertFalse(is_even(number))

    def test_two_then_odd_numbers(self):
        """Test that two is even, before the table tests that three and five are not."""
        self.assertTrue(is_even(2))
        for case, number in (("three", 3), ("five", 5)):
            with self.subTest(case=case):
                self.assertFalse(is_even(number))

    def test_odd_numbers_by_a_shared_message(self):
        """Test that three and five are not even, both cases given the same message."""
        for number in (3, 5):
            with self.subTest("odd", number=number):
                self.assertFalse(is_even(number))

    def test_odd_numbers_twice(self):
        """Test that three and five are not even, running each case twice."""
        for _ in range(2):
            for case, number in (("three", 3), ("five", 5)):
                with self.subTest(case=case):
                    self.assertFalse(is_even(number))

    def test_odd_numbers_in_groups(self):
        """Test that three and five are not even, in a table nested in a table of groups."""
        for group, numbers in (("low", (3,)), ("high", (5,))):
            with self.subTest(group):
                for number in numbers:
                    with self.subTest(number=number):
                        self.assertFalse(is_even(number))

    def test_odd_numbers_by_message(self):
        """Test that three and five are not even, naming each case by its message."""
        for case, number in (("three", 3), ("five", 5)):
            with self.subTest(case):
                self.assertFalse(is_even(number))


class KillsTestLoaderTest(unittest.TestCase):
    """Unit tests for the loader of the tests that carry a kills decorator."""

    def test_only_the_tests_carrying_a_kills_decorator_are_loaded(self):
        """Test that the loader loads only the tests carrying a kills decorator, beneath a patch or not."""
        self.assertEqual(
            sorted(KillsTestLoader().getTestCaseNames(IsEvenTest)),
            sorted(
                (
                    _DECORATED_TEST,
                    _SUBTEST_DECORATED_TEST,
                    "test_an_odd_number_patched_beneath_the_decorator",
                    _PATCHED_ABOVE_TEST,
                    _SEVERAL_MUTATIONS_TEST,
                    _STACKED_TEST,
                    _STACKED_ACROSS_A_PATCH_TEST,
                    "test_an_odd_number_against_a_mutation_that_raises",
                )
            ),
        )
