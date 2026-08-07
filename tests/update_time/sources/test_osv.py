"""Unit tests for the OSV module."""

import unittest
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import ANY, Mock, patch

from update_time.domain.version import Reference
from update_time.domain.vulnerability import RISK_LEVELS, Vulnerability
from update_time.io.log import Logger
from update_time.sources.osv import _RISK_LEVEL_BANDS, Ecosystem, get_vulnerabilities

from tests.helpers import mock_response
from tests.update_time.helpers import LoggingTestCase, osv_advisory, osv_api, vulnerability

if TYPE_CHECKING:
    from collections.abc import Sequence

_ADVISORY = "PYSEC-2021-109"
_SUMMARY = "SQL injection in QuerySet.order_by"
# The summary of the second defect, and of the second record of one defect, for the tests that serve either.
_OTHER_SUMMARY = "Denial of service in Django"
_REFERENCE = Reference("django", "3.2.0")
# Further advisory ids, for the tests that serve more than one record. OSV holds a record per database, and the
# databases name one defect by a `GHSA`, a `CVE`, a `PYSEC`, and a `BIT` identifier.
_GHSA = "GHSA-1111-1111-1111"
_CVE = "CVE-2021-1111"
_BIT = "BIT-django-2021-1111"
# A CVSS vector the `cvss` package refuses, since `AV:X` is not a value the metric can take.
_MALFORMED_VECTOR = "CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"


def _expected_vulnerability(
    advisory: str, level: str, summary: str = _SUMMARY, aliases: list[str] | None = None
) -> Vulnerability:
    """Return the vulnerability a defect reported at the advisory, at the level, and known by the aliases reads as.

    Defaults the summary these tests share, and orders its arguments as they vary, since every record here reports
    the same defect at a different level.
    """
    return vulnerability(advisory, summary, level, aliases)


