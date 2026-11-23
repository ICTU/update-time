"""Make a test kill the mutations it is meant to kill, so that an ordinary test run keeps the evidence.

A green test proves nothing on its own: it may assert what the code cannot get wrong, or mock away the collaborator
it claims to check. Breaking the behaviour is the evidence. A decorated test runs its own body, then puts each of the
mutations it registers in place in turn and runs itself against the mutated code, failing unless that breaks it. Each
mutation names the regression it stands in for, so a test that does not kill a mutation reports its regression. A
mutation is applied in memory, so no file on disk is touched and an interrupted run cannot leave a source file broken.
"""

import ast
import functools
import importlib
import importlib.machinery
import importlib.util
import inspect
import os
import re
import sys
import types
import unittest
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, TypeVar, Unpack, cast
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

# Holds the id of the test being re-run against its own mutation. That test must run its body and stop there:
# checking its mutations again would check them against themselves, without end. Only the test it names stands
# aside, so a decorated test the re-run reaches on its way is checked as usual. An environment variable rather than
# a global, because checking a mutation of this module drops it from `sys.modules` and imports it again.
_CHECKED_TEST = "_MUTATION_TIME_CHECKED_TEST"

# Set for the whole of a `just mutate` run, which applies a mutation of its own: the registered mutations stand aside,
# so that run's kill list holds the tests that failed on the mutation it was given.
CHECKS_OFF = "_MUTATION_TIME_CHECKS_OFF"

# The attribute a decorated test carries, holding the mutations it registers. A `kills` stacked above finds it there
# and adds its mutation. A decorator written between the two holds a copy of it, since `functools.wraps` copies the
# attributes of whatever it wraps.
_REGISTERED = "_registered_mutations"

_Method = TypeVar("_Method", bound="Callable[..., object]")
_Value = TypeVar("_Value")


class _Function(Protocol):
    """A function or a method, carrying the module and the qualified name it was defined under."""

    __module__: str
    __qualname__: str


# A mutation changes the code of a module, or of a definition the module holds.
type _Anchor = types.ModuleType | _Function | types.MethodType | property | functools.cached_property[Any]


class _Outside(Enum):
    """What a test checks outside its table, for a mutation to name in place of a case."""

    OUTSIDE = "the checks outside the table"


OUTSIDE = _Outside.OUTSIDE

# A mutation names a case of the test's table, or what the test checks outside it.
type _CaseName = str | _Outside


class Outcome(StrEnum):
    """What checking a mutation showed.

    KILLED and SURVIVED judge the test: it failed against the mutation, or it did not. STALE and BROKEN judge the
    mutation instead, and call for rewriting the mutation rather than the test. Stale means it was never applied:
    reading or parsing the file failed, the source lacks the anchor, the anchor holds the snippet other than once,
    the replacement equals the snippet, the two open on the same indentation, or the anchor is a module while one
    function holds the snippet. Stale also means the mutation names a case that the test has more than once, or
    not at all.
    Broken means the mutated source did not import, or the test errored with an error other than the one the
    mutation declares.
    """

    KILLED = "killed"
    SURVIVED = "survived"
    STALE = "stale"
    BROKEN = "broken"


@dataclass(frozen=True)
class Case:
    """A case of a test's table, known by the message and the parameters its subTest was given."""

    message: str | None
    params: dict[str, object]
    description: str  # What unittest prints for a case that fails: its message and its parameters

    def is_named(self, name: str) -> bool:
        """Return whether the name is the case's message, its description, or the value of its only parameter."""
        return name in (self.message, self.description) or list(self.params.values()) == [name]


@dataclass(frozen=True)
class Result:
    """What checking a mutation showed, and what went wrong where the outcome does not say.

    Stale and broken each have several causes, so a result of either carries the one that applied. Killed and
    survived speak for themselves. A killed result carries the cases the test's table ran.
    """

    outcome: Outcome
    reason: str = ""
    cases: tuple[Case, ...] = ()


