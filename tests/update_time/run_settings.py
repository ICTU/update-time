"""The patches that set what a run is configured with, as its command-line options do."""

from typing import TYPE_CHECKING

from update_time.domain.staleness import STALE_AFTER

from tests.environment import patch_environ

if TYPE_CHECKING:
    from unittest.mock import _patch_dict  # simplint: ignore[private-import-path] # no public patcher type


def staleness_disabled() -> _patch_dict:
    """Return a patch that switches the staleness check off, as `--stale-after 0` does."""
    return patch_environ({STALE_AFTER.name: "0"})
