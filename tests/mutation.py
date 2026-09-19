"""Make a test kill the mutations it is meant to kill, so that an ordinary test run keeps the evidence.

A green test proves nothing on its own: it may assert what the code cannot get wrong, or mock away the collaborator
it claims to check. Breaking the behaviour is the evidence. A decorated test runs its own body, then puts each of the
mutations it registers in place in turn and runs itself against the mutated code, failing unless that breaks it. Each
mutation names the regression it stands in for, so a test that does not kill a mutation reports its regression. A
mutation is applied in memory, so no file on disk is touched and an interrupted run cannot leave a source file broken.
"""

import ast
import contextlib
import functools
import os
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeVar, cast
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# Holds the id of the test being re-run against its own mutation. That test must run its body and stop there:
# checking its mutations again would check them against themselves, without end. Only the test it names stands
# aside, so a decorated test the re-run reaches on its way is checked as usual. An environment variable rather than
# a global, because checking a mutation drops this module from `sys.modules` and imports it afresh.
CHECKED_TEST = "_UPDATE_TIME_MUTATION_CHECKED_TEST"

# Set for the whole of a `just mutate` run, which applies a mutation of its own: the registered checks stand aside,
# so that run's kill list holds the tests that failed on the mutation it was given.
CHECKS_OFF = "_UPDATE_TIME_MUTATION_CHECKS_OFF"

# The package the suite is discovered under, whose modules a mutation makes stale.
_SUITE = "tests"

# The attribute a decorated test carries, holding the mutations it registers. A second `kills` on the same test
# finds it there and fails the test. It survives a decorator written between the two, since `functools.wraps`
# copies the attributes of whatever it wraps.
_REGISTERED = "_registered_mutations"

# What a test is told when its registration cannot be honoured. `_failing` puts each after the test's own id.
_NO_MUTATION = "registers no mutation; give its kills decorator at least one"
_STACKED = "registers mutations in more than one kills decorator; give them all to one"

_Method = TypeVar("_Method", bound="Callable[..., object]")


class _Function(Protocol):
    """A function or a method, carrying the module and the qualified name it was defined under."""

    __module__: str
    __qualname__: str


class Outcome(StrEnum):
    """What checking a mutation showed.

    KILLED and SURVIVED judge the test: it failed against the mutation, or it did not. STALE and BROKEN judge the
    mutation instead, and call for rewriting the mutation rather than the test. Stale means it was never applied:
    reading or parsing the file failed, the source lacks the anchor, the anchor holds the snippet other than once,
    or the replacement equals the snippet.
    Broken means the mutated source did not import, or the test errored with an error other than the one the
    mutation declares.
    """

    KILLED = "killed"
    SURVIVED = "survived"
    STALE = "stale"
    BROKEN = "broken"


@dataclass(frozen=True)
class Result:
    """What checking a mutation showed, and what went wrong where the outcome does not say.

    Stale and broken each have several causes, so a result of either carries the one that applied. Killed and
    survived speak for themselves.
    """

    outcome: Outcome
    reason: str = ""


