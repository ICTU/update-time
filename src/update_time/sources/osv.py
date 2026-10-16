"""Open Source Vulnerabilities, the advisory database aggregating GitHub's, PyPA's, and others.

Unlike the registries the other sources talk to, OSV does not resolve versions: it answers which advisories name the
version a reference is pinned to. For GitHub Actions, OSV does not match versions. It answers with every advisory
naming the action, and Update-time matches the version itself.
"""

from dataclasses import dataclass
from enum import StrEnum
from functools import cache, reduce
from typing import TYPE_CHECKING, NotRequired, Self, TypedDict

from cvss import CVSS3, CVSS4
from cvss.exceptions import CVSSError
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from update_time.domain.vulnerability import Vulnerability
from update_time.io.fetch import fetch
from update_time.io.log import get_logger
from update_time.sources.github import repository_of

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from update_time.domain.reference import Reference

_LOG = get_logger("osv")


class Ecosystem(StrEnum):
    """The ecosystems Update-time asks OSV about, in OSV's own spelling."""

    PYPI = "PyPI"
    NPM = "npm"
    MAVEN = "Maven"
    GITHUB_ACTIONS = "GitHub Actions"

    @property
    def matches_versions(self) -> bool:
        """Return whether OSV matches a version against the ecosystem's advisories, which it does not for actions."""
        return self is not Ecosystem.GITHUB_ACTIONS

    def package_name(self, dependency: str) -> str:
        """Return the name OSV knows the dependency by in the ecosystem: for an action, the repository holding it."""
        return str(repository_of(dependency)) if self is Ecosystem.GITHUB_ACTIONS else dependency


class _Severity(TypedDict):
    """A severity an advisory reports, as a CVSS vector string of the named version."""

    type: str
    score: str


# One step of a range: the version that introduces the vulnerability, that fixes it, or that it last affects, keyed
# by `introduced`, `fixed`, or `last_affected`.
type _Event = dict[str, str]


class _Range(TypedDict):
    """A range of versions or commits, as events that say in order which of them are affected."""

    type: str
    events: list[_Event]


class _Package(TypedDict):
    """A dependency as OSV knows it: its name, and the ecosystem it belongs to."""

    name: str
    ecosystem: str


class _Affected(TypedDict):
    """An advisory's affected versions of one dependency, listed one by one or stated as ranges."""

    package: NotRequired[_Package]
    versions: NotRequired[list[str]]
    ranges: NotRequired[list[_Range]]
    database_specific: NotRequired[dict[str, str]]


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
    affected: NotRequired[list[_Affected]]


@dataclass(frozen=True)
class _Query:
    """What OSV is asked about a reference: the name OSV knows its dependency by, the ecosystem, and its version."""

    name: str
    ecosystem: Ecosystem
    version: str  # Empty where OSV does not match the ecosystem's versions

    @classmethod
    def of(cls, reference: Reference, ecosystem: Ecosystem) -> Self:
        """Return the query asking OSV about the reference."""
        version = reference.current_version if ecosystem.matches_versions else ""
        return cls(ecosystem.package_name(reference.dependency), ecosystem, version)

    @property
    def package(self) -> _Package:
        """Return the package OSV knows the dependency as."""
        return {"name": self.name, "ecosystem": self.ecosystem}

    @property
    def payload(self) -> dict[str, object]:
        """Return the query as `/v1/query` and `/v1/querybatch` both take it."""
        payload: dict[str, object] = {"package": self.package}
        return payload | {"version": self.version} if self.version else payload


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

    One request to `/v1/querybatch` asks about every reference and answers which of them OSV holds advisories for.
    Each of those costs one more request to `/v1/query`, for the advisories in full. References with the same query
    share that request.
    """
    queries = [_Query.of(reference, ecosystem) for reference in references]
    answers = _queries_with_advisories(queries)
    return [
        _vulnerabilities(query, reference.current_version, has_advisories=has_advisories)
        for reference, query, has_advisories in zip(references, queries, answers, strict=True)
    ]


def _vulnerabilities(query: _Query, version: str, *, has_advisories: bool | None) -> list[Vulnerability] | None:
    """Return the vulnerabilities of the version, or None when OSV did not answer."""
    if has_advisories is None:
        return None
    return _reported_vulnerabilities(query, version) if has_advisories else []


def _queries_with_advisories(queries: Sequence[_Query]) -> list[bool | None]:
    """Return, for each query, whether OSV holds any advisory for it, or None where OSV did not answer it.

    For PyPI, npm, and Maven, an advisory counts when it names the query's version. For GitHub Actions, every
    advisory naming the action counts.

    OSV leaves a query unanswered when OSV cannot be reached, or when the batch answers fewer queries than it was
    asked. Such a query gets None rather than False.
    """
    payloads = [query.payload for query in queries]
    response = fetch(f"{_OSV_API}/v1/querybatch", _LOG, method="post", json={"queries": payloads})
    results = response.json().get("results", []) if response is not None else []
    answers = results[: len(queries)]
    return [bool(answer.get("vulns")) for answer in answers] + [None] * (len(queries) - len(answers))


def _reported_vulnerabilities(query: _Query, version: str) -> list[Vulnerability] | None:
    """Return the vulnerabilities OSV reports for the version, or None when they can't be read."""
    if (records := _advisory_records(query)) is None:
        return None
    return _folded([_vulnerability(record) for record in _records_naming_the_version(records, query, version)])


