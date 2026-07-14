"""Local fixit rules, enabled via `[tool.fixit]` in pyproject.toml."""

import libcst as cst
from fixit import Invalid, LintRule, Valid

# The unittest assertion methods that compare an actual value against an expected one.
_EQUALITY_ASSERT_METHODS = frozenset(
    {
        "assertEqual",
        "assertNotEqual",
        "assertAlmostEqual",
        "assertNotAlmostEqual",
        "assertListEqual",
        "assertTupleEqual",
        "assertSetEqual",
        "assertDictEqual",
        "assertSequenceEqual",
        "assertCountEqual",
        "assertMultiLineEqual",
    }
)

_LITERAL_NAMES = frozenset({"True", "False", "None"})
_LITERAL_TYPES = (cst.SimpleString, cst.ConcatenatedString, cst.FormattedString, cst.Integer, cst.Float, cst.Imaginary)


def _is_expected_like(node: cst.BaseExpression) -> bool:
    """Return whether the expression is hard-coded, so it reads as the expected value of an equality assertion."""
    if isinstance(node, _LITERAL_TYPES):
        return True
    if isinstance(node, cst.Name):
        return node.value in _LITERAL_NAMES
    if isinstance(node, (cst.List, cst.Tuple, cst.Set)):
        return all(_is_expected_like(element.value) for element in node.elements)
    if isinstance(node, cst.Dict):
        return all(
            isinstance(element, cst.DictElement)
            and _is_expected_like(element.key)
            and _is_expected_like(element.value)
            for element in node.elements
        )
    if isinstance(node, cst.UnaryOperation):
        return _is_expected_like(node.expression)
    if isinstance(node, cst.BinaryOperation):
        return _is_expected_like(node.left) and _is_expected_like(node.right)
    return False


class AssertEqualActualFirst(LintRule):
    """Require the arguments of equality assertions in (actual, expected) order.

    This mirrors SonarCloud rule S3415 and the Python documentation's convention
    (`self.assertEqual('foo'.upper(), 'FOO')`): when an assertion compares a computed value against a hard-coded
    one, the computed (actual) value comes first. Like SonarCloud, the rule only fires when it can tell the two
    apart: a hard-coded first argument and a computed second one.
    """

    MESSAGE = "Pass the actual value first and the expected value second: assertEqual(actual, expected)"

    VALID = [
        Valid('self.assertEqual(function(), "expected")'),
        Valid("self.assertEqual(expected, function())"),  # A variable can't be recognised as the expected value.
        Valid('self.assertEqual("either", "or")'),  # Two hard-coded values can't be told apart.
        Valid("self.assertEqual(function(), other_function())"),
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
    ]

    def visit_Call(self, node: cst.Call) -> None:
        """Report an assertion whose hard-coded expected value precedes its computed actual value, with the fix."""
        if not (isinstance(node.func, cst.Attribute) and node.func.attr.value in _EQUALITY_ASSERT_METHODS):
            return
        positional = [argument for argument in node.args if argument.keyword is None and not argument.star]
        if len(positional) < 2:
            return
        expected, actual = positional[0], positional[1]
        if not _is_expected_like(expected.value) or _is_expected_like(actual.value):
            return
        arguments = list(node.args)
        arguments[arguments.index(expected)] = expected.with_changes(value=actual.value)
        arguments[arguments.index(actual)] = actual.with_changes(value=expected.value)
        self.report(node, replacement=node.with_changes(args=arguments))
