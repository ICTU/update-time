"""Code for the tests of the mutation decorator to mutate, so that they never break code written for another purpose."""


def is_even(number: int) -> bool:
    """Return whether the number is even."""
    return number % 2 == 0
