"""Measure what each registered `@kills` mutation is worth, by running the suite against every one of them.

A registration claims that one test kills one mutation. Whether it earns its place depends on what else kills the
same mutation: where only the tests registered on it kill it, it guards something no other test does, and where
a dozen tests kill it as well, the suite would notice that regression anyway. This walks the distinct mutations the
suite registers, applies each, runs the suite with the `@kills` checks switched off, and reports the tests that
failed against it. A test that fails without any mutation fails against every one, so the sweep refuses to start
while the suite is red.

Each mutation is executed in memory rather than written to the file, so the working tree is never touched and
other work can carry on while the sweep runs. The mutations are measured one per process, which both keeps them
from disturbing each other and puts every core to work. It still costs a suite run per mutation, so it is a
periodic measurement rather than a check. `just test-mutations` runs it.
"""

import concurrent.futures
import importlib
import sys
import unittest
from typing import TYPE_CHECKING, Any

from tests.mutation import Mutation, suite_failures

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# The attribute `kills` leaves on a decorated test, holding the mutations registered on it.
_REGISTERED = "_registered_mutations"


def registrations() -> Iterator[tuple[str, Mutation]]:
    """Yield every mutation the suite registers, with the id of the test it is registered on."""
    # The loader nests suites within suites, and how deeply is its own business, so the walk is untyped.
    discovered: Any = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
    for suite in discovered:
        for case in suite:
            for test in case:
                method = getattr(test, test._testMethodName, None)  # noqa: SLF001
                for mutation in getattr(method, _REGISTERED, ()):
                    yield test.id(), mutation


def _identity(mutation: Mutation) -> tuple[str, str, str]:
    """Return what tells one mutation from another: the module it changes, and the change itself."""
    return (mutation.module.__name__, mutation.old, mutation.new)


def grouped(registered: Iterable[tuple[str, Mutation]]) -> dict[tuple[str, str, str], tuple[Mutation, list[str]]]:
    """Return each distinct mutation with the tests registered on it.

    What kills a mutation depends on the mutation alone, so one registered on several tests is measured once and
    read against all of them.
    """
    groups: dict[tuple[str, str, str], tuple[Mutation, list[str]]] = {}
    for test, mutation in registered:
        _first, tests = groups.setdefault(_identity(mutation), (mutation, []))
        tests.append(test)
    return groups


def summary(measured: list[tuple[list[str], str, list[str] | None]]) -> str:
    """Return what the measurement showed: how many mutations are killed only by the tests registered on them.

    A mutation registered on several tests is killed by all of them by construction, so it is judged against the
    whole group rather than against one test. A mutation whose snippet no longer matches names no killers, so it
    is counted apart: it guards nothing until its snippet is repointed at the line it means. A mutation nothing
    kills is counted apart too, since a group that kills nothing guards nothing. The widest are named so they can
    be re-aimed or dropped.
    """
    stale = [tests for tests, _regression, found in measured if found is None]
    alone = [tests for tests, _regression, found in measured if found and set(found) <= set(tests)]
    widest = sorted(measured, key=lambda measurement: len(measurement[2] or []), reverse=True)[:10]
    lines = [
        f"mutations measured: {len(measured)}, registered on {sum(len(tests) for tests, _r, _f in measured)} tests",
        f"  killed only by the tests registered on it: {len(alone)}",
        f"  the snippet no longer matches the file: {len(stale)}",
        "  killed by the most tests:",
        *(f"    {len(found or []):>4}  {_named(tests)} — {regression}" for tests, regression, found in widest),
    ]
    return "\n".join(lines)


def _named(tests: list[str]) -> str:
    """Return how the report names a group of registered tests: one of them, and how many share the mutation."""
    return tests[0] if len(tests) == 1 else f"{tests[0]} (+{len(tests) - 1} more)"


def _measured(request: tuple[str, str, str]) -> list[str] | None:  # pragma: no cover
    """Return the killers of the mutation the request describes, in the worker process measuring it.

    A module cannot be pickled, so the worker is handed the name to import rather than the module itself.
    """
    module, old, new = request
    return Mutation(importlib.import_module(module), old, new, "").killers()


def main() -> None:  # pragma: no cover
    """Measure every distinct mutation, one per process, and print what it showed."""
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
            measured.append((tests, mutation.regression, found))
    sys.stdout.write(f"{summary(measured)}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
