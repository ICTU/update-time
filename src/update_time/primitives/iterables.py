"""Filtering the values an iterable yields."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Hashable, Iterable


def unique[T: Hashable](items: Iterable[T]) -> list[T]:
    """Return the items without repeats, in the order each first appears."""
    return list(dict.fromkeys(items))
