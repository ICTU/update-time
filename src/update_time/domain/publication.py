"""Which sources date their releases, for the cooldown and the staleness check to measure against."""

from update_time.primitives.capability import capability

publication_date_reporting, reports_publication_dates = capability("reports_publication_dates")
