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
    Broken means the mutated source did not import, or the test errored with an error other than the one its
    registration declares.
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
    """

    module: types.ModuleType
    old: str
    new: str
    regression: str = ""

    @property
    def _path(self) -> str:
        """Return the file the module was loaded from."""
        return self.module.__file__ or ""

    def check(self, test_name: str, by_raising: str = "") -> Result:
        """Return what checking the mutation showed: whether the test fails against it, or the file has moved on.

        The file must hold the snippet exactly once, so that a mutation cannot break a line it was not aimed at.
        Whatever the mutation is at fault for comes back as an outcome to report: a file that cannot be read, a source
        that will not import. Anything else raises, since a defect in the checking is not the mutation's to answer for.
        """
        if self.new == self.old:
            return Result(Outcome.STALE, "the snippet and its replacement are the same, so nothing changes")
        try:
            source = Path(self._path).read_text()
        except OSError as error:
            return Result(Outcome.STALE, _reason(error))
        if (occurrences := source.count(self.old)) != 1:
            return Result(Outcome.STALE, f"the snippet occurs {occurrences} times rather than once")
        try:
            test_result = self._run_test(test_name, source)
        except _SourceError as error:
            return Result(Outcome.BROKEN, str(error))
        if test_result.errors:
            raised = _raised(test_result.errors)
            return Result(Outcome.KILLED) if raised == by_raising else Result(Outcome.BROKEN, raised)
        return Result(Outcome.KILLED if test_result.failures else Outcome.SURVIVED)

    def _run_test(self, test_name: str, source: str) -> unittest.TestResult:
        """Run the test with the mutated source installed under the module's name.

        What the run imported is dropped afterwards, so the caller keeps the modules it had.
        """
        mutated_source = source.replace(self.old, self.new)
        imported = dict(sys.modules)
        try:
            self._purge(test_name)
            sys.modules[self.module.__name__] = _executed_module(mutated_source, self.module)
            result = unittest.TestResult()
            unittest.TestLoader().loadTestsFromName(test_name).run(result)
        finally:
            sys.modules.clear()
            sys.modules.update(imported)
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


class _SourceError(Exception):
    """Raised where the mutated source will not import, which is the mutation's fault rather than the checker's."""


def _executed_module(source: str, module: types.ModuleType) -> types.ModuleType:
    """Return the module the source holds, executed but not yet installed under the name it replaces."""
    executed = types.ModuleType(module.__name__)
    executed.__file__ = module.__file__
    try:
        exec(compile(source, executed.__file__ or "", "exec"), executed.__dict__)  # noqa: S102 # nosec
    except Exception as error:
        raise _SourceError(_reason(error)) from error
    return executed


def _raised(errors: list[tuple[unittest.TestCase, str]]) -> str:
    """Return the last line of the first error a test run reported, which is what the test raised."""
    return errors[0][1].strip().splitlines()[-1]


def _reason(error: Exception) -> str:
    """Return the error's name and message, which is why the mutation could not be judged."""
    return f"{type(error).__name__}: {error}"


def _failure(mutation: Mutation, result: Result) -> str:
    """Return the message a mutation the test did not kill fails that test with, leading with the regression.

    A survivor is one the test ran against and passed. A stale or broken mutation is one the check could not judge,
    and its message carries why, which says whether the mutation needs rewriting or the code moved.
    """
    if result.outcome == Outcome.SURVIVED:
        return f"{mutation.regression} — the test did not kill this mutation of {mutation.module.__name__}"
    return f"{mutation.regression} — this mutation of {mutation.module.__name__} is {result.outcome}: {result.reason}"


def _fail_unless_killed(test_case: unittest.TestCase, mutation: Mutation, by_raising: str) -> None:
    """Check the mutation against the test case and fail it unless the mutation was killed.

    Checking re-runs the very test this is called from, so the sentinel is set for the duration to tell that re-run
    to run the test's body and stop there. The re-run loads the test afresh by name, since the running test case
    belongs to a class the unmutated module was imported into.
    """
    os.environ[_CHECKING] = "1"
    try:
        result = mutation.check(test_case.id(), by_raising)
    finally:
        del os.environ[_CHECKING]
    test_case.assertEqual(result.outcome, Outcome.KILLED, _failure(mutation, result))


def kills(mutation: Mutation, by_raising: str = "") -> Callable[[_Method], _Method]:
    """Return a decorator making the decorated test kill the mutation it is meant to kill.

    The test runs its own body first and then checks the mutation, so a test already failing for a reason of its
    own never reports the mutation as killed.

    A regression that crashes makes the test raise rather than fail, and so does a mutation that broke the test's
    scaffolding. Naming the error in `by_raising` tells the two apart: this test kills the mutation by raising
    that error, while any other error is reported as broken.
    """

    def decorate(method: _Method) -> _Method:
        @functools.wraps(method)
        def wrapper(self: unittest.TestCase, *args: object, **kwargs: object) -> object:
            if os.environ.get(_CHECKING):
                return method(self, *args, **kwargs)
            returned = method(self, *args, **kwargs)
            _fail_unless_killed(self, mutation, by_raising)
            return returned

        return cast("_Method", wrapper)

    return decorate
