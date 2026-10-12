"""Expand each test package named on the command line into the test modules under it, for `unittest` to run.

`unittest` loads a package named on its command line as a module. A package's `__init__.py` does not hold the tests,
so `unittest` would not run them.
"""

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


def expanded_test_names(names: Iterable[str]) -> list[str]:
    """Return the names, each package among them replaced by the test modules under it."""
    return [expanded for name in names for expanded in _expanded(name)]


def _expanded(name: str) -> list[str]:
    """Return the test modules under the package the name names, or the name itself where it does not name one."""
    package = Path(*name.split("."))
    if not package.is_dir():
        return [name]
    return sorted(".".join(module.with_suffix("").parts) for module in package.rglob("test_*.py"))


def main() -> int:
    """Write the names on the command line, each package among them expanded, on one line."""
    sys.stdout.write(" ".join(expanded_test_names(sys.argv[1:])) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