@dataclass(frozen=True)
class Mutation:
    """A change to a source file and the regression it stands in for.

    The regression names what breaks when the change is made. It explains the failure when a test does not kill
    the mutation.

    A regression that crashes makes the test raise rather than fail, and so does a mutation that broke the test's
    scaffolding. Naming the error in `raises` tells the two apart: the test kills the mutation by raising that
    error, while any other error is reported as broken.

    A mutation that names a case of the test's table is judged on that case alone, whatever the other cases report.
    One that names `OUTSIDE` is judged on what the test checks outside its table.

    The anchor names the code to change: a module, or a class, function, method, classmethod, property or cached
    property the module holds. The qualified name reaches a member through its class, and only the source of the
    definition is changed. A module anchor is for a snippet that spans several definitions or lies outside them. A
    module anchor is stale when one function or method holds the snippet.
    """

    anchor: _Anchor
    old: str
    new: str
    regression: str
    raises: str = ""
    case: _CaseName | None = None

    @property
    def _unwrapped(self) -> types.ModuleType | _Function | types.MethodType:
        """Return the anchor in the form that carries its names: a property's getter, a cached property's function."""
        if isinstance(self.anchor, functools.cached_property):
            return cast("_Function", self.anchor.func)
        return cast("_Function", self.anchor.fget) if isinstance(self.anchor, property) else self.anchor

    @property
    def module(self) -> types.ModuleType:
        """Return the module whose source the mutation changes.

        A function anchor is resolved through `sys.modules`, so read this before a purge rather than after one.
        """
        anchor = self._unwrapped
        if isinstance(anchor, types.ModuleType):
            return anchor
        return sys.modules[anchor.__module__]

    @property
    def _qualified_name(self) -> str:
        """Return the qualified name of the definition the anchor names, or the empty string for a module anchor."""
        anchor = self._unwrapped
        return "" if isinstance(anchor, types.ModuleType) else anchor.__qualname__

    @property
    def anchor_name(self) -> str:
        """Return the name a report calls the anchor by: the module's name, with the qualified name after it."""
        return ".".join(filter(None, (self.module.__name__, self._qualified_name)))

    @property
    def path(self) -> str:
        """Return the file the module was loaded from."""
        return self.module.__file__ or ""

    def _source(self) -> str:
        """Return the source of the file the mutation changes."""
        try:
            return _READ_TEXT(Path(self.path))
        except OSError as error:
            raise StaleError(_reason(error)) from error

    def _mutated(self, source: str) -> str:
        """Return the source with the snippet replaced inside the anchor's span."""
        mutated = mutated_source(source, self._qualified_name, self.old, self.new)
        if not self._qualified_name and (function := _function_holding(source, self.old)):
            message = f"the snippet lies inside {function}, so anchor the mutation on {function}"
            raise StaleError(message)
        return mutated

    @property
    def lines(self) -> range:
        """Return the numbers of the lines the snippet covers in its file, counted from one."""
        source = self._source()
        offset = _occurrences(source, self._qualified_name, self.old)[0]
        first = source.count("\n", 0, offset) + 1
        return range(first, first + self.old.count("\n") + 1)

    def check(self, test_name: str) -> Result:
        """Return what checking the mutation showed: whether the test or its case fails, or the file has moved on.

        Whatever the mutation is at fault for comes back as an outcome to report: a source it cannot be applied to,
        one that will not import. Anything else raises, since a defect in the checking is not the mutation's to
        answer for.
        """
        try:
            source = self._source()
            mutated = self._mutated(source)
        except StaleError as error:
            return Result(Outcome.STALE, str(error))
        try:
            run = self._run_test(test_name, source, mutated)
        except _SourceError as error:
            return Result(Outcome.BROKEN, str(error))
        try:
            failures, errors = self._reports(run)
        except StaleError as error:
            return Result(Outcome.STALE, str(error))
        if errors:
            raised = _raised(errors)
            if raised != self.raises:
                return Result(Outcome.BROKEN, raised)
        if not failures and not errors:
            return Result(Outcome.SURVIVED)
        return Result(Outcome.KILLED, cases=tuple(run.cases))

    def _reports(self, run: _RecordedRun) -> tuple[_Reports, _Reports]:
        """Return the failures and the errors to judge the mutation on: those of the case it names, or of the test.

        A test that stopped before its table is judged on what it reported outside the table. Where the test has no
        case of the name, or more than one, the name is stale.
        """
        if not isinstance(self.case, str):
            return run.reports(self.case)
        count = run.cases_named(self.case)
        if count > 1:
            message = f"the test has {count} cases named {self.case!r}"
            raise StaleError(message)
        if count:
            return run.reports(self.case)
        outside_failures, outside_errors = run.reports(OUTSIDE)
        if not run.cases and (outside_failures or outside_errors):
            return outside_failures, outside_errors
        message = f"the test has no case named {self.case!r}"
        raise StaleError(message)

    def _failed_in(self, run: _RecordedRun) -> bool:
        """Return whether what the mutation is judged on failed in the run, without raising."""
        try:
            failures, errors = self._reports(run)
        except StaleError:
            return False
        return bool(failures) and not errors

    def _function(self) -> types.FunctionType | None:
        """Return the function whose code the mutation changes, or None for a module or a class anchor.

        A mutation of this module gets None as well: swapping its code in place would change the check that runs it.
        """
        anchor = self._unwrapped
        unbound = anchor.__func__ if isinstance(anchor, types.MethodType) else anchor
        function = inspect.unwrap(unbound) if callable(unbound) else unbound
        if not isinstance(function, types.FunctionType) or function.__module__ == __name__:
            return None
        return function

    def _mutated_function(self, source: str, mutated: str) -> types.FunctionType | None:
        """Return the anchored function as the mutated source defines it, or None where that won't run or define it."""
        start, end = _span(source, self._qualified_name)
        # The snippet lies inside the span, so the mutated definition ends where the replacement moved the end to.
        end += len(self.new) - len(self.old)
        line_start = mutated.rfind("\n", 0, start) + 1
        lines_above = mutated.count("\n", 0, line_start)
        # An indented definition is nested in an `if` on the line above it, so its strings keep their indentation.
        header = "\n" * (lines_above - 1) + "if True:\n" if start > line_start else "\n" * lines_above
        definition = header + mutated[line_start:end]
        defined: dict[str, Any] = {}
        try:
            exec(compile(definition, self.path, "exec"), dict(vars(self.module)), defined)  # noqa: S102 # nosec
        except Exception:  # noqa: BLE001  # Executing the module afresh reports what the definition raises.
            return None
        return defined.get(self._qualified_name.rsplit(".", maxsplit=1)[-1])

    def _mutated_assignments(self, source: str, mutated: str) -> dict[str, object] | None:
        """Return the names the module-level assignments around the snippet bind, as the mutated source binds them.

        That is None for an anchor other than a module, for this module, and where the replacement reaches a statement
        other than an assignment to plain names, which would change a shared object rather than a name. It is None as
        well where the mutated statements do not parse or raise when executed.
        """
        if self._qualified_name or self.module.__name__ == __name__:
            return None
        start = _occurrences(source, "", self.old)[0]
        try:
            statements = _statements_overlapping(mutated, start, start + len(self.new))
        except SyntaxError:
            return None
        names = [_names_assigned(statement) for statement in statements]
        if not names or None in names:
            return None
        namespace = dict(vars(self.module))
        try:
            exec(compile(ast.Module(body=statements, type_ignores=[]), self.path, "exec"), namespace)  # noqa: S102 # nosec
        except Exception:  # noqa: BLE001  # Executing the module afresh reports what the statements raise.
            return None
        return {name: namespace[name] for assigned in names if assigned for name in assigned}

    def _run_test(self, test_name: str, source: str, mutated: str) -> _RecordedRun:
        """Run the test against the mutated source, with the anchored function's definition swapped in place.

        A module anchor on module-level assignments has the names they bind swapped instead. The test runs against the
        module executed afresh as well where what the mutation is judged on raises or does not fail against them. A
        constant computed from the mutated one, or a name another module imported, still holds the original value. The
        module is executed afresh where neither can be swapped: for any other module or a class anchor, for a function
        of this module, and for a mutated definition that does not parse, raises when executed, defines another name,
        or needs other free variables than the function has.
        """
        function = self._function()
        if function is None and (assignments := self._mutated_assignments(source, mutated)) is not None:
            result = self._run_test_with_names_bound(test_name, assignments)
            if self._failed_in(result):
                return result
            return self._run_test_afresh(test_name, mutated)
        mutated_function = None if function is None else self._mutated_function(source, mutated)
        if function is None or mutated_function is None:
            return self._run_test_afresh(test_name, mutated)
        original = _definition(function)
        try:
            _define(function, _definition(mutated_function))
        except ValueError:  # The code needs other free variables than the function has, such as a method's class.
            return self._run_test_afresh(test_name, mutated)
        try:
            return self._run_test_in_place(test_name)
        finally:
            _define(function, original)

    def _run_test_with_names_bound(self, test_name: str, names: dict[str, object]) -> _RecordedRun:
        """Run the test with the module's names bound to the mutated values, binding the original values again after."""
        namespace = vars(self.module)
        originals = {name: namespace[name] for name in names if name in namespace}
        namespace.update(names)
        try:
            return self._run_test_in_place(test_name)
        finally:
            namespace.update(originals)
            for name in names.keys() - originals.keys():
                del namespace[name]

    def _run_test_in_place(self, test_name: str) -> _RecordedRun:
        """Run the test with its modules loaded as they are, a cached anchor forgetting its answers before and after."""
        self._clear_cache()
        try:
            return _recorded_run(test_name)
        finally:
            self._clear_cache()

    def _clear_cache(self) -> None:
        """Forget the answers a cached anchor remembers, so the test is not served one the other code gave."""
        if cache_clear := getattr(self._unwrapped, "cache_clear", None):
            cache_clear()

    def _run_test_afresh(self, test_name: str, mutated: str) -> _RecordedRun:
        """Run the test with the mutated source installed under the module's name, and bound on its parent package.

        The module is read before the purge drops it, and `sys.modules` is put back however the run ends, so a mutated
        module never outlives it.
        """
        module = self.module
        imported = dict(sys.modules)
        try:
            self._purge(test_name)
            _bind_on_parent(_executed_module(mutated, module))
            return _recorded_run(test_name)
        finally:
            sys.modules.clear()
            sys.modules.update(imported)

    def _purge(self, test_name: str) -> None:
        """Drop the packages of the mutated module and of the test from `sys.modules`, so the test is imported again.

        Dropping the test's own module is not enough. A module that imported the mutated one holds the original, and
        hands that to the test whenever the test reaches the mutated code through it.
        """
        roots = {_root(self.module.__name__), _root(test_name)}
        for name in [name for name in sys.modules if _root(name) in roots]:
            del sys.modules[name]


