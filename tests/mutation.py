"""Make a test kill the mutations it is meant to kill, so that an ordinary test run keeps the evidence.

A green test proves nothing on its own: it may assert what the code cannot get wrong, or mock away the collaborator
it claims to check. Breaking the behaviour is the evidence. A decorated test runs its own body, then puts each of the
mutations it registers in place in turn and runs itself against the mutated code, failing unless that breaks it. Each
mutation names the regression it stands in for, so a test that does not kill a mutation reports its regression. A
mutation is applied in memory, so no file on disk is touched and an interrupted run cannot leave a source file broken.
"""

import functools
import os
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable

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


class Outcome(StrEnum):
    """What checking a mutation showed.

    KILLED and SURVIVED judge the test: it failed against the mutation, or it did not. STALE and BROKEN judge the
    mutation instead, and call for rewriting the mutation rather than the test. Stale means it was never applied:
    the file could not be read, the snippet was not there exactly once, or the replacement was the snippet itself.
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
    """

    module: types.ModuleType
    old: str
    new: str
    regression: str = ""
    raises: str = ""

    @property
    def _path(self) -> str:
        """Return the file the module was loaded from."""
        return self.module.__file__ or ""

    def check(self, test_name: str) -> Result:
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
            return Result(Outcome.KILLED) if raised == self.raises else Result(Outcome.BROKEN, raised)
        return Result(Outcome.KILLED if test_result.failures else Outcome.SURVIVED)

    def killers(self) -> list[str] | None:
        """Return the ids of the tests that fail against this mutation, or None when it cannot be applied.

        Where `check` runs the one test the mutation is registered on, this runs the whole suite. The mutated
        source is executed in memory and installed under the module's name, so the working tree is never written
        to. It cannot be applied when the snippet is not in the file exactly once, or when the mutated source will
        not import.
        """
        source = Path(self._path).read_text()
        if source.count(self.old) != 1:
            return None
        imported = dict(sys.modules)
        try:
            self._purge(_SUITE)
            sys.modules[self.module.__name__] = _executed_module(source.replace(self.old, self.new, 1), self.module)
            return suite_failures()
        except _SourceError:
            return None
        finally:
            sys.modules.clear()
            sys.modules.update(imported)

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
            _loaded_test(test_name).run(result)
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
    checked = os.environ.get(CHECKS_OFF)
    os.environ[CHECKS_OFF] = "1"
    try:
        result = unittest.TestResult()
        unittest.defaultTestLoader.discover(_SUITE, top_level_dir=".").run(result)
    finally:
        if checked is None:
            del os.environ[CHECKS_OFF]
        else:
            os.environ[CHECKS_OFF] = checked
    return sorted({test.id().partition(" (")[0] for test, _traceback in result.failures + result.errors})


def _failure(mutation: Mutation, result: Result) -> str:
    """Return the message a mutation the test did not kill fails that test with, leading with the regression.

    A survivor is one the test ran against and passed. A stale or broken mutation is one the check could not judge,
    and its message carries why, which says whether the mutation needs rewriting or the code moved.
    """
    if result.outcome == Outcome.SURVIVED:
        return f"{mutation.regression} — the test did not kill this mutation of {mutation.module.__name__}"
    return f"{mutation.regression} — this mutation of {mutation.module.__name__} is {result.outcome}: {result.reason}"


# Where a survived registration is recorded, so how often the mechanism catches a test that stopped guarding can
# be counted later. Only SURVIVED is recorded: it is rare and it is the event the registrations exist for, where a
# stale snippet is common and fixed within the minute.
_SURVIVALS = Path("tests/mutation-survivals.md")


def _record_survival(test_name: str, mutation: Mutation) -> None:
    """Record the survived registration in `_SURVIVALS`, creating the file when it holds nothing yet.

    A mutation of a test module is passed over: the framework's own targets survive by design, so recording them
    would fill the file with entries that say nothing about a guard. An entry names the day it was recorded on and
    is written once, so re-running the suite while the test is being fixed adds nothing, where the same
    registration surviving on a later day is recorded as the separate event it is.
    """
    if mutation.module.__name__.startswith("tests."):
        return
    recorded = _SURVIVALS.read_text() if _SURVIVALS.exists() else "# Registrations that survived\n\n"
    entry = f"- {datetime.now(UTC):%Y-%m-%d} `{test_name}` — {mutation.regression}\n"
    if entry not in recorded:
        _SURVIVALS.write_text(recorded + entry)


def _fail_unless_killed(test_case: unittest.TestCase, mutations: tuple[Mutation, ...]) -> None:
    """Check each mutation against the test case, failing it once for every mutation that was not killed.

    Checking re-runs the very test this is called from, so the sentinel names that test for the duration, telling
    the re-run to run its body and stop there. The re-run loads the test afresh by name, since the running test case
    belongs to a class the unmutated module was imported into.
    """
    test_name = test_case.id()
    checked = os.environ.get(CHECKED_TEST)
    os.environ[CHECKED_TEST] = test_name
    try:
        for mutation in mutations:
            with test_case.subTest(regression=mutation.regression):
                result = mutation.check(test_name)
                if result.outcome is Outcome.SURVIVED:
                    _record_survival(test_name, mutation)
                test_case.assertEqual(result.outcome, Outcome.KILLED, _failure(mutation, result))
    finally:
        if checked is None:
            del os.environ[CHECKED_TEST]
        else:
            os.environ[CHECKED_TEST] = checked


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
            if os.environ.get(CHECKED_TEST) == self.id() or os.environ.get(CHECKS_OFF):
                return result
            _fail_unless_killed(self, mutations)
            return result

        setattr(wrapper, _REGISTERED, mutations)
        return cast("_Method", wrapper)

    return decorate
