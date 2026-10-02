"""Test fixtures the updater tests share: what their sources answer about a package, and when it was published."""

from tests.update_time.fixtures import FRESH_DATE, PAST_COOLDOWN_DATE
from tests.update_time.updaters.helpers import osv_vulnerability

# A distribution upload time so recent that the release it dates is neither stale nor older than its cooldown.
PYPI_RECENT_UPLOAD = FRESH_DATE.isoformat()

# A distribution upload time past the default cooldown that the default staleness threshold passes over, and a
# threshold of 90 days does not.
PYPI_PAST_COOLDOWN_UPLOAD = PAST_COOLDOWN_DATE.isoformat()

# The advisory the updater tests pin django to a vulnerable version for, and what Update-time reads it as. Shared,
# since the requirements.txt, pyproject.toml, and inline-script tests all check the same pin against the same answer.
DJANGO_ADVISORY, DJANGO_VULNERABILITY = osv_vulnerability("GHSA-2gwj-7jmv-h26r", "SQL Injection in Django", "critical")

# A second advisory affecting those same pins, for the tests that need OSV to report two. Rated moderate, so a `high`
# risk level in force filters it out where the critical one above survives.
OTHER_DJANGO_ADVISORY, OTHER_DJANGO_VULNERABILITY = osv_vulnerability(
    "GHSA-1111-1111-1111", "Denial of service in Django", "moderate"
)

# An advisory naming no package, for the tests whose pins are about something other than which package they name.
ADVISORY, VULNERABILITY = osv_vulnerability("GHSA-2222-2222-2222", "Remote code execution", "critical")
