"""Unit tests for the shared reference-update decision."""

import unittest
from typing import TYPE_CHECKING
from unittest.mock import Mock, call

from update_time.domain.bound import BLOCK_ALL_UPDATES, NO_BOUND, Verb
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion
from update_time.domain.marker import Marker, Threshold
from update_time.domain.publication import publication_date_reporting
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.yank import yank_reporting
from update_time.references.resolve import latest_version

from tests.helpers import patch_environ
from tests.update_time.fixtures import DIGEST
from tests.update_time.helpers import bound, new_version_getter, reference, resolved_reference

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.reference import Reference, ResolvedReference

# The threshold `ignore[stale<90]` parses to, carrying the directive the parser sets alongside every value it reads.
_STALE_THRESHOLD = Threshold(value=90, directive="ignore[stale<90]")


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
        self.latest_version(Marker(ignore_update=True), get_new_version)
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

    def test_reports_staleness(self):
        """Test that the resolved version is reported on for staleness, against the global threshold by default."""
        self.latest_version()
        self.log.report_staleness.assert_called_once_with(self.resolved(), Marker(), STALE_AFTER.default)

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

    def test_warns_about_yank(self):
        """Test that the resolved version is checked for a yank."""
        self.latest_version()
        self.log.warn_if_yanked.assert_called_once_with(self.resolved())

    def test_ignore_stale_still_returns_the_update(self):
        """Test that `ignore[stale]` leaves the update in place, and reaches the logger to report its hold-back."""
        marker = Marker(ignore_stale=True, raw="ignore[stale]")
        latest = self.latest_version(marker)
        self.assertEqual(latest, DependencyVersion(version="3.15"))
        self.log.report_staleness.assert_called_once_with(self.resolved(), marker, STALE_AFTER.default)

    def test_ignore_yanked_skips_the_yank_warning(self):
        """Test that `ignore[yanked]` holds back the yank check while the update is still returned."""
        latest = self.latest_version(Marker(ignore_yanked=True))
        self.assertEqual(latest, DependencyVersion(version="3.15"))
        self.log.warn_if_yanked.assert_not_called()

    def test_ignore_yanked_logs_the_held_back_yank_warning(self):
        """Test that `ignore[yanked]` hands the resolved version to the logger, which reports what it held back."""
        marker = Marker(ignore_yanked=True, raw="ignore[yanked]")
        self.latest_version(marker)
        self.log.ignored_yank.assert_called_once_with(self.resolved(), marker)

    def test_warns_about_a_redundant_yank_scope(self):
        """Test that `ignore[yanked]` is reported as redundant when the source never reports a yank."""
        marker = Marker(ignore_yanked=True, raw="ignore[yanked]")
        self.latest_version(marker)
        self.log.redundant_yank_scope.assert_called_once_with(self.reference(), marker)

    def test_no_redundant_yank_scope_when_the_source_reports_yanks(self):
        """Test that `ignore[yanked]` is not reported as redundant when the source can report a yank."""
        get_new_version = yank_reporting(new_version_getter("3.15"))
        self.latest_version(Marker(ignore_yanked=True), get_new_version)
        self.log.redundant_yank_scope.assert_not_called()

    def test_warns_about_a_redundant_cooldown_item(self):
        """Test that a `cooldown` item is reported as redundant when the source dates none of its versions."""
        marker = Marker(cooldown=Threshold(value=30), raw="ignore[cooldown<30]")
        latest = self.latest_version(marker)
        self.log.redundant_cooldown_item.assert_called_once_with(self.reference(), marker)
        self.assertEqual(latest, DependencyVersion(version="3.15"))

    def test_no_redundant_cooldown_item_when_the_source_dates_its_versions(self):
        """Test that a `cooldown` item is not reported when the source dates its versions."""
        get_new_version = publication_date_reporting(new_version_getter("3.15"))
        self.latest_version(Marker(cooldown=Threshold(value=30)), get_new_version)
        self.log.redundant_cooldown_item.assert_not_called()

    def test_the_capability_is_judged_per_reference(self):
        """Test that a getter dating some of its dependencies only is judged by the reference the marker sits on.

        One case per warning the publication date decides: the `cooldown` item's and the `stale` directive's.
        """

        def dates_the_releases_of(dependency: str) -> bool:
            """Return whether the source dates the dependency's releases, which it does for `node` alone."""
            return dependency == "node"

        get_new_version = publication_date_reporting(new_version_getter("3.15"), when=dates_the_releases_of)
        warnings = (
            (Marker(cooldown=Threshold(value=30), raw="ignore[cooldown<30]"), self.log.redundant_cooldown_item),
            (Marker(stale=_STALE_THRESHOLD, raw="ignore[stale<90]"), self.log.redundant_stale_source),
        )
        for marker, warn in warnings:
            for dependency, reported in (("node", False), ("python", True)):
                with self.subTest(marker=marker.raw, dependency=dependency):
                    self.log.reset_mock()  # Judge each reference on the calls of its own run.
                    self.latest_version(marker, get_new_version, dependency)
                    expected = [call(self.reference(dependency), marker)] if reported else []
                    self.assertEqual(warn.call_args_list, expected)

    def test_no_redundant_cooldown_item_when_the_marker_sets_no_cooldown(self):
        """Test that a marker is not reported when it sets no cooldown, carrying no item or an inverted one."""
        markers = {
            "no cooldown item": Marker(),
            "inverted cooldown item": Marker(
                cooldown=Threshold(inverted_item="cooldown>=30"), raw="ignore[cooldown>=30]"
            ),
        }
        for case, marker in markers.items():
            with self.subTest(case=case):
                self.latest_version(marker)
                self.log.redundant_cooldown_item.assert_not_called()

    def test_warns_about_a_redundant_stale_item(self):
        """Test that a `stale` item is reported as redundant when the source dates none of its versions."""
        marker = Marker(stale=_STALE_THRESHOLD, raw="ignore[stale<90]")
        latest = self.latest_version(marker)
        self.log.redundant_stale_source.assert_called_once_with(self.reference(), marker)
        self.assertEqual(latest, DependencyVersion(version="3.15"))

    def test_no_redundant_stale_item_when_the_source_dates_its_versions(self):
        """Test that a `stale` item is not reported when the source dates its versions."""
        get_new_version = publication_date_reporting(new_version_getter("3.15"))
        self.latest_version(Marker(stale=Threshold(value=90)), get_new_version)
        self.log.redundant_stale_source.assert_not_called()

    def test_warns_about_a_redundant_stale_scope(self):
        """Test that a bare `ignore[stale]` is reported as redundant when the source dates none of its versions."""
        marker = Marker(ignore_stale=True, raw="ignore[stale]")
        self.latest_version(marker)
        self.log.redundant_stale_source.assert_called_once_with(self.reference(), marker)

    def test_no_redundant_stale_item_when_the_marker_sets_no_threshold(self):
        """Test that a marker is not reported when it sets no threshold, carrying no item or an inverted one."""
        markers = {
            "no stale item": Marker(),
            "inverted stale item": Marker(stale=Threshold(inverted_item="stale>=90"), raw="ignore[stale>=90]"),
        }
        for case, marker in markers.items():
            with self.subTest(case=case):
                self.latest_version(marker)
                self.log.redundant_stale_source.assert_not_called()

    def test_ignore_update_returns_none(self):
        """Test that `ignore[update]` holds back the update, after the staleness check has still run."""
        self.assertIsNone(self.latest_version(Marker(ignore_update=True)))
        self.log.report_staleness.assert_called_once()
