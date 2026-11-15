"""Hold a tally, and import the module that writes its summary, which imports this module back."""

from tests.mutation_subject_package import report


class Tally:
    """A tally whose start is a class attribute, so a mutation of the start executes this module afresh."""

    start = 0

    def summary(self) -> str:
        """Return the summary of the tally."""
        return report.summary_of_start()
