"""Unit tests for the record of what a source reports about a dependency."""

import unittest

from update_time.domain.dependency import DependencyVersion
from update_time.domain.source import Source

from tests.mutation import Mutation, kills


class ReleaseTest(unittest.TestCase):
    """Unit tests for the release a source reports for a version."""

    @kills(
        Mutation(
            Source.release,
            "changes = NO_CHANGES if self.changes is None else self.changes(dependency, version)",
            "changes = self.changes(dependency, version)",
            "a source without a changes getter is asked for changes all the same",
            raises="TypeError: 'NoneType' object is not callable",
        ),
        Mutation(
            Source.release,
            "published = None if self.publication_date is None else self.publication_date(dependency, version)",
            "published = self.publication_date(dependency, version)",
            "a source without a publication getter is asked for a date all the same",
            raises="TypeError: 'NoneType' object is not callable",
        ),
    )
    def test_a_source_without_changes_or_dates_releases_the_version_alone(self):
        """Test that a source without a changes getter or a publication getter reports the version and nothing else."""
        self.assertEqual(Source().release("left-pad", "1.3.0"), DependencyVersion(version="1.3.0"))
