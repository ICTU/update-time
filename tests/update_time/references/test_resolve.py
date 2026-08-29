"""Unit tests for the shared reference-update decision."""

import unittest
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import Mock, call

from update_time.domain.archival import archival_reporting
from update_time.domain.bound import BLOCK_ALL_UPDATES, NO_BOUND, Verb
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import Archival, DependencyVersion, FloatingPin, Project
from update_time.domain.publication import publication_date_reporting
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.vulnerability import vulnerability_reporting
from update_time.domain.yank import yank_reporting
from update_time.markers.directive import Reason
from update_time.markers.marker import Marker, Scope, Threshold
from update_time.references import resolve
from update_time.references.resolve import latest_version, report_project_checks

from tests.helpers import patch_environ
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import BARE_IGNORE, DIGEST
from tests.update_time.helpers import bound, reference, resolved_reference, staleness_disabled
from tests.update_time.references.helpers import mock_project_getter, new_version_getter

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.reference import Reference, ResolvedReference

# The threshold `ignore[stale<90]` parses to, carrying the directive the parser sets alongside every value it reads.
_STALE_THRESHOLD = Threshold(value=90, directive="ignore[stale<90]")
_COOLDOWN_ITEM = Threshold(value=30, directive="ignore[cooldown<30]")


@dataclass(frozen=True)
class _RedundancyCase:
    """One directive judged for redundancy, and the two things that decide whether it is reported.

    `capability` registers a getter as able to apply the directive, which is what keeps it from being reported.
    """

    marker: Marker
    reason: Reason
    capability: Callable[[NewVersionGetter], NewVersionGetter]


# Each directive a source may be unable to apply, keyed by the text the warning names it with. A marker that sets a
# value carries the directive that set it, because a threshold on its own names none. A marker that holds a scope
# back carries no text, because the scope has one spelling, which the warning spells out.
_REDUNDANT_DIRECTIVES = {
    "ignore[yanked]": _RedundancyCase(Marker(ignored_scopes=Scope.YANKED), Reason.NO_YANK_CONCEPT, yank_reporting),
    "ignore[archived]": _RedundancyCase(
        Marker(ignored_scopes=Scope.ARCHIVED), Reason.NO_ARCHIVAL_SIGNAL, archival_reporting
    ),
    "ignore[vulnerable]": _RedundancyCase(
        Marker(ignored_scopes=Scope.VULNERABLE), Reason.NO_VULNERABILITY_REPORTS, vulnerability_reporting
    ),
    "ignore[stale]": _RedundancyCase(
        Marker(ignored_scopes=Scope.STALE), Reason.NO_STALENESS_DATES, publication_date_reporting
    ),
    "ignore[stale<90]": _RedundancyCase(
        Marker(stale=_STALE_THRESHOLD), Reason.NO_STALENESS_DATES, publication_date_reporting
    ),
    "ignore[cooldown<30]": _RedundancyCase(
        Marker(cooldown=_COOLDOWN_ITEM), Reason.NO_COOLDOWN_DATES, publication_date_reporting
    ),
}


@dataclass(frozen=True)
class _ReportedCheck:
    """One of the checks the decision reports the resolved version on.

    `scope` is what a marker silences the check with, `reporter` names the `Logger` method it reports through, and
    `arguments` are what that method takes beside the resolved reference and the marker.
    """

    scope: Scope
    reporter: str
    arguments: tuple[object, ...] = ()


# Each check the decision reports on, keyed by the scope silencing it. Only the staleness check takes an argument of
# its own, the threshold it judges the publication date against.
_CHECKS = {
    "stale": _ReportedCheck(Scope.STALE, "report_staleness", (STALE_AFTER.default,)),
    "yanked": _ReportedCheck(Scope.YANKED, "report_yank"),
    "archived": _ReportedCheck(Scope.ARCHIVED, "report_archival"),
}


