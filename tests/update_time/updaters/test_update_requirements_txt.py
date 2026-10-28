"""Unit tests for the requirements.txt update script."""

import contextlib
from unittest.mock import ANY, Mock, patch

from update_time.domain.vulnerability import IGNORE_VULNERABILITIES, VULNERABILITY_LEVEL
from update_time.io.log import Logger
from update_time.manifests import requirements_txt as requirements_txt_manifest
from update_time.markers import directive as directive_module
from update_time.markers.directive import Reason
from update_time.markers.marker import Marker, Scope, Threshold
from update_time.primitives.location import Location
from update_time.references import vulnerability as vulnerability_module
from update_time.updaters.update_requirements_txt import update_requirements_txts

from tests.helpers import patch_environ
from tests.mutation import Mutation, kills
from tests.update_time.helpers import (
    LoggingTestCase,
    archival_check_disabled,
    message_of,
    project_checks_disabled,
    published_version,
    staleness_disabled,
)
from tests.update_time.manifests.helpers import requirements_file
from tests.update_time.sources.helpers import PYPI_OLD_UPLOAD, osv_advisory, pypi_index, pypi_release, yanked_file
from tests.update_time.updaters.fixtures import (
    DJANGO_ADVISORY,
    DJANGO_VULNERABILITY,
    OTHER_DJANGO_ADVISORY,
    OTHER_DJANGO_VULNERABILITY,
    PYPI_PAST_COOLDOWN_UPLOAD,
    PYPI_RECENT_UPLOAD,
)
from tests.update_time.updaters.helpers import (
    OSV_BATCH_URL,
    assert_osv_asked_about,
    dated_pypi_index,
    no_vulnerabilities,
    osv,
    osv_queries,
    unreachable_osv,
    vulnerability_check_disabled,
)

_PUBLISHED = published_version("1.1", PYPI_OLD_UPLOAD)

# The endpoint a package's versions and release dates are read from; `queried_packages` reads its name back off it.
_PYPI_INDEX_URL = "https://pypi.org/simple/"


