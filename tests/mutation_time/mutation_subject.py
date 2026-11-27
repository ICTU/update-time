"""Code for the tests of the mutation decorator to mutate, so that they never break code written for another purpose."""

from functools import cache, cached_property
from typing import Final, override


def is_positive_even(count: int) -> bool:
    """Return whether the count is positive and even, where a nested function decides whether it is positive."""

    def positive() -> bool:
        return count > 0

    return positive() and is_even(count)


def is_even(number: int) -> bool:
    """Return whether the number is even."""
    return number % 2 == 0


def is_multiple_of_three(value: int) -> bool:
    """Return whether the value is a multiple of three."""
    return value % 3 == 0


@cache
def is_odd(number: int) -> bool:
    """Return whether the number is odd, remembering each answer."""
    return number % 2 != 0


class Doubler:
    """A value doubler whose two methods end on the same snippet, so an anchor has to reach the method itself.

    Its property, cached property, and classmethod are here for an anchor to name, none of them being a plain function
    or a module.
    """

    @property
    def one_doubled(self) -> int:
        """Return one doubled."""
        return self.doubled(1)

    def doubled(self, value: int) -> int:
        """Return the value doubled."""
        return value * 2

    @cached_property
    def three_doubled(self) -> int:
        """Return three doubled."""
        return self.doubled(3)

    def quadrupled(self, value: int) -> int:
        """Return the value quadrupled."""
        return value * 2 * 2

    @classmethod
    def two_doubled(cls) -> int:
        """Return two doubled."""
        return cls().doubled(2)


class Banner:
    """A banner of two lines, whose methods indent the second line or leave it flush left."""

    def indented(self) -> str:
        """Return a banner whose second line keeps the indentation of the method's body."""
        # The banner's text, indented as the body is.
        return """first
        second"""

    def flush_left(self) -> str:
        """Return a banner whose second line starts at the left margin."""
        # The banner's text, flush left.
        return """first
second"""


def scaled(value: int, factor: int = 3, *, offset: int = 0) -> int:
    """Return the value scaled by the factor and shifted by the offset, defaults a mutation can change."""
    return value * factor + offset


class Greeter:
    """A greeter whose method's default names the class's constant, so that method's definition needs its class."""

    GREETING = "hello"

    def greeting(self, name: str, greeting: str = GREETING) -> str:
        """Return the greeting addressed to the name."""
        return f"{greeting}, {name}"


class DelegatingDoubler(Doubler):
    """A doubler whose method calls the one it overrides, so that method's code needs the class defining it."""

    @override
    def doubled(self, value: int) -> int:
        """Return the value doubled by the doubler this one extends."""
        return super().doubled(value * 1)


def is_multiple_of_five(value: int) -> bool:
    """Return whether the value is a multiple of five, quoting the `FIVE: Final[int] = 5` below to repeat it."""
    return not value % FIVE


# What a multiple of five divides by, a module-level constant for a mutation to change.
FIVE: Final[int] = 5
# What a multiple of ten divides by, derived from the constant above when the module is executed.
_TEN: Final[int] = FIVE * 2


def is_multiple_of_ten(value: int) -> bool:
    """Return whether the value is a multiple of ten."""
    return not value % _TEN


@cache
def first[Item](items: tuple[Item, ...]) -> Item:
    """Return the first of the items, remembering each answer, in a function whose name a type parameter follows."""
    return items[0]
