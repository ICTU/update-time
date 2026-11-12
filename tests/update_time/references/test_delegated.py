"""Unit tests for the checks a delegated dependency gets."""

import unittest
from pathlib import Path
from unittest.mock import Mock

from update_time.domain.reference import Reference
from update_time.domain.source import Source
from update_time.domain.staleness import STALE_AFTER
from update_time.primitives.location import Location
from update_time.references import delegated as delegated_module
from update_time.references import vulnerability as vulnerability_module
from update_time.references.delegated import warn_about_delegated_checks, warn_about_projects

from tests.mutation import Mutation, kills
from tests.update_time.helpers import resolved_reference
from tests.update_time.references.helpers import mock_project_getter


class WarnAboutProjectsTest(unittest.TestCase):
    """Unit tests for the project pass the delegating updaters share."""

    def setUp(self):
        """Create one file's references and a mock logger to share."""
        self.location = Location(Path("pyproject.toml"), 4)
        self.declared = [Reference("humanize", "", self.location)]
        self.log = Mock()

    def test_reports_each_resolved_reference(self):
        """Test that each resolved reference gets both project checks, with a marker holding nothing back."""
        get_project = mock_project_getter()
        warn_about_projects([self.declared], get_project, self.log)
        get_project.assert_called_once_with("humanize")
        resolved = resolved_reference("humanize", self.location)
        self.log.report_staleness.assert_called_once_with(resolved, STALE_AFTER.default)
        self.log.report_archival.assert_called_once_with(resolved)


class WarnAboutDelegatedChecksTest(unittest.TestCase):
    """Unit tests for the checks the delegating updaters run over the dependencies a source serves."""

    @kills(
        Mutation(
            delegated_module.warn_about_delegated_checks,
            "if source.project is not None:",
            "if True:",
            "a source without a project getter is asked about projects all the same",
            raises="TypeError: 'NoneType' object is not callable",
        ),
        Mutation(
            vulnerability_module.warn_about_vulnerable_dependencies,
            "if (ecosystem := source.ecosystem) is None:\n        return",
            "ecosystem = source.ecosystem",
            "a source without an ecosystem is asked about at OSV all the same",
            raises="KeyError: None",
        ),
    )
    def test_a_source_without_a_project_getter_is_asked_about_the_pins_alone(self):
        """Test that a source without a project getter skips the project checks, and still checks the pins' yanks."""
        log = Mock()
        get_yank = Mock(return_value=None)
        pinned = Reference("humanize", "1.0.0", Location(Path("pyproject.toml"), 4))
        warn_about_delegated_checks([[pinned]], log, Source(yank=get_yank))
        get_yank.assert_called_once_with("humanize", "1.0.0")
        log.report_staleness.assert_not_called()
        log.report_archival.assert_not_called()
