"""Patches of the environment a test runs in: its environment variables and its time zone."""

import contextlib
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

from mutation_time.mutation import _sentinels

if TYPE_CHECKING:
    from collections.abc import Generator
    from unittest.mock import _patch_dict  # simplint: ignore[private-import-path] # no public patcher type


@contextlib.contextmanager
def time_zone(name: str) -> Generator[None]:
    """Set the process's time zone, and restore the machine's own zone afterwards, whatever happens in between."""
    try:
        with patch_environ({"TZ": name}):
            time.tzset()
            yield
    finally:
        time.tzset()


def patch_environ(environment_variables: dict[str, str] | None = None, *, clear: bool | None = None) -> _patch_dict:
    """Mock os.environ with the given environment variables.

    If none are given, clear the environment, unless overridden by an explicit clear=True or clear=False.
    """
    clear = not environment_variables if clear is None else clear
    # Carry the sentinels through a cleared environment, so the registered mutations run as they would outside it.
    return patch.dict("os.environ", _sentinels() | (environment_variables or {}), clear=clear)
