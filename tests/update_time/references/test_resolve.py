"""Unit tests for the shared reference-update decision."""

import unittest
from typing import TYPE_CHECKING
from unittest.mock import Mock

from update_time.domain.bound import BLOCK_ALL_UPDATES, NO_BOUND, Verb
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.marker import Marker, Threshold
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.version import DependencyVersion, Reference
from update_time.domain.yank import yank_reporting
from update_time.references.resolve import latest_version

from tests.helpers import patch_environ
from tests.update_time.fixtures import DIGEST
from tests.update_time.helpers import bound, new_version_getter

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter


class LatestVersionTest(unittest.TestCase):
    """Unit tests for deciding the latest version to update a reference to."""

    def setUp(self) -> None:
        """Use a mock logger and path so log calls can be asserted."""
        super().setUp()
        self.log = Mock()
        self.path = Mock()

    def latest_version(
        self, marker: Marker | None = None, get_new_version: NewVersionGetter | None = None
    ) -> DependencyVersion | None:
        """Run the decision for a `python` reference at version 3.14, resolving 3.15 unless overridden."""
        get_new_version = new_version_getter("3.15") if get_new_version is None else get_new_version
        return latest_version(
            Reference("python", "3.14"), get_new_version, Marker() if marker is None else marker, self.path, self.log
        )

    def test_returns_the_resolved_version(self):
        """Test that the version the getter resolves is returned."""
        self.assertEqual(self.latest_version(), DependencyVersion(version="3.15"))

    def test_returns_the_resolved_version_even_when_unchanged(self):
        """Test that a resolved version equal to the current one is still returned, not turned into None.

        An unchanged version may still carry a newer digest worth pinning, so the decision resolves it rather than
        collapsing it to None.
        """
        latest = self.latest_version(get_new_version=new_version_getter("3.14", DIGEST))
        self.assertEqual(latest, DependencyVersion(version="3.14", sha=DIGEST))

    def test_passes_the_version_bound_to_the_getter(self):
        """Test that the marker's version bound reaches the getter, so the source only picks admitted versions."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.14.1"))
        self.latest_version(Marker(version_bound=bound(Verb.ALLOW, "update<3.15")), get_new_version)
        get_new_version.assert_called_once_with("python", "3.14", bound(Verb.ALLOW, "update<3.15"), COOLDOWN.default)

    def test_ignore_update_passes_a_block_all_bound_to_the_getter(self):
        """Test that a held-back update asks the source to keep the current version rather than resolve an update.

        The source then reports on the version the reference stays on, so a frozen pin that was yanked is detected.
        """
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
        self.log.warn_if_redundant_bound.assert_called_once_with("python", marker, "3.14", self.path)

    def test_warns_about_staleness(self):
        """Test that the resolved version is checked for staleness, against the global threshold by default."""
        self.latest_version()
        self.log.warn_if_stale.assert_called_once_with(
            "python", DependencyVersion(version="3.15"), self.path, STALE_AFTER.default
        )

    @patch_environ({STALE_AFTER.name: "30"})
    def test_the_configured_threshold_is_used_for_the_staleness_warning(self):
        """Test that the threshold the run was configured with is used, rather than the built-in default."""
        self.latest_version()
        self.log.warn_if_stale.assert_called_once_with("python", DependencyVersion(version="3.15"), self.path, 30)

    def test_the_markers_threshold_is_used_for_the_staleness_warning(self):
        """Test that a reference carrying its own staleness threshold is judged by that one, not the global one."""
        self.latest_version(Marker(stale=Threshold(value=90)))
        self.log.warn_if_stale.assert_called_once_with("python", DependencyVersion(version="3.15"), self.path, 90)

    def test_warns_about_an_inverted_stale_item(self):
        """Test that a `stale` item comparing the wrong way is reported, and the global threshold is used."""
        marker = Marker(stale=Threshold(inverted_item="stale>=90"), raw="ignore[stale>=90]")
        self.latest_version(marker)
        self.log.inverted_stale_item.assert_called_once_with("python", "stale>=90", self.path)
        self.log.warn_if_stale.assert_called_once_with(
            "python", DependencyVersion(version="3.15"), self.path, STALE_AFTER.default
        )

    def test_warns_about_an_inverted_cooldown_item(self):
        """Test that a `cooldown` item comparing the wrong way is reported, and the global cooldown is used."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.15"))
        marker = Marker(cooldown=Threshold(inverted_item="cooldown>=30"), raw="ignore[cooldown>=30]")
        self.latest_version(marker, get_new_version)
        self.log.inverted_cooldown_item.assert_called_once_with("python", "cooldown>=30", self.path)
        get_new_version.assert_called_once_with("python", "3.14", NO_BOUND, COOLDOWN.default)

    def test_warns_about_an_inverted_vulnerability_item(self):
        """Test that a `vulnerable` item comparing the wrong way is reported."""
        marker = Marker(vulnerable=Threshold(inverted_item="vulnerable>=high"), raw="ignore[vulnerable>=high]")
        self.latest_version(marker)
        self.log.inverted_vulnerable_item.assert_called_once_with("python", "vulnerable>=high", self.path)

    def test_warns_about_yank(self):
        """Test that the resolved version is checked for a yank."""
        self.latest_version()
        self.log.warn_if_yanked.assert_called_once_with("python", DependencyVersion(version="3.15"), self.path)

    def test_ignore_stale_skips_the_staleness_warning(self):
        """Test that `ignore[stale]` holds back the staleness check while the update is still returned."""
        latest = self.latest_version(Marker(ignore_stale=True))
        self.assertEqual(latest, DependencyVersion(version="3.15"))
        self.log.warn_if_stale.assert_not_called()

    def test_ignore_stale_logs_the_held_back_staleness_warning(self):
        """Test that `ignore[stale]` hands the resolved version to the logger, which reports what it held back."""
        marker = Marker(ignore_stale=True, raw="ignore[stale]")
        self.latest_version(marker)
        self.log.ignored_staleness.assert_called_once_with(
            "python", DependencyVersion(version="3.15"), marker, self.path, STALE_AFTER.default
        )

    def test_the_markers_threshold_is_used_for_the_held_back_staleness_warning(self):
        """Test that the hold-back is judged by the same threshold as the warning it stands in for."""
        marker = Marker(ignore_stale=True, stale=Threshold(value=90), raw="ignore[stale] ignore[stale<90]")
        self.latest_version(marker)
        self.log.ignored_staleness.assert_called_once_with(
            "python", DependencyVersion(version="3.15"), marker, self.path, 90
        )

    def test_ignore_yanked_skips_the_yank_warning(self):
        """Test that `ignore[yanked]` holds back the yank check while the update is still returned."""
        latest = self.latest_version(Marker(ignore_yanked=True))
        self.assertEqual(latest, DependencyVersion(version="3.15"))
        self.log.warn_if_yanked.assert_not_called()

    def test_ignore_yanked_logs_the_held_back_yank_warning(self):
        """Test that `ignore[yanked]` hands the resolved version to the logger, which reports what it held back."""
        marker = Marker(ignore_yanked=True, raw="ignore[yanked]")
        self.latest_version(marker)
        self.log.ignored_yank.assert_called_once_with("python", DependencyVersion(version="3.15"), marker, self.path)

    def test_warns_about_a_redundant_yank_scope(self):
        """Test that `ignore[yanked]` is reported as redundant when the source never reports a yank."""
        marker = Marker(ignore_yanked=True, raw="ignore[yanked]")
        self.latest_version(marker)
        self.log.redundant_yank_scope.assert_called_once_with("python", marker, self.path)

    def test_no_redundant_yank_scope_when_the_source_reports_yanks(self):
        """Test that `ignore[yanked]` is not reported as redundant when the source can report a yank."""
        get_new_version = yank_reporting(new_version_getter("3.15"))
        self.latest_version(Marker(ignore_yanked=True), get_new_version)
        self.log.redundant_yank_scope.assert_not_called()

    def test_ignore_update_returns_none(self):
        """Test that `ignore[update]` holds back the update, after the staleness check has still run."""
        self.assertIsNone(self.latest_version(Marker(ignore_update=True)))
        self.log.warn_if_stale.assert_called_once()