# What a function's definition sets apart from its name: its code, and the defaults its signature evaluates.
type _Definition = tuple[types.CodeType, tuple[object, ...] | None, dict[str, object] | None]


def _definition(function: types.FunctionType) -> _Definition:
    """Return the code and the defaults the function was defined with."""
    return function.__code__, function.__defaults__, function.__kwdefaults__


def _define(function: types.FunctionType, definition: _Definition) -> None:
    """Give the function the code and the defaults of the definition, the code first, which Python may refuse."""
    function.__code__, function.__defaults__, function.__kwdefaults__ = definition


def _root(name: str) -> str:
    """Return the top-level package of the dotted module or test name."""
    return name.split(".", maxsplit=1)[0]


def mutated_source(source: str, qualified_name: str, old: str, new: str) -> str:
    """Return the source with the snippet replaced inside the definition the qualified name points at.

    An empty qualified name stands for the whole source. The anchor must hold the snippet exactly once, so that a
    mutation cannot break a line it was not aimed at. Whatever stops it from being applied raises `StaleError`.
    """
    if new == old:
        message = "the snippet and its replacement are the same, so nothing changes"
        raise StaleError(message)
    if old[:1] in (" ", "\t") and new[:1] == old[:1]:
        message = "the snippet and its replacement open on the same indentation, so drop that indentation from both"
        raise StaleError(message)
    if len(offsets := _occurrences(source, qualified_name, old)) != 1:
        message = f"the snippet occurs {len(offsets)} times rather than once"
        raise StaleError(message)
    return source[: offsets[0]] + new + source[offsets[0] + len(old) :]