class LatestVersionTest(unittest.TestCase):
    """Unit tests for deciding the latest version to update a reference to."""

    def setUp(self) -> None:
        """Use a mock logger and path so log calls can be asserted."""
        super().setUp()
        self.log = Mock()
        self.path = Mock()

    def reference(self, dependency: str = "python") -> Reference:
        """Return the reference the decision is run for, as the logger is handed it."""
        return reference(dependency, self.path, "3.14")

    def resolved(self, release: str = "3.15") -> ResolvedReference:
        """Return the resolved reference the decision hands the logger, for the version it resolved."""
        return resolved_reference("python", self.path, DependencyVersion(version=release), "3.14")

    def latest_version(
        self,
        marker: Marker | None = None,
        get_new_version: NewVersionGetter | None = None,
        dependency: str = "python",
    ) -> DependencyVersion | None:
        """Run the decision for the dependency's reference at version 3.14, resolving 3.15 unless overridden."""
        get_new_version = new_version_getter("3.15") if get_new_version is None else get_new_version
        marker = Marker() if marker is None else marker
        return latest_version(self.reference(dependency), get_new_version, marker, self.log)

    def test_returns_the_resolved_version(self):
        """Test that the version the getter resolves is returned."""
        self.assertEqual(self.latest_version(), DependencyVersion(version="3.15"))

    def test_returns_the_resolved_version_even_when_unchanged(self):
        """Test that a resolved version equal to the current one is still returned, not turned into None."""
        latest = self.latest_version(get_new_version=new_version_getter("3.14", DIGEST))
        self.assertEqual(latest, DependencyVersion(version="3.14", sha=DIGEST))

    def test_passes_the_version_bound_to_the_getter(self):
        """Test that the marker's version bound reaches the getter, so the source only picks admitted versions."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.14.1"))
        self.latest_version(Marker(version_bound=bound(Verb.ALLOW, "update<3.15")), get_new_version)
        get_new_version.assert_called_once_with("python", "3.14", bound(Verb.ALLOW, "update<3.15"), COOLDOWN.default)

    def test_ignore_update_passes_a_block_all_bound_to_the_getter(self):
        """Test that a held-back update asks the source to keep the current version rather than resolve an update."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.14"))
        self.latest_version(Marker(ignored_scopes=Scope.UPDATE), get_new_version)
        get_new_version.assert_called_once_with("python", "3.14", BLOCK_ALL_UPDATES, COOLDOWN.default)

    @patch_environ({COOLDOWN.name: "30"})
    def test_passes_the_configured_cooldown_to_the_getter(self):
        """Test that the cooldown the run was configured with reaches the getter, rather than the built-in default."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.15"))
        self.latest_version(get_new_version=get_new_version)
        get_new_version.assert_called_once_with("python", "3.14", NO_BOUND, 30)

    def test_the_markers_cooldown_is_passed_to_the_getter(self):
        """Test that a reference carrying its own cooldown is resolved with that one, not the global one."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.15"))
        self.latest_version(Marker(cooldown=Threshold(value=30)), get_new_version)
        get_new_version.assert_called_once_with("python", "3.14", NO_BOUND, 30)

    def test_warns_about_a_redundant_bound(self):
        """Test that the reference's bound is checked for redundancy against its current version."""
        marker = Marker(version_bound=bound(Verb.IGNORE, "patch-update"))
        self.latest_version(marker)
        self.log.warn_if_redundant_bound.assert_called_once_with(self.reference(), marker)

    def test_the_resolved_version_reaches_every_check(self):
        """Test that the version the run resolves is handed to each check, which warns or reports its hold-back."""
        for name, check in _CHECKS.items():
            with self.subTest(check=name):
                self.log.reset_mock()  # Judge each case on the calls of its own run.
                self.latest_version()
                getattr(self.log, check.reporter).assert_called_once_with(self.resolved(), Marker(), *check.arguments)

    @patch_environ({STALE_AFTER.name: "30"})
    def test_the_configured_threshold_is_used_for_the_staleness_warning(self):
        """Test that the threshold the run was configured with is used, rather than the built-in default."""
        self.latest_version()
        self.log.report_staleness.assert_called_once_with(self.resolved(), Marker(), 30)

    def test_the_markers_threshold_is_used_for_the_staleness_warning(self):
        """Test that a reference carrying its own staleness threshold is judged by that one, not the global one."""
        marker = Marker(stale=Threshold(value=90))
        self.latest_version(marker)
        self.log.report_staleness.assert_called_once_with(self.resolved(), marker, 90)

    def test_warns_about_an_inverted_stale_item(self):
        """Test that a `stale` item comparing the wrong way is reported, and the global threshold is used."""
        marker = Marker(stale=Threshold(inverted_item="stale>=90"), raw="ignore[stale>=90]")
        self.latest_version(marker)
        self.log.inverted_stale_item.assert_called_once_with(self.reference(), "stale>=90")
        self.log.report_staleness.assert_called_once_with(self.resolved(), marker, STALE_AFTER.default)

    def test_warns_about_an_inverted_cooldown_item(self):
        """Test that a `cooldown` item comparing the wrong way is reported, and the global cooldown is used."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.15"))
        marker = Marker(cooldown=Threshold(inverted_item="cooldown>=30"), raw="ignore[cooldown>=30]")
        self.latest_version(marker, get_new_version)
        self.log.inverted_cooldown_item.assert_called_once_with(self.reference(), "cooldown>=30")
        get_new_version.assert_called_once_with("python", "3.14", NO_BOUND, COOLDOWN.default)

    def test_warns_about_an_inverted_vulnerability_item(self):
        """Test that a `vulnerable` item comparing the wrong way is reported."""
        marker = Marker(vulnerable=Threshold(inverted_item="vulnerable>=high"), raw="ignore[vulnerable>=high]")
        self.latest_version(marker)
        self.log.inverted_vulnerable_item.assert_called_once_with(self.reference(), "vulnerable>=high")

    def test_a_marker_silencing_a_warning_still_returns_the_update(self):
        """Test that a marker silencing a warning leaves the update in place, and reaches the check that reports it."""
        for name, check in _CHECKS.items():
            with self.subTest(check=name):
                self.log.reset_mock()  # Judge each case on the calls of its own run.
                marker = Marker(ignored_scopes=check.scope)
                latest = self.latest_version(marker)
                self.assertEqual(latest, DependencyVersion(version="3.15"))
                getattr(self.log, check.reporter).assert_called_once_with(self.resolved(), marker, *check.arguments)

    def test_warns_about_a_directive_the_source_cannot_apply(self):
        """Test that a directive is reported as redundant when the source cannot apply it, and the update stands."""
        for directive, case in _REDUNDANT_DIRECTIVES.items():
            with self.subTest(directive=directive):
                self.log.reset_mock()  # Judge each case on the calls of its own run.
                latest = self.latest_version(case.marker)
                self.assertEqual(latest, DependencyVersion(version="3.15"))
                self.log.redundant_directive.assert_called_once_with(self.reference(), directive, case.reason)

    def test_no_redundant_directive_when_the_source_can_apply_it(self):
        """Test that a directive is not reported when the source has the capability the directive needs."""
        for directive, case in _REDUNDANT_DIRECTIVES.items():
            with self.subTest(directive=directive):
                self.log.reset_mock()  # Judge each case on the calls of its own run.
                latest = self.latest_version(case.marker, case.capability(new_version_getter("3.15")))
                self.assertEqual(latest, DependencyVersion(version="3.15"))
                self.log.redundant_directive.assert_not_called()

    def test_the_capability_is_judged_per_reference(self):
        """Test that a getter dating some of its dependencies only is judged by the reference the marker sits on.

        One case per warning the publication date decides: the `cooldown` item's and the `stale` directive's.
        """

        def dates_the_releases_of(dependency: str) -> bool:
            """Return whether the source dates the dependency's releases, which it does for `node` alone."""
            return dependency == "node"

        get_new_version = publication_date_reporting(new_version_getter("3.15"), when=dates_the_releases_of)
        for directive in ("ignore[cooldown<30]", "ignore[stale<90]"):
            case = _REDUNDANT_DIRECTIVES[directive]
            for dependency, reported in (("node", False), ("python", True)):
                with self.subTest(directive=directive, dependency=dependency):
                    self.log.reset_mock()  # Judge each reference on the calls of its own run.
                    self.latest_version(case.marker, get_new_version, dependency)
                    expected = [call(self.reference(dependency), directive, case.reason)] if reported else []
                    self.assertEqual(self.log.redundant_directive.call_args_list, expected)

    def test_no_redundant_directive_when_the_marker_sets_no_value(self):
        """Test that a marker is not reported when it sets no value, carrying no item or an inverted one."""
        markers = {
            "no item at all": Marker(),
            "inverted cooldown item": Marker(
                cooldown=Threshold(inverted_item="cooldown>=30"), raw="ignore[cooldown>=30]"
            ),
            "inverted stale item": Marker(stale=Threshold(inverted_item="stale>=90"), raw="ignore[stale>=90]"),
        }
        for case, marker in markers.items():
            with self.subTest(case=case):
                self.log.reset_mock()  # Judge each case on the calls of its own run.
                latest = self.latest_version(marker)
                self.assertEqual(latest, DependencyVersion(version="3.15"))
                self.log.redundant_directive.assert_not_called()

    def test_warns_about_a_redundant_floating_pin_directive(self):
        """Test that `allow[floating-pin]` is reported as redundant when the reference's pin does not float."""
        marker = Marker(allowed_scopes=Scope.FLOATING_PIN, raw="allow[floating-pin]")
        latest = self.latest_version(marker)
        self.log.redundant_directive.assert_called_once_with(
            self.reference(), "allow[floating-pin]", Reason.NOTHING_FLOATING
        )
        self.assertEqual(latest, DependencyVersion(version="3.15"))

    @kills(
        Mutation(
            resolve,
            "return Reason.NOTHING_FLOATING if latest is not None and latest.floating is None else None",
            'return Reason.NOTHING_FLOATING if latest is not None and latest.floating != "resolved" else None',
            "a floating pin the source could not resolve is reported as redundant, although its tag still floats",
        )
    )
    def test_no_redundant_floating_pin_directive_for_a_pin_the_source_could_not_resolve(self):
        """Test that `allow[floating-pin]` is not reported for a floating pin the source resolved no version for.

        One case per reason it reports, each of which leaves the tag floating. A pin it did resolve is covered by
        `test_marker_keeps_the_tag_floating`, per file format.
        """
        marker = Marker(allowed_scopes=Scope.FLOATING_PIN, raw="allow[floating-pin]")
        unresolved = [pin for pin in FloatingPin if pin is not FloatingPin.RESOLVED]
        for floating in unresolved:
            with self.subTest(floating=floating):
                self.log.reset_mock()  # Judge each outcome on the calls of its own run.
                release = DependencyVersion(version="3.15", floating=floating)
                self.assertEqual(self.latest_version(marker, Mock(return_value=release)), release)
                self.log.redundant_directive.assert_not_called()

    @kills(
        Mutation(
            resolve,
            "if not marker.allows(Scope.FLOATING_PIN):",
            "if not marker.allows(Scope.FLOATING_PIN) and Scope.FLOATING_PIN not in marker.written_scopes:",
            "the explicit default is reported as redundant, as if it kept the pin floating",
        )
    )
    def test_no_redundant_directive_for_the_ignore_spelling_of_the_floating_pin(self):
        """Test that `ignore[floating-pin]` is not reported, since it asks for what happens without it."""
        marker = Marker(written_scopes=Scope.FLOATING_PIN, raw="ignore[floating-pin]")
        self.assertEqual(self.latest_version(marker), DependencyVersion(version="3.15"))
        self.log.redundant_directive.assert_not_called()

    def test_warns_about_a_floating_pin_directive_beside_a_bare_ignore(self):
        """Test that `allow[floating-pin]` is reported when a bare `ignore` freezes the reference, unqueried."""
        get_new_version = Mock()
        marker = BARE_IGNORE.merge(Marker(allowed_scopes=Scope.FLOATING_PIN, raw="ignore allow[floating-pin]"))
        self.latest_version(marker, get_new_version)
        self.log.redundant_directive.assert_called_once_with(
            self.reference(), "allow[floating-pin]", Reason.UPDATE_HELD_BACK
        )
        get_new_version.assert_not_called()  # A frozen reference is left alone, so no source is asked about it.

    def test_a_frozen_reference_keeping_its_pin_floating_is_warned_about_once(self):
        """Test that `ignore[update] allow[floating-pin]` draws one warning, though two call sites can report it."""
        raw = "ignore[update] allow[floating-pin]"
        marker = Marker(ignored_scopes=Scope.UPDATE, allowed_scopes=Scope.FLOATING_PIN, raw=raw)
        self.assertIsNone(self.latest_version(marker))
        self.log.redundant_directive.assert_called_once_with(
            self.reference(), "allow[floating-pin]", Reason.UPDATE_HELD_BACK
        )

    def test_a_frozen_reference_is_warned_about_although_its_tag_floats(self):
        """Test that a frozen reference's `allow[floating-pin]` is reported, since the freeze keeps its tag as well."""
        raw = "ignore[update] allow[floating-pin]"
        marker = Marker(ignored_scopes=Scope.UPDATE, allowed_scopes=Scope.FLOATING_PIN, raw=raw)
        floating = DependencyVersion(version="3.14.7", sha=DIGEST, floating=FloatingPin.RESOLVED)
        self.assertIsNone(self.latest_version(marker, Mock(return_value=floating)))
        self.log.redundant_directive.assert_called_once_with(
            self.reference(), "allow[floating-pin]", Reason.UPDATE_HELD_BACK
        )

    def test_ignore_update_returns_none(self):
        """Test that `ignore[update]` holds back the update, after the staleness check has still run."""
        self.assertIsNone(self.latest_version(Marker(ignored_scopes=Scope.UPDATE)))
        self.log.report_staleness.assert_called_once()