@no_vulnerabilities
@patch("requests.Session.get")
@patch("pathlib.Path.rglob")
class UpdateRequirementsTxtTest(LoggingTestCase):
    """Unit tests for the update requirements.txt function."""

    def discovered_requirements_txt(self, rglob: Mock, contents: str, *, sibling_in: bool = False) -> Mock:
        """Return the single requirements file the scan discovers, holding the contents."""
        requirements_txt = requirements_file(contents, sibling_in=sibling_in)
        rglob.return_value = [requirements_txt]
        return requirements_txt

    def pypi(self, *versions: str, bump: bool = False, upload_time: str = PYPI_OLD_UPLOAD) -> list:
        """Return mock PyPI responses: the Index API versions, plus per-version metadata when a bump is expected."""
        responses = [pypi_index(*versions)]
        if bump:
            responses.append(pypi_release(upload_time))
        return responses

    def queried_packages(self, mock_get: Mock) -> list[str]:
        """Return the packages whose PyPI index was fetched, in the order they were fetched."""
        urls = [call.args[0] for call in mock_get.call_args_list if call.args[0].startswith(_PYPI_INDEX_URL)]
        return [url.removeprefix(_PYPI_INDEX_URL).removesuffix("/") for url in urls]

    def stale_pypi(self, *versions: str, upload_time: str = PYPI_OLD_UPLOAD, archived: bool = False) -> list[Mock]:
        """Return a mock Index API response listing the versions and a distribution file with the given upload time."""
        return [dated_pypi_index(*versions, upload_time=upload_time, archived=archived)]

    def loose_requirement(self, rglob: Mock, marker: str = "") -> Mock:
        """Return the discovered file declaring `humanize>=4` with the marker, a requirement naming a range."""
        comment = f"  # update-time: {marker}" if marker else ""
        return self.discovered_requirements_txt(rglob, f"humanize>=4{comment}\n")

    def django_pin(self, rglob: Mock, get: Mock, marker: str = "") -> Mock:
        """Return the discovered file pinning `django==3.2.0` with the marker, PyPI listing that version as newest."""
        comment = f"  # update-time: {marker}" if marker else ""
        requirements_txt = self.discovered_requirements_txt(rglob, f"django==3.2.0{comment}\n")
        get.side_effect = self.pypi("3.2.0")
        return requirements_txt

    def test_no_change(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin already on the latest version is left unchanged."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "flask==1.0\n")
        mock_get.side_effect = self.pypi("1.0")
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        self.assert_path_logged(requirements_txt)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_a_pin_whose_metadata_reports_no_project_urls(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin whose metadata reports the project URLs as null is updated, and so is the pin beside it."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "flask==1.0\nhumanize==4.14.0\n")
        mock_get.side_effect = [
            pypi_index("1.0", "1.1"),
            pypi_release(project_urls=None),
            *self.pypi("4.14.0", "4.15.0", bump=True),
        ]
        update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with("flask==1.1\nhumanize==4.15.0\n")

    def test_cooldown_marker_is_not_reported_as_redundant(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a `cooldown` marker on a pin holds something back, since PyPI dates its versions."""
        contents = "flask==1.0  # update-time: set[cooldown=30]\n"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
        mock_get.side_effect = self.pypi("1.0")
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        self.assert_no_warnings_logged()

    def test_ignore_hash_drift_is_not_reported_as_redundant(self, mock_rglob: Mock, mock_get: Mock):
        """Test that `ignore[hash-drift]` on a pin is not reported as redundant."""
        requirements_txt = self.discovered_requirements_txt(
            mock_rglob, "flask==1.0  # update-time: ignore[hash-drift]\n"
        )
        mock_get.side_effect = self.pypi("1.0")
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        self.assertEqual(self.queried_packages(mock_get), ["flask"])
        self.assert_no_warnings_logged()

    def test_hash_drift_opt_in_is_reported_as_redundant(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an `allow[hash-drift]` on a pin is reported for its own reason, whether or not it is held back."""
        for marker in ("allow[hash-drift]", "ignore[update] allow[hash-drift]"):
            with self.subTest(marker=marker):
                contents = f"flask==1.0  # update-time: {marker}\n"
                requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
                mock_get.side_effect = self.pypi("1.0")
                update_requirements_txts()
                requirements_txt.write_text.assert_not_called()
                self.assert_redundant_directive_logged(
                    Reason.NO_HASH_PIN, "flask", Location(requirements_txt, 1), "allow[hash-drift]"
                )

    def test_stale_dependency_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin whose newest release is old is warned about, without being changed."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "humanize==4.15.0\n")
        mock_get.side_effect = self.stale_pypi("4.15.0")  # No newer version; newest release is old.
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        self.assert_stale_dependency_logged("humanize", "4.15.0", Location(requirements_txt, 1))

    def test_stale_loose_requirement_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a requirement declared without an exact pin is warned about when its newest release is old.

        Each case declares the same package with a specifier that pins no exact version: another of the PEP 440
        operators, or a `==` naming a range. The index lists the releases out of order, so the warning names the
        newest rather than the last.
        """
        for specifier in (">=4", ">4", "<=5", "<5", "!=4.14.0", "~=4.15", "===4.15.0", "==4.15.*"):
            with self.subTest(specifier=specifier):
                requirements_txt = self.discovered_requirements_txt(mock_rglob, f"humanize{specifier}\n")
                mock_get.side_effect = self.stale_pypi("4.15.0", "4.14.0")  # The package's newest release is old.
                update_requirements_txts()
                requirements_txt.write_text.assert_not_called()
                self.assert_stale_dependency_logged("humanize", "4.15.0", Location(requirements_txt, 1))

    @kills(
        Mutation(
            requirements_txt_manifest,
            r'_VERSION_ENDS = r"(?![A-Za-z0-9._!+*,-])(?!\s*,)"',
            r'_VERSION_ENDS = r"(?![A-Za-z0-9._!+*,-])"',
            "a version a space and a comma follow still ends its specifier, so a requirement combining two of them "
            "is rewritten into one no version satisfies",
        )
    )
    def test_a_requirement_combining_specifiers_is_not_updated(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a requirement combining `==` with another specifier is checked but never rewritten.

        PEP 508 allows space around the comma separating the specifiers, so each case spells it another way.
        """
        for requirement in ("humanize==4.15.0,!=4.15.1", "humanize==4.15.0 ,!=4.15.1"):
            with self.subTest(requirement=requirement):
                requirements_txt = self.discovered_requirements_txt(mock_rglob, f"{requirement}\n")
                mock_get.side_effect = [*self.stale_pypi("4.15.0", "4.16.0"), pypi_release(PYPI_OLD_UPLOAD)]
                update_requirements_txts()
                requirements_txt.write_text.assert_not_called()
                self.assert_stale_dependency_logged("humanize", "4.16.0", Location(requirements_txt, 1))

    def test_two_spellings_of_one_package_cost_one_request(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a package a file spells two ways is fetched once, under the name PyPI spells it.

        Both lines are checked and reported under the name their own line carries.
        """
        contents = "typing_extensions==4.0.0\nTyping-Extensions>=4\n"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
        # Answer any package, so a second fetch is caught by the assertion below rather than by running out.
        mock_get.return_value = self.stale_pypi("4.0.0")[0]
        update_requirements_txts()
        self.assertEqual(self.queried_packages(mock_get), ["typing-extensions"])
        stale = {"version": "4.0.0", "days": ANY, "threshold": ANY}
        self.assert_logged_among_others(
            message_of(Logger.report_staleness, "days"),
            dependency="typing_extensions",
            location=Location(requirements_txt, 1),
            **stale,
        )
        self.assert_logged_among_others(
            message_of(Logger.report_staleness, "days"),
            dependency="Typing-Extensions",
            location=Location(requirements_txt, 2),
            **stale,
        )

    def test_a_requirement_whose_newest_release_is_recent_is_not_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a requirement whose newest release is recent is not warned about, however it is spelled."""
        for case, requirement in {"an exact pin": "humanize==4.15.0", "a range": "humanize>=4"}.items():
            with self.subTest(case=case):
                mock_get.reset_mock()
                self.discovered_requirements_txt(mock_rglob, f"{requirement}\n")
                mock_get.side_effect = self.stale_pypi("4.15.0", upload_time=PYPI_RECENT_UPLOAD)
                update_requirements_txts()
                self.assertEqual(self.queried_packages(mock_get), ["humanize"])
                self.assert_no_warnings_logged()

    def test_staleness_disabled_still_looks_up_a_loose_requirement(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a loose requirement is still looked up when the staleness check is switched off."""
        requirements_txt = self.loose_requirement(mock_rglob)
        mock_get.side_effect = self.stale_pypi("4.15.0", archived=True)
        with staleness_disabled:
            update_requirements_txts()
        self.assert_archived_dependency_logged("humanize", Location(requirements_txt, 1))

    def test_a_stale_and_archived_requirement_is_reported_as_both(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a requirement whose project is archived and whose newest release is old gets both warnings."""
        requirements_txt = self.loose_requirement(mock_rglob)
        mock_get.side_effect = self.stale_pypi("4.15.0", archived=True)
        update_requirements_txts()
        location = Location(requirements_txt, 1)
        self.assert_stale_dependency_logged("humanize", "4.15.0", location, among_others=True)
        self.assert_archived_dependency_logged("humanize", location, among_others=True)

    def test_the_markers_threshold_survives_the_check_being_switched_off(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a loose requirement with a threshold of its own is looked up although the check is switched off.

        The newest release is 100 days old, so it is stale against the marker's 90, which the `--stale-after 0` in
        force run-wide does not override.
        """
        requirements_txt = self.loose_requirement(mock_rglob, "set[stale-after=90]")
        published = PYPI_PAST_COOLDOWN_UPLOAD
        mock_get.side_effect = self.stale_pypi("4.15.0", upload_time=published)
        with staleness_disabled:
            update_requirements_txts()
        self.assertEqual(self.queried_packages(mock_get), ["humanize"])
        self.assert_stale_dependency_logged("humanize", "4.15.0", Location(requirements_txt, 1))

    def test_stale_requirement_without_a_specifier_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a requirement declaring no version at all is warned about when its newest release is old."""
        for case, requirement in {
            "no specifier": "humanize",
            "extras": "humanize[fast]",
            "an environment marker": 'humanize ; python_version < "3.12"',
            "a comment": "humanize  # keep",
        }.items():
            with self.subTest(case=case):
                requirements_txt = self.discovered_requirements_txt(mock_rglob, f"{requirement}\n")
                mock_get.side_effect = self.stale_pypi("4.15.0")
                update_requirements_txts()
                requirements_txt.write_text.assert_not_called()
                self.assert_stale_dependency_logged("humanize", "4.15.0", Location(requirements_txt, 1))

    def test_archived_loose_requirement_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a requirement pinning no exact version is warned about when PyPI declares its project archived."""
        requirements_txt = self.loose_requirement(mock_rglob)
        mock_get.side_effect = [pypi_index("4.15.0", archived=True)]
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        self.assert_archived_dependency_logged("humanize", Location(requirements_txt, 1))

    def test_archival_check_disabled(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an archived project is not warned about when the check is switched off run-wide."""
        self.loose_requirement(mock_rglob)
        mock_get.side_effect = [pypi_index("4.15.0", archived=True)]
        with archival_check_disabled:
            update_requirements_txts()
        self.assertEqual(self.queried_packages(mock_get), ["humanize"])  # the project was examined, just not reported
        self.assert_no_warnings_logged()

    def test_ignore_archived_is_redundant_with_the_archival_check_switched_off(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an `ignore[archived]` is redundant in a run passing `--ignore-archived`, looked up or not."""
        cases = {
            "a pin": ("humanize==4.15.0", archival_check_disabled),
            "a range": ("humanize>=4", archival_check_disabled),
            "a range in a run switching both checks off": ("humanize>=4", project_checks_disabled),
        }
        for case, (requirement, run) in cases.items():
            with self.subTest(case=case):
                contents = f"{requirement}  # update-time: ignore[archived]\n"
                requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
                mock_get.side_effect = self.stale_pypi("4.15.0", upload_time=PYPI_RECENT_UPLOAD)
                with run:
                    update_requirements_txts()
                self.assert_redundant_directive_logged(
                    Reason.ARCHIVAL_CHECK_OFF, "humanize", Location(requirements_txt, 1), "ignore[archived]"
                )

    def test_a_scope_needing_a_version_is_redundant_on_a_loose_requirement(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a scope whose check needs a version reports that the requirement pins no version."""
        vulnerable = Reason.NO_VERSION_TO_CHECK_FOR_A_VULNERABILITY
        for directive, reason in {
            "ignore[yanked]": Reason.NO_VERSION_TO_CHECK_FOR_A_YANK,
            "set[vulnerability-level=none]": vulnerable,
            f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]": vulnerable,
            "set[vulnerability-level=high]": vulnerable,
        }.items():
            with self.subTest(directive=directive):
                requirements_txt = self.loose_requirement(mock_rglob, directive)
                mock_get.side_effect = self.pypi("4.15.0")  # Undated, so the staleness warning is not given.
                update_requirements_txts()
                self.assert_logged(
                    message_of(Logger.redundant_directive),
                    reason=reason,
                    directive=directive,
                    dependency="humanize",
                    location=Location(requirements_txt, 1),
                )

    def test_a_marker_on_a_loose_requirement_is_logged_as_recognised(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a marker on a loose requirement is reported as recognised, whatever it holds back."""
        for directive, marker in {
            "set[stale-after=0]": Marker(stale=Threshold(value=0)),
            "ignore[update]": Marker(ignored_scopes=Scope.UPDATE),
        }.items():
            with self.subTest(directive=directive):
                requirements_txt = self.loose_requirement(mock_rglob, directive)
                mock_get.side_effect = self.stale_pypi("4.15.0")
                update_requirements_txts()
                self.assert_recognised_marker_logged("humanize", Location(requirements_txt, 1), marker)

    def test_a_directive_steering_the_update_is_redundant_on_a_loose_requirement(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that a cooldown or a bound on a requirement naming a range is reported.

        A reference carrying both an `ignore[update]` and a bound is reported under the `ignore[update]`, which
        holds back the more.
        """
        markers = {
            "set[cooldown=30]": "set[cooldown=30]",
            "ignore[update]": "ignore[update]",
            "allow[update<5]": "allow[update<5]",
            "ignore[minor-update]": "ignore[minor-update]",
            "ignore[update] allow[update<5]": "ignore[update]",
        }
        for marker, directive in markers.items():
            with self.subTest(marker=marker):
                requirements_txt = self.loose_requirement(mock_rglob, marker)
                mock_get.side_effect = self.pypi("4.15.0")  # Undated, so the staleness warning is not given.
                update_requirements_txts()
                self.assert_logged(
                    message_of(Logger.redundant_directive),
                    reason=Reason.NO_VERSION_TO_UPDATE,
                    directive=directive,
                    dependency="humanize",
                    location=Location(requirements_txt, 1),
                )

    def test_hash_drift_opt_in_is_redundant_on_a_loose_requirement(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an `allow[hash-drift]` on a requirement naming a range is reported as redundant."""
        requirements_txt = self.loose_requirement(mock_rglob, "allow[hash-drift]")
        mock_get.side_effect = self.pypi("4.15.0")  # Undated, so the staleness warning is not given.
        update_requirements_txts()
        self.assert_redundant_directive_logged(
            Reason.NO_HASH_PIN, "humanize", Location(requirements_txt, 1), "allow[hash-drift]"
        )

    def test_hash_drift_opt_in_beside_an_ignore_update_that_holds_nothing_back(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an `allow[hash-drift]` on a range keeps its own reason beside an `ignore[update]`."""
        requirements_txt = self.loose_requirement(mock_rglob, "ignore[update] allow[hash-drift]")
        mock_get.side_effect = self.pypi("4.15.0")  # Undated, so the staleness warning is not given.
        update_requirements_txts()
        location = Location(requirements_txt, 1)
        redundant = message_of(Logger.redundant_directive)
        fields = {"dependency": "humanize", "location": location}
        self.assert_logged_among_others(redundant, directive="allow[hash-drift]", reason=Reason.NO_HASH_PIN, **fields)
        self.assert_logged_among_others(
            redundant, directive="ignore[update]", reason=Reason.NO_VERSION_TO_UPDATE, **fields
        )
        self.assert_none_logged(redundant, "update held back", reason=Reason.DRIFT_NEVER_ADOPTED)

    def test_floating_pin_opt_in_on_a_loose_requirement_is_reported_as_redundant(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that an `allow[floating-pin]` on a range is reported, with or without an `ignore[update]` beside it."""
        reason = Reason.RANGE_NEVER_PINNED
        for marker in ("allow[floating-pin]", "ignore[update] allow[floating-pin]"):
            with self.subTest(marker=marker):
                requirements_txt = self.loose_requirement(mock_rglob, marker)
                mock_get.side_effect = self.pypi("4.15.0")  # Undated, so the staleness warning is not given.
                update_requirements_txts()
                redundant = message_of(Logger.redundant_directive)
                fields = {"dependency": "humanize", "location": Location(requirements_txt, 1)}
                self.assert_logged_among_others(redundant, directive="allow[floating-pin]", reason=reason, **fields)
                self.assertEqual(len(self.records_of(redundant, directive="allow[floating-pin]")), 1)

    def test_a_loose_requirement_the_index_lists_no_version_for_is_not_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a requirement whose package the index lists no version for is not warned about.

        That is what PyPI answers for a name it does not know, a misspelled one included.
        """
        self.loose_requirement(mock_rglob)
        mock_get.side_effect = [pypi_index()]
        update_requirements_txts()
        self.assertEqual(self.queried_packages(mock_get), ["humanize"])
        self.assert_no_warnings_logged()

    def test_indented_requirements_are_recognised(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a requirement indented with a space or a tab is read as one, its indentation kept."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "  flask==1.0\n\thumanize>=4\n")
        mock_get.side_effect = [*self.pypi("1.0", "1.1", bump=True), *self.stale_pypi("4.15.0")]
        update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with("  flask==1.1\n\thumanize>=4\n")
        self.assert_stale_dependency_logged("humanize", "4.15.0", Location(requirements_txt, 2))

    def test_a_loose_requirement_on_a_prerelease_is_warned_about(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a requirement whose package published prereleases only is warned about, naming the newest."""
        requirements_txt = self.loose_requirement(mock_rglob)
        mock_get.side_effect = self.stale_pypi("5.0.0rc1")
        update_requirements_txts()
        self.assert_stale_dependency_logged("humanize", "5.0.0rc1", Location(requirements_txt, 1))

    def test_a_threshold_keeping_quiet_about_an_old_release_is_logged_as_silencing_it(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that a threshold keeping quiet about a release the run warns about is logged as silencing it."""
        for threshold in (0, 5000):
            directive = f"set[stale-after={threshold}]"
            with self.subTest(directive=directive):
                requirements_txt = self.loose_requirement(mock_rglob, directive)
                mock_get.side_effect = self.stale_pypi("4.15.0")  # The package's newest release is from 2020.
                update_requirements_txts()
                self.assert_no_warnings_logged()
                self.assert_ignored_staleness_logged("humanize", Location(requirements_txt, 1), directive)

    @kills(
        Mutation(
            directive_module,
            "Reason.NO_ARCHIVAL_SIGNAL,",
            "Reason.NO_ARCHIVAL_SIGNAL,\n        Reason.NO_VERSION_TO_CHECK_FOR_A_YANK,",
            "ignore[archived] on a loose requirement is reported as redundant, though archival needs the package's "
            "name alone",
        )
    )
    def test_ignore_archived_marker_silences_a_loose_requirement(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an `ignore[archived]` marker on a loose requirement's line silences its archival warning."""
        requirements_txt = self.loose_requirement(mock_rglob, "ignore[archived]")
        mock_get.side_effect = [pypi_index("4.15.0", archived=True)]
        update_requirements_txts()
        self.assert_no_warnings_logged()
        self.assert_ignored_archival_logged("humanize", Location(requirements_txt, 1), "ignore[archived]")

    def test_the_markers_threshold_is_used_for_a_loose_requirement(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a loose requirement carrying its own staleness threshold is judged by that one, not the global.

        The newest release is 100 days old, which is stale against the marker's 90 and not against the global 365,
        so the warning is given only when the marker's threshold is the one applied.
        """
        requirements_txt = self.loose_requirement(mock_rglob, "set[stale-after=90]")
        published = PYPI_PAST_COOLDOWN_UPLOAD
        mock_get.side_effect = self.stale_pypi("4.15.0", upload_time=published)
        update_requirements_txts()
        self.assert_stale_dependency_logged("humanize", "4.15.0", Location(requirements_txt, 1))

    def test_a_threshold_judging_a_recent_release_as_the_run_does_is_redundant(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a threshold judging a recent release as the run does is redundant, a threshold of 0 included."""
        runs: dict[str, tuple[contextlib.AbstractContextManager[object], dict[str, int]]] = {
            "the default run": (contextlib.nullcontext(), {"default_threshold": 365}),
            "a run switching the check off": (staleness_disabled, {"run_threshold": 0}),
            "a run switching both checks off": (project_checks_disabled, {"run_threshold": 0}),
        }
        for threshold in (90, 0):
            directive = f"set[stale-after={threshold}]"
            requirements_txt = self.loose_requirement(mock_rglob, directive)
            for case, (run, run_fields) in runs.items():
                with self.subTest(threshold=threshold, case=case):
                    mock_get.side_effect = self.stale_pypi("4.15.0", upload_time=PYPI_RECENT_UPLOAD)
                    with run:
                        update_requirements_txts()
                    self.assert_logged(
                        message_of(Logger.redundant_stale_threshold, *run_fields),
                        directive=directive,
                        dependency="humanize",
                        location=Location(requirements_txt, 1),
                        version="4.15.0",
                        days=ANY,
                        threshold=threshold,
                        **run_fields,
                    )
                    self.assert_no_ignored_staleness_logged()

    def test_an_invalid_item_on_a_loose_requirement_is_reported(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an unreadable item is reported and silences nothing, so the staleness check still runs."""
        requirements_txt = self.loose_requirement(mock_rglob, "ignore[stlae]")
        mock_get.side_effect = self.stale_pypi("4.15.0")
        update_requirements_txts()
        self.assert_invalid_bracket_item_logged("humanize", Location(requirements_txt, 1), "stlae", among_others=True)
        self.assert_stale_dependency_logged("humanize", "4.15.0", Location(requirements_txt, 1), among_others=True)

    def test_a_readable_item_beside_an_unreadable_one_still_acts(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a `set[stale-after=0]` sharing a bracket with an unreadable item still silences the warning."""
        requirements_txt = self.loose_requirement(mock_rglob, "set[stale-after=0, stlae]")
        mock_get.side_effect = self.stale_pypi("4.15.0")
        update_requirements_txts()
        # The unreadable item is the run's only warning, so the staleness warning was silenced:
        self.assert_invalid_bracket_item_logged("humanize", Location(requirements_txt, 1), "stlae")
        self.assert_no_staleness_warning_logged("humanize", Location(requirements_txt, 1))

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
        """Test that a pin frozen on a yanked version by `ignore[update]` is still warned about."""
        contents = "humanize==4.15.0  # update-time: ignore[update]\n"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
        yanked = [yanked_file("humanize-4.15.0.tar.gz", reason="broke Python 3.10")]
        mock_get.side_effect = [pypi_index("4.15.0", "4.16.0", files=yanked), pypi_release()]
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()  # the marker keeps the pin on the yanked version
        self.assert_yanked_dependency_logged("humanize", "4.15.0", Location(requirements_txt, 1))

    def test_ignore_update_marker_still_warns_about_an_archived_project(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin frozen by `ignore[update]` is still warned about when its project is archived."""
        contents = "humanize==4.15.0  # update-time: ignore[update]\n"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
        mock_get.side_effect = [pypi_index("4.15.0", "4.16.0", archived=True)]
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()  # the marker keeps the pin where it is
        self.assert_archived_dependency_logged("humanize", Location(requirements_txt, 1))

    def test_ignore_yanked_marker_silences_the_warning(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an `ignore[yanked]` marker on the pin's line silences the yank warning."""
        contents = "humanize==4.15.0  # update-time: ignore[yanked]\n"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
        yanked = [yanked_file("humanize-4.15.0.tar.gz", reason="broke Python 3.10")]
        mock_get.side_effect = [pypi_index("4.15.0", files=yanked)]
        update_requirements_txts()
        self.assert_no_warnings_logged()
        self.assert_ignored_yank_logged("humanize", Location(requirements_txt, 1), "ignore[yanked]")

    def test_ignore_yanked_marker_on_a_pin_moved_off_its_yanked_release_is_redundant(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that an `ignore[yanked]` on a pin the run moves off its yanked release is reported as redundant."""
        contents = "humanize==4.15.0  # update-time: ignore[yanked]\n"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
        yanked = [yanked_file("humanize-4.15.0.tar.gz", reason="broke Python 3.10")]
        mock_get.side_effect = [pypi_index("4.15.0", "4.16.0", files=yanked), pypi_release()]
        update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with("humanize==4.16.0  # update-time: ignore[yanked]\n")
        self.assert_logged(
            message_of(Logger.report_yank, "directive", "version"),
            directive="ignore[yanked]",
            dependency="humanize",
            location=Location(requirements_txt, 1),
            version="4.16.0",
        )

    def test_ignore_archived_marker_silences_the_warning(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an `ignore[archived]` marker on the pin's line silences the archival warning."""
        contents = "humanize==4.15.0  # update-time: ignore[archived]\n"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
        mock_get.side_effect = [pypi_index("4.15.0", archived=True)]
        update_requirements_txts()
        self.assert_no_warnings_logged()
        self.assert_ignored_archival_logged("humanize", Location(requirements_txt, 1), "ignore[archived]")

    def test_archived_dependency_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin on a project PyPI declares archived is warned about, and updated all the same."""
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "humanize==4.14.0\n")
        mock_get.side_effect = [pypi_index("4.14.0", "4.15.0", archived="superseded by humanize2"), pypi_release()]
        update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with("humanize==4.15.0\n")
        reason = ' ("superseded by humanize2")'
        self.assert_archived_dependency_logged("humanize", Location(requirements_txt, 1), reason)

    def test_vulnerable_dependency_warned(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin left on a version OSV reports an advisory for is warned about, without being changed."""
        requirements_txt = self.django_pin(mock_rglob, mock_get)  # No newer version; the pin's release is vulnerable.
        with osv(DJANGO_ADVISORY):
            update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(requirements_txt, 1))

    def test_vulnerability_check_disabled(self, mock_rglob: Mock, mock_get: Mock):
        """Test that no vulnerability is reported, and OSV is not asked at all, when the check is switched off."""
        requirements_txt = self.django_pin(mock_rglob, mock_get)
        with osv(DJANGO_ADVISORY) as mock_post, vulnerability_check_disabled:
            update_requirements_txts()
        mock_post.assert_not_called()
        self.assert_path_logged(requirements_txt)  # the file was checked for updates, just not for vulnerabilities
        self.assert_no_warnings_logged()

    def test_vulnerability_below_the_risk_level_in_force(self, mock_rglob: Mock, mock_get: Mock):
        """Test that of the advisories affecting a pin, only those at or above the level in force are warned about."""
        requirements_txt = self.django_pin(mock_rglob, mock_get)
        with osv(DJANGO_ADVISORY, OTHER_DJANGO_ADVISORY), patch_environ({VULNERABILITY_LEVEL.name: "high"}):
            update_requirements_txts()
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(requirements_txt, 1))

    def test_the_markers_risk_level_decides_what_is_warned_about(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin carrying its own risk level is judged by that one, not by the level in force run-wide."""
        directive = "set[vulnerability-level=high]"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, f"django==3.2.0  # update-time: {directive}\n")
        mock_get.side_effect = self.pypi("3.2.0", "3.3.0", bump=True)
        with osv(DJANGO_ADVISORY, OTHER_DJANGO_ADVISORY):
            update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with(f"django==3.3.0  # update-time: {directive}\n")
        location = Location(requirements_txt, 1)
        self.assert_vulnerable_dependency_logged("django", "3.3.0", DJANGO_VULNERABILITY, location)
        self.assert_ignored_vulnerability_logged("django", location, OTHER_DJANGO_VULNERABILITY.advisory, directive)

    def test_a_higher_risk_level_reporting_what_the_run_does_is_redundant(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a level above the run's is redundant where every vulnerability is at or above both."""
        directive = "set[vulnerability-level=high]"
        requirements_txt = self.django_pin(mock_rglob, mock_get, directive)
        with osv(DJANGO_ADVISORY):
            update_requirements_txts()
        self.assert_redundant_vulnerable_level_logged(
            "django", "3.2.0", "high", Location(requirements_txt, 1), directive
        )
        self.assert_vulnerable_dependency_logged(
            "django", "3.2.0", DJANGO_VULNERABILITY, Location(requirements_txt, 1), among_others=True
        )

    @kills(
        Mutation(
            vulnerability_module._report_redundant_suppressions,
            "if (level := marker.vulnerable.value) is not None and all(",
            "if (level := marker.vulnerable.value) is not None and run_wide_level != NO_RISK_LEVEL and all(",
            "a risk level is never redundant in a run switching the check off",
        )
    )
    def test_a_risk_level_judging_the_vulnerabilities_as_the_run_does_is_redundant(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that a risk level is redundant where it and the run's level report the same vulnerabilities."""
        cases = {
            "the run's own level": ("high", "high", (DJANGO_ADVISORY, OTHER_DJANGO_ADVISORY), True),
            "none where the run reports nothing either": ("none", "high", (OTHER_DJANGO_ADVISORY,), True),
            "none in a run switching the check off": ("none", "none", (OTHER_DJANGO_ADVISORY,), True),
            "a lower level reporting what the run does not": ("low", "high", (OTHER_DJANGO_ADVISORY,), False),
        }
        for case, (level, run_level, advisories, redundant) in cases.items():
            with self.subTest(case=case):
                directive = f"set[vulnerability-level={level}]"
                requirements_txt = self.django_pin(mock_rglob, mock_get, directive)
                with osv(*advisories), patch_environ({VULNERABILITY_LEVEL.name: run_level}):
                    update_requirements_txts()
                if redundant:
                    location = Location(requirements_txt, 1)
                    self.assert_redundant_vulnerable_level_logged(
                        "django", "3.2.0", level, location, directive, run_level=run_level
                    )
                    self.assert_no_ignored_vulnerability_logged()
                else:
                    self.assert_no_redundant_suppression_logged()

    def test_the_markers_risk_level_survives_the_check_being_switched_off(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin with a risk level of its own is looked up although the check is switched off run-wide."""
        contents = "django==3.2.0  # update-time: set[vulnerability-level=high]\nflask==1.0\n"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
        mock_get.side_effect = [pypi_index("3.2.0"), pypi_index("1.0")]
        with osv(DJANGO_ADVISORY, OTHER_DJANGO_ADVISORY) as mock_post, vulnerability_check_disabled:
            update_requirements_txts()
        mock_post.assert_any_call(OSV_BATCH_URL, timeout=ANY, json=osv_queries(("django", "3.2.0")))
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(requirements_txt, 1))

    def test_clean_pins_cost_one_osv_request(self, mock_rglob: Mock, mock_get: Mock):
        """Test that the pins of one file are looked up in a single batch, which answers for a clean file on its own."""
        self.discovered_requirements_txt(mock_rglob, "django==3.2.0\nflask==1.0\n")
        mock_get.side_effect = [pypi_index("3.2.0"), pypi_index("1.0")]
        with osv() as mock_post:
            update_requirements_txts()
        assert_osv_asked_about(mock_post, ("django", "3.2.0"), ("flask", "1.0"))
        self.assert_no_warnings_logged()

    def test_a_requirement_pinning_a_range_queries_no_vulnerabilities(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a requirement pinning a range is looked up at PyPI but not at OSV, which matches a version."""
        self.discovered_requirements_txt(mock_rglob, "django==3.2.*\n")
        mock_get.side_effect = self.stale_pypi("3.2.0", upload_time=PYPI_RECENT_UPLOAD)
        with osv(DJANGO_ADVISORY) as mock_post:
            update_requirements_txts()
        mock_post.assert_not_called()
        self.assertEqual(self.queried_packages(mock_get), ["django"])
        self.assert_no_warnings_logged()

    def test_no_suppression_is_reported_when_osv_cannot_be_reached(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an unreachable OSV leaves every form of suppression unjudged, rather than reported as dead."""
        for case, directive in {
            "the scope": "set[vulnerability-level=none]",
            "a named advisory": f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]",
            "a risk level": "set[vulnerability-level=high]",
        }.items():
            with self.subTest(case=case):
                self.django_pin(mock_rglob, mock_get, directive)
                with unreachable_osv() as mock_post:
                    update_requirements_txts()
                mock_post.assert_called()  # Without this, a run that never asked OSV would pass the assertion below.
                self.assert_no_redundant_suppression_logged()

    def test_vulnerability_level_none_marker_silences_the_warning(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a `set[vulnerability-level=none]` marker silences the vulnerability warning and nothing else."""
        contents = "django==3.2.0  # update-time: set[stale-after=0] set[vulnerability-level=none]\n"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
        mock_get.side_effect = self.pypi("3.2.0", "3.3.0", bump=True)
        with osv(DJANGO_ADVISORY) as mock_post:
            update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with(
            "django==3.3.0  # update-time: set[stale-after=0] set[vulnerability-level=none]\n"
        )
        mock_post.assert_called()
        self.assert_no_warnings_logged()
        self.assert_ignored_vulnerability_logged(
            "django", Location(requirements_txt, 1), DJANGO_VULNERABILITY.advisory, "set[vulnerability-level=none]"
        )

    def test_switching_the_check_off_silences_a_vulnerability_of_unknown_severity(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that `set[vulnerability-level=none]` silences a vulnerability whose risk level cannot be read."""
        directive = "set[vulnerability-level=none]"
        requirements_txt = self.django_pin(mock_rglob, mock_get, directive)
        with osv(osv_advisory("PYSEC-2021-98", DJANGO_VULNERABILITY.summary)):
            update_requirements_txts()
        self.assert_no_warnings_logged()
        self.assert_ignored_vulnerability_logged("django", Location(requirements_txt, 1), "PYSEC-2021-98", directive)

    def test_ignore_vulnerable_advisory_marker_silences_that_advisory(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an `ignore[vulnerable=ID]` marker silences the warning about the advisory it names."""
        advisory = DJANGO_VULNERABILITY.advisory
        directive = f"ignore[vulnerable={advisory}]"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, f"django==3.2.0  # update-time: {directive}\n")
        mock_get.side_effect = self.pypi("3.2.0", "3.3.0", bump=True)
        with osv(DJANGO_ADVISORY):
            update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with(f"django==3.3.0  # update-time: {directive}\n")
        self.assert_no_warnings_logged()
        self.assert_ignored_vulnerability_logged("django", Location(requirements_txt, 1), advisory, directive)

    def test_ignore_vulnerable_advisory_marker_accepts_any_identifier_of_the_vulnerability(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that the marker silences the warning when it names any identifier the vulnerability is known by.

        The line names the identifier OSV answered under, whichever identifier the marker spelled.
        """
        cve, pysec, bit = "CVE-2021-1111", "PYSEC-2021-109", "BIT-django-2021-1111"
        far_cve = "CVE-2021-4444"  # Named by the tied-in advisory alone, so the merge brings it to the vulnerability
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
                requirements_txt = self.django_pin(mock_rglob, mock_get, directive)
                location = Location(requirements_txt, 1)
                with osv(*records):
                    update_requirements_txts()
                self.assert_no_warnings_logged()
                self.assert_ignored_vulnerability_logged("django", location, DJANGO_VULNERABILITY.advisory, directive)

    def test_ignore_vulnerable_advisory_marker_still_warns_about_another_advisory(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that a marker naming one advisory leaves the pin's other advisories warned about."""
        directive = f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]"
        requirements_txt = self.django_pin(mock_rglob, mock_get, directive)
        with osv(DJANGO_ADVISORY, OTHER_DJANGO_ADVISORY):
            update_requirements_txts()
        location = Location(requirements_txt, 1)
        self.assert_vulnerable_dependency_logged("django", "3.2.0", OTHER_DJANGO_VULNERABILITY, location)
        self.assert_ignored_vulnerability_logged("django", location, DJANGO_VULNERABILITY.advisory, directive)

    def test_a_silenced_vulnerability_names_the_directive_that_silenced_it(self, mock_rglob: Mock, mock_get: Mock):
        """Test that the line for a silenced vulnerability names the advisory or the level that silenced it, alone."""
        advisory = f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]"
        level = "set[vulnerability-level=high]"
        requirements_txt = self.django_pin(mock_rglob, mock_get, f"{advisory} {level}")
        with osv(
            DJANGO_ADVISORY, OTHER_DJANGO_ADVISORY
        ):  # A critical vulnerability the advisory names, and a moderate one.
            update_requirements_txts()
        location = Location(requirements_txt, 1)
        self.assert_ignored_vulnerability_logged("django", location, DJANGO_VULNERABILITY.advisory, advisory)
        self.assert_ignored_vulnerability_logged("django", location, OTHER_DJANGO_VULNERABILITY.advisory, level)

    def test_two_silenced_vulnerabilities_are_reported_one_line_per_advisory(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a pin whose marker silences two vulnerabilities gets a line naming each of their advisories."""
        directive = "set[vulnerability-level=none]"
        requirements_txt = self.django_pin(mock_rglob, mock_get, directive)
        with osv(DJANGO_ADVISORY, OTHER_DJANGO_ADVISORY):
            update_requirements_txts()
        location = Location(requirements_txt, 1)
        self.assert_no_warnings_logged()
        self.assert_ignored_vulnerability_logged("django", location, DJANGO_VULNERABILITY.advisory, directive)
        self.assert_ignored_vulnerability_logged("django", location, OTHER_DJANGO_VULNERABILITY.advisory, directive)

    def test_a_redundant_advisory_names_its_own_item_alone(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a dead advisory item is reported without naming a risk level beside it that is still live."""
        advisory = f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]"
        requirements_txt = self.django_pin(mock_rglob, mock_get, f"{advisory} set[vulnerability-level=high]")
        with osv(OTHER_DJANGO_ADVISORY):  # A moderate vulnerability the advisory item does not name.
            update_requirements_txts()
        self.assert_redundant_vulnerable_advisory_logged("django", "3.2.0", Location(requirements_txt, 1), advisory)
        # The level silences the moderate vulnerability, so it is not reported as silencing nothing itself.
        self.assert_no_redundant_vulnerable_level_logged()

    def test_every_dead_vulnerable_form_names_itself(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a version with no vulnerability at all reports each `vulnerable` form under its own directive."""
        advisory = f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]"
        directives = f"set[vulnerability-level=none] {advisory}"
        requirements_txt = self.django_pin(mock_rglob, mock_get, directives)
        with osv():  # No vulnerability, so both forms silence nothing.
            update_requirements_txts()
        location = Location(requirements_txt, 1)
        self.assert_redundant_vulnerable_level_logged(
            "django", "3.2.0", "none", location, "set[vulnerability-level=none]"
        )
        self.assert_redundant_vulnerable_advisory_logged("django", "3.2.0", location, advisory)
        self.assert_no_ignored_vulnerability_logged()

    def test_a_redundant_level_names_its_own_item_alone(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a dead risk level is reported without naming an advisory beside it that is still live."""
        advisory = f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]"
        requirements_txt = self.django_pin(mock_rglob, mock_get, f"{advisory} set[vulnerability-level=high]")
        with osv(DJANGO_ADVISORY):  # A critical vulnerability, so nothing falls below the level.
            update_requirements_txts()
        self.assert_redundant_vulnerable_level_logged(
            "django", "3.2.0", "high", Location(requirements_txt, 1), "set[vulnerability-level=high]"
        )
        # The advisory silences that vulnerability, so it is not reported as silencing nothing itself.
        self.assert_none_logged(message_of(Logger.redundant_vulnerable_advisory), "a redundant advisory")

    def test_ignore_vulnerable_advisory_marker_is_redundant_when_no_vulnerability_answers_to_it(
        self, mock_rglob: Mock, mock_get: Mock
    ):
        """Test that a marker naming an advisory none of the version's vulnerabilities answers to is redundant."""
        directive = f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]"
        requirements_txt = self.django_pin(mock_rglob, mock_get, directive)
        with osv(OTHER_DJANGO_ADVISORY):
            update_requirements_txts()
        location = Location(requirements_txt, 1)
        self.assert_redundant_vulnerable_advisory_logged("django", "3.2.0", location, directive)
        self.assert_vulnerable_dependency_logged(
            "django", "3.2.0", OTHER_DJANGO_VULNERABILITY, location, among_others=True
        )

    def test_globally_ignored_advisory_silences_the_warning(self, mock_rglob: Mock, mock_get: Mock):
        """Test that an advisory ignored run-wide is not warned about, and that the silencing names the option."""
        requirements_txt = self.django_pin(mock_rglob, mock_get)
        with osv(DJANGO_ADVISORY), patch_environ({IGNORE_VULNERABILITIES.name: DJANGO_VULNERABILITY.advisory}):
            update_requirements_txts()
        self.assert_no_warnings_logged()
        location = Location(requirements_txt, 1)
        self.assert_globally_ignored_vulnerability_logged(
            "django", location, DJANGO_VULNERABILITY.advisory, DJANGO_VULNERABILITY.advisory
        )

    @kills(
        Mutation(
            vulnerability_module._report,
            "logger.globally_ignored_vulnerability(reference, vulnerability, silenced_by)",
            "logger.globally_ignored_vulnerability(reference, vulnerability, frozenset({vulnerability.advisory}))",
            "the option's line names the identifier OSV answered under rather than the one the reader passed",
        )
    )
    def test_the_option_line_names_the_advisory_and_what_was_passed(self, mock_rglob: Mock, mock_get: Mock):
        """Test that the option's line names the advisory OSV answered under, and quotes the identifier passed."""
        cve = "CVE-2021-1111"
        requirements_txt = self.django_pin(mock_rglob, mock_get)
        with osv(DJANGO_ADVISORY | {"aliases": [cve]}), patch_environ({IGNORE_VULNERABILITIES.name: cve}):
            update_requirements_txts()
        self.assert_no_warnings_logged()
        self.assert_globally_ignored_vulnerability_logged(
            "django", Location(requirements_txt, 1), DJANGO_VULNERABILITY.advisory, cve
        )

    def test_a_marker_is_reported_where_the_option_names_the_same_advisory(self, mock_rglob: Mock, mock_get: Mock):
        """Test that where the marker and the option both name the advisory, the marker's silencing is reported."""
        directive = f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]"
        requirements_txt = self.django_pin(mock_rglob, mock_get, directive)
        with osv(DJANGO_ADVISORY), patch_environ({IGNORE_VULNERABILITIES.name: DJANGO_VULNERABILITY.advisory}):
            update_requirements_txts()
        self.assert_no_warnings_logged()
        location = Location(requirements_txt, 1)
        self.assert_ignored_vulnerability_logged("django", location, DJANGO_VULNERABILITY.advisory, directive)
        self.assert_no_globally_ignored_vulnerability_logged()

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
        contents = 'flask[async]==1.0 ; python_version < "3.12"  # keep\n'
        requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
        mock_get.side_effect = self.pypi("1.0", "1.1", bump=True)
        update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with('flask[async]==1.1 ; python_version < "3.12"  # keep\n')
        self.assert_path_logged(requirements_txt)
        self.assert_new_version_logged("flask", _PUBLISHED, Location(requirements_txt, 1))
        self.assert_no_warnings_logged()

    def test_spaces_around_equals_preserved(self, mock_rglob: Mock, mock_get: Mock):
        """Test that spaces around `==` and the aligned inline comment are preserved when bumping the version."""
        contents = "certifi == 2020.4.5.1          # used by requests\n"
        requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
        mock_get.side_effect = self.pypi("2020.4.5.1", "2020.4.5.2", bump=True)
        update_requirements_txts()
        requirements_txt.write_text.assert_called_once_with("certifi == 2020.4.5.2          # used by requests\n")
        self.assert_path_logged(requirements_txt)
        self.assert_new_version_logged(
            "certifi", published_version("2020.4.5.2", PYPI_OLD_UPLOAD), Location(requirements_txt, 1)
        )
        self.assert_no_warnings_logged()

    def test_loose_specifiers_untouched(self, mock_rglob: Mock, mock_get: Mock):
        """Test that non-exact specifiers are left untouched, and that only the lines naming a package are queried.

        The lines below the three requirements each hold something a requirements file may carry that resolves to
        no PyPI release.
        """
        lines = (
            "flask>=1.0",
            "django~=2.0",
            "requests",
            "-e .",
            "-r other.txt",
            "-c constraints.txt",
            "--index-url https://example.com",
            "git+https://x/y.git",
            "https://example.com/pkg-1.0.whl",
            "pkg @ git+https://x/y.git",
            "mypkg-1.0.tar.gz",
            "mypkg-1.0-py3-none-any.whl",
            "./dist/mypkg-1.0.whl",
            "",
            "# c",
        )
        contents = "".join(f"{line}\n" for line in lines)
        requirements_txt = self.discovered_requirements_txt(mock_rglob, contents)
        # Answer any package, so a line that should name none is caught by the assertion below rather than by
        # running out of responses.
        mock_get.return_value = pypi_index("1.1")
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        self.assertEqual(self.queried_packages(mock_get), ["flask", "django", "requests"])
        self.assert_path_logged(requirements_txt)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_held_back_by_cooldown(self, mock_rglob: Mock, mock_get: Mock):
        """Test that the newest version published within the cooldown is logged as held back, past a yanked one."""
        fresh_and_yanked = pypi_release(PYPI_RECENT_UPLOAD, yanked=True)
        cases = {
            "a release": [pypi_index("1.0", "1.1")],
            "a release below a yanked one": [pypi_index("1.0", "1.1", "1.2"), fresh_and_yanked],
        }
        for case, responses in cases.items():
            with self.subTest(case):
                requirements_txt = self.discovered_requirements_txt(mock_rglob, "flask==1.0\n")
                mock_get.side_effect = [*responses, pypi_release(PYPI_RECENT_UPLOAD)]
                update_requirements_txts()
                requirements_txt.write_text.assert_not_called()
                self.assert_held_back_by_cooldown_logged("flask", "1.1", Location(requirements_txt, 1))
                self.assert_no_new_version_logged()
                self.assert_no_warnings_logged()

    def test_a_compiled_file_is_skipped_entirely(self, mock_rglob: Mock, mock_get: Mock):
        """Test that a compiled file is skipped whole, its pins left as the file wrote them.

        Which files count as compiled is pinned by the tests of `is_compiled` itself.
        """
        requirements_txt = self.discovered_requirements_txt(mock_rglob, "flask==1.0 \\\n    --hash=sha256:abc\n")
        update_requirements_txts()
        requirements_txt.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_skipped_logged(requirements_txt, "compiled or hash-pinned requirements file")
        self.assert_no_debug_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()