@cache
def _advisory_records(query: _Query) -> list[_Record] | None:
    """Return the full records of the advisories OSV returns for the query, or None when they can't be read.

    The request is cached, so references with the same query share it: two references to one action, or two pins of
    one version in different files.
    """
    response = fetch(f"{_OSV_API}/v1/query", _LOG, method="post", json=query.payload)
    return None if response is None else response.json().get("vulns", [])


def _records_naming_the_version(records: list[_Record], query: _Query, version: str) -> list[_Record]:
    """Return the records naming the version as affected.

    OSV picks them out already, except for GitHub Actions.
    """
    if query.ecosystem.matches_versions:
        return records
    pinned = Version(version)
    return [record for record in records if _names(record, query.package, pinned)]


def _names(record: _Record, package: _Package, version: Version) -> bool:
    """Return whether one of the record's `affected` entries for the package names the version as affected."""
    entries = [entry for entry in record.get("affected", []) if _is_about(entry, package)]
    return any(_entry_names(entry, version, record["id"]) for entry in entries)


def _is_about(entry: _Affected, package: _Package) -> bool:
    """Return whether the entry is about the package.

    It compares the name and the ecosystem alone, because OSV may identify the package by a purl as well. An entry
    about a Git repository leaves the package out.
    """
    about = entry.get("package")
    return about is not None and (about["name"], about["ecosystem"]) == (package["name"], package["ecosystem"])


def _entry_names(entry: _Affected, version: Version, advisory: str) -> bool:
    """Return whether the advisory's entry lists the version, or names it in one of its ranges of versions."""
    if any(_parsed_value(Version, listed, advisory) == version for listed in entry.get("versions", [])):
        return True
    last_known_range = entry.get("database_specific", {}).get("last_known_affected_version_range", "")
    ranges = [range_ for range_ in entry.get("ranges", []) if range_["type"] == "ECOSYSTEM"]
    return any(_in_range(range_, version, last_known_range, advisory) for range_ in ranges)


@cache
def _parsed_value[Parsed](parse: Callable[[str], Parsed], value: str, advisory: str) -> Parsed | None:
    """Return the value parsed, or None after warning that the advisory states a value that does not parse."""
    try:
        return parse(value)
    except (InvalidVersion, InvalidSpecifier) as error:
        _LOG.unreadable_affected_versions(advisory, error)
        return None


def _in_range(range_: _Range, version: Version, last_known_range: str, advisory: str) -> bool:
    """Return whether the advisory's range names the version as affected, passing over a range that does not parse.

    A range that ends in an introduction is open: it names every version from that introduction on. The last known
    affected version range bounds that open part, and is read only there.
    """
    if (events := _parsed_events(range_, advisory)) is None:
        return False
    kind, at = events[-1]
    if kind == "introduced" and at <= version:
        return (_parsed_value(SpecifierSet, last_known_range, advisory) or SpecifierSet()).contains(version)
    return _events_name(events, version)


def _parsed_events(range_: _Range, advisory: str) -> list[tuple[str, Version]] | None:
    """Return the range's events as their kind and their version, or None where a version does not parse."""
    events = []
    for event in range_["events"]:
        for kind, value in event.items():
            if (at := _parsed_value(Version, value, advisory)) is None:
                return None
            events.append((kind, at))
    return events


def _events_name(events: list[tuple[str, Version]], version: Version) -> bool:
    """Return whether the events, read in order, name the version as affected.

    An introduction at or below the version names it. A later fix at or below the version stops naming it, and so
    does a later last affected version below it.
    """
    named = False
    for kind, at in events:
        if kind == "introduced" and at <= version:
            named = True
        elif (kind == "fixed" and at <= version) or (kind == "last_affected" and at < version):
            named = False
    return named


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