def _occurrences(source: str, qualified_name: str, snippet: str) -> list[int]:
    """Return the offset of each occurrence of the snippet in the code of the definition the name points at.

    An occurrence inside a docstring is left out, since a docstring may quote the code it describes.
    """
    start, end = _span(source, qualified_name)
    docstrings = _docstrings(source)
    return [
        match.start()
        for match in re.compile(re.escape(snippet)).finditer(source, start, end)
        if not any(first <= match.start() and match.end() <= last for first, last in docstrings)
    ]


def _docstrings(source: str) -> list[tuple[int, int]]:
    """Return the offsets where each docstring of the source's module, classes, and functions starts and ends."""
    try:
        tree = _parsed(source)
    except SyntaxError:
        return []  # The file is not Python, or does not parse, so every occurrence counts.
    definitions = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return [
        _node_span(source, node.body[0])
        for node in ast.walk(tree)
        if isinstance(node, definitions) and ast.get_docstring(node, clean=False) is not None
    ]


def _span(source: str, qualified_name: str) -> tuple[int, int]:
    """Return the offsets of the part of the source a mutation may change, which is all of it for an empty name."""
    if not qualified_name:
        return 0, len(source)
    definition = _definitions(source).get(qualified_name)
    if definition is None:
        message = f"the source does not define {qualified_name}"
        raise StaleError(message)
    return _node_span(source, definition)


def _function_holding(source: str, snippet: str) -> str:
    """Return the qualified name of the function or method whose span holds the snippet, or the empty string."""
    start = _occurrences(source, "", snippet)[0]
    end = start + len(snippet)
    functions = {name: node for name, node in _definitions(source).items() if not isinstance(node, ast.ClassDef)}
    spans = {name: _node_span(source, node) for name, node in functions.items()}
    return next((name for name, (first, last) in spans.items() if first <= start and end <= last), "")


def _definitions(source: str) -> dict[str, ast.stmt]:
    """Return each class, function and method the source defines, under the qualified name an anchor reaches it by.

    A name defined twice, as a property's getter and setter are, names its first definition, which is the getter.
    """
    try:
        body = _parsed(source).body
    except SyntaxError as error:
        raise StaleError(_reason(error)) from error
    definitions: dict[str, ast.stmt] = {}
    for name, node in _named(body):
        definitions.setdefault(name, node)
    return definitions


