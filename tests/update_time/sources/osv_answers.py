"""What OSV is asked about the versions of a run, and the advisories it answers with."""

from http import HTTPStatus
from typing import TYPE_CHECKING, Final, cast
from unittest.mock import ANY, Mock

from tests.http_mocks import mock_response, patch_post
from tests.update_time.domain_objects import vulnerability

if TYPE_CHECKING:
    from unittest.mock import _patch  # simplint: ignore[private-import-path] # no public patcher type

    from update_time.domain.vulnerability import Vulnerability
    from update_time.formats.json import JSON


def osv_action_entry(action: str, *events: dict[str, str], **fields: JSON) -> dict[str, JSON]:
    """Return an OSV `affected` entry naming the action, stating its affected versions as one range of the events.

    The fields add to the entry, such as the versions it lists, or replace its range with ranges of their own.
    """
    ranges: list[JSON] = [{"type": "ECOSYSTEM", "events": list(events)}] if events else []
    return {"package": osv_action_package(action), "ranges": ranges, **fields}


def osv_action_package(action: str) -> dict[str, str]:
    """Return the package OSV files the action's advisories under."""
    return {"name": action, "ecosystem": "GitHub Actions"}


def osv_vulnerability(advisory: str, summary: str, level: str) -> tuple[dict[str, JSON], Vulnerability]:
    """Return an OSV advisory record and the vulnerability Update-time reads it as.

    Returned as a pair because a test that needs one needs the other: the record is what the mocked API serves, and
    the vulnerability is what the warning is asserted to carry. `level` is spelled the lower-case way Update-time
    reads it, since OSV states it upper-case.
    """
    return osv_advisory(advisory, summary, level.upper()), vulnerability(advisory, summary, level)


def osv_advisory(
    advisory: str,
    summary: str,
    level: str = "",
    aliases: list[str] | None = None,
    vectors: dict[str, str] | None = None,
) -> dict[str, JSON]:
    """Return an OSV advisory record, carrying the risk level a GitHub-reviewed advisory reports when given.

    An advisory nobody reviewed for severity carries no `database_specific` section at all, which is the shape OSV
    returns for the PYSEC records among others; it states a CVSS vector at best, which `vectors` gives per CVSS
    version. `aliases` are the identifiers the other databases name the vulnerability by.
    """
    reviewed = {"database_specific": {"severity": level}} if level else {}
    named = {"aliases": aliases} if aliases else {}
    severities = [{"type": version, "score": vector} for version, vector in (vectors or {}).items()]
    return {"id": advisory, "summary": summary, **reviewed, **named, **({"severity": severities} if severities else {})}


# The OSV endpoint that answers which of a batch of pins are affected. A file's references are asked there together.
OSV_BATCH_URL: Final[str] = "https://api.osv.dev/v1/querybatch"


def assert_osv_asked_about(mock_post: Mock, *pins: tuple[str, str], ecosystem: str = "PyPI") -> None:
    """Assert that OSV was asked about these pins alone, in the one batch request a file costs."""
    mock_post.assert_called_once_with(OSV_BATCH_URL, timeout=ANY, json=osv_queries(*pins, ecosystem=ecosystem))


def osv_queries(*pins: tuple[str, str], ecosystem: str = "PyPI") -> dict[str, JSON]:
    """Return the payload OSV's batch endpoint is asked with for the pins, each a package name and its version."""
    queries: list[JSON] = [
        {"package": {"name": name, "ecosystem": ecosystem}, "version": version} for name, version in pins
    ]
    return {"queries": queries}


def unreachable_osv() -> _patch:
    """Return a patch failing every OSV request, as an OSV a run cannot reach does."""
    status = HTTPStatus.SERVICE_UNAVAILABLE
    unreachable = mock_response(ok=False, status_code=status, reason=status.phrase, url="https://api.osv.dev")
    return patch_post(Mock(return_value=unreachable))


def no_vulnerabilities() -> _patch:
    """Return a patch under which OSV does not report any advisory, for update tests that focus on the update flow."""
    return osv()


def osv(*advisories: dict[str, JSON]) -> _patch:
    """Return a patch answering OSV with the advisories affecting a version, and with none when given none."""
    return patch_post(osv_api(*advisories))


def osv_api(*advisories: dict[str, JSON]) -> Mock:
    """Return a Session.post mock serving both OSV endpoints, reporting the advisories for every version queried.

    Routing by URL keeps tests independent of how many of each request the source makes: the batch endpoint answers
    which of the queried versions are affected, and the details endpoint answers with the advisories in full.
    """

    def serve(url: str, **kwargs: object) -> Mock:
        if not url.endswith("/querybatch"):
            return _osv_response(*advisories)
        queries = cast("dict[str, list]", kwargs["json"])["queries"]
        affected = {"vulns": [{"id": advisory["id"]} for advisory in advisories]} if advisories else {}
        return mock_response({"results": [affected for _query in queries]})

    return Mock(side_effect=serve)


def _osv_response(*advisories: dict[str, JSON]) -> Mock:
    """Return a mock OSV response listing the advisories that affect a version."""
    return mock_response({"vulns": list(advisories)})