@dataclass(frozen=True)
class Mutation:
    """A change to a source file and the regression it stands in for.

    The regression names what breaks when the change is made. It explains the failure when a test does not kill
    the mutation.

    A regression that crashes makes the test raise rather than fail, and so does a mutation that broke the test's
    scaffolding. Naming the error in `raises` tells the two apart: the test kills the mutation by raising that
    error, while any other error is reported as broken.

    Some mutations are killed by tests beyond the ones registered on them. `expected_killers` records how many
    tests kill such a mutation, the registered ones included, so the sweep reports it only once that number moves.

    The anchor names the code to change: a module, or a function, method or property the module holds. The
    qualified name reaches a member through its class, and only the source of the definition is changed.
    """

    anchor: types.ModuleType | _Function | property
    old: str
    new: str
    regression: str = ""
    raises: str = ""
    expected_killers: int | None = None

    @property
    def _unwrapped(self) -> types.ModuleType | _Function:
        """Return the anchor in the form that carries its names, which for a property is the getter behind it."""
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
    def qualified_name(self) -> str:
        """Return the qualified name of the definition the anchor names, or the empty string for a module anchor."""
        anchor = self._unwrapped
        return "" if isinstance(anchor, types.ModuleType) else anchor.__qualname__

    @property
    def _anchor_name(self) -> str:
        """Return the name a report calls the anchor by: the module's name, with the qualified name after it."""
        return ".".join(filter(None, (self.module.__name__, self.qualified_name)))

    @property
    def _path(self) -> str:
        """Return the file the module was loaded from."""
        return self.module.__file__ or ""

    def _mutated(self) -> str:
        """Return the file's source with the snippet replaced inside the anchor's span."""
        try:
            source = Path(self._path).read_text()
        except OSError as error:
            raise StaleError(_reason(error)) from error
        return mutated_source(source, self.qualified_name, self.old, self.new)

    def check(self, test_name: str) -> Result:
        """Return what checking the mutation showed: whether the test fails against it, or the file has moved on.

        Whatever the mutation is at fault for comes back as an outcome to report: a source it cannot be applied to,
        one that will not import. Anything else raises, since a defect in the checking is not the mutation's to
        answer for.
        """
        try:
            mutated = self._mutated()
        except StaleError as error:
            return Result(Outcome.STALE, str(error))
        try:
            test_result = self._run_test(test_name, mutated)
        except _SourceError as error:
            return Result(Outcome.BROKEN, str(error))
        if test_result.errors:
            raised = _raised(test_result.errors)
            return Result(Outcome.KILLED) if raised == self.raises else Result(Outcome.BROKEN, raised)
        return Result(Outcome.KILLED if test_result.failures else Outcome.SURVIVED)

    def killers(self) -> list[str] | None:
        """Return the ids of the tests that fail against this mutation, or None when it cannot be applied.

        Where `check` runs the one test the mutation is registered on, this runs the whole suite. The mutated
        source is executed in memory and installed under the module's name, so the working tree is never written
        to. It cannot be applied when the anchor does not hold the snippet exactly once, or when the mutated source
        will not import.
        """
        try:
            mutated = self._mutated()
        except StaleError:
            return None
        try:
            with self._installed(mutated, _SUITE):
                return suite_failures()
        except _SourceError:
            return None

    @contextlib.contextmanager
    def _installed(self, mutated: str, test_name: str) -> Iterator[None]:
        """Install the mutated source under the module's name, and put `sys.modules` back as it was afterwards.

        The module is read before the purge drops it, and the restore runs however the body ends, so a mutated
        module never outlives the block that installed it.
        """
        module = self.module
        imported = dict(sys.modules)
        try:
            self._purge(test_name)
            sys.modules[module.__name__] = _executed_module(mutated, module)
            yield
        finally:
            sys.modules.clear()
            sys.modules.update(imported)

    def _run_test(self, test_name: str, mutated: str) -> unittest.TestResult:
        """Run the test with the mutated source installed under the module's name."""
        with self._installed(mutated, test_name):
            result = unittest.TestResult()
            _loaded_test(test_name).run(result)
        return result

    def _purge(self, test_name: str) -> None:
        """Drop the packages of the mutated module and of the test from `sys.modules`, so the test is imported again.

        Dropping the test's own module is not enough. A module that imported the mutated one holds the original, and
        hands that to the test whenever the test reaches the mutated code through it.
        """
        roots = {_root(self.module.__name__), _root(test_name)}
        for name in [name for name in sys.modules if _root(name) in roots]:
            del sys.modules[name]


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
    start, end = _span(source, qualified_name)
    if (occurrences := source[start:end].count(old)) != 1:
        message = f"the snippet occurs {occurrences} times rather than once"
        raise StaleError(message)
    return source[:start] + source[start:end].replace(old, new) + source[end:]


def _span(source: str, qualified_name: str) -> tuple[int, int]:
    """Return the offsets of the part of the source a mutation may change, which is all of it for an empty name."""
    if not qualified_name:
        return 0, len(source)
    try:
        body = ast.parse(source).body
    except SyntaxError as error:
        raise StaleError(_reason(error)) from error
    definition = _definition(body, qualified_name.split("."))
    if definition is None:
        message = f"the source does not define {qualified_name}"
        raise StaleError(message)
    start = _offset(source, definition.lineno, definition.col_offset)
    return start, _offset(source, definition.end_lineno or 0, definition.end_col_offset or 0)


def _definition(body: list[ast.stmt], qualified_name: list[str]) -> ast.stmt | None:
    """Return the definition the qualified name points at, or None where the source does not define it."""
    name, *rest = qualified_name
    found = next(
        (
            node
            for node in body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name
        ),
        None,
    )
    if found is None:
        return None
    return _definition(found.body, rest) if rest else found


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
    """Return the module the source holds, executed but not yet installed under the name it replaces."""
    executed = types.ModuleType(module.__name__)
    executed.__file__ = module.__file__
    try:
        exec(compile(source, executed.__file__ or "", "exec"), executed.__dict__)  # noqa: S102 # nosec
    except Exception as error:
        raise _SourceError(_reason(error)) from error
    return executed


def _loaded_test(test_name: str) -> unittest.TestSuite:
    """Return the named test, loaded afresh so that it imports the mutated module.

    Loading executes the test module's body, so a mutation that breaks it raises here rather than while the test runs.
    """
    try:
        return unittest.TestLoader().loadTestsFromName(test_name)
    except Exception as error:
        raise _SourceError(_reason(error)) from error


