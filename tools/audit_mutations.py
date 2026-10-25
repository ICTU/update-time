"""Report the registered mutations that a test other than the registering one kills as well.

Usage: `uv run python -m tools.audit_mutations`. When another test kills a registered mutation as well, the suite
guards it without the registration, so the registration can go.
"""

import io
import os
import sys
import unittest
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

from coverage import Coverage

from tests.mutation import CHECKS_OFF, KillsTestLoader, Mutation, Outcome, registered_mutations

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

# Where the tests sit, and the directory their module names start from.
_TESTS, _TOP_LEVEL = "tests", "."


@dataclass(frozen=True)
class _Finding:
    """A registered mutation that other tests kill as well: the test registering it, its regression, and those tests."""

    registered: str
    regression: str
    killers: list[str]


def _audit(
    registrations: Iterable[tuple[str, Mutation]], contexts_of: Callable[[str], Mapping[int, Iterable[str]]]
) -> list[_Finding]:
    """Return a finding for each registered mutation that a test other than the registering one kills as well.

    A candidate's own registrations stand aside, so checking it does not check them in turn.
    """
    with patch.dict(os.environ, {CHECKS_OFF: "1"}):
        findings = [_finding(test, mutation, contexts_of(mutation.path)) for test, mutation in registrations]
    return [finding for finding in findings if finding.killers]


def _finding(test: str, mutation: Mutation, contexts_by_line: Mapping[int, Iterable[str]]) -> _Finding:
    """Return the registered mutation with the tests besides the registering one that kill it."""
    candidates = _candidates(contexts_by_line, mutation.lines, test)
    killers = sorted(candidate for candidate in candidates if mutation.check(candidate).outcome is Outcome.KILLED)
    return _Finding(test, mutation.regression, killers)


def _candidates(contexts_by_line: Mapping[int, Iterable[str]], lines: range, registered: str) -> set[str]:
    """Return the tests that ran any of the lines, other than the registered test and code outside any test."""
    ran = {context for line in lines for context in contexts_by_line.get(line, ())}
    return ran - {registered, ""}


def _tests(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    """Yield the tests the suite holds, however deep its suites nest."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _tests(item)
        else:
            yield item


def _contexts_of(suite: unittest.TestSuite) -> Callable[[str], Mapping[int, Iterable[str]]]:
    """Run the suite once under coverage, and return the tests that ran each line of a file, by the file's path."""
    coverage = Coverage(data_file=None)
    coverage.set_option("run:dynamic_context", "test_function")
    coverage.start()
    try:
        unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    finally:
        coverage.stop()
    return coverage.get_data().contexts_by_lineno


def main() -> int:
    """Report each registered mutation that other tests kill as well, and fail the run where there is one."""
    with patch.dict(os.environ, {CHECKS_OFF: "1"}):
        contexts_of = _contexts_of(unittest.TestLoader().discover(_TESTS, top_level_dir=_TOP_LEVEL))
    registered = _tests(KillsTestLoader().discover(_TESTS, top_level_dir=_TOP_LEVEL))
    registrations = [(test.id(), mutation) for test in registered for mutation in registered_mutations(test)]
    findings = _audit(registrations, contexts_of)
    for finding in findings:
        sys.stdout.write(f"{finding.registered}: {finding.regression}; also killed by {', '.join(finding.killers)}\n")
    sys.stdout.write(f"registrations other tests kill as well: {len(findings)}\n")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
