"""Report the registered mutations that a test other than the registering one kills as well.

Usage: `uv run python -m tools.audit_mutations`. When another test kills a registered mutation as well, the suite
guards it without the registration, so the registration can go. The audit leaves out the tests of the mutations
themselves.
"""

import io
import os
import sys
import unittest
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

from coverage import Coverage

from tests.helpers import time_zone
from tests.mutation import CHECKS_OFF, KillsTestLoader, Mutation, Outcome, registered_mutations

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

# Where the tests sit, and the directory their module names start from.
_TESTS, _TOP_LEVEL = "tests", "."

# The tests of the mutations themselves. They all run the checker, so they kill each other's mutations by design.
_MUTATION_TESTS = "tests.test_mutation."


@dataclass(frozen=True)
class _Finding:
    """A registered mutation that other tests kill as well: the test registering it, its regression, and those tests."""

    registered: str
    regression: str
    killers: list[str]


@dataclass(frozen=True)
class _SuiteRun:
    """The suite's run under coverage: the tests that ran each line of a file, and the test ids each of them names.

    Coverage names a test after the class that defines it, so a test a mixin defines names that test in each class
    inheriting it.
    """

    contexts_of: Callable[[str], Mapping[int, Iterable[str]]]
    ids_by_context: Mapping[str, list[str]]

    def candidates(self, path: str, lines: range, registered: str) -> list[str]:
        """Return the ids of the tests that ran any of the lines, other than the registered test in any class."""
        contexts_by_line = self.contexts_of(path)
        ran = {context for line in lines for context in contexts_by_line.get(line, ())} - {registered, ""}
        return [test for context in sorted(ran) for test in self.ids_by_context.get(context, [context])]


def _audit(registrations: Iterable[tuple[str, Mutation]], run: _SuiteRun) -> list[_Finding]:
    """Return a finding for each registered mutation that a test other than the registering one kills as well.

    A candidate's own registrations stand aside, so checking it does not check them in turn.
    """
    # A test that reads the local time can kill a mutation in one zone and let it survive in another.
    with patch.dict(os.environ, {CHECKS_OFF: "1"}), time_zone("UTC"):
        findings = [_finding(test, mutation, run) for test, mutation in registrations]
    return [finding for finding in findings if finding.killers]


def _finding(test: str, mutation: Mutation, run: _SuiteRun) -> _Finding:
    """Return the registered mutation with the tests besides the registering one that kill it."""
    killers = sorted(
        candidate
        for candidate in run.candidates(mutation.path, mutation.lines, test)
        if _passes_unmutated(candidate) and mutation.check(candidate).outcome is Outcome.KILLED
    )
    return _Finding(test, mutation.regression, killers)


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
    return _SuiteRun(coverage.get_data().contexts_by_lineno, ids_by_context)


def _context(test: unittest.TestCase) -> str:
    """Return the name coverage gives the test: the name of its method in the class that defines it."""
    method = getattr(test, test.id().rpartition(".")[2])
    return f"{method.__module__}.{method.__qualname__}"


def main() -> int:
    """Report each registered mutation that other tests kill as well, and fail the run where there is one."""
    with patch.dict(os.environ, {CHECKS_OFF: "1"}):
        run = _suite_run(unittest.TestLoader().discover(_TESTS, top_level_dir=_TOP_LEVEL))
    discovered = _tests(KillsTestLoader().discover(_TESTS, top_level_dir=_TOP_LEVEL))
    registered = [test for test in discovered if not test.id().startswith(_MUTATION_TESTS)]
    # Each mutation is audited once per test that registers it, however many classes inherit a mixin's test.
    registrations = dict.fromkeys(
        (_context(test), mutation) for test in registered for mutation in registered_mutations(test)
    )
    findings = _audit(registrations, run)
    for finding in findings:
        sys.stdout.write(f"{finding.registered}: {finding.regression}; also killed by {', '.join(finding.killers)}\n")
    sys.stdout.write(f"registrations other tests kill as well: {len(findings)}\n")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
