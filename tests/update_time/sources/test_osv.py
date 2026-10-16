"""Unit tests for the OSV module."""

import unittest
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import ANY, Mock, patch

from update_time.domain.reference import Reference
from update_time.domain.vulnerability import RISK_LEVELS, Vulnerability
from update_time.io.log import Logger
from update_time.primitives.location import Location
from update_time.sources import osv
from update_time.sources.osv import _RISK_LEVEL_BANDS, Ecosystem, get_vulnerabilities

from tests.helpers import mock_response
from tests.mutation import Mutation, kills
from tests.update_time.helpers import (
    LoggingTestCase,
    osv_action_entry,
    osv_action_package,
    osv_advisory,
    osv_api,
    vulnerability,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_ADVISORY = "PYSEC-2021-109"
_SUMMARY = "SQL injection in QuerySet.order_by"
# The summary of the second vulnerability, and of a vulnerability's second advisory, for the tests that serve either.
_OTHER_SUMMARY = "Denial of service in Django"
_REFERENCE = Reference("django", "3.2.0", Location(Path("requirements.txt"), 1))
# Further advisory ids, for the tests that serve more than one advisory. OSV holds an advisory per database, and the
# databases name one vulnerability by a `GHSA`, a `CVE`, a `PYSEC`, and a `BIT` identifier.
_GHSA = "GHSA-1111-1111-1111"
_CVE = "CVE-2021-1111"
_BIT = "BIT-django-2021-1111"
# A CVSS vector the `cvss` package refuses, since `AV:X` is not a value the metric can take.
_MALFORMED_VECTOR = "CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"


def _expected_vulnerability(
    advisory: str, level: str, summary: str = _SUMMARY, aliases: list[str] | None = None
) -> Vulnerability:
    """Return the vulnerability an advisory reports, at the level, and known by the aliases."""
    return vulnerability(advisory, summary, level, aliases)


@patch("requests.Session.post")
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

        The vectors band as high on v3 and critical on v4.
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
        """Test that an advisory reporting no risk level is read as a vulnerability of no level, rather than failing."""
        self.assert_level(mock_post, osv_advisory(_ADVISORY, _SUMMARY), "")

    @kills(
        Mutation(
            osv._vulnerability,
            'frozenset(record.get("aliases") or []),',
            'frozenset(record.get("aliases", [])),',
            "an advisory whose aliases OSV reports as null ends the run with a traceback",
            raises="TypeError: 'NoneType' object is not iterable",
        ),
        Mutation(
            osv._banded_risk_level,
            'for severity in record.get("severity") or []}',
            'for severity in record.get("severity", [])}',
            "an advisory whose severity OSV reports as null ends the run with a traceback",
            raises="TypeError: 'NoneType' object is not iterable",
        ),
    )
    def test_advisory_with_a_null_array(self, mock_post: Mock):
        """Test that an advisory whose severity or aliases OSV reports as null is read as a vulnerability still."""
        for key in ("severity", "aliases"):
            with self.subTest(key=key):
                self.assert_vulnerabilities(
                    mock_post,
                    [osv_advisory(_ADVISORY, _SUMMARY) | {key: None}],
                    [_expected_vulnerability(_ADVISORY, "")],
                )

    def test_a_vulnerability_reported_twice_is_reported_once_at_its_rated_advisory(self, mock_post: Mock):
        """Test that a vulnerability several databases report is warned about once, at the advisory that rates it."""
        ghsa = osv_advisory(_GHSA, _SUMMARY, level="HIGH", aliases=[_CVE])
        pysec = osv_advisory(_ADVISORY, _SUMMARY, aliases=[_CVE])
        bit = osv_advisory(_BIT, _SUMMARY)
        tying_ghsa = osv_advisory(_GHSA, _SUMMARY, level="HIGH", aliases=[_CVE, _BIT])
        malformed = osv_advisory(_ADVISORY, _SUMMARY, aliases=[_CVE], vectors={"CVSS_V3": _MALFORMED_VECTOR})
        two_advisories = [_expected_vulnerability(_GHSA, "high", aliases=[_CVE, _ADVISORY])]
        three_advisories = [_expected_vulnerability(_GHSA, "high", aliases=[_CVE, _ADVISORY, _BIT])]
        cases = {
            "rated first": ((ghsa, pysec), two_advisories),
            "rated second": ((pysec, ghsa), two_advisories),
            "rated last, tying the others": ((pysec, bit, tying_ghsa), three_advisories),
            "rated second, the other's vector malformed": ((malformed, ghsa), two_advisories),
        }
        for case, (advisories, expected) in cases.items():
            with self.subTest(case=case):
                self.assert_vulnerabilities(mock_post, advisories, expected)

    def test_a_vulnerability_its_advisories_rate_differently_is_reported_at_the_most_severe(self, mock_post: Mock):
        """Test that a vulnerability the databases rate differently is read at its severest rating, in either order."""
        high = osv_advisory(_GHSA, _SUMMARY, level="HIGH", aliases=[_CVE])
        critical = osv_advisory(_ADVISORY, _OTHER_SUMMARY, level="CRITICAL", aliases=[_CVE])
        expected = [_expected_vulnerability(_ADVISORY, "critical", _OTHER_SUMMARY, aliases=[_CVE, _GHSA])]
        cases = {"most severe first": (critical, high), "most severe second": (high, critical)}
        for case, advisories in cases.items():
            with self.subTest(case=case):
                self.assert_vulnerabilities(mock_post, advisories, expected)

    def test_a_vulnerability_its_advisories_rate_alike_is_reported_at_the_first_of_them(self, mock_post: Mock):
        """Test that a vulnerability the databases rate alike is read at the advisory OSV answered with first."""
        ghsa = osv_advisory(_GHSA, _SUMMARY, level="HIGH", aliases=[_CVE])
        pysec = osv_advisory(_ADVISORY, _OTHER_SUMMARY, level="HIGH", aliases=[_CVE])
        cases = {
            "the GHSAn advisory first": (
                (ghsa, pysec),
                [_expected_vulnerability(_GHSA, "high", aliases=[_CVE, _ADVISORY])],
            ),
            "the PYSEC advisory first": (
                (pysec, ghsa),
                [_expected_vulnerability(_ADVISORY, "high", _OTHER_SUMMARY, aliases=[_CVE, _GHSA])],
            ),
        }
        for case, (advisories, expected) in cases.items():
            with self.subTest(case=case):
                self.assert_vulnerabilities(mock_post, advisories, expected)

    def test_an_advisory_rated_by_a_cvss_vector_is_compared_at_the_level_it_bands_into(self, mock_post: Mock):
        """Test that an advisory carrying a CVSS vector ranks at the vector's level, above and below a stated one."""
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
        """Test that an advisory naming another's own id among its aliases is read as the same vulnerability."""
        ghsa = osv_advisory(_GHSA, _SUMMARY, level="HIGH", aliases=[_CVE])
        pysec = osv_advisory(_ADVISORY, _SUMMARY, aliases=[_GHSA])
        expected = [_expected_vulnerability(_GHSA, "high", aliases=[_CVE, _ADVISORY])]
        for order, advisories in {"named first": (ghsa, pysec), "named second": (pysec, ghsa)}.items():
            with self.subTest(order=order):
                self.assert_vulnerabilities(mock_post, advisories, expected)

    def test_advisories_tied_together_only_through_a_third_are_reported_once(self, mock_post: Mock):
        """Test that advisories sharing no identifier are read as one vulnerability when a third names both."""
        other_cve, third_cve = "CVE-2021-2222", "CVE-2021-3333"
        ghsa = osv_advisory(_GHSA, _SUMMARY, level="HIGH", aliases=[_CVE])
        pysec = osv_advisory(_ADVISORY, _SUMMARY, aliases=[_CVE, _BIT])
        bit = osv_advisory(_BIT, _SUMMARY)
        tied = [_expected_vulnerability(_GHSA, "high", aliases=[_CVE, _ADVISORY, _BIT])]
        # Two vulnerabilities of two advisories each, tied by a third naming one identifier of either. The second
        # advisory of each pair carries what no other does — an alias of its own, and the only risk level — so a
        # merge that keeps just the advisory a vulnerability was opened with is reported at another id, and at no
        # level.
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
            "the tying advisory first": ((pysec, ghsa, bit), tied),
            "the tying advisory between": ((ghsa, pysec, bit), tied),
            "the tying advisory last": ((ghsa, bit, pysec), tied),
            "the tied vulnerabilities hold several advisories each": ((*pysec_pair, *bit_pair, tying_ghsa), tied_pairs),
        }
        for case, (advisories, expected) in cases.items():
            with self.subTest(case):
                self.assert_vulnerabilities(mock_post, advisories, expected)

    def test_vulnerabilities_naming_nothing_in_common_are_each_reported(self, mock_post: Mock):
        """Test that two vulnerabilities affecting one version are each warned about, however alike their advisories."""
        other_cve = "CVE-2021-2222"
        cases = {
            "one vulnerability merged, one on its own": (
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
        """Test that a pin the batch reports advisories for is left unanswered when they can't be read."""
        status = HTTPStatus.SERVICE_UNAVAILABLE
        unreachable = mock_response(ok=False, status_code=status, reason=status.phrase, url="https://osv")
        with_advisories = mock_response({"results": [{"vulns": [{"id": _ADVISORY}]}]})

        def serve(url: str, **_kwargs: object) -> Mock:
            return with_advisories if url.endswith("/querybatch") else unreachable

        mock_post.side_effect = serve
        self.assertEqual(get_vulnerabilities([_REFERENCE], Ecosystem.PYPI), [None])
        self.assert_could_not_fetch_logged("https://osv", status=status, reason=status.phrase)

    def test_unreachable_osv(self, mock_post: Mock):
        """Test that an unreachable OSV returns an answer per pin, each of them none, and is warned about."""
        status = HTTPStatus.SERVICE_UNAVAILABLE
        mock_post.return_value = mock_response(ok=False, status_code=status, reason=status.phrase, url="https://osv")
        self.assertEqual(
            get_vulnerabilities(
                [_REFERENCE, Reference("flask", "1.0", Location(Path("requirements.txt"), 2))], Ecosystem.PYPI
            ),
            [None, None],
        )
        self.assert_could_not_fetch_logged("https://osv", status=status, reason=status.phrase)


_ACTION = "tj-actions/changed-files"
_ACTION_PACKAGE = osv_action_package(_ACTION)
# What OSV reports for an action version an advisory names.
_ACTION_VULNERABILITIES = [_expected_vulnerability(_GHSA, "high")]


def _action_vulnerabilities(version: str) -> list[list[Vulnerability] | None]:
    """Return what OSV is read as reporting for a reference to the action at the version."""
    return get_vulnerabilities(
        [Reference(_ACTION, version, Location(Path(".github/workflows/ci.yml"), 1))], Ecosystem.GITHUB_ACTIONS
    )


def _advisory_affecting(advisory: str, entry: dict[str, object]) -> dict[str, object]:
    """Return an OSV advisory record whose one `affected` entry is the given one."""
    return osv_advisory(advisory, _SUMMARY, level="HIGH") | {"affected": [entry]}


def _action_advisory(advisory: str, *events: dict[str, str], **fields: object) -> dict[str, object]:
    """Return an OSV advisory record whose one `affected` entry names the action (see `osv_action_entry`)."""
    return _advisory_affecting(advisory, osv_action_entry(_ACTION, *events, **fields))


@patch("requests.Session.post")
class GitHubActionVulnerabilitiesTest(LoggingTestCase):
    """Unit tests for getting the vulnerabilities of the version a GitHub Action is pinned to."""

    def assert_cases(self, cases: dict[str, tuple[str, list[Vulnerability]]]) -> None:
        """Assert that each case's action version is read as its expected vulnerabilities."""
        for case, (version, expected) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(_action_vulnerabilities(version), [expected])

    def test_references_to_one_action_share_the_request_for_its_advisories(self, mock_post: Mock):
        """Test that OSV is asked about an action without a version, once in full for references at two versions."""
        mock_post.side_effect = osv_api(_action_advisory(_GHSA, {"introduced": "0"}, {"fixed": "46.0.1"}))
        self.assertEqual(_action_vulnerabilities("v45.0.0"), [_ACTION_VULNERABILITIES])
        self.assertEqual(_action_vulnerabilities("v46.0.1"), [[]])
        query = {"package": _ACTION_PACKAGE}
        batch = {"queries": [query]}
        self.assertEqual([call.kwargs["json"] for call in mock_post.call_args_list], [batch, query, batch])

    def test_a_version_is_reported_from_the_introduction_up_to_the_fix(self, mock_post: Mock):
        """Test that an action version is reported from the range's introduction on, and no longer at its fix."""
        mock_post.side_effect = osv_api(_action_advisory(_GHSA, {"introduced": "2.0.0"}, {"fixed": "46.0.1"}))
        self.assert_cases(
            {
                "at the introduction": ("v2.0.0", _ACTION_VULNERABILITIES),
                "below the introduction": ("v1.9.9", []),
                "at the fix": ("v46.0.1", []),
                "past the fix": ("v47", []),
            }
        )

    def test_a_version_is_reported_up_to_and_including_the_last_affected_one(self, mock_post: Mock):
        """Test that an action version is reported up to the range's last affected version, and no longer past it."""
        mock_post.side_effect = osv_api(_action_advisory(_GHSA, {"introduced": "2.0.0"}, {"last_affected": "45.0.7"}))
        self.assert_cases(
            {"at the last affected version": ("v45.0.7", _ACTION_VULNERABILITIES), "past it": ("v45.0.8", [])}
        )

    @kills(
        Mutation(
            osv._events_name,
            "named = False\n    return named",
            "return False\n    return named",
            "a range is read as ending at its first fix, so a version after a reintroduction goes unreported",
        )
    )
    def test_a_version_is_reported_where_a_range_reintroduces_the_vulnerability_after_a_fix(self, mock_post: Mock):
        """Test that an action version is reported between a range's second introduction and its second fix."""
        events = ({"introduced": "1.0.0"}, {"fixed": "2.0.0"}, {"introduced": "3.0.0"}, {"fixed": "4.0.0"})
        mock_post.side_effect = osv_api(_action_advisory(_GHSA, *events))
        self.assertEqual(_action_vulnerabilities("v3.5.0"), [_ACTION_VULNERABILITIES])

    def test_an_entry_for_another_package_or_for_none_is_passed_over(self, mock_post: Mock):
        """Test that an advisory's entry for another ecosystem, another action, or no package passes the action over."""
        ranges = [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}]
        entries: dict[str, dict[str, object]] = {
            "another ecosystem": {"package": {"name": _ACTION, "ecosystem": "npm"}, "ranges": ranges},
            "another action": {
                "package": {"name": "tj-actions/branch-names", "ecosystem": "GitHub Actions"},
                "ranges": ranges,
            },
            "no package": {"ranges": ranges},
        }
        for case, entry in entries.items():
            with self.subTest(case=case):
                mock_post.side_effect = osv_api(_advisory_affecting(_GHSA, entry))
                self.assertEqual(_action_vulnerabilities("v45.0.0"), [[]])

    @kills(
        Mutation(
            osv._entry_names,
            ' if range_["type"] == "ECOSYSTEM"',
            "",
            "a range of commits is read as a range of versions, and its commit SHA warned about as unreadable",
        )
    )
    def test_a_git_range_is_passed_over(self, mock_post: Mock):
        """Test that a `GIT` range is passed over, so a version the `ECOSYSTEM` range fixes stays unreported."""
        versions = {"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "0.1.22"}]}
        commits = {
            "type": "GIT",
            "events": [{"introduced": "0"}, {"fixed": "f77273f4c914e4bf38440cf36a0369cb64a37489"}],
        }
        mock_post.side_effect = osv_api(_action_advisory(_GHSA, ranges=[versions, commits]))
        self.assertEqual(_action_vulnerabilities("v0.1.22"), [[]])
        self.assert_no_warnings_logged()

    def test_an_affected_version_that_does_not_parse_is_passed_over_with_a_warning(self, mock_post: Mock):
        """Test that the check reads what it can of an advisory whose affected versions do not all parse."""
        unreadable_bound = {"last_known_affected_version_range": "unknown"}
        cases = {
            "a listed version": (_action_advisory(_GHSA, versions=["latest", "1"]), _ACTION_VULNERABILITIES),
            "a range's version": (_action_advisory(_GHSA, {"introduced": "0"}, {"fixed": "unknown"}), []),
            "a last known affected version range": (
                _action_advisory(_GHSA, {"introduced": "0"}, database_specific=unreadable_bound),
                _ACTION_VULNERABILITIES,
            ),
        }
        for case, (advisory, expected) in cases.items():
            with self.subTest(case=case):
                mock_post.side_effect = osv_api(advisory)
                self.assertEqual(_action_vulnerabilities("v1"), [expected])
                self.assert_logged(Logger._MESSAGE_UNREADABLE_AFFECTED_VERSIONS, advisory=_GHSA, error=ANY)

    def test_an_unreadable_version_is_warned_about_once_however_many_references_meet_it(self, mock_post: Mock):
        """Test that two references to one action, whose advisory states a version that does not parse, warn once."""
        advisories = {
            "a listed version": _action_advisory(_GHSA, versions=["latest", "1"]),
            "a range's version": _action_advisory(_GHSA, {"introduced": "0"}, {"fixed": "unknown"}),
        }
        for case, advisory in advisories.items():
            with self.subTest(case=case):
                mock_post.side_effect = osv_api(advisory)
                for version in ("v1", "v2"):
                    _action_vulnerabilities(version)
                self.assert_logged(Logger._MESSAGE_UNREADABLE_AFFECTED_VERSIONS, advisory=_GHSA, error=ANY)

    def test_a_closed_range_leaves_an_unreadable_last_known_affected_version_range_unread(self, mock_post: Mock):
        """Test that a closed range warns about nothing beside a last known affected version range it cannot read."""
        unreadable_bound = {"last_known_affected_version_range": "unknown"}
        mock_post.side_effect = osv_api(
            _action_advisory(_GHSA, {"introduced": "0"}, {"fixed": "2"}, database_specific=unreadable_bound)
        )
        self.assertEqual(_action_vulnerabilities("v1"), [_ACTION_VULNERABILITIES])
        self.assert_no_warnings_logged()

    def test_a_listed_version_is_reported_however_precisely_it_is_spelled(self, mock_post: Mock):
        """Test that an action version an advisory lists is reported, spelled with or without its zero components."""
        mock_post.side_effect = osv_api(_action_advisory(_GHSA, versions=["1"]))
        self.assert_cases(
            {
                "as listed": ("v1", _ACTION_VULNERABILITIES),
                "with zero components": ("v1.0.0", _ACTION_VULNERABILITIES),
                "another version": ("v1.1", []),
            }
        )

    @kills(
        Mutation(
            osv._in_range,
            'if kind == "introduced" and at <= version:',
            'if kind == "introduced":',
            "an open range names a version below its introduction, wherever the last known affected range holds it",
        )
    )
    def test_the_last_known_affected_version_range_bounds_an_open_range(self, mock_post: Mock):
        """Test that an open range reports an action version only within its last known affected version range."""
        bounded = {"last_known_affected_version_range": "< 3.0.0"}
        mock_post.side_effect = osv_api(_action_advisory(_GHSA, {"introduced": "2.26.11"}, database_specific=bounded))
        self.assert_cases(
            {
                "below the introduction": ("v2.26.10", []),
                "below the bound": ("v2.27.0", _ACTION_VULNERABILITIES),
                "at the bound": ("v3.0.0", []),
            }
        )

    @kills(
        Mutation(
            osv._entry_names,
            '"")',
            '"<0")',
            "an action version in an open range goes unreported where the advisory leaves out its last known affected "
            "version range",
        )
    )
    def test_an_open_range_without_a_last_known_affected_version_range_reports_every_later_version(
        self, mock_post: Mock
    ):
        """Test that an open range without a last known affected version range reports a version long after it opens."""
        mock_post.side_effect = osv_api(_action_advisory(_GHSA, {"introduced": "2.26.11"}))
        self.assertEqual(_action_vulnerabilities("v4.0.0"), [_ACTION_VULNERABILITIES])


class RiskLevelBandTest(unittest.TestCase):
    """Test that the CVSS bands and the risk levels the domain orders name the same levels."""

    def test_the_bands_name_every_risk_level(self):
        """Test that the bands name each risk level once, most severe first, and no level the domain doesn't order.

        A level added to one and not the other would leave scores banding to a level nothing compares against, or a
        threshold no score can reach.
        """
        banded = [level for _lowest_score, level in _RISK_LEVEL_BANDS if level]
        self.assertEqual(banded, list(reversed(RISK_LEVELS)))
