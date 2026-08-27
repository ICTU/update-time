"""Unit tests for the package.json file format, with file I/O mocked."""

import json
import unittest
from typing import TYPE_CHECKING

from update_time.file_formats import json as json_format
from update_time.file_formats import package_json
from update_time.primitives.location import Location

from tests.helpers import mock_path

if TYPE_CHECKING:
    from unittest.mock import Mock


class ReferenceMarkerTest(unittest.TestCase):
    """Unit tests for reading the marker a package.json names for one entry, and where that entry sits."""

    @staticmethod
    def location(path: Mock) -> Location:
        """Return where the package.json declares its Node engine."""
        return package_json.reference_marker(json_format.read(path), "engines", "node").reference_location

    def test_the_entry_the_section_declares(self):
        """Test that the entry is located at the line and the column the section declares it on."""
        path = mock_path('{\n  "engines": {\n    "node": "18"\n  }\n}\n')
        self.assertEqual(self.location(path), Location(path, 3, column=4))

    def test_a_file_declaring_no_such_entry(self):
        """Test that an entry the file does not declare is located at the file, rather than at a guessed line."""
        path = mock_path('{"engines": {"npm": ">=10"}}')
        self.assertEqual(self.location(path), Location(path))


class DependencyLocationsTest(unittest.TestCase):
    """Unit tests for reading the direct registry dependencies and where each is declared."""

    def test_every_entry_across_the_sections(self):
        """Test that every entry in the dependency sections is returned, in order, each at its own line."""
        path = mock_path(
            "{\n"
            '  "dependencies": {\n    "react": "^18.0.0",\n    "left-pad": "1.3.0"\n  },\n'
            '  "devDependencies": {\n    "typescript": "~5.4.0",\n    "react": "^18.0.0"\n  },\n'
            '  "optionalDependencies": {\n    "fsevents": "2.3.3"\n  },\n'
            '  "peerDependencies": {\n    "webpack": "^5.0.0"\n  }\n'  # npm installs none of these: excluded
            "}\n"
        )
        self.assertEqual(
            package_json.dependency_locations(path),
            {
                "react": [Location(path, 3, 4), Location(path, 8, 4)],  # declared twice, so it keeps both lines
                "left-pad": [Location(path, 4, 4)],
                "typescript": [Location(path, 7, 4)],
                "fsevents": [Location(path, 11, 4)],
            },
        )

    def test_the_line_each_dependency_is_declared_on(self):
        """Test that each dependency is located at the line its entry sits on."""
        path = mock_path('{\n  "dependencies": {\n    "react": "^18.0.0",\n    "left-pad": "1.3.0"\n  }\n}\n')
        self.assertEqual(
            package_json.dependency_locations(path),
            {"react": [Location(path, 3, 4)], "left-pad": [Location(path, 4, 4)]},
        )

    def test_a_name_declared_in_a_section_that_is_not_installed_from(self):
        """Test that a dependency is located in the section declaring it, not at a key of the same name elsewhere."""
        path = mock_path(
            "{\n"
            '  "peerDependencies": {\n    "react": "^18.0.0"\n  },\n'
            '  "overrides": {\n    "left-pad": "1.3.0"\n  },\n'
            '  "dependencies": {\n    "react": "^18.0.0",\n    "left-pad": "^1.0.0"\n  }\n'
            "}\n"
        )
        self.assertEqual(
            package_json.dependency_locations(path),
            {"react": [Location(path, 9, 4)], "left-pad": [Location(path, 10, 4)]},
        )

    def test_non_registry_specs_skipped(self):
        """Test that git, file, workspace, alias and github-shorthand specs are skipped (only registry ranges kept)."""
        contents = json.dumps(
            {
                "dependencies": {
                    "clipboard": "^2.0.11",  # registry range: kept
                    "bats": "git+https://github.com/calj/bats.git",  # git: has ':'
                    "local": "file:../local",  # file: has ':'
                    "shared": "workspace:*",  # workspace: has ':'
                    "aliased": "npm:other@^1.0.0",  # alias: has ':'
                    "forked": "github:org/repo",  # github shorthand: has ':' and '/'
                }
            }
        )
        located = package_json.dependency_locations(mock_path(contents))
        self.assertEqual(list(located), ["clipboard"])

    def test_no_dependencies(self):
        """Test that a package.json without dependency sections yields nothing."""
        self.assertEqual(package_json.dependency_locations(mock_path('{"name": "x"}')), {})
