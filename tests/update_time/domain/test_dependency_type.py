"""Unit tests for the dependency type module."""

import unittest

from update_time.domain import dependency_type as dependency_type_module
from update_time.domain.dependency_type import DEPENDENCY_TYPES

from tests.mutation import Mutation, kills


class DependencyTypesTest(unittest.TestCase):
    """Unit tests for the declared dependency types."""

    @kills(
        Mutation(
            dependency_type_module,
            'jsdelivr_npm_urls=DependencyType("jsDelivr npm URLs", (file_type.SPHINX_CONFIG,)),',
            'jsdelivr_npm_urls=DependencyType("jsDelivr npm URLs", ()),',
            "a dependency type that declares no file goes unnoticed, and the help stops naming its files",
        )
    )
    def test_every_dependency_type_declares_a_file(self):
        """Test that every dependency type declares at least one file type."""
        self.assertNotEqual(list(DEPENDENCY_TYPES), [])  # An empty declaration would pass the next assertion vacuously
        without_file_types = [
            dependency_type.name for dependency_type in DEPENDENCY_TYPES if not dependency_type.file_types
        ]
        self.assertEqual(without_file_types, [])
