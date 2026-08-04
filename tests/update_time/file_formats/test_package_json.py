"""Unit tests for the package.json file format, with file I/O mocked."""

import json
import unittest

from update_time.file_formats import package_json

from tests.helpers import mock_path


class ReadTest(unittest.TestCase):
    """Unit tests for parsing a package.json."""

    def test_read(self):
        """Test that a package.json is parsed into a dict."""
        self.assertEqual(
            package_json.read(mock_path('{"name": "x", "version": "1.0"}')), {"name": "x", "version": "1.0"}
        )


class DependencyNamesTest(unittest.TestCase):
    """Unit tests for reading the direct registry dependency names."""

    def test_names_across_sections(self):
        """Test that registry dependencies are collected from every section, deduplicated, in order."""
        contents = json.dumps(
            {
                "dependencies": {"react": "^18.0.0", "left-pad": "1.3.0"},
                "devDependencies": {"typescript": "~5.4.0", "react": "^18.0.0"},  # react repeats: deduplicated
                "optionalDependencies": {"fsevents": "2.3.3"},
                "peerDependencies": {"webpack": "^5.0.0"},  # not installed here: excluded
            }
        )
        self.assertEqual(
            package_json.dependency_names(mock_path(contents)), ["react", "left-pad", "typescript", "fsevents"]
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
        self.assertEqual(package_json.dependency_names(mock_path(contents)), ["clipboard"])

    def test_no_dependencies(self):
        """Test that a package.json without dependency sections yields an empty list."""
        self.assertEqual(package_json.dependency_names(mock_path('{"name": "x"}')), [])
