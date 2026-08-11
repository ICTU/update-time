"""Local fixit rules, enabled via `[tool.fixit]` in pyproject.toml."""

import inspect
import re
import unittest

import libcst as cst
import libcst.matchers as m
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

# The number of positional arguments an equality assertion compares, and so the fewest it can be reported on.
_COMPARED_ARGUMENTS = 2

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


def _self_call_names(node: cst.CSTNode) -> set[str]:
    """Return the names of the `self.<name>(...)` methods called anywhere inside the node."""
    calls = m.findall(node, m.Call(func=m.Attribute(value=m.Name("self"))))
    functions = (cst.ensure_type(call, cst.Call).func for call in calls)
    return {cst.ensure_type(function, cst.Attribute).attr.value for function in functions}


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
            expected_replacement=_test_method('expected: str = "Version 1.0"', "self.assertEqual(changes(), expected)"),
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
        if len(positional) < _COMPARED_ARGUMENTS:
            return
        expected, actual = positional[0], positional[1]
        if not self._reads_as_expected(expected.value) or self._reads_as_expected(actual.value):
            return
        arguments = list(node.args)
        arguments[arguments.index(expected)] = expected.with_changes(value=actual.value)
        arguments[arguments.index(actual)] = actual.with_changes(value=expected.value)
        self.report(node, replacement=node.with_changes(args=arguments))


class SubTestPerCase(LintRule):
    """Require a loop that asserts to name each of its cases with a `subTest`.

    A loop over a table of cases runs one assertion per case, so without a `subTest` the first case to fail hides
    every case after it and the failure names none of them. A `subTest` per case makes each one run and report
    under its own label.
    """

    MESSAGE = "Wrap each case in `with self.subTest(...)`, so a failing case names itself and the rest still run"

    VALID = [
        # A loop that asserts nothing has no cases to name.
        Valid(_test_method("for line in lines:", "    self.rewrite(line)")),
        # A nested case table, its cases named on the inner loop.
        Valid(
            _test_method(
                "for status in statuses:",
                "    for key in keys:",
                "        with self.subTest(status=status, key=key):",
                "            self.assertEqual(changes(status, key), expected)",
            )
        ),
    ]
    INVALID = [
        Invalid(_test_method("for name in names:", "    self.assertTrue(matches(name))")),
    ]

    def visit_For(self, node: cst.For) -> None:
        """Report a loop that asserts somewhere inside it without calling `subTest` anywhere inside it.

        Looking anywhere inside the loop means a nested case table is reported at most once: a `subTest` on the
        inner loop keeps the outer loop from being reported as well.
        """
        called = _self_call_names(node)
        if any(name.startswith("assert") for name in called) and "subTest" not in called:
            self.report(node)


# An identifier, as the code spells a name, and an identifier a docstring quotes between backticks. A backticked
# run holding anything else — a `path:line`, an `# update-time: ignore` marker — is skipped here, and the twin
# check would skip it in any case, since no name the code binds has that shape.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BACKTICKED_NAME = re.compile(f"`({_IDENTIFIER.pattern})`")


def _twin(name: str) -> str:
    """Return the name under the opposite visibility: the private spelling of a public name, and the other way on."""
    return name.removeprefix("_") if name.startswith("_") else f"_{name}"


def _quoted_words(module: cst.Module) -> set[str]:
    """Return the identifier-shaped words the module's strings spell out, the backticked runs in them left out.

    A word spelled out in a string is data the module works with — a path in a URL, a key, a word of a message —
    so a docstring quoting that word quotes the data rather than a name the code binds.
    """
    strings = m.findall(module, m.SimpleString() | m.FormattedString())
    sources = (_BACKTICKED_NAME.sub(" ", module.code_for_node(string)) for string in strings)
    return {word for source in sources for word in _IDENTIFIER.findall(source)}


_QUERY_ENDPOINT = '''\
def _query(reference):
    return {"version": reference}


def reported_vulnerabilities(reference):
    """Return what the `query` endpoint reports for the reference."""
    return fetch("https://api.osv.dev/v1/query", json=_query(reference))
'''

_USED_NAME = '''\
def _pin(reference):
    return reference


def pin(references):
    """Return each reference pinned by `_pin`."""
    return [_pin(reference) for reference in references]
'''

