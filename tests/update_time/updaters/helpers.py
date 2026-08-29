"""Test helpers the updater tests share: the sources they answer, and the checks they switch off."""

import datetime
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from update_time.domain.vulnerability import NO_RISK_LEVEL, VULNERABILITY_LEVEL

from tests.helpers import mock_response, patch_environ
from tests.update_time.helpers import PYPI_OLD_UPLOAD, osv_advisory, osv_api, pypi_index, vulnerability

if TYPE_CHECKING:
    from unittest.mock import _patch

    from update_time.domain.vulnerability import Vulnerability

# Reusable class decorator that mocks the Docker Hub auth token request made by sources.docker_hub.api_headers
# when DOCKER_HUB_USERNAME/DOCKER_HUB_TOKEN are set, so the image updater tests never make a real network call.
mock_docker_hub_auth = patch("requests.post", Mock(return_value=mock_response({"access_token": "token"})))  # nosec[B105]

# A distribution upload time inside every window, so the release it dates is neither stale nor past its cooldown.
PYPI_RECENT_UPLOAD = datetime.datetime.now(datetime.UTC).isoformat()


def dated_pypi_index(*versions: str, upload_time: str = PYPI_OLD_UPLOAD, archived: bool | str = False) -> Mock:
    """Return a mock Index API response listing the versions, the newest dated by one distribution file."""
    return pypi_index(*versions, files=[{"upload-time": upload_time}], archived=archived)


def osv_vulnerability(advisory: str, summary: str, level: str) -> tuple[dict[str, object], Vulnerability]:
    """Return an OSV advisory record and the vulnerability Update-time reads it as.

    Returned as a pair because a test that needs one needs the other: the record is what the mocked API serves, and
    the vulnerability is what the warning is asserted to carry. `level` is spelled the lower-case way Update-time
    reads it, since OSV states it upper-case.
    """
    return osv_advisory(advisory, summary, level.upper()), vulnerability(advisory, summary, level)


# The advisory the updater tests pin django to a vulnerable version for, and what Update-time reads it as. Shared,
# since the requirements.txt, pyproject.toml, and inline-script tests all check the same pin against the same answer.
DJANGO_ADVISORY, DJANGO_VULNERABILITY = osv_vulnerability("GHSA-2gwj-7jmv-h26r", "SQL Injection in Django", "critical")


def osv(*advisories: dict[str, object]) -> _patch:
    """Return a patch answering OSV with the advisories affecting a version, and with none when given none."""
    return patch("requests.post", osv_api(*advisories))


def unreachable_osv() -> _patch:
    """Return a patch failing every OSV request, as an OSV a run cannot reach does."""
    status = HTTPStatus.SERVICE_UNAVAILABLE
    unreachable = mock_response(ok=False, status_code=status, reason=status.phrase, url="https://api.osv.dev")
    return patch("requests.post", Mock(return_value=unreachable))


# Reusable class decorator that answers OSV with no advisories, for update tests that focus on the update flow.
# Without it their pins are looked up at OSV for real, since nothing else in those tests patches `requests.post`.
no_vulnerabilities = osv()

# Reusable decorator that switches the vulnerability check off, for update tests that focus on the update flow and
# would otherwise trigger the vulnerability pass's own OSV request.
vulnerability_check_disabled = patch_environ({VULNERABILITY_LEVEL.name: NO_RISK_LEVEL})