class ReportProjectChecksTest(unittest.TestCase):
    """Unit tests for the checks a reference the run resolves no update for still gets."""

    def setUp(self) -> None:
        """Use a mock logger and path so log calls can be asserted."""
        super().setUp()
        self.log = Mock()
        self.path = Mock()

    def project(self, archival: Archival) -> Mock:
        """Return a getter answering with the project's archival, registered as a source that reports one."""
        return archival_reporting(Mock(return_value=Project(archival=archival)))

    def test_reports_both_checks_for_the_project(self):
        """Test that what the getter answers reaches the archival report and the staleness report alike."""
        archival = Archival(archived=True)
        marker = Marker()
        report_project_checks(reference("humanize", self.path), marker, self.log, self.project(archival))
        project = Project(archival=archival)
        resolved = resolved_reference("humanize", self.path, DependencyVersion(version="", project=project))
        self.log.report_archival.assert_called_once_with(resolved, marker)
        self.log.report_staleness.assert_called_once_with(resolved, marker, STALE_AFTER.default)

    @kills(
        Mutation(
            resolve,
            "    if not project_is_checked(get_project, reference.dependency, threshold):\n"
            "        return\n"
            "    release = DependencyVersion.unpinned(get_project(reference.dependency))",
            "    release = DependencyVersion.unpinned(get_project(reference.dependency))",
            "a source is asked about a reference no check needs an answer for, so the run pays for the request",
        )
    )
    def test_a_source_reporting_no_archival_is_not_asked_with_the_staleness_check_switched_off(self):
        """Test that switching the staleness check off leaves such a source unasked, so it costs no request."""
        get_project = mock_project_getter()
        with staleness_disabled:
            report_project_checks(reference("humanize", self.path), Marker(), self.log, get_project)
        get_project.assert_not_called()
        self.log.report_staleness.assert_not_called()
        self.log.report_archival.assert_not_called()
