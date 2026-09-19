"""Measure what each registered `@kills` mutation is worth, by running the suite against every one of them.

A registration claims that one test kills one mutation. Whether it earns its place depends on what else kills the
same mutation: where only the tests registered on it kill it, it guards something no other test does, and where
a dozen tests kill it as well, the suite would notice that regression anyway. This walks the distinct mutations the
suite registers, applies each, runs the suite with the `@kills` checks switched off, and reports the tests that
failed against it. A test that fails without any mutation fails against every one, so the sweep refuses to start
while the suite is red.

Each mutation is executed in memory rather than written to the file, so the working tree is never touched and
other work can carry on while the sweep runs. The mutations are measured one per process, which both keeps them
from disturbing each other and puts every core to work. The sweep fails when any registration no longer holds, so
CI reports one that went stale or moved. It costs a suite run per mutation, so `just check` leaves it out and CI
runs it on its own. `just test-mutations` runs it.
"""

import concurrent.futures
import importlib
import sys
import unittest
from typing import TYPE_CHECKING, Any

from tests.mutation import Mutation, registered_mutations, suite_failures

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

# How the sweep tells one mutation from another: the module it changes, the qualified name of the anchor within it,
# and the change itself. A Mutation is told apart by more, since `check` also reads the error a kill may raise, which
# `killers` never does. Strings throughout, so a worker process can be handed one.
type _Identity = tuple[str, str, str, str]

# What measuring one mutation yielded: the tests registered on it, the mutation itself, and the tests that killed
# it, or None where its snippet no longer matches the file.
type _Measurement = tuple[list[str], Mutation, list[str] | None]


def registrations() -> Iterator[tuple[str, Mutation]]:
    """Yield every mutation the suite registers, with the id of the test it is registered on."""
    # The loader nests suites within suites, and how deeply is its own business, so the walk is untyped.
    discovered: Any = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
    for suite in discovered:
        for case in suite:
            for test in case:
                method = getattr(test, test._testMethodName, None)  # noqa: SLF001
                for mutation in registered_mutations(method):
                    yield test.id(), mutation


def _identity(mutation: Mutation) -> _Identity:
    """Return what tells one mutation from another: the code it changes, and the change itself."""
    return (mutation.module.__name__, mutation.qualified_name, mutation.old, mutation.new)


def grouped(registered: Iterable[tuple[str, Mutation]]) -> dict[_Identity, tuple[Mutation, list[str]]]:
    """Return each distinct mutation with the tests registered on it.

    What kills a mutation depends on the mutation alone, so one registered on several tests is measured once and
    read against all of them.
    """
    groups: dict[_Identity, tuple[Mutation, list[str]]] = {}
    for test, mutation in registered:
        _first, tests = groups.setdefault(_identity(mutation), (mutation, []))
        tests.append(test)
    return groups


def deviations(measured: Sequence[_Measurement]) -> list[_Measurement]:
    """Return the measurements the sweep reports: a stale snippet, or killers other than expected."""
    return [measurement for measurement in measured if measurement[2] is None or _deviates(*measurement)]


def summary(measured: Sequence[_Measurement]) -> str:
    """Return what the measurement showed: how many mutations are killed only by the tests registered on them.

    A mutation registered on several tests is killed by all of them by construction, so it is judged against the
    whole group rather than against one test. A mutation whose snippet no longer matches names no killers, so it
    is counted apart: it guards nothing until its snippet is repointed at the line it means. A mutation nothing
    kills is counted apart too, since a group that kills nothing guards nothing. The report names each mutation
    killed by a different number of tests than expected, so it can be re-aimed or dropped.
    """
    stale = [tests for tests, _mutation, found in measured if found is None]
    alone = [tests for tests, _mutation, found in measured if found and set(found) <= set(tests)]
    by_killers = sorted(measured, key=lambda measurement: len(measurement[2] or []), reverse=True)
    unexpected = [measurement for measurement in by_killers if _deviates(*measurement)]
    lines = [
        f"mutations measured: {len(measured)}, registered on {sum(len(tests) for tests, _m, _f in measured)} tests",
        f"  killed only by the tests registered on it: {len(alone)}",
        f"  the snippet no longer matches the file: {len(stale)}",
        "  killed by a different number of tests than expected:",
        *(
            f"    {len(found or []):>4} (expected {_expected(tests, mutation)})  {_named(tests)}"
            f" — {mutation.regression}"
            for tests, mutation, found in unexpected
        ),
    ]
    return "\n".join(lines)


def _expected(tests: list[str], mutation: Mutation) -> int:
    """Return how many tests are expected to kill the mutation, the ones registered on it unless it declares more.

    Since `@kills` fails a test that does not kill its own mutation, the registered tests are always among the
    killers, so a matching count means the killers are exactly those tests.
    """
    return len(tests) if mutation.expected_killers is None else mutation.expected_killers


def _deviates(tests: list[str], mutation: Mutation, found: list[str] | None) -> bool:
    """Return whether the mutation was killed by a different number of tests than expected.

    A stale mutation is left out: what it needs is its snippet repointed, which the line above reports.
    """
    return found is not None and len(found) != _expected(tests, mutation)


def _named(tests: list[str]) -> str:
    """Return how the report names a group of registered tests: one of them, and how many share the mutation."""
    return tests[0] if len(tests) == 1 else f"{tests[0]} (+{len(tests) - 1} more)"


def _measured(request: _Identity) -> list[str] | None:
    """Return the killers of the mutation the request describes, in the worker process measuring it.

    A module cannot be pickled, so the worker is handed names to resolve rather than the anchor itself. An empty
    qualified name stands for the module, so the mutation is then anchored to the module itself.
    """
    module, qualified_name, old, new = request
    anchor = importlib.import_module(module)
    for name in filter(None, qualified_name.split(".")):
        anchor = getattr(anchor, name)
    return Mutation(anchor, old, new, "").killers()


def main() -> None:  # pragma: no cover
    """Measure every distinct mutation, one per process, print what it showed, and fail on a registration that moved."""
    if failing := suite_failures():
        sys.stderr.write("Nothing can be read from a sweep while these tests fail without any mutation:\n")
        sys.stderr.writelines(f"    {test}\n" for test in failing)
        raise SystemExit(1)
    groups = grouped(registrations())
    total = len(groups)
    measured = []
    # One mutation per process: measuring several in one leaves the later ones reading the earlier ones' state.
    with concurrent.futures.ProcessPoolExecutor(max_tasks_per_child=1) as pool:
        measurements = zip(groups.values(), pool.map(_measured, groups), strict=True)
        for index, ((mutation, tests), found) in enumerate(measurements, start=1):
            killed = "stale" if found is None else str(len(found))
            sys.stdout.write(
                f"{index:>{len(str(total))}}/{total}  {killed:>5}  {_named(tests)} — {mutation.regression}\n"
            )
            sys.stdout.flush()
            measured.append((tests, mutation, found))
    sys.stdout.write(f"{summary(measured)}\n")
    if deviations(measured):
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
