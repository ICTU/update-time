"""Local fixit rules, enabled via `[tool.fixit]` in pyproject.toml."""

import inspect
import unittest

import libcst as cst
from fixit import Invalid, LintRule, Valid


def _compares_actual_to_expected(assert_method_name: str) -> bool:
    """Return whether the `unittest.TestCase` assertion method compares an actual value against an expected one.

    These assertions are recognisable by their symmetric parameter names: either `first` and `second` (such as
    `assertEqual`) or a numbered pair (such as `list1` and `list2` of `assertListEqual`). Order-sensitive
    comparisons such as `assertGreaterEqual` (`a` and `b`) and asymmetric assertions such as `assertIn`
    (`member` and `container`) don't follow this naming pattern.
    """
    method = getattr(unittest.TestCase, assert_method_name)
    parameters = list(inspect.signature(method).parameters)[1:3]
    return parameters == ["first", "second"] or tuple(parameter[-1] for parameter in parameters) == ("1", "2")


_EQUALITY_ASSERT_METHODS = frozenset(
    name for name in dir(unittest.TestCase) if name.startswith("assert") and _compares_actual_to_expected(name)
)

_LITERAL_NAMES = frozenset({"True", "False", "None"})
_LITERAL_TYPES = (cst.SimpleString, cst.ConcatenatedString, cst.FormattedString, cst.Integer, cst.Float, cst.Imaginary)
_DISPLAY_TYPES = (cst.List, cst.Tuple, cst.Set, cst.Dict)  # Collections written out, not built by a comprehension


def _is_expected_like(node: cst.BaseExpression) -> bool:
    """Return whether the expression is written out in the assertion, so it reads as its expected value.

    A collection written out as a list, tuple, set, or dict counts however its elements are produced: the assertion
    lays out the value it expects, instead of getting that value from the code under test.
    """
    if isinstance(node, (*_LITERAL_TYPES, *_DISPLAY_TYPES)):
        return True
    if isinstance(node, cst.Name):
        return node.value in _LITERAL_NAMES
    if isinstance(node, cst.UnaryOperation):
        return _is_expected_like(node.expression)
    if isinstance(node, cst.BinaryOperation):
        return _is_expected_like(node.left) and _is_expected_like(node.right)
    return False


def _test_method(*body: str) -> str:
    """Return a test method with the given lines as its body, for a test case that needs more than one statement."""
    return "def test_changes(self):\n" + "".join(f"    {line}\n" for line in body)


class AssertEqualActualFirst(LintRule):
    """Require the arguments of equality assertions in (actual, expected) order.

    This mirrors SonarCloud rule S3415 and the Python documentation's convention
    (`self.assertEqual('foo'.upper(), 'FOO')`): when an assertion compares a computed value against one written out
    in the test, the computed (actual) value comes first. Like SonarCloud, the rule only fires when it can tell the
    two apart: a computed second argument, and a first argument that is either written out in the assertion or held
    by a variable the test bound to a written-out value.
    """

    MESSAGE = "Pass the actual value first and the expected value second: assertEqual(actual, expected)"

    VALID = [
        Valid('self.assertEqual(function(), "expected")'),
        Valid("self.assertEqual(expected, function())"),  # A variable the test doesn't bind can't be recognised.
        # A variable holding a computed value is not the expected one, however it is named.
        Valid(_test_method("expected = changes()", "self.assertEqual(expected, other_changes())")),
        Valid('self.assertEqual("either", "or")'),  # Two hard-coded values can't be told apart.
        Valid("self.assertEqual(function(), other_function())"),
        Valid("self.assertEqual([item for item in items], expected)"),  # A comprehension is computed, not written out.
    ]
    INVALID = [
        Invalid(
            'self.assertEqual("expected", function())',
            expected_replacement='self.assertEqual(function(), "expected")',
        ),
        Invalid(
            "self.assertEqual([1, 2], function(), 'message')",
            expected_replacement="self.assertEqual(function(), [1, 2], 'message')",
        ),
        Invalid(
            'self.assertEqual([Path("/file.txt")], list(glob("*.txt")))',
            expected_replacement='self.assertEqual(list(glob("*.txt")), [Path("/file.txt")])',
        ),
        # The actual value is an attribute rather than a call.
        Invalid(
            "self.assertEqual([call(first)], persist.call_args_list)",
            expected_replacement="self.assertEqual(persist.call_args_list, [call(first)])",
        ),
        # The expected value is written out into a variable first, as a long one usually is.
        Invalid(
            _test_method('expected = "Version 1.0"', "self.assertEqual(expected, changes())"),
            expected_replacement=_test_method('expected = "Version 1.0"', "self.assertEqual(changes(), expected)"),
        ),
        # An annotated assignment binds the expected value just as a plain one does.
        Invalid(
            _test_method('expected: str = "Version 1.0"', "self.assertEqual(expected, changes())"),
            expected_replacement=_test_method(
                'expected: str = "Version 1.0"', "self.assertEqual(changes(), expected)"
            ),
        ),
        # An annotation carrying no value binds nothing, so it leaves the earlier binding in place.
        Invalid(
            _test_method('expected = "Version 1.0"', "expected: str", "self.assertEqual(expected, changes())"),
            expected_replacement=_test_method(
                'expected = "Version 1.0"', "expected: str", "self.assertEqual(changes(), expected)"
            ),
        ),
    ]

    def __init__(self) -> None:
        """Start with one frame of tracked names, for the assertions that sit outside any function."""
        super().__init__()
        self._written_out_names: list[set[str]] = [set()]

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        """Track the names this function binds separately, so those of an enclosing function don't leak into it."""
        self._written_out_names.append(set())

    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:
        """Forget the names bound by the function that ends here."""
        self._written_out_names.pop()

    def visit_Assign(self, node: cst.Assign) -> None:
        """Track every name the statement binds, since `first = second = value` binds more than one."""
        bound_names = {target.target.value for target in node.targets if isinstance(target.target, cst.Name)}
        self._track(bound_names, node.value)

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        """Track an annotated assignment the same way as a plain one, when it carries a value to bind."""
        if node.value is not None and isinstance(node.target, cst.Name):
            self._track({node.target.value}, node.value)

    def _track(self, bound_names: set[str], value: cst.BaseExpression) -> None:
        """Remember the names bound to a written-out value, and forget those bound to a computed value instead."""
        names = self._written_out_names[-1]
        if _is_expected_like(value):
            names |= bound_names
        else:
            names -= bound_names

    def _reads_as_expected(self, node: cst.BaseExpression) -> bool:
        """Return whether the expression reads as the expected value: written out here, or bound to one earlier."""
        return _is_expected_like(node) or (isinstance(node, cst.Name) and node.value in self._written_out_names[-1])

    def visit_Call(self, node: cst.Call) -> None:
        """Report an assertion whose expected value precedes its computed actual value, with the fix."""
        if not (isinstance(node.func, cst.Attribute) and node.func.attr.value in _EQUALITY_ASSERT_METHODS):
            return
        positional = [argument for argument in node.args if argument.keyword is None and not argument.star]
        if len(positional) < 2:
            return
        expected, actual = positional[0], positional[1]
        if not self._reads_as_expected(expected.value) or self._reads_as_expected(actual.value):
            return
        arguments = list(node.args)
        arguments[arguments.index(expected)] = expected.with_changes(value=actual.value)
        arguments[arguments.index(actual)] = actual.with_changes(value=expected.value)
        self.report(node, replacement=node.with_changes(args=arguments))