def _raised(errors: list[tuple[unittest.TestCase, str]]) -> str:
    """Return the last line of the first error a test run reported, which is what the test raised."""
    return errors[0][1].strip().splitlines()[-1]


def _reason(error: Exception) -> str:
    """Return the error's name and message, which is why the mutation could not be judged."""
    return f"{type(error).__name__}: {error}"


def suite_failures() -> list[str]:
    """Return the ids of the tests that fail, with the `@kills` checks switched off so none checks its own.

    A test is named once however many of its subTest cases failed, since a subTest carries its parameters in its id.
    """
    result = unittest.TestResult()
    with patch.dict(os.environ, {CHECKS_OFF: "1"}):
        unittest.defaultTestLoader.discover(_SUITE, top_level_dir=".").run(result)
    return sorted({test.id().partition(" (")[0] for test, _traceback in result.failures + result.errors})


def _failure(mutation: Mutation, result: Result) -> str:
    """Return the message a mutation the test did not kill fails that test with, leading with the regression.

    A survivor is one the test ran against and passed. A stale or broken mutation is one the check could not judge,
    and its message carries why, which says whether the mutation needs rewriting or the code moved.
    """
    if result.outcome == Outcome.SURVIVED:
        return f"{mutation.regression} — the test did not kill this mutation of {mutation._anchor_name}"
    return f"{mutation.regression} — this mutation of {mutation._anchor_name} is {result.outcome}: {result.reason}"


# Where a survived registration is recorded, so how often the mechanism catches a test that stopped guarding can
# be counted later. Only SURVIVED is recorded: it is rare and it is the event the registrations exist for, where a
# stale snippet is common and fixed within the minute.
_SURVIVALS = Path("tests/mutation-survivals.md")


def _captured[Method](method: Method, name: str) -> Method:
    """Return the `Path` method, raising when a patch has already replaced it.

    A patch made with `autospec` reads as a function all the same, so the plain form is what this catches. The copy
    a mutation check imports inside the test it checks is left alone, since it never writes to `_SURVIVALS`.
    """
    if not isinstance(method, types.FunctionType) and not os.environ.get(CHECKED_TEST):
        message = f"tests/mutation.py was imported while Path.{name} was patched, so the record it keeps is not safe"
        raise TypeError(message)
    return method


# The methods that look up, read, and write `_SURVIVALS`. They are taken from `Path` here, at import, because
# recording runs inside the test whose registration survived, and that test may have patched them.
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
    belongs to a class the unmutated module was imported into.
    """
    test_name = test_case.id()
    with patch.dict(os.environ, {CHECKED_TEST: test_name}):
        for mutation in mutations:
            with test_case.subTest(regression=mutation.regression):
                result = mutation.check(test_name)
                if result.outcome is Outcome.SURVIVED:
                    _record_survival(test_name, mutation)
                test_case.assertEqual(result.outcome, Outcome.KILLED, _failure(mutation, result))


def _failing(method: Callable[..., object], complaint: str) -> Callable[..., object]:
    """Return a stand-in that fails with the complaint, for a test whose registration cannot be honoured."""

    @functools.wraps(method)
    def fail(self: unittest.TestCase, *_args: object, **_kwargs: object) -> object:
        return self.fail(f"{self.id()} {complaint}")

    return fail


def kills(*mutations: Mutation) -> Callable[[_Method], _Method]:
    """Return a decorator making the decorated test kill the mutations it is meant to kill.

    The test runs its own body first and then checks the mutations, so a test already failing for a reason of its
    own never reports a mutation as killed. One decorator holds every mutation a test kills, so there is never a
    reason to write two. A second decorator on the same test fails it, as does one given no mutation at all.
    """

    def decorate(method: _Method) -> _Method:
        if not mutations:
            return cast("_Method", _failing(method, _NO_MUTATION))
        if hasattr(method, _REGISTERED):
            return cast("_Method", _failing(method, _STACKED))

        @functools.wraps(method)
        def wrapper(self: unittest.TestCase, *args: object, **kwargs: object) -> object:
            result = method(self, *args, **kwargs)
            if os.environ.get(CHECKED_TEST) == self.id() or os.environ.get(CHECKS_OFF) == "1":
                return result
            _fail_unless_killed(self, mutations)
            return result

        setattr(wrapper, _REGISTERED, mutations)
        return cast("_Method", wrapper)

    return decorate


def registered_mutations(method: object) -> tuple[Mutation, ...]:
    """Return the mutations `kills` registered on the method, and none where it registers none."""
    return getattr(method, _REGISTERED, ())
