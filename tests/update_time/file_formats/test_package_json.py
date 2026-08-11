"""Unit tests for the package.json file format, with file I/O mocked."""

import json
import unittest

from update_time.file_formats import package_json
from update_time.primitives.location import Location

from tests.helpers import mock_path


class ReadTest(unittest.TestCase):
    """Unit tests for parsing a package.json."""

    def test_read(self):
        """Test that a package.json is parsed into a dict."""
        self.assertEqual(
            package_json.read(mock_path('{"name": "x", "version": "1.0"}')), {"name": "x", "version": "1.0"}
        )


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
                "react": [Location(path, 3), Location(path, 8)],  # declared twice, so it keeps both lines
                "left-pad": [Location(path, 4)],
                "typescript": [Location(path, 7)],
                "fsevents": [Location(path, 11)],
            },
        )

    def test_the_line_each_dependency_is_declared_on(self):
        """Test that each dependency is located at the line its entry sits on."""
        path = mock_path('{\n  "dependencies": {\n    "react": "^18.0.0",\n    "left-pad": "1.3.0"\n  }\n}\n')
        self.assertEqual(
            package_json.dependency_locations(path),
            {"react": [Location(path, 3)], "left-pad": [Location(path, 4)]},
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
            {"react": [Location(path, 9)], "left-pad": [Location(path, 10)]},
        )

    def test_a_dependency_whose_entry_cannot_be_found(self):
        """Test that a dependency the search cannot find is located at the file, rather than at a guessed line."""
        path = mock_path('{"dependencies": {"\\u0072eact": "^18.0.0"}}')  # a JSON escape: the key parses as `react`
        self.assertEqual(package_json.dependency_locations(path), {"react": [Location(path)]})

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
