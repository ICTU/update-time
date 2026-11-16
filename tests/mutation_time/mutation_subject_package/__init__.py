"""Code for the tests of the mutation decorator to mutate: a package whose modules import each other."""

from tests.mutation_time.mutation_subject_package.step import STEP


class Counter:
    """A counter whose start is a class attribute, so a mutation of the start executes the package afresh."""

    start = 0

    def count(self) -> int:
        """Return the count after one step."""
        return self.start + STEP
