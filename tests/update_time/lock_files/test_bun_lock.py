"""Unit tests for the lock bun writes, with file I/O mocked."""

import unittest
from pathlib import Path

from update_time.lock_files import bun_lock

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time.lock_files.helpers import bun_lock_contents


class LockedVersionsTest(unittest.TestCase):
    """Unit tests for the versions a bun.lock records for the dependencies of each workspace."""

    @kills(
        Mutation(
            bun_lock._workspace_versions,
            "for section in DEPENDENCY_SECTIONS",
            "for section in DEPENDENCY_SECTIONS[:1]",
            "the versions bun moved for dev and optional dependencies go unreported",
        ),
        Mutation(
            bun_lock._entry,
            "packages[own] if own in packages else packages[dependency]",
            "packages[dependency]",
            "a member that resolves a dependency to a version of its own gets the version the workspaces share",
        ),
        Mutation(
            bun_lock._version,
            ' or ":" in version',
            "",
            "a workspace member, a git repository, or a path reads as the version of a dependency",
        ),
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
            "entries that do not name a registry version": (
                {"": {"dependencies": {"b": "workspace:*", "git": "github:org/git", "local": "file:./local"}}},
                {"b": "b@workspace:packages/b", "git": "git@github:org/git#98e8ff1", "local": "local@file:local"},
                {Path(): {}},
            ),
        }
        for case, (workspaces, packages, expected) in cases.items():
            with self.subTest(case):
                self.assertEqual(bun_lock.locked_versions(mock_path(bun_lock_contents(workspaces, packages))), expected)

    @kills(
        Mutation(
            bun_lock._version,
            "next(iter(package.elements()), yaml.Document())",
            "package.elements()[0]",
            "an empty bun.lock entry stops the run",
            raises="IndexError: list index out of range",
        )
    )
    def test_entry_without_a_resolution_is_left_out(self):
        """Test that the lock records no version for a dependency whose entry does not start with a resolution."""
        lock = bun_lock_contents({"": {"dependencies": {"package": "^1.0.0"}}}, {"package": "package@1.0.0"})
        entry = '["package@1.0.0", "", {}, "sha512-…"]'
        cases = {"an empty entry": "[]", "an entry whose first element is not a string": "[5]"}
        for case, entry_without_a_resolution in cases.items():
            with self.subTest(case):
                contents = lock.replace(entry, entry_without_a_resolution)
                self.assertEqual(bun_lock.locked_versions(mock_path(contents)), {Path(): {}})

    @kills(
        Mutation(
            bun_lock.locked_versions,
            ' or "lockfileVersion" not in document',
            "",
            "a bun.lock holding merge conflict markers reads as a lock that records nothing",
        )
    )
    def test_lock_that_does_not_hold_the_json_bun_writes_raises(self):
        """Test that a lock that does not parse, or does not name the version of its format, raises a ValueError."""
        cases = {"not JSON": "{", "a merge conflict": "<<<<<<< HEAD\n{}\n"}
        for case, contents in cases.items():
            with self.subTest(case), self.assertRaisesRegex(ValueError, "it does not hold the JSON bun writes"):
                bun_lock.locked_versions(mock_path(contents))
