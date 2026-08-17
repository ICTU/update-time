"""Make a test kill the mutation it is meant to kill, so that an ordinary test run keeps the evidence.

A green test proves nothing on its own: it may assert what the code cannot get wrong, or mock away the collaborator
it claims to check. Breaking the behaviour is the evidence. A decorated test runs its own body, then puts its
mutation in place and runs itself against the mutated code, failing unless that breaks it. Each mutation names the
regression it stands in for, so a test that does not kill the mutation reports the regression. The mutation is
applied in memory, so no file on disk is touched and an interrupted run cannot leave a source file broken.
"""

import functools
import os
import sys
import types
import unittest
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable

# Set while a test is being re-run against its own mutation. The re-run must run the test's body and stop there:
# checking the mutation again would check it against itself, without end. An environment variable rather than a
# global, because checking a mutation drops this module from `sys.modules` and imports it afresh.
_CHECKING = "_UPDATE_TIME_CHECKING_MUTATION"

_Method = TypeVar("_Method", bound="Callable[..., object]")


class Outcome(StrEnum):
    """What checking a mutation showed.

    KILLED and SURVIVED judge the test: it failed against the mutation, or it did not. STALE and BROKEN judge the
    mutation instead, and call for rewriting the mutation rather than the test. Stale means it was never applied:
    the file could not be read, the snippet was not there exactly once, or the replacement was the snippet itself.
    Broken means the mutated source did not import, or the test errored instead of failing.
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
    """A change to a source file, the test that is meant to kill it, and the regression the change stands in for."""

    path: str
    old: str
    new: str
    test: str
    regression: str = ""


def _module_name(path: str) -> str:
    """Return the dotted name the module at the path is imported under."""
    return path.removeprefix("src/").removesuffix(".py").replace("/", ".")


def _root(name: str) -> str:
    """Return the top-level package of the dotted module or test name."""
    return name.split(".", maxsplit=1)[0]


def _purge(mutation: Mutation) -> None:
    """Drop the packages the mutation names from `sys.modules`, so that loading the test imports them again.

    Dropping the test's own module is not enough. A module that imported the mutated one holds the original, and
    hands that to the test whenever the test reaches the mutated code through it.
    """
    roots = {_root(_module_name(mutation.path)), _root(mutation.test)}
    for name in [name for name in sys.modules if _root(name) in roots]:
        del sys.modules[name]


class _SourceError(Exception):
    """Raised where the mutated source will not import, which is the mutation's fault rather than the checker's."""


def _executed_module(source: str, path: str) -> types.ModuleType:
    """Return the module the source holds, executed but not yet installed under its name."""
    module = types.ModuleType(_module_name(path))
    module.__file__ = path
    try:
        exec(compile(source, path, "exec"), module.__dict__)  # noqa: S102 # nosec
    except Exception as error:
        raise _SourceError(_reason(error)) from error
    return module


def _run_test(mutation: Mutation, mutated: str) -> unittest.TestResult:
    """Run the mutation's test with the mutated source installed under the module's name.

    What the run imported is dropped afterwards, so the caller keeps the modules it had.
    """
    imported = dict(sys.modules)
    try:
        _purge(mutation)
        sys.modules[_module_name(mutation.path)] = _executed_module(mutated, mutation.path)
        result = unittest.TestResult()
        unittest.TestLoader().loadTestsFromName(mutation.test).run(result)
    finally:
        sys.modules.clear()
        sys.modules.update(imported)
    return result


def _raised(errors: list[tuple[unittest.TestCase, str]]) -> str:
    """Return the last line of the first error a test run reported, which is what the test raised."""
    return errors[0][1].strip().splitlines()[-1]


def _reason(error: Exception) -> str:
    """Return the error's name and message, which is why the mutation could not be judged."""
    return f"{type(error).__name__}: {error}"


def check(mutation: Mutation) -> Result:
    """Return what checking the mutation showed: whether the test fails against it, or the file has moved on.

    The file must hold the snippet exactly once, so that a mutation cannot break a line it was not aimed at.
    Whatever the mutation is at fault for comes back as an outcome to report: a file that cannot be read, a source
    that will not import. Anything else raises, since a defect in the checking is not the mutation's to answer for.
    """
    if mutation.new == mutation.old:
        return Result(Outcome.STALE, "the snippet and its replacement are the same, so nothing changes")
    try:
        source = Path(mutation.path).read_text()
    except OSError as error:
        return Result(Outcome.STALE, _reason(error))
    if (occurrences := source.count(mutation.old)) != 1:
        return Result(Outcome.STALE, f"the snippet occurs {occurrences} times rather than once")
    try:
        test_result = _run_test(mutation, source.replace(mutation.old, mutation.new))
    except _SourceError as error:
        return Result(Outcome.BROKEN, str(error))
    if test_result.errors:
        return Result(Outcome.BROKEN, _raised(test_result.errors))
    return Result(Outcome.KILLED if test_result.failures else Outcome.SURVIVED)


def _failure(mutation: Mutation, result: Result) -> str:
    """Return the message a mutation the test did not kill fails that test with, leading with the regression.

    A survivor is one the test ran against and passed. A stale or broken mutation is one the check could not judge,
    and its message carries why, which says whether the mutation needs rewriting or the code moved.
    """
    if result.outcome == Outcome.SURVIVED:
        return f"{mutation.regression} — the test did not kill this mutation of {mutation.path}"
    return f"{mutation.regression} — this mutation of {mutation.path} is {result.outcome}: {result.reason}"


def _fail_unless_killed(test: unittest.TestCase, mutation: Mutation) -> None:
    """Check the mutation and fail the test unless it was killed.

    Checking re-runs the very test this is called from, so the sentinel is set for the duration to tell that re-run
    to run the test's body and stop there.
    """
    os.environ[_CHECKING] = "1"
    try:
        result = check(mutation)
    finally:
        del os.environ[_CHECKING]
    test.assertEqual(result.outcome, Outcome.KILLED, _failure(mutation, result))


def kills(path: str, old: str, new: str, regression: str) -> Callable[[_Method], _Method]:
    """Return a decorator making the decorated test kill the mutation it is meant to kill.

    The test runs its own body first and then checks the mutation, so a test already failing for a reason of its
    own never reports the mutation as killed. The regression names what the change breaks — read off the mutated
    code, since a mutation the test kills only through a stub's exact-argument match breaks no behaviour and so has
    none to name. It leads the failure message when the test does not kill the mutation.
    """

    def decorate(method: _Method) -> _Method:
        # In the body of its class a test method is still a plain function, which is what carries its qualified name.
        function = cast(types.FunctionType, method)
        mutation = Mutation(path, old, new, f"{function.__module__}.{function.__qualname__}", regression)

        @functools.wraps(function)
        def wrapper(self: unittest.TestCase, *args: object, **kwargs: object) -> object:
            if os.environ.get(_CHECKING):
                return method(self, *args, **kwargs)
            returned = method(self, *args, **kwargs)
            _fail_unless_killed(self, mutation)
            return returned

        return cast("_Method", wrapper)

    return decorate
