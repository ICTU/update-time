"""Summarize the tally of the module that imports this one."""

from tests.mutation_subject_package import tally


def summary_of_start() -> str:
    """Return a summary naming where the tally starts."""
    return f"starts at {tally.Tally.start}"
