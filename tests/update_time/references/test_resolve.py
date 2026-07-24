"""Unit tests for the shared reference-update decision."""

import unittest
from typing import TYPE_CHECKING
from unittest.mock import Mock

from update_time.domain.bound import BLOCK_ALL_UPDATES, Verb
from update_time.domain.marker import Marker
from update_time.domain.version import DependencyVersion, Reference
from update_time.references.resolve import latest_version

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
        get_new_version.assert_called_once_with("python", "3.14", bound(Verb.ALLOW, "update<3.15"))

    def test_ignore_update_passes_a_block_all_bound_to_the_getter(self):
        """Test that a held-back update asks the source to keep the current version rather than resolve an update.

        The source then reports on the version the reference stays on, so a frozen pin that was yanked is detected.
        """
        get_new_version = Mock(return_value=DependencyVersion(version="3.14"))
        self.latest_version(Marker(ignore_update=True), get_new_version)
        get_new_version.assert_called_once_with("python", "3.14", BLOCK_ALL_UPDATES)

    def test_warns_about_a_redundant_bound(self):
        """Test that the reference's bound is checked for redundancy against its current version."""
        marker = Marker(version_bound=bound(Verb.IGNORE, "patch-update"))
        self.latest_version(marker)
        self.log.warn_if_redundant_bound.assert_called_once_with("python", marker, "3.14", self.path)

    def test_warns_about_staleness(self):
        """Test that the resolved version is checked for staleness."""
        self.latest_version()
        self.log.warn_if_stale.assert_called_once_with("python", DependencyVersion(version="3.15"), self.path)

    def test_warns_about_yank(self):
        """Test that the resolved version is checked for a yank."""
        self.latest_version()
        self.log.warn_if_yanked.assert_called_once_with("python", DependencyVersion(version="3.15"), self.path)

    def test_ignore_stale_skips_the_staleness_warning(self):
        """Test that `ignore[stale]` holds back the staleness check while the update is still returned."""
        latest = self.latest_version(Marker(ignore_stale=True))
        self.assertEqual(latest, DependencyVersion(version="3.15"))
        self.log.warn_if_stale.assert_not_called()

    def test_ignore_yanked_skips_the_yank_warning(self):
        """Test that `ignore[yanked]` holds back the yank check while the update is still returned."""
        latest = self.latest_version(Marker(ignore_yanked=True))
        self.assertEqual(latest, DependencyVersion(version="3.15"))
        self.log.warn_if_yanked.assert_not_called()

    def test_ignore_update_returns_none(self):
        """Test that `ignore[update]` holds back the update, after the staleness check has still run."""
        self.assertIsNone(self.latest_version(Marker(ignore_update=True)))
        self.log.warn_if_stale.assert_called_once()
