"""Which sources declare a project archived, for the archival check to report on."""

from update_time.primitives.capability import capability

archival_reporting, reports_archival = capability("reports_archival")
