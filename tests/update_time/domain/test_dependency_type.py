"""Unit tests for the dependency type module."""

import unittest

from mutation_time.mutation import kills
from update_time.domain import dependency_type as dependency_type_module
from update_time.domain import file_type
from update_time.domain.dependency_type import DEPENDENCY_TYPES
from update_time.domain.file_type import FileType

from tests.update_time.base_test_cases import module_values


class DependencyTypesTest(unittest.TestCase):
    """Unit tests for the declared dependency types."""

    @kills.replace(
        dependency_type_module,
        "(SPHINX_CONFIG,)",
        "()",
        "a dependency type that declares no file goes unnoticed, and the help stops naming its files",
    )
    def test_every_dependency_type_declares_a_file(self):
        """Test that every dependency type declares at least one file type."""
        self.assertNotEqual(list(DEPENDENCY_TYPES), [])  # An empty declaration would pass the next assertion vacuously
        without_file_types = [
            dependency_type.name for dependency_type in DEPENDENCY_TYPES if not dependency_type.file_types
        ]
        self.assertEqual(without_file_types, [])

    def test_every_file_type_belongs_to_a_dependency_type(self):
        """Test that every file type the scan walks is declared by a dependency type, so the help names it."""
        file_types = module_values(file_type, FileType)
        self.assertNotEqual(file_types, [])  # An empty list would pass the next assertion vacuously
        declared = {declared for dependency_type in DEPENDENCY_TYPES for declared in dependency_type.file_types}
        self.assertEqual([each.name for each in file_types if each not in declared], [])
