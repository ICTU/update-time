"""Open Source Vulnerabilities, the advisory database aggregating GitHub's, PyPA's, and others.

Unlike the registries the other sources talk to, OSV resolves no versions: it answers which advisories affect the
version a reference is pinned to, whichever ecosystem that version comes from.
"""

from enum import StrEnum
from functools import reduce
from typing import TYPE_CHECKING, NotRequired, TypedDict

from cvss import CVSS3, CVSS4
from cvss.exceptions import CVSSError

from update_time.domain.vulnerability import Vulnerability
from update_time.io.fetch import fetch
from update_time.io.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from update_time.domain.reference import Reference

_LOG = get_logger("osv")


class Ecosystem(StrEnum):
    """The registries OSV matches a version against the advisories of, in OSV's own spelling."""

    PYPI = "PyPI"
    NPM = "npm"


class _Severity(TypedDict):
    """A severity an advisory reports, as a CVSS vector string of the named version."""

    type: str
    score: str


class _Record(TypedDict):
    """One database's OSV record: what it is about, how bad its reviewers judged it to be, and what else names it.

    OSV types the `aliases` and `severity` arrays as an array or null, so a database that states neither may state a
    null rather than leave the field out.
    """

    id: str
    aliases: NotRequired[list[str] | None]
    summary: NotRequired[str]
    severity: NotRequired[list[_Severity] | None]
    database_specific: NotRequired[dict[str, str]]


# The CVSS vector versions OSV reports and the class that scores each, newest first, so an advisory carrying more
# than one is read at its newest assessment. The two disagree on a quarter of the advisories carrying both.
_CVSS_VERSIONS = (("CVSS_V4", CVSS4), ("CVSS_V3", CVSS3))

# GitHub's risk levels, as the lowest CVSS base score each one covers, most severe first. A score below the `low`
# band means the advisory reports no risk at all, which is no level to name. The levels are the ones `RISK_LEVELS`
# orders, which the tests check these bands against, so that a level added to one is added to the other.
_RISK_LEVEL_BANDS = ((9.0, "critical"), (7.0, "high"), (4.0, "moderate"), (0.1, "low"), (0.0, ""))


_OSV = "https://osv.dev"
_OSV_API = "https://api.osv.dev"


def get_vulnerabilities(references: Sequence[Reference], ecosystem: Ecosystem) -> list[list[Vulnerability] | None]:
    """Return the vulnerabilities OSV reports for each reference's version, or None where it did not answer.

    The references are looked up in one batch, which answers only which of them are affected, and each one that
    turns out to be affected costs one further request for the advisories in full. A set of references OSV reports
    nothing for therefore costs a single request, however many it holds.
    """
    affected = _affected_references(references, ecosystem)
    return [
        _vulnerabilities(reference, ecosystem, affected=reference_is_affected)
        for reference, reference_is_affected in zip(references, affected, strict=True)
    ]


def _vulnerabilities(
    reference: Reference, ecosystem: Ecosystem, *, affected: bool | None
) -> list[Vulnerability] | None:
    """Return the advisories affecting the reference, an empty list when none do, or None when OSV did not answer."""
    if affected is None:
        return None
    return _reported_vulnerabilities(reference, ecosystem) if affected else []


def _affected_references(references: Sequence[Reference], ecosystem: Ecosystem) -> list[bool | None]:
    """Return, for each reference, whether OSV reports any advisory affecting the version it is pinned to.

    A reference OSV answers for is affected when the batch reports advisory ids for it. One it does not answer for —
    because OSV could not be reached, or because the batch came back short — is neither affected nor unaffected but
    unanswered, since a caller that read silence as "nothing affects it" would report a live suppression as dead.
    The answers are counted out against the references asked about, so they cannot end up misaligned.
    """
    queries = [_query(reference, ecosystem) for reference in references]
    response = fetch(f"{_OSV_API}/v1/querybatch", _LOG, method="post", json={"queries": queries})
    results = response.json().get("results", []) if response is not None else []
    answers = results[: len(references)]
    return [bool(answer.get("vulns")) for answer in answers] + [None] * (len(references) - len(answers))


