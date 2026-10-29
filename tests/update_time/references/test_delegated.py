"""Unit tests for the checks a delegated dependency gets."""

import unittest
from pathlib import Path
from unittest.mock import Mock

from update_time.domain.archival import archival_reporting
from update_time.domain.dependency import DependencyVersion
from update_time.domain.staleness import STALE_AFTER
from update_time.markers.marker import Marker
from update_time.markers.reference import SteeredReference
from update_time.primitives.location import Location
from update_time.references import delegated as delegated_module
from update_time.references.delegated import warn_about_projects

from tests.mutation import Mutation, kills
from tests.update_time.fixtures import STALE_AFTER_90
from tests.update_time.helpers import resolved_reference, staleness_disabled
from tests.update_time.references.helpers import mock_reference_resolver


class WarnAboutProjectsTest(unittest.TestCase):
    """Unit tests for the project pass the delegating updaters share."""

    def setUp(self):
        """Create one file's references, a resolved release, and a mock logger for the tests to share."""
        self.location = Location(Path("pyproject.toml"), 4)
        self.declared = [SteeredReference("humanize", "", self.location)]
        self.release = DependencyVersion(version="1.0.0")
        self.log = Mock()

    def test_reports_each_resolved_reference(self):
        """Test that each resolved reference gets both project checks, with a marker holding nothing back."""
        resolved = resolved_reference("humanize", self.location, self.release)
        projects = mock_reference_resolver(resolved)
        warn_about_projects([self.declared], projects, self.log)
        projects.assert_called_once_with(self.declared)
        self.log.report_staleness.assert_called_once_with(resolved, STALE_AFTER.default)
        self.log.report_archival.assert_called_once_with(resolved, Marker())

    def test_a_resolver_reporting_no_archival_is_not_asked_with_the_staleness_check_switched_off(self):
        """Test that a threshold of 0 skips the pass, so such a resolver never runs and makes no request."""
        projects = mock_reference_resolver(resolved_reference("humanize", self.location, self.release))
        with staleness_disabled:
            warn_about_projects([self.declared], projects, self.log)
        projects.assert_not_called()
        self.log.report_staleness.assert_not_called()
        self.log.report_archival.assert_not_called()

    @kills(
        Mutation(
            delegated_module._checked_references,
            "project_is_checked(projects, reference.dependency, reference.marker)",
            "project_is_checked(projects, reference.dependency, type(reference.marker)())",
            "a reference's own threshold does not reach the gate, so a run with the check off looks it up for none",
        )
    )
    def test_a_resolver_reporting_no_archival_is_asked_about_a_reference_with_a_threshold_of_its_own(self):
        """Test that a run with the staleness check off asks about the references setting a threshold of their own."""
        location = Location(self.location.path, 5)
        reference_with_a_threshold = SteeredReference("rich", "", location, marker=STALE_AFTER_90)
        resolved = resolved_reference("rich", location, self.release, marker=STALE_AFTER_90)
        projects = mock_reference_resolver(resolved)
        with staleness_disabled:
            warn_about_projects([[*self.declared, reference_with_a_threshold]], projects, self.log)
        projects.assert_called_once_with([reference_with_a_threshold])
        self.log.report_staleness.assert_called_once_with(resolved, 90)

    def test_asks_about_the_dependency_the_resolver_is_about_to_resolve(self):
        """Test that the resolver's archival capability is asked about one dependency, not about a batch of them."""
        subjects: list[object] = []

        def record(subject: object) -> bool:
            """Record the subject the capability is asked about, and answer that this resolver reports none."""
            subjects.append(subject)
            return False

        projects = archival_reporting(mock_reference_resolver(), when=record)
        with staleness_disabled:
            warn_about_projects([self.declared], projects, self.log)
        self.assertEqual(subjects, ["humanize"])
        projects.assert_not_called()