@functools.cache
def _parsed(source: str) -> ast.Module:
    """Return the syntax tree of the source, parsed once however many mutations change it."""
    return ast.parse(source)


def _named(body: list[ast.stmt], prefix: str = "") -> Iterator[tuple[str, ast.stmt]]:
    """Yield each class, function and method the body defines, recursing into classes but not into functions."""
    for node in body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            yield f"{prefix}{node.name}", node
        if isinstance(node, ast.ClassDef):
            yield from _named(node.body, f"{prefix}{node.name}.")


def _node_span(source: str, node: ast.stmt) -> tuple[int, int]:
    """Return the offsets in the source where the node starts and ends, the newline ending its last line included."""
    start = _offset(source, node.lineno, node.col_offset)
    end = _offset(source, node.end_lineno or 0, node.end_col_offset or 0)
    if source.startswith("\n", end):
        end += 1
    return start, end


def _statements_overlapping(source: str, start: int, end: int) -> list[ast.stmt]:
    """Return the top-level statements of the source that the part of it from the start to the end reaches into."""
    statements = []
    for node in ast.parse(source).body:
        first, last = _node_span(source, node)
        if first < end and start < last:
            statements.append(node)
    return statements


def _names_assigned(statement: ast.stmt) -> list[str] | None:
    """Return the names the statement assigns a value to, or None where it assigns to anything but plain names.

    Assigning to an item or an attribute changes a shared object rather than binding a name, so it gets None.
    """
    if not isinstance(statement, ast.Assign):
        return None
    names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
    return names if len(names) == len(statement.targets) else None


def _offset(source: str, line: int, column: int) -> int:
    """Return the offset that the one-based line and the column point at in the source."""
    return sum(len(text) for text in source.splitlines(keepends=True)[: line - 1]) + column


class StaleError(Exception):
    """Raised where the mutation cannot be applied to the source at all, carrying the reason to report.

    That is the mutation's fault rather than the test's.
    """


class _SourceError(Exception):
    """Raised where the mutated source will not import, on its own or when the test module imports it.

    Either way that is the mutation's fault rather than the checker's.
    """


def _executed_module(source: str, module: types.ModuleType) -> types.ModuleType:
    """Return the module the source holds, installed under the name it replaces before the source runs.

    An import installs a module before running it as well, so a module the source imports that imports this one back
    gets the mutated module. The module carries the spec of the one it replaces, so a test it holds loads by name.
    """
    # An imported module has a spec.
    executed = importlib.util.module_from_spec(cast("importlib.machinery.ModuleSpec", module.__spec__))
    sys.modules[module.__name__] = executed
    try:
        exec(compile(source, executed.__file__ or "", "exec"), executed.__dict__)  # noqa: S102 # nosec
    except Exception as error:
        raise _SourceError(_reason(error)) from error
    return executed


def _bind_on_parent(module: types.ModuleType) -> None:
    """Bind the module on its parent package, as an import binds a submodule it loads.

    The parent is imported where the module's source did not import it, so a test naming the module by its dotted name
    reaches it.
    """
    parent, _, child = module.__name__.rpartition(".")
    if not parent:
        return
    try:
        package = importlib.import_module(parent)
    except Exception as error:
        raise _SourceError(_reason(error)) from error
    setattr(package, child, module)


def _loaded_test(test_name: str) -> unittest.TestSuite:
    """Return the named test, importing its module where a purge dropped it, so that the module gets the mutated one.

    Importing executes the test module's body, so a mutation that breaks it raises here rather than while the test runs.
    """
    try:
        return unittest.TestLoader().loadTestsFromName(test_name)
    except Exception as error:
        raise _SourceError(_reason(error)) from error


# The failures, or the errors, of a test run: pairs of a failing test or case and its traceback.
type _Reports = list[tuple[unittest.TestCase, str]]


def _reports_of(reports: _Reports, name: _CaseName) -> _Reports:
    """Return the failures or errors of the case with the name, or of the test outside its table."""
    if name is OUTSIDE:
        return [(test, traceback) for test, traceback in reports if _case_of(test) is None]
    return [(test, traceback) for test, traceback in reports if (case := _case_of(test)) and case.is_named(name)]


