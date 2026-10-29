"""Unit tests for the shared reference-update decision."""

import unittest
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import Mock, call

from update_time.domain.archival import archival_reporting
from update_time.domain.bound import BLOCK_ALL_UPDATES, NO_BOUND, Verb
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import Archival, DependencyVersion, FloatingPin, PinnedDependency, Project
from update_time.domain.publication import publication_date_reporting
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.vulnerability import vulnerability_reporting
from update_time.domain.yank import yank_reporting
from update_time.markers.directive import Reason
from update_time.markers.marker import Marker, Scope
from update_time.references import resolve as resolve_module
from update_time.references.resolve import latest_version, report_project_checks

from tests.helpers import patch_environ
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import COOLDOWN_30, DIGEST, STALE_AFTER_90, VULNERABILITY_LEVEL_NONE
from tests.update_time.helpers import (
    bound,
    reference,
    resolved_reference,
    staleness_disabled,
)
from tests.update_time.references.helpers import mock_project_getter, new_version_getter

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.bound import NewVersionGetter, VersionBound
    from update_time.domain.reference import Reference
    from update_time.markers.reference import SteeredResolvedReference


@dataclass(frozen=True)
class _RedundancyCase:
    """One directive judged for redundancy, and the two things that decide whether it is reported.

    `capability` registers a getter as able to apply the directive, which is what keeps it from being reported.
    """

    marker: Marker
    reason: Reason
    capability: Callable[[NewVersionGetter], NewVersionGetter]


# Each directive a source may be unable to apply, keyed by the text the warning names it with. A marker that sets a
# value is parsed from that text, so it carries the directive the warning names. A marker that holds a scope back
# carries no text, because the scope has one spelling, which the warning spells out.
_REDUNDANT_DIRECTIVES = {
    "ignore[yanked]": _RedundancyCase(Marker(ignored_scopes=Scope.YANKED), Reason.NO_YANK_CONCEPT, yank_reporting),
    "ignore[archived]": _RedundancyCase(
        Marker(ignored_scopes=Scope.ARCHIVED), Reason.NO_ARCHIVAL_SIGNAL, archival_reporting
    ),
    "set[vulnerability-level=none]": _RedundancyCase(
        VULNERABILITY_LEVEL_NONE, Reason.NO_VULNERABILITY_REPORTS, vulnerability_reporting
    ),
    "set[stale-after=90]": _RedundancyCase(STALE_AFTER_90, Reason.NO_STALENESS_DATES, publication_date_reporting),
    "set[cooldown=30]": _RedundancyCase(COOLDOWN_30, Reason.NO_COOLDOWN_DATES, publication_date_reporting),
}


@dataclass(frozen=True)
class _ReportedCheck:
    """One of the checks a marker silences that the decision reports the resolved version on.

    `scope` is what a marker silences the check with, and `reporter` names the `Logger` method it reports through.
    """

    scope: Scope
    reporter: str


