"""Unit tests for the file type module."""

import unittest

from update_time.domain import file_type as file_type_module
from update_time.domain.file_type import FileType

from tests.mutation import Mutation, kills


class FileTypesTest(unittest.TestCase):
    """Unit tests for the declared file types."""

    @kills(
        Mutation(
            file_type_module,
            'GITLAB_CI_CONFIG = FileType(".gitlab-ci.yml", (".gitlab-ci.yml",), recursive=False)',
            'GITLAB_CI_CONFIG = FileType(".gitlab-ci.yml", (), recursive=False)',
            "a file type that names no glob pattern goes unnoticed, and none of its files is ever scanned",
        )
    )
    def test_every_file_type_names_a_pattern(self):
        """Test that every declared file type names at least one glob pattern."""
        declared = [value for value in vars(file_type_module).values() if isinstance(value, FileType)]
        self.assertNotEqual(declared, [])  # A module declaring none would pass the assertion below vacuously
        self.assertEqual([file_type.name for file_type in declared if not file_type.patterns], [])
