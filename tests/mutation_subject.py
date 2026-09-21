"""Code for the tests of the mutation decorator to mutate, so that they never break code written for another purpose."""


def is_even(number: int) -> bool:
    """Return whether the number is even."""
    return number % 2 == 0


def is_multiple_of_three(value: int) -> bool:
    """Return whether the value is a multiple of three."""
    return value % 3 == 0


class Doubler:
    """A value doubler whose two methods end on the same snippet, so an anchor has to reach the method itself.

    Its property is here for an anchor to name, a property being neither a plain function nor a module.
    """

    def doubled(self, value: int) -> int:
        """Return the value doubled."""
        return value * 2

    def quadrupled(self, value: int) -> int:
        """Return the value quadrupled."""
        return value * 2 * 2

    @property
    def one_doubled(self) -> int:
        """Return one doubled."""
        return self.doubled(1)
