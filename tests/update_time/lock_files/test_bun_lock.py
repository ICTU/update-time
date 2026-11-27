"""Unit tests for the lock bun writes, with file I/O mocked."""

import unittest
from pathlib import Path

from mutation_time.mutation import kills
from update_time.lock_files.bun_lock import _entry, _version, _workspace_versions, locked_versions

from tests.file_mocks import mock_path

from .lock_file_contents import bun_lock_contents


class LockedVersionsTest(unittest.TestCase):
    """Unit tests for the versions a bun.lock records for the dependencies of each workspace."""

    @kills.append(
        _workspace_versions,
        "for section in DEPENDENCY_SECTIONS",
        "[:1]",
        "the versions bun moved for dev and optional dependencies go unreported",
        case="every dependency section",
    )
    @kills.delete(
        _workspace_versions,
        " if path else None",
        "a root named after a package it depends on gets the versions that package's own dependencies resolve to",
        case="a root named after a package it depends on",
    )
    @kills.delete(
        _entry,
        "packages[own] if member is not None and own in packages else ",
        "a member that resolves a dependency to a version of its own gets the version the workspaces share",
        case="a scoped member's own version",
    )
    @kills.delete(
        _version,
        ' or ":" in version',
        "a workspace member, a git repository, or a path reads as the version of a dependency",
        case="entries that do not name a registry version",
    )
    def test_versions_per_workspace(self):
        """Test that each workspace gets the version the lock records for it, and an entry without one is left out."""
        cases = {
            "every dependency section": (
                {
                    "": {
                        "dependencies": {"a": "^1.0.0"},
                        "devDependencies": {"b": "^1.0.0"},
                        "optionalDependencies": {"c": "^1.0.0"},
                    }
                },
                {"a": "a@1.0.0", "b": "b@2.0.0", "c": "c@3.0.0"},
                {Path(): {"a": "1.0.0", "b": "2.0.0", "c": "3.0.0"}},
            ),
            "a scoped member's own version": (
                {
                    "": {"dependencies": {"is-number": "^6.0.0"}},
                    "packages/b": {"name": "@scope/b", "dependencies": {"is-number": "^7.0.0"}},
                },
                {"is-number": "is-number@6.0.0", "@scope/b/is-number": "is-number@7.0.0"},
                {Path(): {"is-number": "6.0.0"}, Path("packages/b"): {"is-number": "7.0.0"}},
            ),
            "a root named after a package it depends on": (
                {"": {"name": "elysia", "dependencies": {"cookie": "^1.0.0", "elysia": "^1.0.0"}}},
                {"cookie": "cookie@1.1.1", "elysia": "elysia@1.4.16", "elysia/cookie": "cookie@1.1.0"},
                {Path(): {"cookie": "1.1.1", "elysia": "1.4.16"}},
            ),
            "entries that do not name a registry version": (
                {"": {"dependencies": {"b": "workspace:*", "git": "github:org/git", "local": "file:./local"}}},
                {"b": "b@workspace:packages/b", "git": "git@github:org/git#98e8ff1", "local": "local@file:local"},
                {Path(): {}},
            ),
        }
        for case, (workspaces, packages, expected) in cases.items():
            with self.subTest(case):
                self.assertEqual(locked_versions(mock_path(bun_lock_contents(workspaces, packages))), expected)

    @kills.replace(
        _version,
        "next(iter(package.elements()), Document())",
        "package.elements()[0]",
        "an empty bun.lock entry stops the run",
        raises="IndexError: list index out of range",
        case="an empty entry",
    )
    @kills.replace(
        _version,
        "Document()).string()",
        "Document())._value",
        "a bun.lock entry whose first element is not a string stops the run",
        raises="AttributeError: 'int' object has no attribute 'rsplit'",
        case="an entry whose first element is not a string",
    )
    def test_entry_without_a_resolution_is_left_out(self):
        """Test that the lock records no version for a dependency whose entry does not start with a resolution."""
        lock = bun_lock_contents({"": {"dependencies": {"package": "^1.0.0"}}}, {"package": "package@1.0.0"})
        entry = '["package@1.0.0", "", {}, "sha512-…"]'
        cases = {"an empty entry": "[]", "an entry whose first element is not a string": "[5]"}
        for case, entry_without_a_resolution in cases.items():
            with self.subTest(case):
                contents = lock.replace(entry, entry_without_a_resolution)
                self.assertEqual(locked_versions(mock_path(contents)), {Path(): {}})

    @kills.delete(
        locked_versions,
        ' or "lockfileVersion" not in document',
        "a bun.lock holding merge conflict markers reads as a lock that records nothing",
        case="a merge conflict",
    )
    @kills.replace(
        locked_versions,
        'if document is None or "lockfileVersion"',
        'if "lockfileVersion"',
        "a bun.lock that does not parse ends the run with a traceback",
        raises="TypeError: argument of type 'NoneType' is not a container or iterable",
        case="not JSON",
    )
    def test_lock_that_does_not_hold_the_json_bun_writes_raises(self):
        """Test that a lock that does not parse, or does not name the version of its format, raises a ValueError."""
        for case, contents in {"not JSON": "{", "a merge conflict": "<<<<<<< HEAD\n{}\n"}.items():
            with self.subTest(case), self.assertRaisesRegex(ValueError, "it does not hold the JSON bun writes"):
                locked_versions(mock_path(contents))
