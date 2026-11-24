"""Unit tests for the shared reference-update decision."""

import unittest
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import Mock, call

from mutation_time.mutation import kills
from update_time.domain.bound import BLOCK_ALL_UPDATES, NO_BOUND, BoundRedundancy, Verb
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import (
    NO_PROJECT,
    Archival,
    ArchivedSubject,
    DependencyVersion,
    FloatingPin,
    PinnedDependency,
    Project,
    ProjectGetter,
    Release,
    Unserved,
    Yank,
    every_project,
)
from update_time.domain.directive import Reason
from update_time.domain.marker import NO_MARKER, Marker, Scope
from update_time.domain.reference import Reference, ResolvedReference
from update_time.domain.source import Source, every_dependency
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.vulnerability import Ecosystem
from update_time.references import resolve as resolve_module
from update_time.references.resolve import latest_version, report_project_checks, report_yank

from tests.helpers import patch_environ
from tests.update_time.fixtures import (
    COOLDOWN_30,
    DIGEST,
    PAST_COOLDOWN_DATE,
    STALE_AFTER_90,
    STALE_DATE,
    STALE_PROJECT,
    VULNERABILITY_LEVEL_NONE,
)
from tests.update_time.helpers import (
    bound,
    resolved_reference,
    staleness_disabled,
)
from tests.update_time.references.helpers import (
    mock_new_version_getter,
    mock_project_getter,
    new_version_getter,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.bound import NewVersionGetter, VersionBound


@dataclass(frozen=True)
class _RedundancyCase:
    """One directive judged for redundancy, and the two things that decide whether it is reported.

    `capability` returns a source of the getter that can apply the directive, which is what keeps it from being
    reported.
    """

    marker: Marker
    reason: Reason
    capability: Callable[[NewVersionGetter], Source]


def _dating_every_dependency(get_new_version: NewVersionGetter) -> Source:
    """Return a source of the getter that dates the versions of every dependency."""
    return Source(new_version=get_new_version, dates_versions_of=every_dependency)


def _with_a_yank_getter(get_new_version: NewVersionGetter) -> Source:
    """Return a source of the getter that looks up whether a version was yanked."""
    return Source(yank=lambda _dependency, _version: None, new_version=get_new_version)


def _with_a_project_getter_declaring_archival(get_new_version: NewVersionGetter) -> Source:
    """Return a source of the getter whose project getter declares whether the project is archived."""
    return Source(
        project=ProjectGetter(lambda _dependency: Project(), declares_archival=every_project),
        new_version=get_new_version,
    )


def _with_an_ecosystem(get_new_version: NewVersionGetter) -> Source:
    """Return a source of the getter whose dependencies have an ecosystem, so OSV holds advisories for them."""
    return Source(ecosystem=Ecosystem.PYPI, new_version=get_new_version)


# Each directive a source may be unable to apply, keyed by the text the warning names it with. A marker that sets a
# value is parsed from that text, so it carries the directive the warning names. A marker that holds a scope back
# carries no text, because the scope has one spelling, which the warning spells out.
_REDUNDANT_DIRECTIVES = {
    "ignore[yanked]": _RedundancyCase(Marker(ignored_scopes=Scope.YANKED), Reason.NO_YANK_CONCEPT, _with_a_yank_getter),
    "ignore[archived]": _RedundancyCase(
        Marker(ignored_scopes=Scope.ARCHIVED), Reason.NO_ARCHIVAL_SIGNAL, _with_a_project_getter_declaring_archival
    ),
    "set[vulnerability-level=none]": _RedundancyCase(
        VULNERABILITY_LEVEL_NONE, Reason.NO_VULNERABILITY_REPORTS, _with_an_ecosystem
    ),
    "set[stale-after=90]": _RedundancyCase(STALE_AFTER_90, Reason.NO_STALENESS_DATES, _dating_every_dependency),
    "set[cooldown=30]": _RedundancyCase(COOLDOWN_30, Reason.NO_COOLDOWN_DATES, _dating_every_dependency),
}


_YANKED = DependencyVersion("3.15", yank=Yank(yanked=True))
_IGNORE_YANKED = Marker(ignored_scopes=Scope.YANKED)
_IGNORE_ARCHIVED = Marker(ignored_scopes=Scope.ARCHIVED)

# A project whose newest release is stale under a threshold of 90 days, and not under the default.
_STALE_UNDER_90 = Project(newest=Release("3.16", PAST_COOLDOWN_DATE))

# The logger methods that warn about a project.
_PROJECT_REPORTS = ("stale_dependency", "archived_dependency")


@dataclass(frozen=True)
class _CheckCase[StateT]:
    """What the source answers a check, the reference's marker, and the `Logger` method reporting them, if any.

    `arguments` holds what the report is called with besides the reference.
    """

    state: StateT
    marker: Marker = NO_MARKER
    report: str = ""
    arguments: tuple[object, ...] = ()

    def expected_calls(self, resolved: ResolvedReference) -> list[object]:
        """Return the calls the logger gets for the reference: the report, or none."""
        return [getattr(call, self.report)(resolved, *self.arguments)] if self.report else []


_YANK_CASES = {
    "yank silenced": _CheckCase(Yank(yanked=True), _IGNORE_YANKED, "ignored_warning", (Scope.YANKED, "ignore[yanked]")),
    "ignore[yanked] on a version not yanked": _CheckCase(
        Yank(), _IGNORE_YANKED, "redundant_ignore", (Scope.YANKED, "ignore[yanked]", "3.15")
    ),
    "not yanked": _CheckCase(Yank()),
}

_ARCHIVAL_CASES = {
    "archival silenced": _CheckCase(
        Archival(archived=True), _IGNORE_ARCHIVED, "ignored_warning", (Scope.ARCHIVED, "ignore[archived]")
    ),
    "ignore[archived] on a project not archived": _CheckCase(
        Archival(), _IGNORE_ARCHIVED, "redundant_ignore", (Scope.ARCHIVED, "ignore[archived]", ArchivedSubject.PROJECT)
    ),
    "not archived": _CheckCase(Archival()),
}


class LatestVersionTest(unittest.TestCase):
    """Unit tests for deciding the latest version to update a reference to."""

    def setUp(self) -> None:
        """Use a mock logger and path so log calls can be asserted."""
        super().setUp()
        self.log = Mock()
        self.path = Mock()

    def reference(self, dependency: str = "python", marker: Marker = NO_MARKER) -> Reference:
        """Return the reference the decision is run for, steered by the marker, as the logger is handed it."""
        return Reference(dependency, "3.14", self.path, marker=marker)

    def resolved(self, marker: Marker = NO_MARKER, project: Project = NO_PROJECT) -> ResolvedReference:
        """Return the resolved reference the decision hands the logger, for version 3.15 and the project."""
        reference = self.reference(marker=marker)
        return ResolvedReference.from_reference(reference, release=DependencyVersion("3.15"), project=project)

    def latest_version(
        self,
        marker: Marker = NO_MARKER,
        get_new_version: NewVersionGetter | None = None,
        dependency: str = "python",
        get_project: ProjectGetter | None = None,
    ) -> DependencyVersion | None:
        """Run the decision for the dependency's reference at version 3.14, resolving 3.15 unless overridden."""
        get_new_version = new_version_getter("3.15") if get_new_version is None else get_new_version
        source = Source(new_version=get_new_version, project=get_project)
        return latest_version(self.reference(dependency, marker), source, self.log)

    def assert_resolved(
        self, get_new_version: Mock, version_bound: VersionBound = NO_BOUND, cooldown: int = COOLDOWN.default
    ) -> None:
        """Assert the source was asked once about the reference's pin, under the bound and cooldown given."""
        get_new_version.assert_called_once_with(self.reference().pinned, version_bound, cooldown)

    def test_returns_the_resolved_version(self):
        """Test that the version the getter resolves is returned."""
        self.assertEqual(self.latest_version(), DependencyVersion(version="3.15"))

    @kills.replace(
        resolve_module.latest_version,
        "DependencyVersion(version=reference.current_version)",
        'DependencyVersion(version="")',
        "a source without a getter drops the version the reference names",
    )
    def test_a_source_without_a_getter_offers_no_update(self):
        """Test that a source without a getter leaves the reference on the version it names."""
        latest = latest_version(self.reference(), Source(), self.log)
        self.assertEqual(latest, DependencyVersion(version="3.14"))

    def test_returns_the_resolved_version_even_when_unchanged(self):
        """Test that a resolved version equal to the current one is still returned, not turned into None."""
        latest = self.latest_version(get_new_version=new_version_getter("3.14", DIGEST))
        self.assertEqual(latest, DependencyVersion(version="3.14", sha=DIGEST))

    def test_passes_the_version_bound_to_the_getter(self):
        """Test that the marker's version bound reaches the getter, so the source only picks admitted versions."""
        get_new_version = mock_new_version_getter(DependencyVersion(version="3.14.1"))
        self.latest_version(Marker(version_bound=bound(Verb.ALLOW, "update<3.15")), get_new_version)
        self.assert_resolved(get_new_version, bound(Verb.ALLOW, "update<3.15"))

    def test_ignore_update_passes_a_block_all_bound_to_the_getter(self):
        """Test that a held-back update asks the source to keep the current version rather than resolve an update."""
        get_new_version = mock_new_version_getter(DependencyVersion(version="3.14"))
        self.latest_version(Marker(ignored_scopes=Scope.UPDATE), get_new_version)
        self.assert_resolved(get_new_version, BLOCK_ALL_UPDATES)

    @patch_environ({COOLDOWN.name: "30"})
    def test_passes_the_configured_cooldown_to_the_getter(self):
        """Test that the cooldown the run was configured with reaches the getter, rather than the built-in default."""
        get_new_version = mock_new_version_getter(DependencyVersion(version="3.15"))
        self.latest_version(get_new_version=get_new_version)
        self.assert_resolved(get_new_version, cooldown=30)

    def test_the_markers_cooldown_is_passed_to_the_getter(self):
        """Test that a reference carrying its own cooldown is resolved with that one, not the global one."""
        get_new_version = mock_new_version_getter(DependencyVersion(version="3.15"))
        self.latest_version(COOLDOWN_30, get_new_version)
        self.assert_resolved(get_new_version, cooldown=30)

    def test_warns_about_a_redundant_bound(self):
        """Test that the reference's bound is checked for redundancy against its current version."""
        marker = Marker(version_bound=bound(Verb.IGNORE, "patch-update"))
        self.latest_version(marker)
        self.log.redundant_bound.assert_called_once_with(self.reference(marker=marker), BoundRedundancy.BLOCKS_ALL)

    def test_the_resolved_version_reaches_every_check(self):
        """Test that the resolved version and the project the source reports reach the yank and archival checks."""
        archival = Archival(archived=True)
        source = Source(
            new_version=mock_new_version_getter(_YANKED), project=mock_project_getter(Project(archival=archival))
        )
        latest_version(self.reference(), source, self.log)
        project = Project(archival=archival)
        resolved = ResolvedReference.from_reference(self.reference(), release=_YANKED, project=project)
        self.log.archived_dependency.assert_called_once_with(resolved, archival)
        self.log.yanked_dependency.assert_called_once_with(resolved)

    @patch_environ({STALE_AFTER.name: "30"})
    def test_the_configured_threshold_is_used_for_the_staleness_warning(self):
        """Test that the threshold the run was configured with is used, rather than the built-in default."""
        self.latest_version(get_project=mock_project_getter(_STALE_UNDER_90))
        resolved = self.resolved(project=_STALE_UNDER_90)
        self.log.stale_dependency.assert_called_once_with(resolved, _STALE_UNDER_90.newest, 30)

    @kills.replace(
        resolve_module._warn_if_stale,
        "stale_release(resolved.project, resolved.stale_after)",
        "stale_release(resolved.project, STALE_AFTER.get())",
        "the marker's threshold is ignored, so the run's threshold decides the staleness warning",
    )
    def test_the_markers_threshold_is_used_for_the_staleness_warning(self):
        """Test that a reference carrying its own staleness threshold is judged by that one, not the global one."""
        self.latest_version(STALE_AFTER_90, get_project=mock_project_getter(_STALE_UNDER_90))
        resolved = self.resolved(STALE_AFTER_90, _STALE_UNDER_90)
        self.log.stale_dependency.assert_called_once_with(resolved, _STALE_UNDER_90.newest, 90)

    def test_a_marker_silencing_a_warning_still_returns_the_update(self):
        """Test that a marker silencing the yank and archival warnings leaves the update in place."""
        marker = Marker(ignored_scopes=Scope.YANKED | Scope.ARCHIVED)
        latest = self.latest_version(marker, mock_new_version_getter(_YANKED))
        self.assertEqual(latest, _YANKED)

    def test_warns_about_a_directive_the_source_cannot_apply(self):
        """Test that a directive is reported as redundant when the source cannot apply it, and the update stands."""
        for directive, case in _REDUNDANT_DIRECTIVES.items():
            with self.subTest(directive=directive):
                self.log.reset_mock()  # Judge each case on the calls of its own run.
                latest = self.latest_version(case.marker)
                self.assertEqual(latest, DependencyVersion(version="3.15"))
                reference = self.reference(marker=case.marker)
                self.log.redundant_directive.assert_called_once_with(reference, directive, case.reason)

    def test_no_redundant_directive_when_the_source_can_apply_it(self):
        """Test that a directive is not reported when the source has the capability the directive needs."""
        for directive, case in _REDUNDANT_DIRECTIVES.items():
            with self.subTest(directive=directive):
                self.log.reset_mock()  # Judge each case on the calls of its own run.
                source = case.capability(new_version_getter("3.15"))
                latest = latest_version(self.reference(marker=case.marker), source, self.log)
                self.assertEqual(latest, DependencyVersion(version="3.15"))
                self.log.redundant_directive.assert_not_called()

    def test_the_capability_is_judged_per_reference(self):
        """Test that a source dating some of its dependencies only is judged by the reference the marker sits on.

        One case per warning the publication date decides: the `cooldown` item's and the `stale` directive's.
        """

        def dates_the_releases_of(pinned: PinnedDependency) -> bool:
            """Return whether the source dates the pinned dependency's releases, which it does for `node` alone."""
            return pinned.name == "node"

        source = Source(new_version=new_version_getter("3.15"), dates_versions_of=dates_the_releases_of)
        for directive in ("set[cooldown=30]", "set[stale-after=90]"):
            case = _REDUNDANT_DIRECTIVES[directive]
            for dependency, reported in (("node", False), ("python", True)):
                with self.subTest(directive=directive, dependency=dependency):
                    self.log.reset_mock()  # Judge each reference on the calls of its own run.
                    reference = self.reference(dependency, case.marker)
                    latest_version(reference, source, self.log)
                    expected = [call(reference, directive, case.reason)] if reported else []
                    self.assertEqual(self.log.redundant_directive.call_args_list, expected)

    def test_warns_about_a_redundant_floating_pin_directive(self):
        """Test that `allow[floating-pin]` is reported as redundant when the reference's pin does not float."""
        marker = Marker(allowed_scopes=Scope.FLOATING_PIN, raw="allow[floating-pin]")
        latest = self.latest_version(marker)
        self.log.redundant_directive.assert_called_once_with(
            self.reference(marker=marker), "allow[floating-pin]", Reason.PIN_NOT_FLOATING
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
                self.assertEqual(self.latest_version(marker, mock_new_version_getter(release)), release)
                self.log.redundant_directive.assert_not_called()

    def test_warns_about_a_floating_pin_directive_on_a_held_back_reference(self):
        """Test that `allow[floating-pin]` on a floating tag `ignore[update]` holds back is reported as redundant."""
        floating = DependencyVersion(version="3.14.7", sha=DIGEST, floating=FloatingPin.RESOLVED)
        marker = Marker(ignored_scopes=Scope.UPDATE, allowed_scopes=Scope.FLOATING_PIN)
        self.assertIsNone(self.latest_version(marker, mock_new_version_getter(floating)))
        self.log.redundant_directive.assert_called_once_with(
            self.reference(marker=marker), "allow[floating-pin]", Reason.NEVER_PINNED
        )

    @kills.replace(
        resolve_module._reported_project,
        "return _archival_alone(source.project, reference.dependency)",
        "return NO_PROJECT",
        "an image on a tag Docker Hub does not serve is not checked for archival",
        case="a version not served",
    )
    @kills.delete(
        resolve_module._reported_project,
        " or latest.unserved is Unserved.REPOSITORY",
        "an image whose repository Docker Hub does not serve is checked for staleness and archival all the same",
        case="a repository not served",
    )
    @kills.replace(
        resolve_module._reported_project,
        "if latest.unserved is Unserved.VERSION:",
        "if latest.unserved is not None:",
        "a repository that does not tag a release is not checked for staleness",
        case="a repository that does not tag a release",
    )
    def test_the_project_reported_depends_on_what_the_source_serves(self):
        """Test that a repository not served drops the project, and a version not served drops its newest release."""
        archival = Archival(archived=True, subject=ArchivedSubject.REPOSITORY)
        project = Project(newest=Release("3.16", STALE_DATE), archival=archival)
        cases = {
            "a repository not served": (Unserved.REPOSITORY, []),
            "a version not served": (Unserved.VERSION, ["archived_dependency"]),
            "a repository that does not tag a release": (
                Unserved.RELEASES,
                ["stale_dependency", "archived_dependency"],
            ),
        }
        for case, (unserved, expected) in cases.items():
            with self.subTest(case=case):
                self.log.reset_mock()  # Judge each case on the calls of its own run.
                served = DependencyVersion(version="3.15", unserved=unserved)
                source = Source(
                    project=ProjectGetter(Mock(return_value=project), declares_archival=every_project),
                    new_version=mock_new_version_getter(served),
                )
                latest_version(self.reference(), source, self.log)
                reports = [name for name, *_ in self.log.method_calls if name in _PROJECT_REPORTS]
                self.assertEqual(reports, expected)

    @kills.force_false(
        resolve_module._reported_project,
        "not _project_is_checked(source.project, reference)",
        "a run with the check off asks the source about a project it does not check",
        case="no threshold of its own",
    )
    @kills.delete(
        resolve_module._project_is_checked,
        "reference.marker.stale.value is not None or ",
        "a marker's threshold goes unchecked in a run with the check off",
        case="a threshold of its own",
    )
    def test_a_source_declaring_no_archival_is_asked_only_if_a_marker_sets_a_threshold(self):
        """Test that with the check off, the source is asked about the project only where a marker sets a threshold.

        The source does not declare archival, which would have it asked whatever the marker says.
        """
        for case, (marker, calls) in {
            "no threshold of its own": (NO_MARKER, []),
            "a threshold of its own": (STALE_AFTER_90, [call("python")]),
        }.items():
            with self.subTest(case=case):
                get_project = mock_project_getter()
                source = Source(project=get_project, new_version=new_version_getter("3.15"))
                with staleness_disabled:
                    latest_version(self.reference(marker=marker), source, self.log)
                self.assertEqual(get_project.call_args_list, calls)

    def test_ignore_update_returns_none(self):
        """Test that `ignore[update]` holds back the update, after the staleness check has still run."""
        get_project = mock_project_getter(STALE_PROJECT)
        self.assertIsNone(self.latest_version(Marker(ignored_scopes=Scope.UPDATE), get_project=get_project))
        self.log.stale_dependency.assert_called_once()


class ReportProjectChecksTest(unittest.TestCase):
    """Unit tests for the checks a reference the run resolves no update for still gets."""

    def setUp(self) -> None:
        """Use a mock logger and path so log calls can be asserted."""
        super().setUp()
        self.log = Mock()
        self.path = Mock()

    def reference(self, marker: Marker = NO_MARKER) -> Reference:
        """Return the reference without an exact pin the checks are run for, steered by the marker."""
        return Reference("humanize", "", self.path, marker=marker)

    def report_project_checks(self, project: Project, marker: Marker = NO_MARKER) -> ResolvedReference:
        """Run the checks over the project, steered by the marker, and return the reference checked."""
        report_project_checks(self.reference(marker), self.log, mock_project_getter(project))
        return resolved_reference("humanize", self.path, marker=marker, project=project)

    def test_reports_both_checks_for_the_project(self):
        """Test that what the getter answers reaches the archival report and the staleness report alike."""
        archival, newest = Archival(archived=True), Release("4.15.0", STALE_DATE)
        resolved = self.report_project_checks(Project(newest=newest, archival=archival))
        self.log.archived_dependency.assert_called_once_with(resolved, archival)
        self.log.stale_dependency.assert_called_once_with(resolved, newest, STALE_AFTER.default)

    @kills.force_false(
        resolve_module._report_archival,
        "archival.archived and directive",
        "an ignore[archived] no longer silences the archival warning",
        case="archival silenced",
    )
    @kills.replace(
        resolve_module._report_archival,
        "elif directive:",
        "elif False:",
        "an ignore[archived] that silences nothing goes unreported, so it outlives the archival it was written for",
        case="ignore[archived] on a project not archived",
    )
    @kills.replace(
        resolve_module._report_archival,
        "elif directive:",
        "else:",
        "an unmarked project that its source keeps active gets a redundancy warning for a directive it does not carry",
        case="not archived",
    )
    def test_reports_the_archival_marker_or_nothing(self):
        """Test that a silenced archival is reported as silenced, and that a marker silencing nothing is redundant."""
        for name, case in _ARCHIVAL_CASES.items():
            with self.subTest(case=name):
                self.log.reset_mock()  # Judge each case on the calls of its own run.
                resolved = self.report_project_checks(Project(archival=case.state), case.marker)
                self.assertEqual(self.log.method_calls, case.expected_calls(resolved))

    def test_reports_nothing_where_the_archival_is_unknown(self):
        """Test that an `ignore[archived]` on a project whose source did not say it archived it is not reported."""
        self.report_project_checks(NO_PROJECT, _IGNORE_ARCHIVED)
        self.assertEqual(self.log.method_calls, [])

    def test_a_source_reporting_no_archival_is_not_asked_with_the_staleness_check_switched_off(self):
        """Test that switching the staleness check off leaves such a source unasked, so it costs no request."""
        get_project = mock_project_getter()
        with staleness_disabled:
            report_project_checks(self.reference(), self.log, get_project)
        get_project.assert_not_called()
        self.assertEqual(self.log.method_calls, [])


class ReportYankTest(unittest.TestCase):
    """Unit tests for reporting the yank of the version the run leaves a reference on."""

    @staticmethod
    def report_yank(yank: Yank | None, marker: Marker) -> tuple[ResolvedReference, Mock]:
        """Report the yank of version 3.15, steered by the marker, and return the reference and the logger."""
        log = Mock()
        resolved = resolved_reference("python", Mock(), DependencyVersion("3.15", yank=yank), marker=marker)
        report_yank(resolved, log)
        return resolved, log

    @kills.force_false(
        resolve_module.report_yank,
        "yank.yanked and directive",
        "an ignore[yanked] no longer silences the yank warning",
        case="yank silenced",
    )
    @kills.replace(
        resolve_module.report_yank,
        "elif directive:",
        "elif False:",
        "an ignore[yanked] that silences nothing goes unreported, so it outlives the yank it was written for",
        case="ignore[yanked] on a version not yanked",
    )
    @kills.replace(
        resolve_module.report_yank,
        "elif directive:",
        "else:",
        "an unmarked version that its source kept gets a redundancy warning for a directive it does not carry",
        case="not yanked",
    )
    def test_reports_the_marker_or_nothing(self):
        """Test that a silenced yank is reported as silenced, and that a marker silencing nothing is redundant."""
        for name, case in _YANK_CASES.items():
            with self.subTest(case=name):
                resolved, log = self.report_yank(case.state, case.marker)
                self.assertEqual(log.method_calls, case.expected_calls(resolved))

    def test_reports_nothing_where_the_yank_is_unknown(self):
        """Test that an `ignore[yanked]` on a version whose source did not say whether it was yanked is not reported."""
        _resolved, log = self.report_yank(None, _IGNORE_YANKED)
        self.assertEqual(log.method_calls, [])