def _reported_vulnerabilities(reference: Reference, ecosystem: Ecosystem) -> list[Vulnerability] | None:
    """Return the vulnerabilities OSV reports for one reference, or None when they can't be read.

    The `query` endpoint returns the full record of every advisory affecting the version in one request, so an
    affected reference costs one request however many advisories it carries. A reference whose advisories that
    request does not return is left unanswered: the batch said something affects it, so an empty answer would say
    the opposite of what OSV did report.
    """
    response = fetch(f"{_OSV_API}/v1/query", _LOG, method="post", json=_query(reference, ecosystem))
    if response is None:
        return None
    return _folded([_vulnerability(record) for record in response.json().get("vulns", [])])


def _vulnerability(record: _Record) -> Vulnerability:
    """Return the vulnerability the advisory record reports."""
    return Vulnerability(
        record["id"],
        record.get("summary", ""),
        _risk_level(record),
        f"{_OSV}/{record['id']}",
        frozenset(record.get("aliases") or []),
    )


def _folded(vulnerabilities: list[Vulnerability]) -> list[Vulnerability]:
    """Fold the advisories reporting one vulnerability into one, in the order they were first reported.

    OSV answers with an advisory per database that carries the vulnerability, so a version's advisories are not
    distinct vulnerabilities: `django@3.2.0` returns 62 advisories covering 36 CVEs. The advisories of one
    vulnerability name each other, by their own ids as much as by the identifiers they share, so an advisory joins
    every vulnerability it names something of, folding those into one. Two advisories naming nothing in common are
    therefore read as one as soon as an advisory names both, whether it arrives before them, between them, or after
    both. Each fold keeps the earlier advisory where the two rate alike, so the order OSV answered in is preserved.
    """
    folded: list[Vulnerability] = []
    for vulnerability in vulnerabilities:
        joined = [index for index, other in enumerate(folded) if other.names(vulnerability)]
        merged = reduce(Vulnerability.merged, [folded[index] for index in joined] + [vulnerability])
        folded = [other for index, other in enumerate(folded) if index not in joined]
        folded.insert(joined[0] if joined else len(folded), merged)
    return folded


def _query(reference: Reference, ecosystem: Ecosystem) -> dict[str, object]:
    """Return the query that asks OSV about the reference, which both endpoints take the same way."""
    package = {"name": reference.dependency, "ecosystem": ecosystem}
    return {"package": package, "version": reference.current_version}


def _risk_level(record: _Record) -> str:
    """Return the record's risk level, or no level at all when it reports none Update-time can read.

    A record GitHub reviewed states its level, upper-case, where Update-time reads it as the lower-case word the
    marker language and the command line spell it with. One nobody reviewed states a CVSS vector at best, whose base
    score bands into those same levels.
    """
    if level := record.get("database_specific", {}).get("severity", ""):
        return level.lower()
    return _banded_risk_level(record)


def _banded_risk_level(record: _Record) -> str:
    """Return the level the newest CVSS vector the record carries bands into, or none when it carries none."""
    vectors = {severity["type"]: severity["score"] for severity in record.get("severity") or []}
    for version, cvss in _CVSS_VERSIONS:
        if (vector := vectors.get(version)) is not None:
            return _band(cvss, vector, record["id"])
    return ""


def _band(cvss: type[CVSS3 | CVSS4], vector: str, advisory: str) -> str:
    """Return the level the vector's base score bands into, or no level when the vector cannot be scored.

    A vector the `cvss` package refuses is a fault in the advisory, not in the project being scanned, so it is
    warned about and leaves the advisory without a level, rather than ending the run over it.
    """
    try:
        # The `cvss` package ships no type information, scores a v3 vector as a `Decimal` where it scores a v4 one
        # as a float, and types the score as optional because construction sets it after initialising it to None.
        # Converting is what the bands, which are floats, compare against; an unset score would band as no level.
        score = float(cvss(vector).base_score or 0)
    except CVSSError as error:
        _LOG.malformed_cvss_vector(advisory, error)
        return ""
    return next(level for lowest, level in _RISK_LEVEL_BANDS if score >= lowest)
