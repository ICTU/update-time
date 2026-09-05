"""Which sources declare a project archived, and whether the run checks for archival at all."""

from update_time.primitives.capability import capability
from update_time.primitives.environment import flag

# Private channel that passes --ignore-archived from the CLI to the updater subprocesses: whether the run switches
# the archival check off.
IGNORE_ARCHIVED = flag("_UPDATE_TIME_IGNORE_ARCHIVED")

archival_reporting, reports_archival = capability("reports_archival")


def archival_is_checked() -> bool:
    """Return whether the run checks dependencies for archival at all."""
    return not IGNORE_ARCHIVED.get()