_NAME_FROM_ANOTHER_MODULE = '''\
def changes(dependency):
    """Return what `get_changes` reports for the dependency."""
    return dependency
'''

_RENAMED_PIN = '''\
def _latest_pin(reference):
    return reference


class PinUpdater:
    def update_line(self, match):
        """Return the line, unchanged in each case `latest_pin` declines."""
        return _latest_pin(match)
'''

_MADE_PUBLIC = '''\
def helper(value):
    return value


def caller(value):
    """Return the value `_helper` produced."""
    return helper(value)
'''


class RenamedNameInDocstring(LintRule):
    """Require a name a docstring quotes between backticks to be spelled the way the code spells it.

    Renaming a name leaves the docstrings quoting it untouched, `just rename` by design, so the old spelling
    survives there and nothing else reports it. A quoted name the code has only under the opposite visibility is
    one of those leftovers.
    """

    VALID = [
        Valid(_QUERY_ENDPOINT),  # A word a string spells out is data, whatever the code binds beside it.
        Valid(_USED_NAME),  # The code mentions the quoted name itself, so no rename moved it.
        Valid(_NAME_FROM_ANOTHER_MODULE),  # Neither spelling is here, so the name belongs to another module.
    ]
    INVALID = [
        Invalid(_RENAMED_PIN),  # The name was made private, and the docstring kept the public spelling.
        Invalid(_MADE_PUBLIC),  # The same the other way on: made public, and the docstring kept the private one.
    ]

    def __init__(self) -> None:
        """Start with no names, until the module they are read from is visited."""
        super().__init__()
        self._code_names: set[str] = set()
        self._quoted_words: set[str] = set()

    def visit_Module(self, node: cst.Module) -> None:
        """Collect what the module names and what it spells out, then check its own docstring against them."""
        self._code_names = {cst.ensure_type(name, cst.Name).value for name in m.findall(node, m.Name())}
        self._quoted_words = _quoted_words(node)
        self._report_renamed(node)

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        """Check the function's docstring here, so a report lands on the function rather than on the module."""
        self._report_renamed(node)

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        """Check the class's docstring here, so a report lands on the class rather than on the module."""
        self._report_renamed(node)

    def _report_renamed(self, node: cst.Module | cst.FunctionDef | cst.ClassDef) -> None:
        """Report each name the docstring quotes that the code mentions only under the opposite visibility.

        A word the module's strings spell out is data it works with, whatever the code binds beside it, so a
        docstring quoting that word is left alone.
        """
        for name in _BACKTICKED_NAME.findall(node.get_docstring(clean=False) or ""):
            if name in self._quoted_words:
                continue
            if name not in self._code_names and _twin(name) in self._code_names:
                self.report(node, message=f"Rewrite `{name}` as `{_twin(name)}`: a rename left the docstring behind")


class SubTestOutsideLoop(LintRule):
    """Require a `subTest` in an `assert*` helper to sit inside a loop over cases the helper itself owns.

    Wrapping a helper's whole body in a `subTest` makes an `assert*` method record a failure instead of raising
    one, so a caller asserting a single case carries on as though it had passed. Where a `subTest` does belong is
    a helper that loops over cases of its own.
    """

    MESSAGE = "Move the `subTest` onto the loop over the cases, so this helper still raises for a single case"

    VALID = [
        # A helper that owns the loop names each of its cases with it.
        Valid(
            "def assert_layers(self, *layers):\n"
            "    for layer in layers:\n"
            "        with self.subTest(layer=layer):\n"
            "            self.assertTrue(depends_on(layer))\n"
        ),
    ]
    INVALID = [
        # A helper asserting one case must raise, rather than record the failure and let its caller continue.
        Invalid(
            "def assert_invalid_item(self, item):\n"
            "    with self.subTest(item=item):\n"
            "        self.assertTrue(reported(item))\n"
        ),
    ]

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        """Report an `assert*` helper that calls `subTest` without a loop of its own carrying one."""
        if not node.name.value.startswith("assert"):
            return
        loops = m.findall(node, m.For())
        if "subTest" in _self_call_names(node) and not any("subTest" in _self_call_names(loop) for loop in loops):
            self.report(node)
