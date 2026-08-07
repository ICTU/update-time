"""Open Source Vulnerabilities, the advisory database aggregating GitHub's, PyPA's, and others.

Unlike the registries the other sources talk to, OSV resolves no versions: it answers which advisories affect the
version a reference is pinned to, whichever ecosystem that version comes from.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NotRequired, TypedDict

from cvss import CVSS3, CVSS4
from cvss.exceptions import CVSSError

from update_time.domain.vulnerability import RISK_LEVELS, Vulnerability
from update_time.io.fetch import fetch
from update_time.io.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from update_time.domain.version import Reference

_LOG = get_logger("osv")


class Ecosystem(StrEnum):
    """The registries OSV matches a version against the advisories of, in OSV's own spelling."""

    PYPI = "PyPI"
    NPM = "npm"


class Severity(TypedDict):
    """A severity an advisory reports, as a CVSS vector string of the named version."""

    type: str
    score: str


class Record(TypedDict):
    """One database's OSV record: what it is about, how bad its reviewers judged it to be, and what else names it."""

    id: str
    aliases: NotRequired[list[str]]
    summary: NotRequired[str]
    severity: NotRequired[list[Severity]]
    database_specific: NotRequired[dict[str, str]]


@dataclass(frozen=True)
class Advisory:
    """One database's record of a defect, with the risk level it states already read, so each record is scored once."""

    id: str
    summary: str
    risk_level: str  # `low`, `moderate`, `high`, or `critical`; empty when the record states none Update-time reads
    aliases: frozenset[str]

    @classmethod
    def read(cls, record: Record) -> Advisory:
        """Return the advisory the record reports."""
        return cls(record["id"], record.get("summary", ""), _risk_level(record), frozenset(record.get("aliases", [])))

    @property
    def identifiers(self) -> frozenset[str]:
        """Return the identifiers this record names its defect by: its own id, and the ids it names as aliases."""
        return self.aliases | {self.id}

    @property
    def rating(self) -> int:
        """Return how severely this record rates the defect, ranking a level Update-time cannot read below every one.

        An unreadable level is warned about whatever threshold is in force, but it says nothing about how bad the
        defect is, so a record that states one loses to a record whose reviewers rated the defect.
        """
        return RISK_LEVELS.index(self.risk_level) if self.risk_level in RISK_LEVELS else -1


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
    return _advisories(reference, ecosystem) if affected else []


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


def _advisories(reference: Reference, ecosystem: Ecosystem) -> list[Vulnerability] | None:
    """Return the vulnerabilities OSV reports for one reference, or None when they can't be read.

    The `query` endpoint returns the full record of every advisory affecting the version in one request, so an
    affected reference costs one request however many advisories it carries. A reference whose advisories that
    request does not return is left unanswered: the batch said something affects it, so an empty answer would say
    the opposite of what OSV did report.
    """
    response = fetch(f"{_OSV_API}/v1/query", _LOG, method="post", json=_query(reference, ecosystem))
    if response is None:
        return None
    advisories = [Advisory.read(record) for record in response.json().get("vulns", [])]
    return [_vulnerability(records) for records in _records_per_defect(advisories)]


def _records_per_defect(advisories: list[Advisory]) -> list[list[Advisory]]:
    """Group the advisories by the defect they report, in the order the defects were first reported.

    OSV answers with a record per database that carries the defect, so a version's advisories are not distinct
    defects: `django@3.2.0` returns 62 records covering 36 CVEs. Records of one defect name each other, by their own
    ids as much as by the identifiers they share, so a record joins every defect it names something of, merging
    those into one. Two records naming nothing in common are therefore read as one defect as soon as a record names
    both, whether it arrives before them, between them, or after both.
    """
    defects: list[list[Advisory]] = []
    for advisory in advisories:
        joined = [index for index, defect in enumerate(defects) if advisory.identifiers & _named_by(defect)]
        records = [record for index in joined for record in defects[index]]
        defects = [defect for index, defect in enumerate(defects) if index not in joined]
        defects.insert(joined[0] if joined else len(defects), [*records, advisory])
    return defects


def _named_by(records: list[Advisory]) -> frozenset[str]:
    """Return every identifier the defect's records name it by."""
    return frozenset(identifier for record in records for identifier in record.identifiers)


def _severest_record(records: list[Advisory]) -> Advisory:
    """Return the record rating one defect most severely.

    The databases rate a defect independently, and only some rate it at all, so a defect reported at the first
    record OSV answered with would be reported below the severest rating it was given, and filtered out by a
    threshold that rating reaches.
    """
    return max(records, key=lambda record: record.rating)


def _query(reference: Reference, ecosystem: Ecosystem) -> dict[str, object]:
    """Return the query that asks OSV about the reference, which both endpoints take the same way."""
    package = {"name": reference.dependency, "ecosystem": ecosystem}
    return {"package": package, "version": reference.current_version}


def _vulnerability(records: list[Advisory]) -> Vulnerability:
    """Return one defect's records as a vulnerability, reported at one record and known by every record's ids.

    Reported at the record rating it most severely (see `_severest_record`), and at the level that record rates it
    at. The identifiers come from all of them, so a suppression naming any one resolves against the defect rather
    than against the database's record that happens to carry it.
    """
    advisory = _severest_record(records)
    identifiers = _named_by(records)
    return Vulnerability(
        advisory.id,
        advisory.summary,
        advisory.risk_level,
        f"{_OSV}/{advisory.id}",
        identifiers - {advisory.id},
    )


def _risk_level(record: Record) -> str:
    """Return the record's risk level, or no level at all when it reports none Update-time can read.

    A record GitHub reviewed states its level, upper-case, where Update-time reads it as the lower-case word the
    marker language and the command line spell it with. One nobody reviewed states a CVSS vector at best, whose base
    score bands into those same levels.
    """
    if level := record.get("database_specific", {}).get("severity", ""):
        return level.lower()
    return _banded_risk_level(record)


def _banded_risk_level(record: Record) -> str:
    """Return the level the newest CVSS vector the record carries bands into, or none when it carries none."""
    vectors = {severity["type"]: severity["score"] for severity in record.get("severity", [])}
    for version, cvss in _CVSS_VERSIONS:
        if (vector := vectors.get(version)) is not None:
            return _band(cvss, vector, record["id"])
    return ""


def _band(cvss: type[CVSS3 | CVSS4], vector: str, advisory: str) -> str:
    """Return the level the vector's base score bands into, or no level when the vector cannot be scored.

    A vector the `cvss` package refuses is a defect in the advisory, not in the project being scanned, so it is
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
