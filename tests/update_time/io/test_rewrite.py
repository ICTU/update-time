"""Unit tests for the reference-rewriting engine."""

import re
import unittest
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

from update_time.domain.version import DependencyVersion
from update_time.io.rewrite import rewrite_match, update_references_in_lines

from tests.update_time.helpers import new_version_getter

if TYPE_CHECKING:
    from update_time.domain.version import NewVersionGetter

REGEXP = r"image: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)"
SHA_REGEXP = r"image: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)(?:@(?P<sha>sha256:[a-f0-9]{64}))?"
ACTION_REGEXP = r"uses: (?P<dependency>[\w\d\./-]+)@(?P<sha>[a-f0-9]{40}) # v?(?P<version>[\d\w\.\-]+)"


def _search(pattern: str, text: str) -> re.Match[str]:
    """Search for the pattern in the text and return the match (which the test's inputs always produce)."""
    return cast("re.Match[str]", re.search(pattern, text))


class RewriteMatchTest(unittest.TestCase):
    """Unit tests for replacing only the captured groups within a match."""

    def test_replaces_only_the_captured_span(self):
        """Test that a group is replaced only where it was captured, not where its value recurs within the match."""
        match = _search(r"pkg@(?P<version>[\d.]+)/dist/pkg-[\d.]+\.js", "pkg@2.0.11/dist/pkg-2.0.11.js")
        self.assertEqual("pkg@2.0.12/dist/pkg-2.0.11.js", rewrite_match(match, {"version": "2.0.12"}))

    def test_replaces_multiple_groups(self):
        """Test that several groups are replaced, each at its own captured span."""
        old_sha, new_sha = f"sha256:{'a' * 64}", f"sha256:{'b' * 64}"
        match = _search(SHA_REGEXP, f"image: python:3.14@{old_sha}")
        self.assertEqual(f"image: python:3.15@{new_sha}", rewrite_match(match, {"version": "3.15", "sha": new_sha}))