class _RecordedRun(unittest.TestResult):
    """What a test run recorded: its failures and errors, and every case of its table, whether it passed or not."""

    def __init__(self) -> None:
        super().__init__()
        self.cases: list[Case] = []
        self._groups: list[Mapping[str, object]] = []  # The parameters of the cases that hold cases of their own

    def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase, err: Any) -> None:  # noqa: N802, ANN401
        """Record the case, unless it is a group of a nested table, then what it reported.

        unittest reports a group after the cases it holds, whose parameters chain the group's own parameters.
        """
        params = cast("Any", subtest).params  # unittest hands each case in as the subtest
        if not any(params.maps[0] is group for group in self._groups):
            self.cases.append(cast("Case", _case_of(subtest)))
            self._groups.extend(params.maps[1:])
        super().addSubTest(test, subtest, err)

    def cases_named(self, name: str) -> int:
        """Return how many of the run's cases have the name, counting a case the test ran twice once."""
        return len({case.description for case in self.cases if case.is_named(name)})

    def reports(self, name: _CaseName | None) -> tuple[_Reports, _Reports]:
        """Return the failures and the errors of the case with the name, of the test outside its table, or of all."""
        if name is None:
            return self.failures, self.errors
        return _reports_of(self.failures, name), _reports_of(self.errors, name)


def _recorded_run(test_name: str) -> _RecordedRun:
    """Return what running the named test, loaded by name, recorded."""
    result = _RecordedRun()
    _loaded_test(test_name).run(result)
    return result


def _case_of(test: unittest.TestCase) -> Case | None:
    """Return the case of a table that the test stands for, or None for a test that stands for itself."""
    # unittest runs a case as a test of its own, which carries the case's parameters, and its message where it has
    # one: a case without a message carries a placeholder of unittest's.
    params = getattr(test, "params", None)
    if not isinstance(params, Mapping):
        return None
    message = getattr(test, "_message", None)
    description = cast("Any", test)._subDescription()  # noqa: SLF001 — unittest does not export the case's type
    return Case(message if isinstance(message, str) else None, dict(params), description)


def _raised(errors: _Reports) -> str:
    """Return the last line of the first of the errors, which is what the test or its case raised."""
    return errors[0][1].strip().splitlines()[-1]


def _reason(error: Exception) -> str:
    """Return the error's name and message, which is why the mutation could not be judged."""
    return f"{type(error).__name__}: {error}"


def _failure(mutation: Mutation, result: Result) -> str:
    """Return the message a mutation the test did not kill fails that test with, leading with the regression.

    A survivor is one the test, or the case the mutation names, ran against and passed. A stale or broken mutation
    is one the check could not judge, and its message carries why, which says whether the mutation needs rewriting
    or the code moved.
    """
    if result.outcome == Outcome.SURVIVED:
        return f"{mutation.regression} — {_killer(mutation)} did not kill this mutation of {mutation.anchor_name}"
    return f"{mutation.regression} — this mutation of {mutation.anchor_name} is {result.outcome}: {result.reason}"


def _killer(mutation: Mutation) -> str:
    """Return what was to kill the mutation: the test, the case the mutation names, or the checks outside the table."""
    if mutation.case is None:
        return "the test"
    if mutation.case is OUTSIDE:
        return OUTSIDE.value
    return f"the case {mutation.case!r}"


# Where a survived registration is recorded, so how often the mechanism catches a test that stopped guarding can
# be counted later. Only SURVIVED is recorded: it is rare and it is the event the registrations exist for, where a
# stale snippet is common and fixed within the minute.
_SURVIVALS = Path("tests/mutation_time/mutation-survivals.md")


def _captured[Method](method: Method, name: str) -> Method:
    """Return the `Path` method, raising when a patch has already replaced it.

    A patch made with `autospec` reads as a function all the same, so the plain form is what this catches. Checking a
    mutation of this module runs its test again, which imports this module afresh. That run does not check the test's
    mutations, so its copy of this module is exempt.
    """
    if not isinstance(method, types.FunctionType) and not os.environ.get(_CHECKED_TEST):
        message = (
            f"src/mutation_time/mutation.py was imported while Path.{name} was patched, "
            "so the record it keeps is not safe"
        )
        raise TypeError(message)
    return method


# The methods that look up, read, and write `_SURVIVALS`, and read the source a mutation changes. They are taken from
# `Path` here, at import, because checking and recording run inside the test that registers the mutation, and that
# test may have patched them.
_EXISTS = _captured(Path.exists, "exists")
_READ_TEXT = _captured(Path.read_text, "read_text")
_WRITE_TEXT = _captured(Path.write_text, "write_text")


def _record_survival(test_name: str, mutation: Mutation) -> None:
    """Record the survived registration in `_SURVIVALS`, creating the file when it holds nothing yet.

    A mutation of a test module is passed over: the framework's own targets survive by design, so recording them
    would fill the file with entries that say nothing about a guard. An entry names the day it was recorded on and
    is written once, so re-running the suite while the test is being fixed adds nothing, where the same
    registration surviving on a later day is recorded as the separate event it is.
    """
    if mutation.module.__name__.startswith("tests."):
        return
    recorded = _READ_TEXT(_SURVIVALS) if _EXISTS(_SURVIVALS) else "# Registrations that survived\n\n"
    entry = f"- {datetime.now(UTC):%Y-%m-%d} `{test_name}` — {mutation.regression}\n"
    if entry not in recorded:
        _WRITE_TEXT(_SURVIVALS, recorded + entry)