# A marker can silence each of these checks, which are keyed by the scope silencing them. The decision reports the
# resolved version on each.
_CHECKS = {
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

    def resolved(self, release: str = "3.15", marker: Marker | None = None) -> SteeredResolvedReference:
        """Return the resolved reference the decision hands the logger, for the version it resolved."""
        return resolved_reference("python", self.path, DependencyVersion(version=release), "3.14", marker)

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

    def assert_resolved(
        self, get_new_version: Mock, version_bound: VersionBound = NO_BOUND, cooldown: int = COOLDOWN.default
    ) -> None:
        """Assert the source was asked once about the reference's pin, under the bound and cooldown given."""
        get_new_version.assert_called_once_with(self.reference().pinned, version_bound, cooldown)

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
        self.assert_resolved(get_new_version, bound(Verb.ALLOW, "update<3.15"))

    def test_ignore_update_passes_a_block_all_bound_to_the_getter(self):
        """Test that a held-back update asks the source to keep the current version rather than resolve an update."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.14"))
        self.latest_version(Marker(ignored_scopes=Scope.UPDATE), get_new_version)
        self.assert_resolved(get_new_version, BLOCK_ALL_UPDATES)

    @patch_environ({COOLDOWN.name: "30"})
    def test_passes_the_configured_cooldown_to_the_getter(self):
        """Test that the cooldown the run was configured with reaches the getter, rather than the built-in default."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.15"))
        self.latest_version(get_new_version=get_new_version)
        self.assert_resolved(get_new_version, cooldown=30)

    def test_the_markers_cooldown_is_passed_to_the_getter(self):
        """Test that a reference carrying its own cooldown is resolved with that one, not the global one."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.15"))
        self.latest_version(COOLDOWN_30, get_new_version)
        self.assert_resolved(get_new_version, cooldown=30)

    def test_warns_about_a_redundant_bound(self):
        """Test that the reference's bound is checked for redundancy against its current version."""
        marker = Marker(version_bound=bound(Verb.IGNORE, "patch-update"))
        self.latest_version(marker)
        self.log.warn_if_redundant_bound.assert_called_once_with(self.reference(), marker)

    def test_the_resolved_version_reaches_every_check(self):
        """Test that the version the run resolves is handed to each check, which warns or reports what silenced it."""
        for name, check in _CHECKS.items():
            with self.subTest(check=name):
                self.log.reset_mock()  # Judge each case on the calls of its own run.
                self.latest_version()
                getattr(self.log, check.reporter).assert_called_once_with(self.resolved(), Marker())

    @patch_environ({STALE_AFTER.name: "30"})
    def test_the_configured_threshold_is_used_for_the_staleness_warning(self):
        """Test that the threshold the run was configured with is used, rather than the built-in default."""
        self.latest_version()
        self.log.report_staleness.assert_called_once_with(self.resolved(), 30)

    def test_the_markers_threshold_is_used_for_the_staleness_warning(self):
        """Test that a reference carrying its own staleness threshold is judged by that one, not the global one."""
        self.latest_version(STALE_AFTER_90)
        self.log.report_staleness.assert_called_once_with(self.resolved(marker=STALE_AFTER_90), 90)

    def test_a_marker_silencing_a_warning_still_returns_the_update(self):
        """Test that a marker silencing a warning leaves the update in place, and reaches the check that reports it."""
        for name, check in _CHECKS.items():
            with self.subTest(check=name):
                self.log.reset_mock()  # Judge each case on the calls of its own run.
                marker = Marker(ignored_scopes=check.scope)
                latest = self.latest_version(marker)
                self.assertEqual(latest, DependencyVersion(version="3.15"))
                getattr(self.log, check.reporter).assert_called_once_with(self.resolved(marker=marker), marker)

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

        def dates_the_releases_of(pinned: PinnedDependency) -> bool:
            """Return whether the source dates the pinned dependency's releases, which it does for `node` alone."""
            return pinned.name == "node"

        get_new_version = publication_date_reporting(new_version_getter("3.15"), when=dates_the_releases_of)
        for directive in ("set[cooldown=30]", "set[stale-after=90]"):
            case = _REDUNDANT_DIRECTIVES[directive]
            for dependency, reported in (("node", False), ("python", True)):
                with self.subTest(directive=directive, dependency=dependency):
                    self.log.reset_mock()  # Judge each reference on the calls of its own run.
                    self.latest_version(case.marker, get_new_version, dependency)
                    expected = [call(self.reference(dependency), directive, case.reason)] if reported else []
                    self.assertEqual(self.log.redundant_directive.call_args_list, expected)

    def test_warns_about_a_redundant_floating_pin_directive(self):
        """Test that `allow[floating-pin]` is reported as redundant when the reference's pin does not float."""
        marker = Marker(allowed_scopes=Scope.FLOATING_PIN, raw="allow[floating-pin]")
        latest = self.latest_version(marker)
        self.log.redundant_directive.assert_called_once_with(
            self.reference(), "allow[floating-pin]", Reason.PIN_NOT_FLOATING
        )
        self.assertEqual(latest, DependencyVersion(version="3.15"))

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

    def test_warns_about_a_floating_pin_directive_on_a_held_back_reference(self):
        """Test that `allow[floating-pin]` on a floating tag `ignore[update]` holds back is reported as redundant."""
        floating = DependencyVersion(version="3.14.7", sha=DIGEST, floating=FloatingPin.RESOLVED)
        marker = Marker(ignored_scopes=Scope.UPDATE, allowed_scopes=Scope.FLOATING_PIN)
        self.assertIsNone(self.latest_version(marker, Mock(return_value=floating)))
        self.log.redundant_directive.assert_called_once_with(
            self.reference(), "allow[floating-pin]", Reason.NEVER_PINNED
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
        self.log.report_staleness.assert_called_once_with(resolved, STALE_AFTER.default)

    def test_a_source_reporting_no_archival_is_not_asked_with_the_staleness_check_switched_off(self):
        """Test that switching the staleness check off leaves such a source unasked, so it costs no request."""
        get_project = mock_project_getter()
        with staleness_disabled:
            report_project_checks(reference("humanize", self.path), Marker(), self.log, get_project)
        get_project.assert_not_called()
        self.log.report_staleness.assert_not_called()
        self.log.report_archival.assert_not_called()

    @kills(
        Mutation(
            resolve_module.report_project_checks,
            "project_is_checked(get_project, reference.dependency, marker)",
            "project_is_checked(get_project, reference.dependency, type(marker)())",
            "a reference's own threshold does not reach the gate, so a run with the check off looks it up for none",
        )
    )
    def test_a_source_reporting_no_archival_is_asked_about_a_reference_with_a_threshold_of_its_own(self):
        """Test that a reference setting a staleness threshold of its own is looked up with the check switched off."""
        get_project = mock_project_getter()
        with staleness_disabled:
            report_project_checks(reference("humanize", self.path), STALE_AFTER_90, self.log, get_project)
        get_project.assert_called_once_with("humanize")
        resolved = resolved_reference(
            "humanize", self.path, DependencyVersion(version="", project=Project()), marker=STALE_AFTER_90
        )
        self.log.report_staleness.assert_called_once_with(resolved, 90)