class UpdateReferencesTest(unittest.TestCase):
    """Unit tests for updating the references in a list of lines."""

    def setUp(self) -> None:
        """Use a mock logger and path so log calls can be asserted."""
        super().setUp()
        self.logger = Mock()
        self.path = Mock()

    def rewrite(self, lines: list[str], regexp: str, get_new_version: NewVersionGetter) -> list[str]:
        """Run the rewrite engine over the lines with the given regexp and new-version getter."""
        return update_references_in_lines(
            lines, regexp, get_new_version=get_new_version, logger=self.logger, path=self.path
        )

    def test_no_reference(self):
        """Test that lines without a reference are returned unchanged."""
        lines = ["line1", "line2"]
        self.assertEqual(lines, self.rewrite(lines, "regexp", new_version_getter("1.1")))
        self.logger.new_version.assert_not_called()

    def test_new_version(self):
        """Test that a reference is updated to the new version."""
        new_lines = self.rewrite(["line1", "image: python:3.14"], REGEXP, new_version_getter("3.15"))
        self.assertEqual(["line1", "image: python:3.15"], new_lines)
        self.logger.new_version.assert_called_with("python", DependencyVersion(version="3.15"), self.path)

    def test_new_version_with_sha(self):
        """Test that both the version and the digest of an already-pinned reference are updated."""
        old_sha, new_sha = "a" * 40, "b" * 40
        new_lines = self.rewrite(
            [f"uses: action/action@{old_sha} # v3.14"], ACTION_REGEXP, new_version_getter("3.15", new_sha)
        )
        self.assertEqual([f"uses: action/action@{new_sha} # v3.15"], new_lines)
        self.logger.new_version.assert_called_with(
            "action/action", DependencyVersion(version="3.15", sha=new_sha), self.path
        )

    def test_unchanged_version(self):
        """Test that a reference already at the latest version is left unchanged."""
        lines = ["line1", "image: python:3.14"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, new_version_getter("3.14")))
        self.logger.new_version.assert_not_called()

    def test_pin_unpinned_at_latest_version(self):
        """Test that an unpinned reference at the latest version is pinned, logging a pin rather than a new version."""
        sha = f"sha256:{'a' * 64}"
        new_lines = self.rewrite(["line1", "image: python:3.14"], SHA_REGEXP, new_version_getter("3.14", sha))
        self.assertEqual(["line1", f"image: python:3.14@{sha}"], new_lines)
        self.logger.pinned.assert_called_with("python", DependencyVersion(version="3.14", sha=sha), self.path)
        self.logger.new_version.assert_not_called()
        self.logger.digest_drift.assert_not_called()  # An unpinned reference has no pinned digest to drift from.

    def test_digest_drift_warns_without_rewriting(self):
        """Test that a pinned reference whose digest changed at the registry is warned about, not rewritten."""
        old_sha, new_sha = f"sha256:{'a' * 64}", f"sha256:{'b' * 64}"
        lines = [f"image: python:3.14@{old_sha}"]
        self.assertEqual(lines, self.rewrite(lines, SHA_REGEXP, new_version_getter("3.14", new_sha)))
        self.logger.digest_drift.assert_called_once_with("python", "3.14", old_sha, new_sha, self.path)
        self.logger.new_version.assert_not_called()
        self.logger.pinned.assert_not_called()

    def test_matching_digest_not_warned(self):
        """Test that a pinned reference whose digest is unchanged is left alone, without a drift warning."""
        sha = f"sha256:{'a' * 64}"
        lines = [f"image: python:3.14@{sha}"]
        self.assertEqual(lines, self.rewrite(lines, SHA_REGEXP, new_version_getter("3.14", sha)))
        self.logger.digest_drift.assert_not_called()

    def test_pin_unpinned_with_new_version(self):
        """Test that an unpinned reference is pinned and bumped to the latest version at the same time."""
        sha = f"sha256:{'a' * 64}"
        new_lines = self.rewrite(["line1", "image: python:3.14"], SHA_REGEXP, new_version_getter("3.15", sha))
        self.assertEqual(["line1", f"image: python:3.15@{sha}"], new_lines)
        self.logger.new_version.assert_called_with("python", DependencyVersion(version="3.15", sha=sha), self.path)

    def test_unpinned_left_alone_without_digest(self):
        """Test that an unpinned reference is not pinned when no digest is available."""
        lines = ["line1", "image: python:3.14"]
        self.assertEqual(lines, self.rewrite(lines, SHA_REGEXP, new_version_getter("3.14")))
        self.logger.new_version.assert_not_called()

    def test_new_version_sorting_lower_than_current(self):
        """Test the regression where a newer version sorts lexicographically lower than the current one.

        get_new_version returns the highest version (compared as a packaging.Version), so e.g. "3.10" must be
        applied over "3.9" even though the string "3.10" < "3.9".
        """
        new_lines = self.rewrite(["line1", "image: python:3.9"], REGEXP, new_version_getter("3.10"))
        self.assertEqual(["line1", "image: python:3.10"], new_lines)
        self.logger.new_version.assert_called_with("python", DependencyVersion(version="3.10"), self.path)

    def test_version_from_source_applied_even_when_lower(self):
        """Test that any differing version the getter returns is applied, trusting the source.

        The engine no longer guards against downgrades itself; the source functions decide the target version (the
        real ones return the maximum). This lets update_node_engine sync the Node engine down to a downgraded image.
        """
        new_lines = self.rewrite(["line1", "image: python:3.14"], REGEXP, new_version_getter("3.13"))
        self.assertEqual(["line1", "image: python:3.13"], new_lines)
        self.logger.new_version.assert_called_with("python", DependencyVersion(version="3.13"), self.path)

    def test_version_replaced_only_within_the_match(self):
        """Test that the version is rewritten only where the regexp matched it, not elsewhere on the line."""
        new_lines = self.rewrite(["image: node:18 AS build-18"], REGEXP, new_version_getter("20"))
        self.assertEqual(["image: node:20 AS build-18"], new_lines)
        self.logger.new_version.assert_called_with("node", DependencyVersion(version="20"), self.path)

    def test_inline_ignore_marker_pins_line(self):
        """Test that an inline `# update-time: ignore` comment leaves the line untouched, looking up no version."""
        get_new_version = Mock()
        lines = ["image: python:3.14  # update-time: ignore"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", self.path)

    def test_preceding_ignore_marker_pins_next_line(self):
        """Test that a standalone `# update-time: ignore` comment pins the reference on the line below it.

        The marker comment itself carries no reference, so only the pinned reference below it is logged as ignored.
        """
        get_new_version = Mock()
        lines = ["# update-time: ignore", "image: python:3.14"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", self.path)

    def test_inline_marker_does_not_pin_following_line(self):
        """Test that an inline marker pins only its own line, not the reference on the line below it."""
        lines = ["image: a:3.14  # update-time: ignore", "image: b:3.14"]
        new_lines = self.rewrite(lines, REGEXP, new_version_getter("3.15"))
        self.assertEqual(["image: a:3.14  # update-time: ignore", "image: b:3.15"], new_lines)
        self.logger.ignored.assert_called_once_with("a", self.path)

    def test_inline_slash_slash_marker_pins_line(self):
        """Test that a `//`-style ignore marker (as JSONC/devcontainer.json uses) also pins a line inline."""
        get_new_version = Mock()
        lines = ["image: python:3.14  // update-time: ignore"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", self.path)

    def test_preceding_slash_slash_marker_pins_next_line(self):
        """Test that a standalone `//` marker comment pins the reference on the line below it."""
        get_new_version = Mock()
        lines = ["// update-time: ignore", "image: python:3.14"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", self.path)