def _fail_unless_killed(test_case: unittest.TestCase, mutations: tuple[Mutation, ...]) -> None:
    """Check each mutation against the test case, failing it once for every mutation that was not killed.

    Checking re-runs the very test this is called from, so the sentinel names that test for the duration, telling
    the re-run to run its body and stop there. The re-run loads the test afresh by name, since the running test case
    belongs to a class the unmutated module was imported into. Each check is a subTest of `unittest`'s own, since a
    test class that overrides `subTest` does so for its own subTests. Where every mutation was killed, the test case
    fails as well for each case of its table that none of them names. Where the test has a table and a mutation names
    neither a case nor `OUTSIDE`, the test case fails for that mutation instead.
    """
    test_name = test_case.id()
    results = []
    with patch.dict(os.environ, {_CHECKED_TEST: test_name}):
        for mutation in mutations:
            with unittest.TestCase.subTest(test_case, regression=mutation.regression):
                result = mutation.check(test_name)
                results.append(result)
                if result.outcome is Outcome.SURVIVED:
                    _record_survival(test_name, mutation)
                test_case.assertEqual(result.outcome, Outcome.KILLED, _failure(mutation, result))
    if any(result.outcome is not Outcome.KILLED for result in results):
        return  # The mutation that was not killed fails the test case already.
    names = [mutation.case for mutation in mutations if mutation.case is not None]
    if len(names) == len(mutations):
        _fail_for_unnamed_cases(test_case, names, results)
    elif any(result.cases for result in results):
        _fail_for_unnamed_registrations(test_case, mutations)


def _fail_for_unnamed_cases(test_case: unittest.TestCase, names: list[_CaseName], results: list[Result]) -> None:
    """Fail the test case once for each case in the results that none of the names matches."""
    cases = {case.description: case for result in results for case in result.cases}
    case_names = [name for name in names if isinstance(name, str)]
    for case in cases.values():
        if not any(case.is_named(name) for name in case_names):
            with unittest.TestCase.subTest(test_case, case.description):
                test_case.fail("no registration names this case")


def _fail_for_unnamed_registrations(test_case: unittest.TestCase, mutations: tuple[Mutation, ...]) -> None:
    """Fail the test case once for each mutation that names neither a case nor `OUTSIDE`."""
    for mutation in mutations:
        if mutation.case is None:
            with unittest.TestCase.subTest(test_case, regression=mutation.regression):
                test_case.fail("this registration does not name a case of the test's table")


def _registration(mutation: Mutation) -> Callable[[_Method], _Method]:
    """Return a decorator making the decorated test kill the mutation.

    The test runs its own body first, so a test already failing for a reason of its own never reports a mutation as
    killed. It checks its mutations once it has finished and its other cleanups have run, when the patches decorating
    it and the ones its set-up entered have stopped, since checking a mutation runs the test again. A decorator stacked
    above another one adds its mutation to the other's, whatever decorator sits between the two, so the test checks
    its mutations top to bottom.
    """

    def decorate(method: _Method) -> _Method:
        if hasattr(method, _REGISTERED):
            _register_above(method, mutation)
            return method

        @functools.wraps(method)
        def wrapper(self: unittest.TestCase, *args: object, **kwargs: object) -> object:
            result = method(self, *args, **kwargs)
            if not self._outcome.success:  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]
                return result
            if os.environ.get(_CHECKED_TEST) == self.id() or os.environ.get(CHECKS_OFF) == "1":
                return result
            cleanups = self._cleanups  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]
            # `doCleanups` runs the cleanups from the end, so the check at the front of the list runs last:
            cleanups.insert(0, (_fail_unless_killed, (self, getattr(wrapper, _REGISTERED)), {}))
            return result

        setattr(wrapper, _REGISTERED, (mutation,))
        return cast("_Method", wrapper)

    return decorate


def _register_above(method: Callable[..., object], mutation: Mutation) -> None:
    """Put the mutation in front of the ones registered beneath it, in each decorator down to the registration."""
    layer: object = method
    while hasattr(layer, _REGISTERED):
        setattr(layer, _REGISTERED, (mutation, *getattr(layer, _REGISTERED)))
        layer = getattr(layer, "__wrapped__", None)


