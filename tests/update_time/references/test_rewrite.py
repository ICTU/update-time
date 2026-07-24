"""Unit tests for the reference-rewriting engine."""

import re
import unittest
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

from update_time.domain.bound import NO_BOUND, Verb
from update_time.domain.location import Location
from update_time.domain.marker import Marker
from update_time.domain.version import DependencyVersion
from update_time.references.rewrite import ALLOW_IMAGE_DIGEST_DRIFT, rewrite_match, update_references_in_lines

from tests.update_time.fixtures import COMMIT_SHA1 as OLD_SHA
from tests.update_time.fixtures import COMMIT_SHA2 as NEW_SHA
from tests.update_time.fixtures import DIGEST
from tests.update_time.fixtures import DIGEST1 as OLD_DIGEST
from tests.update_time.fixtures import DIGEST2 as NEW_DIGEST
from tests.update_time.helpers import bound, new_version_getter, patch_environ

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter

# The marker a bare `# update-time: ignore` expresses: every check the marker can hold back is held back.
BARE_IGNORE = Marker(ignore_update=True, ignore_stale=True, ignore_yanked=True)

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
        self.assertEqual(rewrite_match(match, {"version": "2.0.12"}), "pkg@2.0.12/dist/pkg-2.0.11.js")

    def test_replaces_multiple_groups(self):
        """Test that several groups are replaced, each at its own captured span."""
        match = _search(SHA_REGEXP, f"image: python:3.14@{OLD_DIGEST}")
        self.assertEqual(
            rewrite_match(match, {"version": "3.15", "sha": NEW_DIGEST}), f"image: python:3.15@{NEW_DIGEST}"
        )


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
        """Test that a reference is updated to the new version, logged at its own 1-based line."""
        new_lines = self.rewrite(["line1", "image: python:3.14"], REGEXP, new_version_getter("3.15"))
        self.assertEqual(new_lines, ["line1", "image: python:3.15"])
        # "line1" then the reference, so the reference is on line 2.
        self.logger.new_version.assert_called_with("python", DependencyVersion(version="3.15"), Location(self.path, 2))

    def test_reference_logged_at_its_own_line_not_the_marker_line(self):
        """Test that a reference governed by a standalone marker is logged at the reference's line, not the marker's."""
        lines = ["line1", "# update-time: ignore[stale]", "image: python:3.14"]
        self.rewrite(lines, REGEXP, new_version_getter("3.15"))
        # The marker sits on line 2 and applies to the reference on line 3; the reported line is the reference's.
        self.logger.new_version.assert_called_with("python", DependencyVersion(version="3.15"), Location(self.path, 3))

    def test_new_version_with_sha(self):
        """Test that both the version and the digest of an already-pinned reference are updated."""
        new_lines = self.rewrite(
            [f"uses: action/action@{OLD_SHA} # v3.14"], ACTION_REGEXP, new_version_getter("3.15", NEW_SHA)
        )
        self.assertEqual(new_lines, [f"uses: action/action@{NEW_SHA} # v3.15"])
        self.logger.new_version.assert_called_with(
            "action/action", DependencyVersion(version="3.15", sha=NEW_SHA), Location(self.path, 1)
        )

    def test_unchanged_version(self):
        """Test that a reference already at the latest version is left unchanged."""
        lines = ["line1", "image: python:3.14"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, new_version_getter("3.14")))
        self.logger.new_version.assert_not_called()

    def test_pin_unpinned_at_latest_version(self):
        """Test that an unpinned reference at the latest version is pinned, logging a pin rather than a new version."""
        new_lines = self.rewrite(["line1", "image: python:3.14"], SHA_REGEXP, new_version_getter("3.14", DIGEST))
        self.assertEqual(new_lines, ["line1", f"image: python:3.14@{DIGEST}"])
        self.logger.pinned.assert_called_with(
            "python", DependencyVersion(version="3.14", sha=DIGEST), Location(self.path, 2)
        )
        self.logger.new_version.assert_not_called()
        self.logger.digest_drift.assert_not_called()  # An unpinned reference has no pinned digest to drift from.

    def test_digest_drift_warns_without_rewriting(self):
        """Test that a pinned reference whose digest changed at the registry is warned about, not rewritten."""
        lines = [f"image: python:3.14@{OLD_DIGEST}"]
        self.assertEqual(lines, self.rewrite(lines, SHA_REGEXP, new_version_getter("3.14", NEW_DIGEST)))
        self.logger.digest_drift.assert_called_once_with(
            "python", "3.14", OLD_DIGEST, NEW_DIGEST, Location(self.path, 1)
        )
        self.logger.new_version.assert_not_called()
        self.logger.pinned.assert_not_called()

    def test_matching_digest_not_warned(self):
        """Test that a pinned reference whose digest is unchanged is left alone, without a drift warning."""
        lines = [f"image: python:3.14@{DIGEST}"]
        self.assertEqual(lines, self.rewrite(lines, SHA_REGEXP, new_version_getter("3.14", DIGEST)))
        self.logger.digest_drift.assert_not_called()

    def test_pin_unpinned_with_new_version(self):
        """Test that an unpinned reference is pinned and bumped to the latest version at the same time."""
        new_lines = self.rewrite(["line1", "image: python:3.14"], SHA_REGEXP, new_version_getter("3.15", DIGEST))
        self.assertEqual(new_lines, ["line1", f"image: python:3.15@{DIGEST}"])
        self.logger.new_version.assert_called_with(
            "python", DependencyVersion(version="3.15", sha=DIGEST), Location(self.path, 2)
        )

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
        self.assertEqual(new_lines, ["line1", "image: python:3.10"])
        self.logger.new_version.assert_called_with("python", DependencyVersion(version="3.10"), Location(self.path, 2))

    def test_version_from_source_applied_even_when_lower(self):
        """Test that any differing version the getter returns is applied, trusting the source.

        The engine no longer guards against downgrades itself; the source functions decide the target version (the
        real ones return the maximum). This lets update_node_engine sync the Node engine down to a downgraded image.
        """
        new_lines = self.rewrite(["line1", "image: python:3.14"], REGEXP, new_version_getter("3.13"))
        self.assertEqual(new_lines, ["line1", "image: python:3.13"])
        self.logger.new_version.assert_called_with("python", DependencyVersion(version="3.13"), Location(self.path, 2))

    def test_version_replaced_only_within_the_match(self):
        """Test that the version is rewritten only where the regexp matched it, not elsewhere on the line."""
        new_lines = self.rewrite(["image: node:18 AS build-18"], REGEXP, new_version_getter("20"))
        self.assertEqual(new_lines, ["image: node:20 AS build-18"])
        self.logger.new_version.assert_called_with("node", DependencyVersion(version="20"), Location(self.path, 1))

    def test_inline_ignore_marker_pins_line(self):
        """Test that an inline `# update-time: ignore` comment leaves the line untouched, looking up no version."""
        get_new_version = Mock()
        lines = ["image: python:3.14  # update-time: ignore"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", BARE_IGNORE, Location(self.path, 1))

    def test_preceding_ignore_marker_pins_next_line(self):
        """Test that a standalone `# update-time: ignore` comment pins the reference on the line below it.

        The marker comment itself carries no reference, so only the pinned reference below it is logged as ignored.
        """
        get_new_version = Mock()
        lines = ["# update-time: ignore", "image: python:3.14"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", BARE_IGNORE, Location(self.path, 2))

    def test_inline_marker_does_not_pin_following_line(self):
        """Test that an inline marker pins only its own line, not the reference on the line below it."""
        lines = ["image: a:3.14  # update-time: ignore", "image: b:3.14"]
        new_lines = self.rewrite(lines, REGEXP, new_version_getter("3.15"))
        self.assertEqual(new_lines, ["image: a:3.14  # update-time: ignore", "image: b:3.15"])
        self.logger.ignored.assert_called_once_with("a", BARE_IGNORE, Location(self.path, 1))

    def test_inline_slash_slash_marker_pins_line(self):
        """Test that a `//`-style ignore marker (as JSONC/devcontainer.json uses) also pins a line inline."""
        get_new_version = Mock()
        lines = ["image: python:3.14  // update-time: ignore"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", BARE_IGNORE, Location(self.path, 1))

    def test_preceding_slash_slash_marker_pins_next_line(self):
        """Test that a standalone `//` marker comment pins the reference on the line below it."""
        get_new_version = Mock()
        lines = ["// update-time: ignore", "image: python:3.14"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", BARE_IGNORE, Location(self.path, 2))

    def test_ignore_update_marker_skips_update_but_still_checks_staleness(self):
        """Test that `ignore[update]` leaves the version unchanged but still runs the staleness check."""
        lines = ["image: python:3.14  # update-time: ignore[update]"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, new_version_getter("3.15")))  # version left as-is
        self.logger.warn_if_stale.assert_called_once()  # staleness still checked
        self.logger.ignored.assert_called_once_with("python", Marker(ignore_update=True), Location(self.path, 1))

    def test_ignore_update_and_stale_still_checks_for_a_yank(self):
        """Test that a scope the marker leaves live keeps the reference queried, so its check still runs.

        `ignore[update]` and `ignore[stale]` silence two of the three scopes; the yank check is not held back, so
        the source is still queried for it rather than the reference being skipped outright.
        """
        lines = ["image: python:3.14  # update-time: ignore[update] ignore[stale]"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, new_version_getter("3.15")))  # version left as-is
        self.logger.warn_if_yanked.assert_called_once()  # the yank check still runs
        self.logger.warn_if_stale.assert_not_called()  # staleness is held back

    def test_ignore_stale_marker_skips_staleness_but_still_updates(self):
        """Test that `ignore[stale]` applies the update but skips the staleness check."""
        lines = ["image: python:3.14  # update-time: ignore[stale]"]
        new_lines = self.rewrite(lines, REGEXP, new_version_getter("3.15"))
        self.assertEqual(new_lines, ["image: python:3.15  # update-time: ignore[stale]"])  # version bumped
        self.logger.warn_if_stale.assert_not_called()  # staleness skipped
        self.logger.ignored.assert_not_called()  # the update is not held back, so nothing is logged as ignored

    def test_allow_digest_drift_marker_adopts_new_digest(self):
        """Test that an inline `allow[digest-drift]` marker re-pins a re-pushed tag's digest instead of warning."""
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: allow[digest-drift]"]
        new_lines = self.rewrite(lines, SHA_REGEXP, new_version_getter("3.14", NEW_DIGEST))
        self.assertEqual(new_lines, [f"image: python:3.14@{NEW_DIGEST}  # update-time: allow[digest-drift]"])
        self.logger.adopted_drift.assert_called_once_with(
            "python", "3.14", OLD_DIGEST, NEW_DIGEST, Location(self.path, 1), "update-time: allow[digest-drift]"
        )
        self.logger.digest_drift.assert_not_called()

    def test_allow_digest_drift_marker_above_line_adopts(self):
        """Test that a standalone `allow[digest-drift]` comment opts the reference on the line below it in."""
        lines = ["# update-time: allow[digest-drift]", f"image: python:3.14@{OLD_DIGEST}"]
        new_lines = self.rewrite(lines, SHA_REGEXP, new_version_getter("3.14", NEW_DIGEST))
        self.assertEqual(new_lines, ["# update-time: allow[digest-drift]", f"image: python:3.14@{NEW_DIGEST}"])
        self.logger.adopted_drift.assert_called_once_with(
            "python", "3.14", OLD_DIGEST, NEW_DIGEST, Location(self.path, 2), "update-time: allow[digest-drift]"
        )

    def test_allow_digest_drift_marker_is_noop_when_version_also_changed(self):
        """Test that when the version has moved too, the normal update path runs and the marker doesn't apply."""
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: allow[digest-drift]"]
        new_lines = self.rewrite(lines, SHA_REGEXP, new_version_getter("3.15", NEW_DIGEST))
        self.assertEqual(new_lines, [f"image: python:3.15@{NEW_DIGEST}  # update-time: allow[digest-drift]"])
        self.logger.new_version.assert_called_once()  # a real version bump, not a drift adoption
        self.logger.adopted_drift.assert_not_called()

    def test_ignore_wins_over_allow_digest_drift_marker(self):
        """Test that a reference marked both `ignore` and `allow[digest-drift]` is left untouched: `ignore` wins."""
        get_new_version = Mock()
        lines = ["# update-time: allow[digest-drift]", f"image: python:3.14@{OLD_DIGEST}  # update-time: ignore"]
        self.assertEqual(lines, self.rewrite(lines, SHA_REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.adopted_drift.assert_not_called()
        self.logger.digest_drift.assert_not_called()

    def test_flag_adopts_digest_drift_repo_wide(self):
        """Test that the --allow-image-digest-drift flag (via its env var) adopts drift without a per-line marker."""
        lines = [f"image: python:3.14@{OLD_DIGEST}"]
        with patch_environ({ALLOW_IMAGE_DIGEST_DRIFT.name: "1"}):
            new_lines = self.rewrite(lines, SHA_REGEXP, new_version_getter("3.14", NEW_DIGEST))
        self.assertEqual(new_lines, [f"image: python:3.14@{NEW_DIGEST}"])
        self.logger.adopted_drift.assert_called_once_with(
            "python", "3.14", OLD_DIGEST, NEW_DIGEST, Location(self.path, 1), "--allow-image-digest-drift"
        )
        self.logger.digest_drift.assert_not_called()

    def test_ignore_wins_over_allow_digest_drift_flag(self):
        """Test that an `ignore` marker still wins over the global --allow-image-digest-drift flag."""
        get_new_version = Mock()
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: ignore"]
        with patch_environ({ALLOW_IMAGE_DIGEST_DRIFT.name: "1"}):
            self.assertEqual(lines, self.rewrite(lines, SHA_REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.adopted_drift.assert_not_called()

    def test_allow_update_bound_passes_bound_to_source(self):
        """Test that an inline `allow[update<…>]` marker passes the bound to the source and applies the result."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: allow[update<3.13]"]
        new_lines = self.rewrite(lines, REGEXP, get_new_version)
        self.assertEqual(new_lines, ["image: python:3.12.9  # update-time: allow[update<3.13]"])
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.ALLOW, "update<3.13"))

    def test_ignore_update_bound_passes_bound_to_source(self):
        """Test that an inline `ignore[update>=…]` marker passes the complement (drop) bound to the source."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: ignore[update>=3.13]"]
        self.rewrite(lines, REGEXP, get_new_version)
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.IGNORE, "update>=3.13"))

    def test_ignore_level_bound_passes_bound_to_source(self):
        """Test that an inline `ignore[minor-update]` marker passes the level bound to the source."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12.1  # update-time: ignore[minor-update]"]
        new_lines = self.rewrite(lines, REGEXP, get_new_version)
        self.assertEqual(new_lines, ["image: python:3.12.9  # update-time: ignore[minor-update]"])
        get_new_version.assert_called_once_with("python", "3.12.1", bound(Verb.IGNORE, "minor-update"))

    def test_allow_level_bound_passes_bound_to_source(self):
        """Test that an inline `allow[minor-update]` marker passes the level bound to the source."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.13.0"))
        lines = ["image: python:3.12.1  # update-time: allow[minor-update]"]
        self.rewrite(lines, REGEXP, get_new_version)
        get_new_version.assert_called_once_with("python", "3.12.1", bound(Verb.ALLOW, "minor-update"))

    def test_level_bound_marker_above_line_passes_bound(self):
        """Test that a standalone `ignore[major-update]` comment bounds the reference on the line below it."""
        get_new_version = Mock(return_value=DependencyVersion(version="7.4"))
        lines = ["# update-time: ignore[major-update]", "image: redis:7.2"]
        new_lines = self.rewrite(lines, REGEXP, get_new_version)
        self.assertEqual(new_lines, ["# update-time: ignore[major-update]", "image: redis:7.4"])
        get_new_version.assert_called_once_with("redis", "7.2", bound(Verb.IGNORE, "major-update"))

    def test_bare_ignore_wins_over_level_bound(self):
        """Test that a reference marked both `ignore` and a level bound is left untouched: `ignore` wins."""
        get_new_version = Mock()
        lines = ["# update-time: ignore", "image: python:3.12  # update-time: allow[minor-update]"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()

    def test_level_bound_combines_with_digest_drift_in_one_bracket(self):
        """Test that an `allow` bracket combines a level bound with the digest-drift opt-in."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.14", sha=NEW_DIGEST))
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: allow[minor-update, digest-drift]"]
        new_lines = self.rewrite(lines, SHA_REGEXP, get_new_version)
        self.assertEqual([lines[0].replace(OLD_DIGEST, NEW_DIGEST)], new_lines)  # the drift opt-in is honoured
        get_new_version.assert_called_once_with("python", "3.14", bound(Verb.ALLOW, "minor-update"))

    def test_redundant_level_bound_is_warned(self):
        """Test that a level bound that blocks every update is warned about."""
        lines = ["image: python:3.12  # update-time: ignore[patch-update]"]
        self.rewrite(lines, REGEXP, new_version_getter("3.12"))
        marker = Marker(version_bound=bound(Verb.IGNORE, "patch-update"))
        self.logger.warn_if_redundant_bound.assert_called_once_with("python", marker, "3.12", Location(self.path, 1))

    def test_unknown_level_in_ignore_falls_back_to_bare_ignore(self):
        """Test that an unknown level name in an `ignore` bracket (a typo) falls back to a bare `ignore`."""
        get_new_version = Mock()
        lines = ["image: python:3.12  # update-time: ignore[mega-update]"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", BARE_IGNORE, Location(self.path, 1))

    def test_bound_marker_above_line_passes_bound(self):
        """Test that a standalone `allow[update<…>]` comment bounds the reference on the line below it."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["# update-time: allow[update<3.13]", "image: python:3.12"]
        new_lines = self.rewrite(lines, REGEXP, get_new_version)
        self.assertEqual(new_lines, ["# update-time: allow[update<3.13]", "image: python:3.12.9"])
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.ALLOW, "update<3.13"))

    def test_directive_list_combines_bound_and_digest_drift(self):
        """Test that a bound and an `allow[digest-drift]` directive listed after one prefix both apply."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.14", sha=NEW_DIGEST))
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: allow[update<3.15] allow[digest-drift]"]
        new_lines = self.rewrite(lines, SHA_REGEXP, get_new_version)
        self.assertEqual([lines[0].replace(OLD_DIGEST, NEW_DIGEST)], new_lines)  # the drift opt-in is honoured
        get_new_version.assert_called_once_with("python", "3.14", bound(Verb.ALLOW, "update<3.15"))
        # The cause names the reference's `allow` directives verbatim, the bound alongside the digest-drift opt-in.
        self.logger.adopted_drift.assert_called_once_with(
            "python",
            "3.14",
            OLD_DIGEST,
            NEW_DIGEST,
            Location(self.path, 1),
            "update-time: allow[update<3.15] allow[digest-drift]",
        )

    def test_directive_list_combines_ignore_stale_and_bound(self):
        """Test that an `ignore[stale]` and an `allow` bound directive listed after one prefix both apply."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: ignore[stale] allow[update<3.13]"]
        new_lines = self.rewrite(lines, REGEXP, get_new_version)
        self.assertEqual([lines[0].replace("python:3.12 ", "python:3.12.9 ")], new_lines)
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.ALLOW, "update<3.13"))
        self.logger.warn_if_stale.assert_not_called()  # the `ignore[stale]` directive is honoured alongside the bound

    def test_directive_list_followed_by_reason(self):
        """Test that free text after the last directive (a reason) is allowed and ends the directive list."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: allow[update<3.13] (pinned until the 3.13 migration)"]
        new_lines = self.rewrite(lines, REGEXP, get_new_version)
        self.assertEqual([lines[0].replace("python:3.12 ", "python:3.12.9 ")], new_lines)
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.ALLOW, "update<3.13"))

    def test_typo_ends_directive_list(self):
        """Test that a mistyped directive ends the list as a reason: the directives before it still apply."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.14", sha=NEW_DIGEST))
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: ignore[stale] alloww[digest-drift]"]
        self.assertEqual(lines, self.rewrite(lines, SHA_REGEXP, get_new_version))
        self.logger.warn_if_stale.assert_not_called()  # the `ignore[stale]` before the typo is honoured
        self.logger.digest_drift.assert_called_once()  # the mistyped drift opt-in is not, so the drift only warns

    def test_unknown_ignore_scope_falls_back_to_bare_ignore(self):
        """Test that an unrecognised `ignore` scope (a typo) falls back to a bare `ignore`, pinning the line."""
        get_new_version = Mock()
        lines = ["image: python:3.12  # update-time: ignore[updaet]"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", BARE_IGNORE, Location(self.path, 1))

    def test_unterminated_allow_bracket_expresses_nothing(self):
        """Test that an `allow[` whose bracket is never closed expresses nothing, leaving the reference to update.

        With no closing `]` the marker regex captures no bracket, so there is no item to report as invalid. The
        malformed directive is a no-op reason and the reference updates as if unmarked. A closed `allow[typo]`,
        which does have an item, warns instead; see `test_unknown_allow_item_warns_and_leaves_reference_unchanged`.
        """
        lines = ["image: python:3.14  # update-time: allow[update<4"]
        new_lines = self.rewrite(lines, REGEXP, new_version_getter("3.15"))
        self.assertEqual(new_lines, ["image: python:3.15  # update-time: allow[update<4"])  # updated, not frozen
        self.logger.ignored.assert_not_called()
        self.logger.invalid_specifier.assert_not_called()

    def test_unknown_allow_item_warns_and_leaves_reference_unchanged(self):
        """Test that a single unrecognised `allow` item (a level-bound typo) warns and leaves the reference unchanged.

        Unlike the `ignore` typo above, which fails safe to a bare `ignore`, a mistyped `allow` bound must not
        silently drop the bound and let the reference update unbounded; it is reported like any malformed bound.
        """
        get_new_version = Mock()
        lines = ["image: python:3.12.1  # update-time: allow[patch-updates]"]  # plural typo of `patch-update`
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))  # left unchanged, not updated
        get_new_version.assert_not_called()
        self.logger.invalid_specifier.assert_called_once_with("python", "patch-updates", Location(self.path, 1))

    def test_unterminated_ignore_bracket_falls_back_to_bare_ignore(self):
        """Test that an `ignore[` whose bracket is never closed falls back to a bare `ignore`, pinning the line."""
        get_new_version = Mock()
        lines = ["image: python:3.14  # update-time: ignore[update<4"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", BARE_IGNORE, Location(self.path, 1))

    def test_comma_separated_items_combine_in_one_bracket(self):
        """Test that a bracket combines comma-separated items: `ignore[stale, update>=3.13]` bounds and silences."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: ignore[stale, update>=3.13]"]
        new_lines = self.rewrite(lines, REGEXP, get_new_version)
        self.assertEqual([lines[0].replace("python:3.12 ", "python:3.12.9 ")], new_lines)
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.IGNORE, "update>=3.13"))
        self.logger.warn_if_stale.assert_not_called()

    def test_comma_separated_allow_items_combine_in_one_bracket(self):
        """Test that an `allow` bracket combines a bound with the digest-drift opt-in."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.14", sha=NEW_DIGEST))
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: allow[update<3.15, digest-drift]"]
        new_lines = self.rewrite(lines, SHA_REGEXP, get_new_version)
        self.assertEqual([lines[0].replace(OLD_DIGEST, NEW_DIGEST)], new_lines)  # the drift opt-in is honoured
        get_new_version.assert_called_once_with("python", "3.14", bound(Verb.ALLOW, "update<3.15"))

    def test_compound_specifier_keeps_its_comma_inside_a_bracket_list(self):
        """Test that a compound specifier's commas are kept apart from the commas that separate bracket items."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: ignore[update>=3.13,<3.15, stale]"]
        self.rewrite(lines, REGEXP, get_new_version)
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.IGNORE, "update>=3.13,<3.15"))
        self.logger.warn_if_stale.assert_not_called()

    def test_combined_ignore_scopes_hold_back_everything(self):
        """Test that every `ignore` scope combined holds back as much as a bare `ignore`."""
        get_new_version = Mock()
        lines = ["image: python:3.12  # update-time: ignore[update] ignore[stale] ignore[yanked]"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()  # Every aspect is held back, so the source is not even queried.
        self.logger.ignored.assert_called_once_with("python", BARE_IGNORE, Location(self.path, 1))

    def test_unrecognised_item_in_comma_list_is_logged(self):
        """Test that an unrecognised item in a comma list warns and leaves the reference unchanged."""
        get_new_version = Mock()
        lines = ["image: python:3.12  # update-time: allow[drift, update<3.13]"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.invalid_specifier.assert_called_once_with("python", "drift", Location(self.path, 1))

    def test_repeated_marker_prefixes_still_combine(self):
        """Test that the older form of combining directives, repeating the `# update-time:` prefix, still works."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: ignore[update>=3.13] # update-time: ignore[stale]"]
        new_lines = self.rewrite(lines, REGEXP, get_new_version)
        self.assertEqual([lines[0].replace("python:3.12 ", "python:3.12.9 ")], new_lines)
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.IGNORE, "update>=3.13"))
        self.logger.warn_if_stale.assert_not_called()

    def test_redundant_bound_is_warned(self):
        """Test that a bound that never has an effect for the current version is warned about."""
        marker = Marker(version_bound=bound(Verb.ALLOW, "update>=3.12"))
        lines = ["image: python:3.12  # update-time: allow[update>=3.12]"]
        self.rewrite(lines, REGEXP, new_version_getter("3.12"))
        self.logger.warn_if_redundant_bound.assert_called_once_with("python", marker, "3.12", Location(self.path, 1))

    def test_allow_update_without_specifier_is_a_noop(self):
        """Test that a bare `allow[update]` (no specifier) applies the update with no bound (the keep-all NO_BOUND)."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.15"))
        lines = ["image: python:3.14  # update-time: allow[update]"]
        new_lines = self.rewrite(lines, REGEXP, get_new_version)
        self.assertEqual(new_lines, ["image: python:3.15  # update-time: allow[update]"])
        get_new_version.assert_called_once_with("python", "3.14", NO_BOUND)

    def test_invalid_specifier_is_logged_and_leaves_reference_unchanged(self):
        """Test that an unparsable specifier is logged and the reference left unchanged, without querying the source."""
        get_new_version = Mock()
        lines = ["image: python:3.12  # update-time: allow[update@@@]"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.invalid_specifier.assert_called_once_with("python", "@@@", Location(self.path, 1))

    def test_invalid_specifier_above_line_is_logged_and_leaves_reference_unchanged(self):
        """Test that an unparsable specifier in a comment above the reference is reported for the reference below."""
        get_new_version = Mock()
        lines = ["# update-time: allow[update@@@]", "image: python:3.12"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.invalid_specifier.assert_called_once_with("python", "@@@", Location(self.path, 2))

    def test_invalid_ignore_specifier_warns_rather_than_freezing(self):
        """Test that a malformed `ignore` bound warns and leaves the reference unchanged, not silently freezes.

        A mistyped `ignore[update…]` bound must be reported like any invalid item, not fall back to a bare
        `ignore` that freezes the reference with no warning. The malformed-bound-versus-unrecognised-item verdict
        reaches the marker parser as the `InvalidSpecifier` the bound constructor raises; were it collapsed to a
        plain not-a-bound, this reference would freeze silently.
        """
        get_new_version = Mock()
        lines = ["image: python:3.12  # update-time: ignore[update@@@]"]
        self.assertEqual(lines, self.rewrite(lines, REGEXP, get_new_version))
        get_new_version.assert_not_called()
        self.logger.invalid_specifier.assert_called_once_with("python", "@@@", Location(self.path, 1))
        self.logger.ignored.assert_not_called()  # reported as invalid, not frozen as a bare `ignore`


class MarkerForwardingTest(unittest.TestCase):
    """Unit test that the rewrite engine hands a matched reference's parsed marker to the logger.

    How `parse_marker` captures the text and how `raw_marker` filters it are covered in `test_marker`; this checks
    only the wiring — that the engine forwards the marker carrying that text to `applying_marker`, the DEBUG line
    the README points at for confirming a marker was recognised.
    """

    def test_engine_forwards_the_verbatim_marker(self):
        """Test that the marker reaching `applying_marker` carries its directives exactly as written."""
        logger = Mock()
        lines = ["image: python:3.12  # update-time: ignore[update] ignore[stale]"]
        update_references_in_lines(
            lines, REGEXP, get_new_version=new_version_getter("3.15"), logger=logger, path=Mock()
        )
        logger.applying_marker.assert_called_once()
        marker = cast("Marker", logger.applying_marker.call_args.args[1])
        self.assertEqual(marker.raw_marker(), "ignore[update] ignore[stale]")
