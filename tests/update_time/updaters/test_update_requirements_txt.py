"""Unit tests for the requirements.txt update script."""

import unittest
from datetime import UTC, datetime
from pathlib import PurePath
from unittest.mock import ANY, MagicMock, Mock, patch

from update_time.domain.vulnerability import IGNORE_VULNERABILITIES, WARN_VULNERABILITY_LEVEL
from update_time.primitives.location import Location
from update_time.updaters.update_requirements_txt import update_requirements_txts

from tests.helpers import mock_path, patch_environ
from tests.update_time.helpers import (
    DJANGO_ADVISORY,
    DJANGO_VULNERABILITY,
    PYPI_OLD_UPLOAD,
    LoggingTestCase,
    no_vulnerabilities,
    osv,
    osv_advisory,
    osv_vulnerability,
    pypi_index,
    pypi_release,
    staleness_disabled,
    unreachable_osv,
    vulnerability_check_disabled,
    yanked_file,
)

_PUBLISHED = "1.1, published: 2020-01-01 00:00"  # How PYPI_OLD_UPLOAD is rendered in the log.

# A second advisory affecting those same pins, for the tests that need OSV to report two. Rated moderate, so a `high`
# risk level in force filters it out where the critical one above survives.
_OTHER_ADVISORY, _OTHER_VULNERABILITY = osv_vulnerability(
    "GHSA-1111-1111-1111", "Denial of service in Django", "moderate"
)

# The endpoint the pins of one file are looked up at, spelled out here and pinned to the source below.
_OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"

# The globs the updater discovers requirements files with, spelled out here and pinned to the updater below.
_GLOB_PATTERNS = ("requirements.txt", "requirements-*.txt", "*-requirements.txt", "requirements/*.txt")


