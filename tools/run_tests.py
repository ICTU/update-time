"""Run the tests named on the command line in shards, each in a process of its own, and report them as one run."""

import argparse
import io
import multiprocessing
import os
import sys
import unittest
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from itertools import repeat
from pathlib import Path
from typing import TYPE_CHECKING

from tests.mutation import KillsTestLoader

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True)
class _ShardResult:
    """What the tests of one shard showed, and the report `unittest` wrote about them."""

    tests_run: int
    failures: int
    errors: int
    report: str

    @property
    def succeeded(self) -> bool:
        """Return whether every test of the shard passed."""
        return not (self.failures or self.errors)


def _expanded_test_names(names: Iterable[str]) -> list[str]:
    """Return the names, each package among them replaced by the test modules under it.

    `unittest` loads a package it is handed by name as a module. A package's `__init__.py` does not hold the tests,
    so `unittest` would not run them.
    """
    return [expanded for name in names for expanded in _expanded(name)]


def _expanded(name: str) -> list[str]:
    """Return the test modules under the package the name names, or the name itself where it does not name one."""
    package = Path(*name.split("."))
    if not package.is_dir():
        return [name]
    return sorted(".".join(module.with_suffix("").parts) for module in package.rglob("test_*.py"))


def _run_shard(name: str, loader: unittest.TestLoader) -> _ShardResult:
    """Run the tests the name names, as the loader loads them."""
    report = io.StringIO()
    result = unittest.TextTestRunner(stream=report, verbosity=0).run(loader.loadTestsFromName(name))
    return _ShardResult(result.testsRun, len(result.failures), len(result.errors), report.getvalue())


def _verdict(results: list[_ShardResult]) -> str:
    """Return the line `unittest` ends a run with, counting the failures and errors of every shard."""
    counts = {
        "failures": sum(result.failures for result in results),
        "errors": sum(result.errors for result in results),
    }
    details = ", ".join(f"{kind}={count}" for kind, count in counts.items() if count)
    return f"FAILED ({details})" if details else "OK"


def _arguments() -> argparse.Namespace:
    """Return the arguments the command line passes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="the tests to run (default: every test module under tests)")
    parser.add_argument("--kills", action="store_true", help="run only the tests carrying a kills decorator")
    return parser.parse_args()


def main() -> int:
    """Run each test module named on the command line, or under `tests`, as a shard, and write how the run went."""
    arguments = _arguments()
    loader = KillsTestLoader() if arguments.kills else unittest.TestLoader()
    # The coverage pass and the mutations pass run side by side, so each takes half the cores. The workers are
    # spawned, which reaches them over pipes: a fork server is reached over a socket, which `tests` refuses.
    workers = max(1, (os.process_cpu_count() or 1) // 2)
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_run_shard, _expanded_test_names(arguments.names or ["tests"]), repeat(loader)))
    sys.stdout.write("".join(result.report for result in results if not result.succeeded))
    sys.stdout.write(f"Ran {sum(result.tests_run for result in results)} tests\n\n{_verdict(results)}\n")
    return 0 if all(result.succeeded for result in results) else 1


if __name__ == "__main__":  # pragma: no cover
    # A shard's result travels back pickled, naming the module that defines its class. Under `coverage run -m`, the
    # `__main__` that name would point to is coverage's rather than this one, so run what the module defines instead.
    from tools import run_tests  # noqa: PLW0406

    sys.exit(run_tests.main())
