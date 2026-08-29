"""Unit tests for the checks a delegated dependency gets."""

import unittest
from pathlib import Path
from unittest.mock import Mock

from update_time.domain.dependency import DependencyVersion
from update_time.domain.staleness import STALE_AFTER
from update_time.markers.marker import Marker
from update_time.primitives.location import Location
from update_time.references.delegated import warn_about_stale_dependencies

from tests.update_time.helpers import resolved_reference, staleness_disabled


class WarnAboutStaleDependenciesTest(unittest.TestCase):
    """Unit tests for the staleness pass the delegating updaters share."""

    def setUp(self):
        """Create a file, a location in it, a resolved release, and a mock logger for the tests to share."""
        self.file = Path("pyproject.toml")
        self.location = Location(self.file, 4)
        self.release = DependencyVersion(version="1.0.0")
        self.log = Mock()

    def test_reports_each_resolved_release(self):
        """Test that each resolved reference is reported, with a marker holding nothing back and the threshold."""
        resolved = resolved_reference("humanize", self.location, self.release)
        newest_releases = Mock(return_value=[resolved])
        warn_about_stale_dependencies([self.file], newest_releases, self.log)
        newest_releases.assert_called_once_with(self.file)
        self.log.report_staleness.assert_called_once_with(resolved, Marker(), STALE_AFTER.default)

    def test_disabled(self):
        """Test that a threshold of 0 skips the pass entirely, so the resolver never runs and makes no request."""
        newest_releases = Mock(return_value=[resolved_reference("humanize", self.location, self.release)])
        with staleness_disabled:
            warn_about_stale_dependencies([self.file], newest_releases, self.log)
        newest_releases.assert_not_called()
        self.log.report_staleness.assert_not_called()
