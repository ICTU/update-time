"""Report the registered mutations that another test of the registering test's module kills as well.

Usage: `uv run python -m tools.audit_mutations`. When another test of its module kills a registered mutation as well,
the module guards it without the registration, so the registration can go. A test of another module may kill it too,
as an updater's test reaches the code a unit test registers a mutation of. The audit leaves out the tests of the
mutations themselves.
"""

import dataclasses
import io
import os
import sys
import unittest
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING
from unittest.mock import patch

from coverage import Coverage

from mutation_time.mutation import CHECKS_OFF, KillsTestLoader, Mutation, Outcome, registered_mutations

from tests.environment import time_zone

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

# Where the tests sit, and the directory their module names start from.
_TESTS, _TOP_LEVEL = "tests", "."


@dataclass(frozen=True, slots=True, kw_only=True)
class _Finding:
    """What the audit finds: a registration, its regression, and the other tests of its module killing its mutation."""

    registered: str
    regression: str
    killers: list[str]


@dataclass(frozen=True, slots=True, kw_only=True)
class _SuiteRun:
    """The suite's run under coverage: the tests that ran each line of a file, and the test ids each of them names.

    Coverage names a test after the class that defines it, so a test a mixin defines names that test in each class
    inheriting it.
    """

    contexts_of: Callable[[str], Mapping[int, Iterable[str]]]
    ids_by_context: Mapping[str, list[str]]

    def candidates(self, path: str, lines: range, registered: str) -> list[str]:
        """Return the ids of the tests of the registered test's modules that ran any of the lines, other than that test.

        A mixin's test runs in the module of each class inheriting it, and its copies count as that test.
        """
        contexts_by_line = self.contexts_of(path)
        ran = {context for line in lines for context in contexts_by_line.get(line, ())} - {registered, ""}
        modules = {_module(test) for test in self._ids(registered)}
        return [test for context in sorted(ran) for test in self._ids(context) if _module(test) in modules]

    def _ids(self, context: str) -> list[str]:
        """Return the ids of the tests the context names: one, or a copy in each class inheriting a mixin's test."""
        return self.ids_by_context.get(context, [context])


def _module(test: str) -> str:
    """Return the module of the test that the id names."""
    return test.rsplit(".", 2)[0]


def _audit(registrations: Iterable[tuple[str, Mutation]], run: _SuiteRun) -> list[_Finding]:
    """Return a finding for each registered mutation that another test of the registering test's module kills as well.

    A candidate's own registrations stand aside, so checking it does not check them in turn. A candidate runs against
    the code as it is once, however many registrations it is a candidate for.
    """
    passes_unmutated = cache(_passes_unmutated)
    # A test that reads the local time can kill a mutation in one zone and let it survive in another.
    with patch.dict(os.environ, {CHECKS_OFF: "1"}), time_zone("UTC"):
        findings = [_finding(test, mutation, run, passes_unmutated) for test, mutation in registrations]
    return [finding for finding in findings if finding.killers]


def _finding(test: str, mutation: Mutation, run: _SuiteRun, passes_unmutated: Callable[[str], bool]) -> _Finding:
    """Return the registered mutation with the other tests of the registering test's module that kill it.

    A mutation names a case of the registering test's table, so the audit checks the other tests without the name.
    """
    unnamed = dataclasses.replace(mutation, case=None)
    killers = sorted(
        candidate
        for candidate in run.candidates(mutation.path, mutation.lines, test)
        if passes_unmutated(candidate) and unnamed.check(candidate).outcome is Outcome.KILLED
    )
    return _Finding(registered=test, regression=mutation.regression, killers=killers)


def _passes_unmutated(test: str) -> bool:
    """Return whether the test passes against the code as it is.

    A test can fail on state an earlier test in the same process left behind. Such a failure says nothing about the
    mutation.
    """
    loaded = unittest.defaultTestLoader.loadTestsFromName(test)
    return unittest.TextTestRunner(stream=io.StringIO()).run(loaded).wasSuccessful()


def _tests(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    """Yield the tests the suite holds, however deep its suites nest."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _tests(item)
        else:
            yield item


def _suite_run(suite: unittest.TestSuite) -> _SuiteRun:
    """Run the suite once under coverage, and return which of its tests ran each line."""
    ids_by_context: dict[str, list[str]] = {}
    for test in _tests(suite):  # Running the suite empties it, so its tests are read first.
        ids_by_context.setdefault(_context(test), []).append(test.id())
    coverage = Coverage(data_file=None)
    coverage.set_option("run:dynamic_context", "test_function")
    coverage.start()
    try:
        unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    finally:
        coverage.stop()
    return _SuiteRun(contexts_of=coverage.get_data().contexts_by_lineno, ids_by_context=ids_by_context)


def _context(test: unittest.TestCase) -> str:
    """Return the name coverage gives the test: the name of its method in the class that defines it."""
    method = getattr(test, test.id().rpartition(".")[2])
    return f"{method.__module__}.{method.__qualname__}"


def main() -> int:
    """Report each registered mutation that other tests of its module kill as well, and fail the run where there is one.

    A test module that does not import fails the run before any audit, since its registrations would go unaudited.
    """
    loader = unittest.TestLoader()
    suite = loader.discover(_TESTS, top_level_dir=_TOP_LEVEL)
    if loader.errors:
        # typeshed declares the errors as exception types, but the loader records each as the text of a traceback.
        sys.stdout.writelines(str(error) for error in loader.errors)
        return 1
    with patch.dict(os.environ, {CHECKS_OFF: "1"}):
        run = _suite_run(suite)
    discovered = _tests(KillsTestLoader().discover(_TESTS, top_level_dir=_TOP_LEVEL))
    # The tests of the mutations themselves. They all run the checker, so they kill each other's mutations by design.
    mutation_tests = "tests.mutation_time."
    registered = [test for test in discovered if not test.id().startswith(mutation_tests)]
    # Each mutation is audited once per test that registers it, however many classes inherit a mixin's test.
    registrations = dict.fromkeys(
        (_context(test), mutation) for test in registered for mutation in registered_mutations(test)
    )
    findings = _audit(registrations, run)
    for finding in findings:
        sys.stdout.write(f"{finding.registered}: {finding.regression}; also killed by {', '.join(finding.killers)}\n")
    sys.stdout.write(f"registrations other tests kill as well: {len(findings)}\n")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
