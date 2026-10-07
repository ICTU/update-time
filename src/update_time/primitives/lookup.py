"""The outcome of looking something up: what was found, or why nothing was."""

from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Protocol

    class Lookup[T](Protocol):
        """What a lookup found, with the reason it failed when it found nothing."""

        @property
        def value(self) -> T | None:
            """Return what the lookup found, or None."""
            ...

        @property
        def reason(self) -> str:
            """Return why the lookup found nothing, or nothing when it found something."""
            ...


@dataclass(frozen=True)
class LookedUp[T]:
    """A lookup made at once: what it found, with the reason it failed when it found nothing."""

    value: T | None
    reason: str = ""
    absent: bool = False  # Whether the source answered that what was looked up does not exist


class DeferredLookup[T]:
    """A lookup made the first time its outcome is read, so an outcome nobody reads costs nothing."""

    def __init__(self, look_up: Callable[[], LookedUp[T]]) -> None:
        """Remember how to look the thing up."""
        self._look_up = look_up

    @cached_property
    def _outcome(self) -> LookedUp[T]:
        """Look the thing up, once."""
        return self._look_up()

    @property
    def value(self) -> T | None:
        """Return what the lookup found, or None."""
        return self._outcome.value

    @property
    def reason(self) -> str:
        """Return why the lookup found nothing, or nothing when it found something."""
        return self._outcome.reason
