"""Unit tests for the checks a delegated dependency gets."""

import unittest
from pathlib import Path
from unittest.mock import Mock

from update_time.domain.archival import archival_reporting
from update_time.domain.dependency import DependencyVersion
from update_time.domain.staleness import STALE_AFTER
from update_time.markers.marker import Marker
from update_time.primitives.location import Location
from update_time.references.delegated import warn_about_projects

from tests.update_time.helpers import resolved_reference, staleness_disabled
from tests.update_time.references.helpers import mock_reference_resolver


class WarnAboutProjectsTest(unittest.TestCase):
    """Unit tests for the project pass the delegating updaters share."""

    def setUp(self):
        """Create a file, a location in it, a resolved release, and a mock logger for the tests to share."""
        self.file = Path("pyproject.toml")
        self.location = Location(self.file, 4)
        self.release = DependencyVersion(version="1.0.0")
        self.log = Mock()

    def test_reports_each_resolved_reference(self):
        """Test that each resolved reference gets both project checks, with a marker holding nothing back."""
        resolved = resolved_reference("humanize", self.location, self.release)
        projects = mock_reference_resolver(resolved)
        warn_about_projects([self.file], projects, self.log)
        projects.assert_called_once_with(self.file)
        self.log.report_staleness.assert_called_once_with(resolved, Marker(), STALE_AFTER.default)
        self.log.report_archival.assert_called_once_with(resolved, Marker())

    def test_disabled_skips_a_resolver_that_reports_no_archival(self):
        """Test that a threshold of 0 skips the pass, so such a resolver never runs and makes no request."""
        projects = mock_reference_resolver(resolved_reference("humanize", self.location, self.release))
        with staleness_disabled:
            warn_about_projects([self.file], projects, self.log)
        projects.assert_not_called()
        self.log.report_staleness.assert_not_called()
        self.log.report_archival.assert_not_called()

    def test_asks_about_the_file_the_resolver_is_about_to_read(self):
        """Test that the resolver's archival capability is asked about the file, not about the list holding it."""
        subjects: list[object] = []

        def record(subject: object) -> bool:
            """Record the subject the capability is asked about, and answer that this resolver reports none."""
            subjects.append(subject)
            return False

        projects = archival_reporting(mock_reference_resolver(), when=record)
        with staleness_disabled:
            warn_about_projects([self.file], projects, self.log)
        self.assertEqual(subjects, [self.file])
        projects.assert_not_called()