@no_vulnerabilities
@patch("requests.get")
@patch("pathlib.Path.rglob")
class UpdateRequirementsTxtTest(LoggingTestCase):
    """Unit tests for the update requirements.txt function."""

    def requirements_file(self, contents: str, *, sibling_in: bool = False) -> Mock:
        """Return a mock requirements file, optionally with a sibling `.in` source file present.

        `is_compiled` treats a file as compiled when `(path.parent / f"{path.stem}.in").exists()`, so model the
        parent's `/` as returning that sibling `.in` file, whose `exists()` reflects `sibling_in`.
        """
        requirements_txt = mock_path(contents)
        requirements_txt.stem = "requirements"  # so the sibling checked for is `requirements.in`
        sibling_in_file = Mock(exists=Mock(return_value=sibling_in))
        requirements_txt.parent = MagicMock()
        requirements_txt.parent.__truediv__.return_value = sibling_in_file
        return requirements_txt

    def discovered_requirements_txt(self, rglob: Mock, contents: str, *, sibling_in: bool = False) -> Mock:
        """Return the single requirements file the scan discovers, holding the contents."""
        requirements_txt = self.requirements_file(contents, sibling_in=sibling_in)
        rglob.return_value = [requirements_txt]
        return requirements_txt

    def pypi(self, *versions: str, bump: bool = False, upload_time: str = PYPI_OLD_UPLOAD) -> list:
        """Return mock PyPI responses: the Index API versions, plus per-version metadata when a bump is expected."""
        responses = [pypi_index(*versions)]
        if bump:
            responses.append(pypi_release(upload_time))
        return responses

    def stale_pypi(self, *versions: str, upload_time: str = PYPI_OLD_UPLOAD) -> list:
        """Return a mock Index API response listing the versions and a distribution file with the given upload time."""
        return [pypi_index(*versions, files=[{"upload-time": upload_time}])]

    def test_no_change(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin already on the latest version is left unchanged."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "flask==1.0\n")
        mock_get.side_effect = self.pypi("1.0")
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        self.assert_path_logged(requirements_txt)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_stale_dependency_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin whose newest release is old is warned about, without being changed."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "humanize==4.15.0\n")
        mock_get.side_effect = self.stale_pypi("4.15.0")  # No newer version; newest release is old.
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        self.assert_stale_dependency_logged("humanize", "4.15.0", Location(requirements_txt, 1))

    def test_yanked_dependency_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin left on a yanked version is warned about, without being changed."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "humanize==4.15.0\n")
        yanked = [yanked_file("humanize-4.15.0.tar.gz", reason="broke Python 3.10")]
        mock_get.side_effect = [
            pypi_index("4.15.0", files=yanked)
        ]  # No newer version; the pin's own release is yanked.
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        self.assert_yanked_dependency_logged("humanize", "4.15.0", Location(requirements_txt, 1))

    def test_ignore_update_marker_still_warns_about_a_yank(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin frozen on a yanked version by `ignore[update]` is still warned about.

        A newer version is available, but the marker keeps the reference on the yanked one, so the warning applies.
        """
        requirements_txt = self.discovered_requirements_txt(
            mock_rglob, "humanize==4.15.0  # update-time: ignore[update]\n"
        )
        yanked = [yanked_file("humanize-4.15.0.tar.gz", reason="broke Python 3.10")]
        mock_get.side_effect = [pypi_index("4.15.0", "4.16.0", files=yanked), pypi_release()]
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()  # the marker keeps the pin on the yanked version
        self.assert_yanked_dependency_logged("humanize", "4.15.0", Location(requirements_txt, 1))

    def test_ignore_yanked_marker_silences_the_warning(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an `ignore[yanked]` marker on the pin's line holds back the yank warning."""
        requirements_txt = self.discovered_requirements_txt(
            mock_rglob, "humanize==4.15.0  # update-time: ignore[yanked]\n"
        )
        yanked = [yanked_file("humanize-4.15.0.tar.gz", reason="broke Python 3.10")]
        mock_get.side_effect = [pypi_index("4.15.0", files=yanked)]
        update_requirements_txts()
        self.assert_no_warnings_logged()
        self.assert_ignored_yank_logged("humanize", Location(requirements_txt, 1), "ignore[yanked]")

    def test_vulnerable_dependency_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin left on a version OSV reports an advisory for is warned about, without being changed."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "django==3.2.0\n")
        mock_get.side_effect = self.pypi("3.2.0")  # No newer version; the pin's own release is vulnerable.
        with osv(DJANGO_ADVISORY):
            update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(requirements_txt, 1))

    def test_vulnerability_check_disabled(self, mock_rglob: Mock, mock_get: Mock):
        """Test that no vulnerability is reported, and OSV is not asked at all, when the check is switched off."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "django==3.2.0\n")
        mock_get.side_effect = self.pypi("3.2.0")
        with osv(DJANGO_ADVISORY) as mock_post, vulnerability_check_disabled:
            update_requirements_txts()
        mock_post.assert_not_called()
        self.assert_path_logged(requirements_txt)  # the file was checked for updates, just not for vulnerabilities
        self.assert_no_warnings_logged()

    def test_vulnerability_below_the_risk_level_in_force(self, mock_rglob: Mock, mock_get: Mock):
        """Test that of the advisories affecting a pin, only those at or above the level in force are warned about."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "django==3.2.0\n")
        mock_get.side_effect = self.pypi("3.2.0")
        with osv(DJANGO_ADVISORY, _OTHER_ADVISORY), patch_environ({WARN_VULNERABILITY_LEVEL.name: "high"}):
            update_requirements_txts()
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(requirements_txt, 1))

    def test_the_markers_risk_level_decides_what_is_warned_about(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin carrying its own risk level is judged by that one, not by the level in force run-wide."""
        directive = "ignore[vulnerable<high]"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, f"django==3.2.0  # update-time: {directive}\n")
        mock_get.side_effect = self.pypi("3.2.0", "3.3.0", bump=True)
        with osv(DJANGO_ADVISORY, _OTHER_ADVISORY):
            update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with(f"django==3.3.0  # update-time: {directive}\n")
        self.assert_vulnerable_dependency_logged("django", "3.3.0", DJANGO_VULNERABILITY, Location(requirements_txt, 1))

    def test_the_markers_risk_level_is_redundant_when_no_vulnerability_falls_below_it(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that a marker setting a risk level no vulnerability falls below is reported as redundant."""
        directive = "ignore[vulnerable<high]"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, f"django==3.2.0  # update-time: {directive}\n")
        mock_get.side_effect = self.pypi("3.2.0")
        with osv(DJANGO_ADVISORY):
            update_requirements_txts()
        self.assert_redundant_vulnerable_level_logged(
            "django", "3.2.0", "high", Location(requirements_txt, 1), directive
        )
        self.assert_vulnerable_dependency_logged(
            "django", "3.2.0", DJANGO_VULNERABILITY, Location(requirements_txt, 1), among_others=True
        )

    def test_an_inverted_comparison_leaves_the_global_risk_level_in_force(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a `vulnerable` item comparing the wrong way is reported and sets no level for the pin."""
        item = "vulnerable>=high"
        requirements_txt = self.discovered_requirements_txt(
            mock_rglob, f"django==3.2.0  # update-time: ignore[{item}]\n"
        )
        mock_get.side_effect = self.pypi("3.2.0")
        with osv(_OTHER_ADVISORY):
            update_requirements_txts()
        self.assert_inverted_vulnerable_item_logged("django", item, Location(requirements_txt, 1))
        self.assert_vulnerable_dependency_logged(
            "django", "3.2.0", _OTHER_VULNERABILITY, Location(requirements_txt, 1), among_others=True
        )

    def test_the_markers_risk_level_survives_the_check_being_switched_off(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin with a risk level of its own is looked up although the check is switched off run-wide."""
        directive = "ignore[vulnerable<high]"
        requirements_txt = self.discovered_requirements_txt(
            mock_rglob, f"django==3.2.0  # update-time: {directive}\nflask==1.0\n"
        )
        mock_get.side_effect = [pypi_index("3.2.0"), pypi_index("1.0")]
        with osv(DJANGO_ADVISORY, _OTHER_ADVISORY) as mock_post, vulnerability_check_disabled:
            update_requirements_txts()
        queries = [{"package": {"name": "django", "ecosystem": "PyPI"}, "version": "3.2.0"}]
        mock_post.assert_any_call(_OSV_BATCH_URL, timeout=ANY, json={"queries": queries})
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(requirements_txt, 1))

    def test_clean_pins_cost_one_osv_request(self, mock_rglob: Mock, mock_get: Mock):
        """Test that the pins of one file are looked up in a single batch, which answers for a clean file on its own."""
        self.discovered_requirements_txt(mock_rglob, "django==3.2.0\nflask==1.0\n")
        mock_get.side_effect = [pypi_index("3.2.0"), pypi_index("1.0")]
        with osv() as mock_post:
            update_requirements_txts()
        queries = [
            {"package": {"name": "django", "ecosystem": "PyPI"}, "version": "3.2.0"},
            {"package": {"name": "flask", "ecosystem": "PyPI"}, "version": "1.0"},
        ]
        mock_post.assert_called_once_with(_OSV_BATCH_URL, timeout=ANY, json={"queries": queries})
        self.assert_no_warnings_logged()

    def test_ignore_marker_queries_no_vulnerabilities(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin held back by a bare `ignore` marker is not looked up at OSV, so it is not warned about."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "django==3.2.0  # update-time: ignore\n")
        with osv(DJANGO_ADVISORY) as mock_post:
            update_requirements_txts()
        mock_post.assert_not_called()
        mock_get.assert_not_called()
        self.assert_ignored_logged("django", Location(requirements_txt, 1), "ignore")
        self.assert_no_redundant_suppression_logged()
        self.assert_no_warnings_logged()

    def test_ignore_vulnerable_marker_reports_nothing_when_it_silences_nothing(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a marker whose only advisory is below the level in force is reported neither way."""
        self.discovered_requirements_txt(mock_rglob, "django==3.2.0  # update-time: ignore[vulnerable]\n")
        mock_get.side_effect = self.pypi("3.2.0")
        with osv(_OTHER_ADVISORY), patch_environ({WARN_VULNERABILITY_LEVEL.name: "high"}):
            update_requirements_txts()
        self.assert_no_warnings_logged()
        self.assert_no_ignored_vulnerability_logged()

    def test_ignore_vulnerable_marker_is_redundant_when_the_pin_has_no_vulnerability(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that an `ignore[vulnerable]` marker on a pin OSV reports no advisory for is reported as redundant."""
        directive = "ignore[vulnerable]"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, f"django==3.2.0  # update-time: {directive}\n")
        mock_get.side_effect = self.pypi("3.2.0")
        with osv():
            update_requirements_txts()
        self.assert_redundant_vulnerable_scope_logged("django", "3.2.0", Location(requirements_txt, 1), directive)
        self.assert_no_ignored_vulnerability_logged()

    def test_no_suppression_is_reported_when_osv_cannot_be_reached(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an unreachable OSV leaves every form of suppression unjudged, rather than reported as dead."""
        for case, directive in {
            "the scope": "ignore[vulnerable]",
            "a named advisory": f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]",
            "a risk level": "ignore[vulnerable<high]",
        }.items():
            with self.subTest(case=case):
                self.mock_log.reset_mock()  # Judge each case on the records of its own run.
                self.discovered_requirements_txt(mock_rglob, f"django==3.2.0  # update-time: {directive}\n")
                mock_get.side_effect = self.pypi("3.2.0")
                with unreachable_osv():
                    update_requirements_txts()
                self.assert_no_redundant_suppression_logged()

    def test_ignore_vulnerable_marker_silences_the_warning(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an `ignore[vulnerable]` marker holds back the vulnerability warning and nothing else."""
        requirements_txt = self.discovered_requirements_txt(
            mock_rglob, "django==3.2.0  # update-time: ignore[vulnerable]\n"
        )
        mock_get.side_effect = self.pypi("3.2.0", "3.3.0", bump=True)
        with osv(DJANGO_ADVISORY) as mock_post:
            update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with("django==3.3.0  # update-time: ignore[vulnerable]\n")
        mock_post.assert_called()
        self.assert_no_warnings_logged()
        self.assert_ignored_vulnerability_logged("django", Location(requirements_txt, 1), "ignore[vulnerable]")

    def test_ignore_vulnerable_advisory_marker_silences_that_advisory(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an `ignore[vulnerable=ID]` marker holds back the warning about the advisory it names."""
        directive = f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, f"django==3.2.0  # update-time: {directive}\n")
        mock_get.side_effect = self.pypi("3.2.0", "3.3.0", bump=True)
        with osv(DJANGO_ADVISORY):
            update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with(f"django==3.3.0  # update-time: {directive}\n")
        self.assert_no_warnings_logged()
        self.assert_ignored_vulnerability_logged("django", Location(requirements_txt, 1), directive)

    def test_ignore_vulnerable_advisory_marker_accepts_any_identifier_of_the_defect(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that the marker silences the warning when it names any identifier the defect is known by."""
        cve, pysec, bit = "CVE-2021-1111", "PYSEC-2021-109", "BIT-django-2021-1111"
        far_cve = "CVE-2021-4444"  # Named by the tied-in record alone, so only the merge brings it to the defect
        reported = DJANGO_ADVISORY | {"aliases": [cve]}
        other_record = osv_advisory(pysec, DJANGO_VULNERABILITY.summary, aliases=[cve])
        tied_in = osv_advisory(bit, DJANGO_VULNERABILITY.summary, aliases=[far_cve])
        tying_record = osv_advisory(pysec, DJANGO_VULNERABILITY.summary, aliases=[cve, bit])
        cases = {
            "an alias of the reported record": (cve, (reported, other_record)),
            "another record's own id": (pysec, (reported, other_record)),
            "an alias tied in only through a third record": (far_cve, (reported, tied_in, tying_record)),
        }
        for case, (identifier, records) in cases.items():
            with self.subTest(case=case):
                directive = f"ignore[vulnerable={identifier}]"
                requirements_txt = self.discovered_requirements_txt(
                    mock_rglob, f"django==3.2.0  # update-time: {directive}\n"
                )
                mock_get.side_effect = self.pypi("3.2.0")
                with osv(*records):
                    update_requirements_txts()
                self.assert_no_warnings_logged()
                self.assert_ignored_vulnerability_logged("django", Location(requirements_txt, 1), directive)

    def test_ignore_vulnerable_advisory_marker_still_warns_about_another_advisory(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that a marker naming one advisory leaves the pin's other advisories warned about."""
        directive = f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, f"django==3.2.0  # update-time: {directive}\n")
        mock_get.side_effect = self.pypi("3.2.0")
        with osv(DJANGO_ADVISORY, _OTHER_ADVISORY):
            update_requirements_txts()
        self.assert_vulnerable_dependency_logged("django", "3.2.0", _OTHER_VULNERABILITY, Location(requirements_txt, 1))
        self.assert_ignored_vulnerability_logged("django", Location(requirements_txt, 1), directive)

    def test_ignore_vulnerable_advisory_marker_is_redundant_when_no_defect_answers_to_it(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that a marker naming an advisory none of the version's defects answers to is reported as redundant."""
        directive = f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, f"django==3.2.0  # update-time: {directive}\n")
        mock_get.side_effect = self.pypi("3.2.0")
        with osv(_OTHER_ADVISORY):
            update_requirements_txts()
        self.assert_redundant_vulnerable_advisory_logged("django", "3.2.0", Location(requirements_txt, 1), directive)
        self.assert_vulnerable_dependency_logged(
            "django", "3.2.0", _OTHER_VULNERABILITY, Location(requirements_txt, 1), among_others=True
        )

    def test_globally_ignored_advisory_silences_the_warning(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an advisory ignored run-wide is not warned about, and that the hold-back names the option."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "django==3.2.0\n")
        mock_get.side_effect = self.pypi("3.2.0")
        with osv(DJANGO_ADVISORY), patch_environ({IGNORE_VULNERABILITIES.name: DJANGO_VULNERABILITY.advisory}):
            update_requirements_txts()
        self.assert_no_warnings_logged()
        self.assert_globally_ignored_vulnerability_logged(
            "django", Location(requirements_txt, 1), DJANGO_VULNERABILITY.advisory
        )

    def test_a_marker_is_reported_where_the_option_names_the_same_advisory(self, mock_rglob: Mock, mock_get: Mock):
        """Test that where the marker and the option both name the advisory, the marker's hold-back is reported."""
        directive = f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, f"django==3.2.0  # update-time: {directive}\n")
        mock_get.side_effect = self.pypi("3.2.0")
        with osv(DJANGO_ADVISORY), patch_environ({IGNORE_VULNERABILITIES.name: DJANGO_VULNERABILITY.advisory}):
            update_requirements_txts()
        self.assert_no_warnings_logged()
        self.assert_ignored_vulnerability_logged("django", Location(requirements_txt, 1), directive)
        self.assert_no_globally_ignored_vulnerability_logged()

    def test_recent_dependency_not_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin whose newest release is recent is not warned about as stale."""
        self.discovered_requirements_txt(mock_rglob, "humanize==4.15.0\n")
        recent = datetime.now(UTC).isoformat()
        mock_get.side_effect = self.stale_pypi("4.15.0", upload_time=recent)
        update_requirements_txts()
        self.assert_no_warnings_logged()

    def test_staleness_disabled(self, mock_rglob: Mock, mock_get: Mock):
        """Test that no staleness warning is emitted when the check is disabled with --stale-after 0."""
        self.discovered_requirements_txt(mock_rglob, "humanize==4.15.0\n")
        mock_get.side_effect = self.stale_pypi("4.15.0")
        with staleness_disabled:
            update_requirements_txts()
        self.assert_no_warnings_logged()

    def test_change(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an exact pin is bumped to the latest version."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "flask==1.0\n")
        mock_get.side_effect = self.pypi("1.0", "1.1", bump=True)
        update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with("flask==1.1\n")
        self.assert_path_logged(requirements_txt)
        self.assert_new_version_logged("flask", _PUBLISHED, Location(requirements_txt, 1))
        self.assert_no_warnings_logged()

    def test_preserves_extras_marker_and_comment(self, mock_rglob: Mock, mock_get: Mock):
        """Test that extras, environment markers and inline comments are preserved when bumping the version."""
        requirements_txt = self.discovered_requirements_txt(
            mock_rglob, 'flask[async]==1.0 ; python_version < "3.12"  # keep\n'
        )
        mock_get.side_effect = self.pypi("1.0", "1.1", bump=True)
        update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with('flask[async]==1.1 ; python_version < "3.12"  # keep\n')
        self.assert_path_logged(requirements_txt)
        self.assert_new_version_logged("flask", _PUBLISHED, Location(requirements_txt, 1))
        self.assert_no_warnings_logged()

    def test_spaces_around_equals_preserved(self, mock_rglob: Mock, mock_get: Mock):
        """Test that spaces around `==` and the aligned inline comment are preserved when bumping the version."""
        requirements_txt = self.discovered_requirements_txt(
            mock_rglob, "certifi == 2020.4.5.1          # used by requests\n"
        )
        mock_get.side_effect = self.pypi("2020.4.5.1", "2020.4.5.2", bump=True)
        update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with("certifi == 2020.4.5.2          # used by requests\n")
        self.assert_path_logged(requirements_txt)
        self.assert_new_version_logged(
            "certifi", "2020.4.5.2, published: 2020-01-01 00:00", Location(requirements_txt, 1)
        )
        self.assert_no_warnings_logged()

    def test_loose_specifiers_untouched(self, mock_rglob: Mock, mock_get: Mock):
        """Test that non-exact specifiers, options, URLs and comments are left untouched and not queried."""
        contents = (
            "flask>=1.0\ndjango~=2.0\nrequests\n-e .\n--index-url https://example.com\ngit+https://x/y.git\n# c\n"
        )
        requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_path_logged(requirements_txt)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_held_back_by_cooldown(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a newer version published within the cooldown period is not picked up."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "flask==1.0\n")
        recent = datetime.now(UTC).isoformat()
        mock_get.side_effect = self.pypi("1.0", "1.1", bump=True, upload_time=recent)
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        self.assert_path_logged(requirements_txt)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_hash_pinned_file_skipped(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a hash-pinned (fully locked) requirements file is skipped entirely."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "flask==1.0 \\\n    --hash=sha256:abc\n")
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_skipped_logged(requirements_txt, "compiled or hash-pinned requirements file")
        self.assert_no_path_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_compiled_header_skipped(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pip-compile/uv generated requirements file is skipped entirely."""
        requirements_txt = self.discovered_requirements_txt(
            mock_rglob, "# This file is autogenerated by pip-compile\nflask==1.0\n"
        )
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_skipped_logged(requirements_txt, "compiled or hash-pinned requirements file")
        self.assert_no_path_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_sibling_in_file_skipped(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a requirements file with a sibling .in source is skipped entirely."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "flask==1.0\n", sibling_in=True)
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_skipped_logged(requirements_txt, "compiled or hash-pinned requirements file")
        self.assert_no_path_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


class RequirementsGlobPatternsTest(unittest.TestCase):
    """Unit tests for which paths the requirements glob patterns match.

    `glob` uses `rglob`, which matches each path against the pattern with `PurePath.full_match`, so matching the
    patterns directly mirrors discovery without touching the file system.
    """

    def matches(self, path: str) -> bool:
        """Return whether any requirements glob pattern matches the path, case-sensitively (as `glob` matches)."""
        return any(PurePath(path).full_match(pattern, case_sensitive=True) for pattern in _GLOB_PATTERNS)

    @patch("update_time.updaters.update_requirements_txt.glob")
    def test_the_updater_looks_for_these_patterns(self, mock_glob: Mock):
        """Test that the updater discovers files with the very patterns the tests below match paths against."""
        mock_glob.return_value = []
        update_requirements_txts()
        mock_glob.assert_called_once_with(*_GLOB_PATTERNS, case_sensitive=True)

    def test_recognized_flat_names(self):
        """Test that the flat requirements naming conventions match."""
        for name in ("requirements.txt", "requirements-dev.txt", "dev-requirements.txt"):
            with self.subTest(name=name):
                self.assertTrue(self.matches(name))

    def test_nested_requirements_directory(self):
        """Test that a requirements file in a nested `requirements/` directory matches."""
        self.assertTrue(self.matches("requirements/base.txt"))

    def test_unrelated_txt_files_ignored(self):
        """Test that unrelated `.txt` files, a `requirements.in` source, and names without a hyphen do not match."""
        for name in ("notes.txt", "constraints.txt", "requirements.in", "requirementsfoo.txt", "foorequirements.txt"):
            with self.subTest(name=name):
                self.assertFalse(self.matches(name))

    def test_case_sensitive(self):
        """Test that matching is case-sensitive, so a differently-cased name does not match."""
        self.assertFalse(self.matches("Requirements.txt"))