@patch("requests.post")
class GetVulnerabilitiesTest(LoggingTestCase):
    """Unit tests for getting the vulnerabilities of the version a reference is pinned to."""

    def assert_vulnerabilities(
        self, mock_post: Mock, advisories: Sequence[dict[str, object]], expected: list[Vulnerability]
    ) -> None:
        """Assert that the reference's version is read as the expected vulnerabilities, given what OSV answers."""
        mock_post.side_effect = osv_api(*advisories)
        self.assertEqual(get_vulnerabilities([_REFERENCE], Ecosystem.PYPI), [expected])

    def assert_level(self, mock_post: Mock, advisory: dict[str, object], level: str) -> None:
        """Assert that the advisory is read as a vulnerability of the given risk level."""
        self.assert_vulnerabilities(mock_post, [advisory], [_expected_vulnerability(_ADVISORY, level)])

    def test_risk_level_derived_from_a_cvss_vector(self, mock_post: Mock):
        """Test that an advisory reporting no risk level is read at the level its CVSS base score bands into."""
        vectors = {
            "critical": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",  # 9.8
            "high": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",  # 7.5
            "moderate": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N",  # 5.3
            "low": "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N",  # 3.7
        }
        for level, vector in vectors.items():
            with self.subTest(level=level):
                self.assert_level(mock_post, osv_advisory(_ADVISORY, _SUMMARY, vectors={"CVSS_V3": vector}), level)

    def test_the_newest_cvss_vector_sets_the_risk_level(self, mock_post: Mock):
        """Test that the v4 vector sets the level of an advisory carrying both, where the two band differently.

        The vectors are the ones PYSEC-2026-457 carries, which band as high on v3 and critical on v4.
        """
        vectors = {
            "CVSS_V3": "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:H",
            "CVSS_V4": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N",
        }
        self.assert_level(mock_post, osv_advisory(_ADVISORY, _SUMMARY, vectors=vectors), "critical")

    def test_malformed_cvss_vector(self, mock_post: Mock):
        """Test that an advisory whose CVSS vector cannot be scored is read at no level, and warned about."""
        advisory = osv_advisory(_ADVISORY, _SUMMARY, vectors={"CVSS_V3": _MALFORMED_VECTOR})
        self.assert_level(mock_post, advisory, "")
        self.assert_logged(Logger._MESSAGE_MALFORMED_CVSS_VECTOR, advisory=_ADVISORY, error=ANY)

    def test_advisory_without_a_severity(self, mock_post: Mock):
        """Test that an advisory reporting no risk level is read as a vulnerability of no level, rather than failing.

        Over a third of the advisories OSV returns carry no `database_specific` section to read a level from.
        """
        self.assert_level(mock_post, osv_advisory(_ADVISORY, _SUMMARY), "")

    def test_a_defect_reported_twice_is_reported_once_at_its_rated_record(self, mock_post: Mock):
        """Test that a defect several databases report is read as one vulnerability, at the record that rates it.

        Only some of the databases rate a defect, so the order OSV happens to answer in must not decide whether the
        warning names a risk level, not even when the rated record is the one tying the others together. A record
        whose CVSS vector cannot be scored leaves the defect unrated, so the rated record wins there too.
        """
        ghsa = osv_advisory(_GHSA, _SUMMARY, level="HIGH", aliases=[_CVE])
        pysec = osv_advisory(_ADVISORY, _SUMMARY, aliases=[_CVE])
        bit = osv_advisory(_BIT, _SUMMARY)
        tying_ghsa = osv_advisory(_GHSA, _SUMMARY, level="HIGH", aliases=[_CVE, _BIT])
        malformed = osv_advisory(_ADVISORY, _SUMMARY, aliases=[_CVE], vectors={"CVSS_V3": _MALFORMED_VECTOR})
        two_records = [_expected_vulnerability(_GHSA, "high", aliases=[_CVE, _ADVISORY])]
        three_records = [_expected_vulnerability(_GHSA, "high", aliases=[_CVE, _ADVISORY, _BIT])]
        cases = {
            "rated first": ((ghsa, pysec), two_records),
            "rated second": ((pysec, ghsa), two_records),
            "rated last, tying the others": ((pysec, bit, tying_ghsa), three_records),
            "rated second, the other's vector malformed": ((malformed, ghsa), two_records),
        }
        for case, (advisories, expected) in cases.items():
            with self.subTest(case=case):
                self.assert_vulnerabilities(mock_post, advisories, expected)

    def test_a_defect_its_records_rate_differently_is_reported_at_the_most_severe(self, mock_post: Mock):
        """Test that a defect the databases rate differently is read at its most severe rating, in either order."""
        high = osv_advisory(_GHSA, _SUMMARY, level="HIGH", aliases=[_CVE])
        critical = osv_advisory(_ADVISORY, _OTHER_SUMMARY, level="CRITICAL", aliases=[_CVE])
        expected = [_expected_vulnerability(_ADVISORY, "critical", _OTHER_SUMMARY, aliases=[_CVE, _GHSA])]
        cases = {"most severe first": (critical, high), "most severe second": (high, critical)}
        for case, advisories in cases.items():
            with self.subTest(case=case):
                self.assert_vulnerabilities(mock_post, advisories, expected)

    def test_a_defect_its_records_rate_alike_is_reported_at_the_first_of_them(self, mock_post: Mock):
        """Test that a defect the databases rate alike is read at the record OSV answered with first."""
        ghsa = osv_advisory(_GHSA, _SUMMARY, level="HIGH", aliases=[_CVE])
        pysec = osv_advisory(_ADVISORY, _OTHER_SUMMARY, level="HIGH", aliases=[_CVE])
        cases = {
            "the GHSA record first": (
                (ghsa, pysec),
                [_expected_vulnerability(_GHSA, "high", aliases=[_CVE, _ADVISORY])],
            ),
            "the PYSEC record first": (
                (pysec, ghsa),
                [_expected_vulnerability(_ADVISORY, "high", _OTHER_SUMMARY, aliases=[_CVE, _GHSA])],
            ),
        }
        for case, (advisories, expected) in cases.items():
            with self.subTest(case=case):
                self.assert_vulnerabilities(mock_post, advisories, expected)

    def test_a_record_rated_by_a_cvss_vector_is_compared_at_the_level_it_bands_into(self, mock_post: Mock):
        """Test that a record carrying a CVSS vector is ranked at the vector's level, above and below a stated one."""
        stated_high = osv_advisory(_GHSA, _SUMMARY, level="HIGH", aliases=[_CVE])
        cases = {
            "banding above the stated level": (
                "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",  # 9.8, critical
                [_expected_vulnerability(_ADVISORY, "critical", _OTHER_SUMMARY, aliases=[_CVE, _GHSA])],
            ),
            "banding below the stated level": (
                "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N",  # 3.7, low
                [_expected_vulnerability(_GHSA, "high", aliases=[_CVE, _ADVISORY])],
            ),
        }
        for case, (vector, expected) in cases.items():
            with self.subTest(case=case):
                scored = osv_advisory(_ADVISORY, _OTHER_SUMMARY, aliases=[_CVE], vectors={"CVSS_V3": vector})
                self.assert_vulnerabilities(mock_post, [stated_high, scored], expected)

    def test_an_advisory_naming_another_by_its_id_is_merged_with_it(self, mock_post: Mock):
        """Test that a record naming another record's own id among its aliases is read as the same defect.

        A database names the other databases' records of a defect by their own ids, so two records can be one defect
        while sharing no alias with each other.
        """
        ghsa = osv_advisory(_GHSA, _SUMMARY, level="HIGH", aliases=[_CVE])
        pysec = osv_advisory(_ADVISORY, _SUMMARY, aliases=[_GHSA])
        expected = [_expected_vulnerability(_GHSA, "high", aliases=[_CVE, _ADVISORY])]
        for order, advisories in {"named first": (ghsa, pysec), "named second": (pysec, ghsa)}.items():
            with self.subTest(order=order):
                self.assert_vulnerabilities(mock_post, advisories, expected)

    def test_advisories_tied_together_only_through_a_third_are_reported_once(self, mock_post: Mock):
        """Test that records naming nothing in common are read as one defect when a third record names both.

        OSV puts its records in no particular order, so the cases run the tying record before the two it ties,
        between them, and after both, and tie defects that already hold several records each.
        """
        other_cve, third_cve = "CVE-2021-2222", "CVE-2021-3333"
        ghsa = osv_advisory(_GHSA, _SUMMARY, level="HIGH", aliases=[_CVE])
        pysec = osv_advisory(_ADVISORY, _SUMMARY, aliases=[_CVE, _BIT])
        bit = osv_advisory(_BIT, _SUMMARY)
        tied = [_expected_vulnerability(_GHSA, "high", aliases=[_CVE, _ADVISORY, _BIT])]
        # Two defects of two records each, tied by a third naming one identifier of either. The second record of
        # each pair carries what no other record does — an alias of its own, and the only risk level — so a merge
        # that keeps just the record a defect was opened with is reported at another id, and at no level.
        pysec_pair = (
            osv_advisory(_ADVISORY, _SUMMARY, aliases=[_CVE]),
            osv_advisory(_CVE, _SUMMARY, aliases=[third_cve]),
        )
        bit_pair = (
            osv_advisory(_BIT, _OTHER_SUMMARY, aliases=[other_cve]),
            osv_advisory(other_cve, _OTHER_SUMMARY, level="HIGH"),
        )
        tying_ghsa = osv_advisory(_GHSA, _SUMMARY, aliases=[_CVE, other_cve])
        tied_pairs = [
            _expected_vulnerability(other_cve, "high", _OTHER_SUMMARY, [_ADVISORY, _CVE, third_cve, _BIT, _GHSA])
        ]
        cases = {
            "the tying record first": ((pysec, ghsa, bit), tied),
            "the tying record between": ((ghsa, pysec, bit), tied),
            "the tying record last": ((ghsa, bit, pysec), tied),
            "the tied defects hold several records each": ((*pysec_pair, *bit_pair, tying_ghsa), tied_pairs),
        }
        for case, (records, expected) in cases.items():
            with self.subTest(case):
                self.assert_vulnerabilities(mock_post, records, expected)

    def test_defects_naming_nothing_in_common_are_each_reported(self, mock_post: Mock):
        """Test that two defects affecting one version stay two vulnerabilities, however alike their records read.

        The cases are two defects with different aliases, two naming no identifier at all, and a merged defect
        beside one on its own, which also pins that a merge keeps the place of the earliest record it merged. No
        record of that merged defect rates it, so it is reported at the record OSV answered with first, at no level.
        """
        other_cve = "CVE-2021-2222"
        cases = {
            "one defect merged, one on its own": (
                (
                    osv_advisory(_ADVISORY, _SUMMARY, aliases=[_CVE]),
                    osv_advisory(_BIT, _SUMMARY),
                    osv_advisory(_GHSA, _OTHER_SUMMARY, level="HIGH", aliases=[other_cve]),
                    osv_advisory(_CVE, _SUMMARY, aliases=[_BIT]),
                ),
                [
                    _expected_vulnerability(_ADVISORY, "", aliases=[_CVE, _BIT]),
                    _expected_vulnerability(_GHSA, "high", _OTHER_SUMMARY, aliases=[other_cve]),
                ],
            ),
            "different aliases": (
                (
                    osv_advisory(_ADVISORY, _SUMMARY, aliases=[_CVE]),
                    osv_advisory(_GHSA, _OTHER_SUMMARY, level="HIGH", aliases=[other_cve]),
                ),
                [
                    _expected_vulnerability(_ADVISORY, "", aliases=[_CVE]),
                    _expected_vulnerability(_GHSA, "high", _OTHER_SUMMARY, aliases=[other_cve]),
                ],
            ),
            "no aliases": (
                (osv_advisory(_ADVISORY, _SUMMARY), osv_advisory(_GHSA, _OTHER_SUMMARY, level="HIGH")),
                [_expected_vulnerability(_ADVISORY, ""), _expected_vulnerability(_GHSA, "high", _OTHER_SUMMARY)],
            ),
        }
        for case, (advisories, expected) in cases.items():
            with self.subTest(case=case):
                self.assert_vulnerabilities(mock_post, advisories, expected)

    def test_unreachable_advisories(self, mock_post: Mock):
        """Test that a pin the batch reports as affected is left unanswered when its advisories can't be read.

        The batch says the pin is affected but not by what, so a run that cannot read the advisories knows less
        about the pin than one that asked and was told nothing, and must not be taken for it.
        """
        status = HTTPStatus.SERVICE_UNAVAILABLE
        unreachable = mock_response(ok=False, status_code=status, reason=status.phrase, url="https://osv")
        affected = mock_response({"results": [{"vulns": [{"id": _ADVISORY}]}]})

        def serve(url: str, **_kwargs: object) -> Mock:
            return affected if url.endswith("/querybatch") else unreachable

        mock_post.side_effect = serve
        self.assertEqual(get_vulnerabilities([_REFERENCE], Ecosystem.PYPI), [None])
        self.assert_could_not_fetch_logged(url="https://osv", status=status, reason=status.phrase)

    def test_unreachable_osv(self, mock_post: Mock):
        """Test that an unreachable OSV leaves every pin without an answer, and is warned about.

        Each pin is left unanswered rather than unaffected, and an answer per pin is returned all the same, so the
        answers cannot end up misaligned with the pins asked about.
        """
        status = HTTPStatus.SERVICE_UNAVAILABLE
        mock_post.return_value = mock_response(ok=False, status_code=status, reason=status.phrase, url="https://osv")
        self.assertEqual(get_vulnerabilities([_REFERENCE, Reference("flask", "1.0")], Ecosystem.PYPI), [None, None])
        self.assert_could_not_fetch_logged(url="https://osv", status=status, reason=status.phrase)


class RiskLevelBandTest(unittest.TestCase):
    """Test that the CVSS bands and the risk levels the domain orders name the same levels."""

    def test_the_bands_name_every_risk_level(self):
        """Test that the bands name each risk level once, most severe first, and no level the domain doesn't order.

        The bands say which level a CVSS base score falls in, and the domain orders the levels a threshold compares
        against. A level added to one and not the other would leave scores banding to a level nothing compares
        against, or a threshold no score can reach.
        """
        banded = [level for _lowest_score, level in _RISK_LEVEL_BANDS if level]
        self.assertEqual(banded, list(reversed(RISK_LEVELS)))