class _Kill(TypedDict, total=False):
    """How the test is to kill a mutation: by raising which error, and in which case of its table."""

    raises: str
    case: _CaseName | None


class _Kills:
    """The decorators making a test kill a mutation: one per change a mutation makes, and `each` to repeat one."""

    def replace(
        self, anchor: _Anchor, old: str, new: str, regression: str, **kill: Unpack[_Kill]
    ) -> Callable[[_Method], _Method]:
        """Return a decorator registering the mutation that replaces the snippet with the new code."""
        return _registration(Mutation(anchor, old, new, regression, **kill))

    def delete(self, anchor: _Anchor, old: str, regression: str, **kill: Unpack[_Kill]) -> Callable[[_Method], _Method]:
        """Return a decorator registering the mutation that deletes the snippet."""
        return _registration(Mutation(anchor, old, "", regression, **kill))

    def prepend(
        self, anchor: _Anchor, old: str, new: str, regression: str, **kill: Unpack[_Kill]
    ) -> Callable[[_Method], _Method]:
        """Return a decorator registering the mutation that inserts the new code before the snippet."""
        return _registration(Mutation(anchor, old, new + old, regression, **kill))

    def append(
        self, anchor: _Anchor, old: str, new: str, regression: str, **kill: Unpack[_Kill]
    ) -> Callable[[_Method], _Method]:
        """Return a decorator registering the mutation that inserts the new code after the snippet."""
        return _registration(Mutation(anchor, old, old + new, regression, **kill))

    def force_true(
        self, anchor: _Anchor, condition: str, regression: str, **kill: Unpack[_Kill]
    ) -> Callable[[_Method], _Method]:
        """Return a decorator registering the mutation that replaces the condition with `True`."""
        return _registration(Mutation(anchor, condition, "True", regression, **kill))

    def force_false(
        self, anchor: _Anchor, condition: str, regression: str, **kill: Unpack[_Kill]
    ) -> Callable[[_Method], _Method]:
        """Return a decorator registering the mutation that replaces the condition with `False`."""
        return _registration(Mutation(anchor, condition, "False", regression, **kill))

    def force_none(
        self, anchor: _Anchor, expression: str, regression: str, **kill: Unpack[_Kill]
    ) -> Callable[[_Method], _Method]:
        """Return a decorator registering the mutation that replaces the expression with `None`."""
        return _registration(Mutation(anchor, expression, "None", regression, **kill))

    def delete_statement(
        self, anchor: _Anchor, statement: str, regression: str, **kill: Unpack[_Kill]
    ) -> Callable[[_Method], _Method]:
        """Return a decorator registering the mutation that deletes the statement.

        The mutation leaves `pass` where the statement was, so a block that held the statement alone still parses.
        """
        return _registration(Mutation(anchor, statement, "pass", regression, **kill))

    def uncache(self, function: _Function, regression: str, **kill: Unpack[_Kill]) -> Callable[[_Method], _Method]:
        """Return a decorator registering the mutation that removes the function's cache decorator.

        The mutation is anchored on the function's module, since the decorator lies outside the function's span.
        """
        definition = f"def {function.__qualname__}("
        module = sys.modules[function.__module__]
        return _registration(Mutation(module, f"@cache\n{definition}", definition, regression, **kill))

    def each(
        self, values: Iterable[_Value], registration: Callable[[_Value], Callable[[_Method], _Method]]
    ) -> Callable[[_Method], _Method]:
        """Return a decorator registering the mutation the registration makes for each of the values, in their order.

        A registration applied to a registered test puts its mutation in front, so the values are applied in reverse.
        """
        registrations = [registration(value) for value in values]

        def decorate(method: _Method) -> _Method:
            for register in reversed(registrations):
                method = register(method)
            return method

        return decorate


kills = _Kills()


def registered_mutations(test: unittest.TestCase) -> tuple[Mutation, ...]:
    """Return the mutations the test's kills decorator registers, or none for a test without one."""
    return getattr(getattr(type(test), test.id().rsplit(".", 1)[-1], None), _REGISTERED, ())


def sentinels() -> dict[str, str]:
    """Return the sentinels the environment sets, with their values."""
    return {
        sentinel: value for sentinel in (CHECKS_OFF, _CHECKED_TEST) if (value := os.environ.get(sentinel)) is not None
    }


class KillsTestLoader(unittest.TestLoader):
    """A test loader that loads only the tests carrying a kills decorator."""

    def getTestCaseNames(self, testCaseClass: type[unittest.TestCase]) -> Sequence[str]:  # noqa: N802, N803
        """Return the names of the class's test methods that carry a kills decorator."""
        names = super().getTestCaseNames(testCaseClass)
        return [name for name in names if hasattr(getattr(testCaseClass, name), _REGISTERED)]
