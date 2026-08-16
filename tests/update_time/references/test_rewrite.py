"""Unit tests for the reference-rewriting engine."""

import unittest
from typing import TYPE_CHECKING, cast
from unittest.mock import ANY, Mock

from update_time.domain.bound import NO_BOUND, Verb
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion
from update_time.domain.directive import Reason
from update_time.domain.drift import ALLOW_HASH_DRIFT, DriftedPin
from update_time.domain.line import located_lines
from update_time.domain.marker import Marker, Scope
from update_time.domain.reference import Reference
from update_time.primitives.location import Location
from update_time.references.rewrite import update_references_in_lines

from tests.helpers import patch_environ
from tests.mutation import kills
from tests.update_time.fixtures import BARE_IGNORE, DIGEST
from tests.update_time.fixtures import COMMIT_SHA1 as OLD_SHA
from tests.update_time.fixtures import COMMIT_SHA2 as NEW_SHA
from tests.update_time.fixtures import DIGEST1 as OLD_DIGEST
from tests.update_time.fixtures import DIGEST2 as NEW_DIGEST
from tests.update_time.helpers import bound, new_version_getter, reference

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter

_REGEXP = r"image: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)"
_SHA_REGEXP = r"image: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)(?:@(?P<sha>sha256:[a-f0-9]{64}))?"
_ACTION_REGEXP = r"uses: (?P<dependency>[\w\d\./-]+)@(?P<sha>[a-f0-9]{40}) # v?(?P<version>[\d\w\.\-]+)"


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
            located_lines(self.path, lines), regexp, get_new_version=get_new_version, logger=self.logger
        )

    def reference(self, dependency: str = "python", version: str = "3.14", line: int = 1) -> Reference:
        """Return the reference the engine hands the logger, for the line it sits on."""
        return reference(dependency, Location(self.path, line), version)

    def drifted(self, line: int = 1) -> DriftedPin:
        """Return the drifted pin the re-pushed `python:3.14` reference these tests use produces."""
        return DriftedPin("python", "3.14", Location(self.path, line), OLD_DIGEST, new_sha=NEW_DIGEST)

    def test_no_reference(self):
        """Test that lines without a reference are returned unchanged."""
        lines = ["line1", "line2"]
        self.assertEqual(self.rewrite(lines, "regexp", new_version_getter("1.1")), lines)
        self.logger.new_version.assert_not_called()

    def test_empty_file(self):
        """Test that a file without any lines is returned unchanged."""
        self.assertEqual(self.rewrite([], _REGEXP, new_version_getter("3.15")), [])
        self.logger.new_version.assert_not_called()

    def test_new_version(self):
        """Test that a reference is updated to the new version, logged at its own 1-based line."""
        new_lines = self.rewrite(["line1", "image: python:3.14"], _REGEXP, new_version_getter("3.15"))
        self.assertEqual(new_lines, ["line1", "image: python:3.15"])
        # "line1" then the reference, so the reference is on line 2.
        self.logger.new_version.assert_called_with(self.reference(line=2), DependencyVersion(version="3.15"))

    def test_reference_logged_at_its_own_line_not_the_marker_line(self):
        """Test that a reference governed by a standalone marker is logged at the reference's line, not the marker's."""
        lines = ["line1", "# update-time: ignore[stale]", "image: python:3.14"]
        self.rewrite(lines, _REGEXP, new_version_getter("3.15"))
        # The marker sits on line 2 and applies to the reference on line 3; the reported line is the reference's.
        self.logger.new_version.assert_called_with(self.reference(line=3), DependencyVersion(version="3.15"))

    def test_new_version_with_sha(self):
        """Test that both the version and the digest of an already-pinned reference are updated."""
        new_lines = self.rewrite(
            [f"uses: action/action@{OLD_SHA} # v3.14"], _ACTION_REGEXP, new_version_getter("3.15", NEW_SHA)
        )
        self.assertEqual(new_lines, [f"uses: action/action@{NEW_SHA} # v3.15"])
        self.logger.new_version.assert_called_with(
            self.reference("action/action", "3.14", line=1), DependencyVersion(version="3.15", sha=NEW_SHA)
        )

    def test_unchanged_version(self):
        """Test that a reference already at the latest version is left unchanged."""
        lines = ["line1", "image: python:3.14"]
        self.assertEqual(self.rewrite(lines, _REGEXP, new_version_getter("3.14")), lines)
        self.logger.new_version.assert_not_called()

    def test_pin_unpinned_at_latest_version(self):
        """Test that an unpinned reference at the latest version is pinned, logging a pin rather than a new version."""
        new_lines = self.rewrite(["line1", "image: python:3.14"], _SHA_REGEXP, new_version_getter("3.14", DIGEST))
        self.assertEqual(new_lines, ["line1", f"image: python:3.14@{DIGEST}"])
        self.logger.pinned.assert_called_with(self.reference(line=2), DependencyVersion(version="3.14", sha=DIGEST))
        self.logger.new_version.assert_not_called()
        self.logger.digest_drift.assert_not_called()  # An unpinned reference has no pinned digest to drift from.

    def test_digest_drift_warns_without_rewriting(self):
        """Test that a pinned reference whose digest changed at the registry is warned about, not rewritten."""
        lines = [f"image: python:3.14@{OLD_DIGEST}"]
        self.assertEqual(self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.14", NEW_DIGEST)), lines)
        self.logger.digest_drift.assert_called_once_with(self.drifted())
        self.logger.new_version.assert_not_called()
        self.logger.pinned.assert_not_called()

    def test_matching_digest_not_warned(self):
        """Test that a pinned reference whose digest is unchanged is left alone, without a drift warning."""
        lines = [f"image: python:3.14@{DIGEST}"]
        self.assertEqual(self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.14", DIGEST)), lines)
        self.logger.digest_drift.assert_not_called()

    def test_pin_unpinned_with_new_version(self):
        """Test that an unpinned reference is pinned and bumped to the latest version at the same time."""
        new_lines = self.rewrite(["line1", "image: python:3.14"], _SHA_REGEXP, new_version_getter("3.15", DIGEST))
        self.assertEqual(new_lines, ["line1", f"image: python:3.15@{DIGEST}"])
        self.logger.new_version.assert_called_with(
            self.reference(line=2), DependencyVersion(version="3.15", sha=DIGEST)
        )

    def test_unpinned_left_alone_without_digest(self):
        """Test that an unpinned reference is not pinned when no digest is available."""
        lines = ["line1", "image: python:3.14"]
        self.assertEqual(self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.14")), lines)
        self.logger.new_version.assert_not_called()

    def test_new_version_sorting_lower_than_current(self):
        """Test the regression where a newer version sorts lexicographically lower than the current one."""
        new_lines = self.rewrite(["line1", "image: python:3.9"], _REGEXP, new_version_getter("3.10"))
        self.assertEqual(new_lines, ["line1", "image: python:3.10"])
        self.logger.new_version.assert_called_with(
            self.reference("python", "3.9", line=2), DependencyVersion(version="3.10")
        )

    def test_version_from_source_applied_even_when_lower(self):
        """Test that any differing version the getter returns is applied, trusting the source."""
        new_lines = self.rewrite(["line1", "image: python:3.14"], _REGEXP, new_version_getter("3.13"))
        self.assertEqual(new_lines, ["line1", "image: python:3.13"])
        self.logger.new_version.assert_called_with(self.reference(line=2), DependencyVersion(version="3.13"))

    def test_inline_ignore_marker_pins_line(self):
        """Test that an inline `# update-time: ignore` comment leaves the line untouched, looking up no version."""
        get_new_version = Mock()
        lines = ["image: python:3.14  # update-time: ignore"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", BARE_IGNORE, Location(self.path, 1))

    def test_preceding_ignore_marker_pins_next_line(self):
        """Test that a standalone `# update-time: ignore` comment pins the reference on the line below it.

        The marker comment itself carries no reference, so only the reference below it is logged as ignored.
        """
        get_new_version = Mock()
        lines = ["# update-time: ignore", "image: python:3.14"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", BARE_IGNORE, Location(self.path, 2))

    def test_inline_marker_does_not_pin_following_line(self):
        """Test that an inline marker pins only its own line, not the reference on the line below it."""
        lines = ["image: a:3.14  # update-time: ignore", "image: b:3.14"]
        new_lines = self.rewrite(lines, _REGEXP, new_version_getter("3.15"))
        self.assertEqual(new_lines, ["image: a:3.14  # update-time: ignore", "image: b:3.15"])
        self.logger.ignored.assert_called_once_with("a", BARE_IGNORE, Location(self.path, 1))

    def test_inverted_item_reported_beside_a_marker_holding_every_check_back(self):
        """Test that an inverted comparison beside a bare `ignore` is reported, with no source queried for it."""
        get_new_version = Mock()
        lines = ["image: python:3.14  # update-time: ignore ignore[stale>=90]"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        self.logger.inverted_stale_item.assert_called_once_with(self.reference(), "stale>=90")
        get_new_version.assert_not_called()

    def test_a_dead_comparison_item_is_reported_however_the_reference_is_held_back(self):
        """Test that an item a source cannot apply is reported, a bare `ignore` beside it included."""
        held_back = "ignore[update] ignore[stale] ignore[yanked]"
        cases = {
            "a cooldown beside the scopes the source answers": (
                f"{held_back} ignore[cooldown<30]",
                "ignore[cooldown<30]",
                Reason.NO_COOLDOWN_DATES,
            ),
            "a cooldown beside a bare ignore": (
                "ignore ignore[cooldown<30]",
                "ignore[cooldown<30]",
                Reason.NO_COOLDOWN_DATES,
            ),
            "a threshold beside a bare ignore": (
                "ignore ignore[stale<90]",
                "ignore[stale<90]",
                Reason.NO_STALENESS_DATES,
            ),
            "a level beside a bare ignore": (
                "ignore ignore[vulnerable<high]",
                "ignore[vulnerable<high]",
                Reason.NO_VULNERABILITY_REPORTS,
            ),
        }
        for case, (marker_text, directive, reason) in cases.items():
            with self.subTest(case=case):
                self.logger.reset_mock()
                lines = [f"image: python:3.14  # update-time: {marker_text}"]
                self.assertEqual(self.rewrite(lines, _REGEXP, new_version_getter("3.15")), lines)
                self.logger.redundant_directive.assert_any_call(self.reference(), directive, reason)

    def test_only_a_scope_the_marker_spells_out_is_reported_redundant(self):
        """Test that a scope the marker spells out is reported redundant, where one a bare `ignore` implies is not."""
        every_scope = "ignore[update] ignore[stale] ignore[yanked] ignore[vulnerable]"
        cases = {
            "spelled out": (every_scope, True, True),
            "implied by a bare ignore": ("ignore", False, False),
            "one spelled out beside a bare ignore": ("ignore ignore[yanked]", True, False),
        }
        for case, (directive, yanked, stale) in cases.items():
            with self.subTest(case=case):
                self.logger.reset_mock()
                lines = [f"image: python:3.14  # update-time: {directive}"]
                self.assertEqual(self.rewrite(lines, _REGEXP, new_version_getter("3.15")), lines)
                reasons = [call.args[2] for call in self.logger.redundant_directive.call_args_list]
                self.assertEqual(Reason.NO_YANK_CONCEPT in reasons, yanked)
                self.assertEqual(Reason.NO_STALENESS_DATES in reasons, stale)

    def test_a_redundant_item_is_reported_under_the_directive_that_set_it(self):
        """Test that the warning names the item the reader wrote, not the scope a bare `ignore` beside it implies."""
        lines = ["image: python:3.14  # update-time: ignore ignore[stale<90]"]
        self.assertEqual(self.rewrite(lines, _REGEXP, new_version_getter("3.15")), lines)
        self.logger.redundant_directive.assert_called_once_with(
            self.reference(), "ignore[stale<90]", Reason.NO_STALENESS_DATES
        )

    def test_inline_slash_slash_marker_pins_line(self):
        """Test that a `//`-style ignore marker (as JSONC/devcontainer.json uses) also pins a line inline."""
        get_new_version = Mock()
        lines = ["image: python:3.14  // update-time: ignore"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", BARE_IGNORE, Location(self.path, 1))

    def test_preceding_slash_slash_marker_pins_next_line(self):
        """Test that a standalone `//` marker comment pins the reference on the line below it."""
        get_new_version = Mock()
        lines = ["// update-time: ignore", "image: python:3.14"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        get_new_version.assert_not_called()
        self.logger.ignored.assert_called_once_with("python", BARE_IGNORE, Location(self.path, 2))

    def test_ignore_update_marker_skips_update_but_still_checks_staleness(self):
        """Test that `ignore[update]` leaves the version unchanged but still runs the staleness check."""
        lines = ["image: python:3.14  # update-time: ignore[update]"]
        self.assertEqual(self.rewrite(lines, _REGEXP, new_version_getter("3.15")), lines)  # version left as-is
        self.logger.report_staleness.assert_called_once()  # staleness still checked
        self.logger.ignored.assert_called_once_with(
            "python", Marker(ignored_scopes=Scope.UPDATE), Location(self.path, 1)
        )

    def test_ignore_update_and_stale_still_checks_for_a_yank(self):
        """Test that a scope the marker leaves live keeps the reference queried, so its check still runs.

        `ignore[update]` and `ignore[stale]` silence two of the three scopes the gate reads; the yank check is not
        held back, so the source is still queried for it rather than the reference being skipped outright.
        """
        lines = ["image: python:3.14  # update-time: ignore[update] ignore[stale]"]
        self.assertEqual(self.rewrite(lines, _REGEXP, new_version_getter("3.15")), lines)  # version left as-is
        marker = Marker(ignored_scopes=Scope.UPDATE | Scope.STALE)
        # The source is queried for the yank, and both reports are handed the marker that holds the staleness back.
        self.logger.report_yank.assert_called_once_with(ANY, marker)
        self.logger.report_staleness.assert_called_once_with(ANY, marker, ANY)

    @kills(
        "src/update_time/references/resolve.py",
        "log.report_staleness(resolved, marker, marker.stale.value_or(STALE_AFTER.get()))",
        "log.report_staleness(resolved, marker.frozen, marker.stale.value_or(STALE_AFTER.get()))",
    )
    def test_ignore_stale_marker_still_updates_and_reports_with_the_marker(self):
        """Test that `ignore[stale]` applies the update and reaches the logger, which decides the warning itself."""
        lines = ["image: python:3.14  # update-time: ignore[stale]"]
        new_lines = self.rewrite(lines, _REGEXP, new_version_getter("3.15"))
        self.assertEqual(new_lines, ["image: python:3.15  # update-time: ignore[stale]"])  # version bumped
        self.logger.report_staleness.assert_called_once_with(ANY, Marker(ignored_scopes=Scope.STALE), ANY)
        self.logger.ignored.assert_not_called()  # the update is not held back, so nothing is logged as ignored

    def test_inverted_stale_item_holds_nothing_back(self):
        """Test that a `stale` item whose comparison is inverted is reported while the reference still updates."""
        lines = ["image: python:3.14  # update-time: ignore[stale>=90]"]
        new_lines = self.rewrite(lines, _REGEXP, new_version_getter("3.15"))
        self.assertEqual(new_lines, ["image: python:3.15  # update-time: ignore[stale>=90]"])  # version bumped
        reference = Reference("python", "3.14", Location(self.path, 1))
        self.logger.inverted_stale_item.assert_called_once_with(reference, "stale>=90")
        self.logger.ignored.assert_not_called()  # the update is not held back, so nothing is logged as ignored

    def test_inverted_item_reported_although_the_reference_is_held_back(self):
        """Test that an inverted comparison is reported beside the three scopes that skip the source."""
        get_new_version = Mock()
        scopes = "ignore[update] ignore[stale] ignore[yanked]"
        lines = [f"image: python:3.14  # update-time: {scopes} ignore[stale>=90]"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        self.logger.inverted_stale_item.assert_called_once_with(self.reference(), "stale>=90")
        get_new_version.assert_not_called()  # The warning costs no request, the item being unreadable on its own.

    @kills(
        "src/update_time/domain/marker.py",
        'return (self.name or "").lower().replace("_", "-")',
        'return (self.name or "").lower()',
    )
    def test_allow_hash_drift_marker_adopts_new_digest(self):
        """Test that an inline `allow[hash-drift]` marker re-pins a re-pushed tag's digest instead of warning."""
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: allow[hash-drift]"]
        new_lines = self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.14", NEW_DIGEST))
        self.assertEqual(new_lines, [f"image: python:3.14@{NEW_DIGEST}  # update-time: allow[hash-drift]"])
        self.logger.adopted_drift.assert_called_once_with(self.drifted(), "update-time: allow[hash-drift]")
        self.logger.digest_drift.assert_not_called()

    def test_allow_hash_drift_marker_above_line_adopts(self):
        """Test that a standalone `allow[hash-drift]` comment opts the reference on the line below it in."""
        lines = ["# update-time: allow[hash-drift]", f"image: python:3.14@{OLD_DIGEST}"]
        new_lines = self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.14", NEW_DIGEST))
        self.assertEqual(new_lines, ["# update-time: allow[hash-drift]", f"image: python:3.14@{NEW_DIGEST}"])
        self.logger.adopted_drift.assert_called_once_with(self.drifted(2), "update-time: allow[hash-drift]")

    def test_allow_hash_drift_marker_is_noop_when_version_also_changed(self):
        """Test that when the version has moved too, the normal update path runs and the marker doesn't apply."""
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: allow[hash-drift]"]
        new_lines = self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.15", NEW_DIGEST))
        self.assertEqual(new_lines, [f"image: python:3.15@{NEW_DIGEST}  # update-time: allow[hash-drift]"])
        self.logger.new_version.assert_called_once()  # a real version bump, not a drift adoption
        self.logger.adopted_drift.assert_not_called()

    def test_ignore_wins_over_allow_hash_drift_marker(self):
        """Test that a reference marked both `ignore` and `allow[hash-drift]` is left untouched: `ignore` wins."""
        get_new_version = Mock()
        lines = ["# update-time: allow[hash-drift]", f"image: python:3.14@{OLD_DIGEST}  # update-time: ignore"]
        self.assertEqual(self.rewrite(lines, _SHA_REGEXP, get_new_version), lines)
        get_new_version.assert_not_called()
        self.logger.adopted_drift.assert_not_called()
        self.logger.digest_drift.assert_not_called()

    def test_flag_adopts_digest_drift_repo_wide(self):
        """Test that the --allow-hash-drift flag (via its env var) adopts drift without a per-line marker."""
        lines = [f"image: python:3.14@{OLD_DIGEST}"]
        with patch_environ({ALLOW_HASH_DRIFT.name: "1"}):
            new_lines = self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.14", NEW_DIGEST))
        self.assertEqual(new_lines, [f"image: python:3.14@{NEW_DIGEST}"])
        self.logger.adopted_drift.assert_called_once_with(self.drifted(), "--allow-hash-drift")
        self.logger.digest_drift.assert_not_called()

    def test_ignore_wins_over_allow_hash_drift_flag(self):
        """Test that an `ignore` marker still wins over the global --allow-hash-drift flag."""
        get_new_version = Mock()
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: ignore"]
        with patch_environ({ALLOW_HASH_DRIFT.name: "1"}):
            self.assertEqual(self.rewrite(lines, _SHA_REGEXP, get_new_version), lines)
        get_new_version.assert_not_called()
        self.logger.adopted_drift.assert_not_called()

    def test_allow_update_bound_passes_bound_to_source(self):
        """Test that an inline `allow[update<…>]` marker passes the bound to the source and applies the result."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: allow[update<3.13]"]
        new_lines = self.rewrite(lines, _REGEXP, get_new_version)
        self.assertEqual(new_lines, ["image: python:3.12.9  # update-time: allow[update<3.13]"])
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.ALLOW, "update<3.13"), COOLDOWN.default)

    def test_cooldown_marker_passes_its_cooldown_to_source(self):
        """Test that an inline `ignore[cooldown<…>]` marker passes its own cooldown to the source, not the global."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: ignore[cooldown<30]"]
        new_lines = self.rewrite(lines, _REGEXP, get_new_version)
        self.assertEqual(new_lines, ["image: python:3.12.9  # update-time: ignore[cooldown<30]"])
        get_new_version.assert_called_once_with("python", "3.12", NO_BOUND, 30)

    def test_inverted_cooldown_marker_reports_and_still_updates(self):
        """Test that an inverted `cooldown` marker is reported and the reference updated under the global cooldown."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: ignore[cooldown>=30]"]
        new_lines = self.rewrite(lines, _REGEXP, get_new_version)
        self.assertEqual(new_lines, ["image: python:3.12.9  # update-time: ignore[cooldown>=30]"])
        get_new_version.assert_called_once_with("python", "3.12", NO_BOUND, COOLDOWN.default)
        self.logger.inverted_cooldown_item.assert_called_once_with(ANY, "cooldown>=30")
        self.logger.invalid_specifier.assert_not_called()

    def test_ignore_update_bound_passes_bound_to_source(self):
        """Test that an inline `ignore[update>=…]` marker passes the complement (drop) bound to the source."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: ignore[update>=3.13]"]
        self.rewrite(lines, _REGEXP, get_new_version)
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.IGNORE, "update>=3.13"), COOLDOWN.default)

    def test_ignore_level_bound_passes_bound_to_source(self):
        """Test that an inline `ignore[minor-update]` marker passes the level bound to the source."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12.1  # update-time: ignore[minor-update]"]
        new_lines = self.rewrite(lines, _REGEXP, get_new_version)
        self.assertEqual(new_lines, ["image: python:3.12.9  # update-time: ignore[minor-update]"])
        get_new_version.assert_called_once_with(
            "python", "3.12.1", bound(Verb.IGNORE, "minor-update"), COOLDOWN.default
        )

    def test_allow_level_bound_passes_bound_to_source(self):
        """Test that an inline `allow[minor-update]` marker passes the level bound to the source."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.13.0"))
        lines = ["image: python:3.12.1  # update-time: allow[minor-update]"]
        self.rewrite(lines, _REGEXP, get_new_version)
        get_new_version.assert_called_once_with("python", "3.12.1", bound(Verb.ALLOW, "minor-update"), COOLDOWN.default)

    def test_level_bound_marker_above_line_passes_bound(self):
        """Test that a standalone `ignore[major-update]` comment bounds the reference on the line below it."""
        get_new_version = Mock(return_value=DependencyVersion(version="7.4"))
        lines = ["# update-time: ignore[major-update]", "image: redis:7.2"]
        new_lines = self.rewrite(lines, _REGEXP, get_new_version)
        self.assertEqual(new_lines, ["# update-time: ignore[major-update]", "image: redis:7.4"])
        get_new_version.assert_called_once_with("redis", "7.2", bound(Verb.IGNORE, "major-update"), COOLDOWN.default)

    def test_bare_ignore_wins_over_level_bound(self):
        """Test that a reference marked both `ignore` and a level bound is left untouched: `ignore` wins."""
        get_new_version = Mock()
        lines = ["# update-time: ignore", "image: python:3.12  # update-time: allow[minor-update]"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        get_new_version.assert_not_called()

    def test_level_bound_combines_with_hash_drift_in_one_bracket(self):
        """Test that an `allow` bracket combines a level bound with the hash-drift opt-in."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.14", sha=NEW_DIGEST))
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: allow[minor-update, hash-drift]"]
        new_lines = self.rewrite(lines, _SHA_REGEXP, get_new_version)
        self.assertEqual(new_lines, [lines[0].replace(OLD_DIGEST, NEW_DIGEST)])  # the drift opt-in is honoured
        get_new_version.assert_called_once_with("python", "3.14", bound(Verb.ALLOW, "minor-update"), COOLDOWN.default)

    def test_redundant_level_bound_is_warned(self):
        """Test that a level bound that blocks every update is warned about."""
        lines = ["image: python:3.12  # update-time: ignore[patch-update]"]
        self.rewrite(lines, _REGEXP, new_version_getter("3.12"))
        marker = Marker(version_bound=bound(Verb.IGNORE, "patch-update"))
        reference = Reference("python", "3.12", Location(self.path, 1))
        self.logger.warn_if_redundant_bound.assert_called_once_with(reference, marker)

    def test_bound_marker_above_line_passes_bound(self):
        """Test that a standalone `allow[update<…>]` comment bounds the reference on the line below it."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["# update-time: allow[update<3.13]", "image: python:3.12"]
        new_lines = self.rewrite(lines, _REGEXP, get_new_version)
        self.assertEqual(new_lines, ["# update-time: allow[update<3.13]", "image: python:3.12.9"])
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.ALLOW, "update<3.13"), COOLDOWN.default)

    def test_directive_list_combines_bound_and_hash_drift(self):
        """Test that a bound and an `allow[hash-drift]` directive listed after one prefix both apply."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.14", sha=NEW_DIGEST))
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: allow[update<3.15] allow[hash-drift]"]
        new_lines = self.rewrite(lines, _SHA_REGEXP, get_new_version)
        self.assertEqual(new_lines, [lines[0].replace(OLD_DIGEST, NEW_DIGEST)])  # the drift opt-in is honoured
        get_new_version.assert_called_once_with("python", "3.14", bound(Verb.ALLOW, "update<3.15"), COOLDOWN.default)
        # The cause names the reference's `allow` directives verbatim, the bound alongside the hash-drift opt-in.
        self.logger.adopted_drift.assert_called_once_with(
            self.drifted(), "update-time: allow[update<3.15] allow[hash-drift]"
        )

    def test_directive_list_combines_ignore_stale_and_bound(self):
        """Test that an `ignore[stale]` and an `allow` bound directive listed after one prefix both apply."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: ignore[stale] allow[update<3.13]"]
        new_lines = self.rewrite(lines, _REGEXP, get_new_version)
        self.assertEqual(new_lines, [lines[0].replace("python:3.12 ", "python:3.12.9 ")])
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.ALLOW, "update<3.13"), COOLDOWN.default)
        # The `stale` item is parsed alongside the bound, so the marker reaching the logger carries both:
        stale_and_bound = Marker(ignored_scopes=Scope.STALE, version_bound=bound(Verb.ALLOW, "update<3.13"))
        self.logger.report_staleness.assert_called_once_with(ANY, stale_and_bound, ANY)

    def test_directive_list_followed_by_reason(self):
        """Test that free text after the last directive (a reason) is allowed and ends the directive list."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: allow[update<3.13] (pinned until the 3.13 migration)"]
        new_lines = self.rewrite(lines, _REGEXP, get_new_version)
        self.assertEqual(new_lines, [lines[0].replace("python:3.12 ", "python:3.12.9 ")])
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.ALLOW, "update<3.13"), COOLDOWN.default)

    def test_typo_ends_directive_list(self):
        """Test that a mistyped directive ends the list as a reason: the directives before it still apply."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.14", sha=NEW_DIGEST))
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: ignore[stale] alloww[hash-drift]"]
        self.assertEqual(self.rewrite(lines, _SHA_REGEXP, get_new_version), lines)
        # The `ignore[stale]` before the typo is parsed, so it reaches the logger:
        self.logger.report_staleness.assert_called_once_with(ANY, Marker(ignored_scopes=Scope.STALE), ANY)
        self.logger.digest_drift.assert_called_once()  # the mistyped drift opt-in is not, so the drift only warns

    def assert_invalid_bracket_item(self, directive: str, bracket_item: str) -> None:
        """Assert that the bracket item is logged as invalid, leaving the reference unchanged but still checked."""
        self.logger.reset_mock()
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.1"))
        lines = [f"image: python:3.12.1  # update-time: {directive}"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)  # left unchanged, not updated
        get_new_version.assert_called_once()
        self.logger.invalid_bracket_item.assert_called_once_with("python", bracket_item, Location(self.path, 1))
        self.logger.ignored.assert_not_called()  # reported as invalid, not frozen as a bare `ignore`
        self.logger.recognised_marker.assert_not_called()

    def test_unrecognised_item_warns_and_leaves_reference_unchanged(self):
        """Test that a single unrecognised bracket item warns and leaves the reference unchanged, under either verb."""
        items = {
            "ignore[updaet]": "updaet",  # a scope typo
            "ignore[mega-update]": "mega-update",  # a level-name typo
            "allow[patch-updates]": "patch-updates",  # a plural typo of `patch-update`
            "ignore[]": "",  # a bracket with nothing in it to recognise
        }
        for directive, item in items.items():
            with self.subTest(directive=directive):
                self.assert_invalid_bracket_item(directive, item)

    def test_unterminated_bracket_warns_and_leaves_reference_unchanged(self):
        """Test that a bracket left unclosed warns under either verb, reporting the unclosed bracket."""
        for directive in ("ignore[update<4", "allow[update<4"):
            with self.subTest(directive=directive):
                self.assert_invalid_bracket_item(directive, "[update<4")

    def test_comma_separated_items_combine_in_one_bracket(self):
        """Test that a bracket combines comma-separated items: `ignore[stale, update>=3.13]` bounds and silences."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: ignore[stale, update>=3.13]"]
        new_lines = self.rewrite(lines, _REGEXP, get_new_version)
        self.assertEqual(new_lines, [lines[0].replace("python:3.12 ", "python:3.12.9 ")])
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.IGNORE, "update>=3.13"), COOLDOWN.default)
        stale_and_bound = Marker(ignored_scopes=Scope.STALE, version_bound=bound(Verb.IGNORE, "update>=3.13"))
        self.logger.report_staleness.assert_called_once_with(ANY, stale_and_bound, ANY)

    def test_comma_separated_allow_items_combine_in_one_bracket(self):
        """Test that an `allow` bracket combines a bound with the hash-drift opt-in."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.14", sha=NEW_DIGEST))
        lines = [f"image: python:3.14@{OLD_DIGEST}  # update-time: allow[update<3.15, hash-drift]"]
        new_lines = self.rewrite(lines, _SHA_REGEXP, get_new_version)
        self.assertEqual(new_lines, [lines[0].replace(OLD_DIGEST, NEW_DIGEST)])  # the drift opt-in is honoured
        get_new_version.assert_called_once_with("python", "3.14", bound(Verb.ALLOW, "update<3.15"), COOLDOWN.default)

    def test_compound_specifier_keeps_its_comma_inside_a_bracket_list(self):
        """Test that a compound specifier's commas are kept apart from the commas that separate bracket items."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: ignore[update>=3.13,<3.15, stale]"]
        self.rewrite(lines, _REGEXP, get_new_version)
        get_new_version.assert_called_once_with(
            "python", "3.12", bound(Verb.IGNORE, "update>=3.13,<3.15"), COOLDOWN.default
        )
        compound = Marker(ignored_scopes=Scope.STALE, version_bound=bound(Verb.IGNORE, "update>=3.13,<3.15"))
        self.logger.report_staleness.assert_called_once_with(ANY, compound, ANY)

    def test_combined_ignore_scopes_hold_back_everything(self):
        """Test that every `ignore` scope combined holds back as much as a bare `ignore`."""
        get_new_version = Mock()
        scopes = "ignore[update] ignore[stale] ignore[yanked] ignore[vulnerable]"
        lines = [f"image: python:3.12  # update-time: {scopes}"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        get_new_version.assert_not_called()  # Every aspect is held back, so the source is not even queried.
        self.logger.ignored.assert_called_once_with("python", BARE_IGNORE, Location(self.path, 1))

    def test_a_marker_naming_update_stale_and_yanked_skips_the_source(self):
        """Test that the three scopes the gate reads skip the source without a `vulnerable` scope beside them."""
        get_new_version = Mock()
        lines = ["image: python:3.12  # update-time: ignore[update] ignore[stale] ignore[yanked]"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        get_new_version.assert_not_called()

    def test_unrecognised_item_in_comma_list_is_logged(self):
        """Test that an unrecognised item in a comma list warns and leaves the reference unchanged."""
        get_new_version = new_version_getter("3.12")
        lines = ["image: python:3.12  # update-time: allow[drift, update<3.13]"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        self.logger.invalid_bracket_item.assert_called_once_with("python", "drift", Location(self.path, 1))

    def test_repeated_marker_prefixes_still_combine(self):
        """Test that the older form of combining directives, repeating the `# update-time:` prefix, still works."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.12.9"))
        lines = ["image: python:3.12  # update-time: ignore[update>=3.13] # update-time: ignore[stale]"]
        new_lines = self.rewrite(lines, _REGEXP, get_new_version)
        self.assertEqual(new_lines, [lines[0].replace("python:3.12 ", "python:3.12.9 ")])
        get_new_version.assert_called_once_with("python", "3.12", bound(Verb.IGNORE, "update>=3.13"), COOLDOWN.default)
        stale_and_bound = Marker(ignored_scopes=Scope.STALE, version_bound=bound(Verb.IGNORE, "update>=3.13"))
        self.logger.report_staleness.assert_called_once_with(ANY, stale_and_bound, ANY)

    def test_redundant_bound_is_warned(self):
        """Test that a bound that never has an effect for the current version is warned about."""
        marker = Marker(version_bound=bound(Verb.ALLOW, "update>=3.12"))
        lines = ["image: python:3.12  # update-time: allow[update>=3.12]"]
        self.rewrite(lines, _REGEXP, new_version_getter("3.12"))
        reference = Reference("python", "3.12", Location(self.path, 1))
        self.logger.warn_if_redundant_bound.assert_called_once_with(reference, marker)

    def test_redundant_bound_is_warned_although_the_reference_is_held_back(self):
        """Test that a bound is judged for redundancy beside the three scopes that skip the source."""
        get_new_version = Mock()
        scopes = "ignore[update] ignore[stale] ignore[yanked]"
        lines = [f"image: python:3.12  # update-time: {scopes} allow[update>=3.12]"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        marker = Marker(
            ignored_scopes=Scope.UPDATE | Scope.STALE | Scope.YANKED,
            version_bound=bound(Verb.ALLOW, "update>=3.12"),
        )
        reference = Reference("python", "3.12", Location(self.path, 1))
        self.logger.warn_if_redundant_bound.assert_called_once_with(reference, marker)
        get_new_version.assert_not_called()

    def test_allow_update_without_specifier_is_a_noop(self):
        """Test that a bare `allow[update]` (no specifier) applies the update with no bound (the keep-all NO_BOUND)."""
        get_new_version = Mock(return_value=DependencyVersion(version="3.15"))
        lines = ["image: python:3.14  # update-time: allow[update]"]
        new_lines = self.rewrite(lines, _REGEXP, get_new_version)
        self.assertEqual(new_lines, ["image: python:3.15  # update-time: allow[update]"])
        get_new_version.assert_called_once_with("python", "3.14", NO_BOUND, COOLDOWN.default)

    def test_invalid_specifier_is_logged_and_leaves_reference_unchanged(self):
        """Test that an unparsable specifier is logged and the reference left unchanged."""
        get_new_version = new_version_getter("3.12")
        lines = ["image: python:3.12  # update-time: allow[update@@@]"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        self.logger.invalid_bracket_item.assert_called_once_with("python", "@@@", Location(self.path, 1))

    def test_invalid_specifier_above_line_is_logged_and_leaves_reference_unchanged(self):
        """Test that an unparsable specifier in a comment above the reference is reported for the reference below."""
        get_new_version = new_version_getter("3.12")
        lines = ["# update-time: allow[update@@@]", "image: python:3.12"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        self.logger.invalid_bracket_item.assert_called_once_with("python", "@@@", Location(self.path, 2))

    def test_invalid_ignore_specifier_warns_rather_than_freezing(self):
        """Test that a malformed `ignore` bound warns and leaves the reference unchanged, not silently freezes."""
        get_new_version = new_version_getter("3.12")
        lines = ["image: python:3.12  # update-time: ignore[update@@@]"]
        self.assertEqual(self.rewrite(lines, _REGEXP, get_new_version), lines)
        self.logger.invalid_bracket_item.assert_called_once_with("python", "@@@", Location(self.path, 1))
        self.logger.ignored.assert_not_called()  # reported as invalid, not frozen as a bare `ignore`


class MarkerForwardingTest(unittest.TestCase):
    """Unit test that the rewrite engine hands a matched reference's parsed marker to the logger."""

    def test_engine_forwards_the_verbatim_marker(self):
        """Test that the marker reaching `recognised_marker` carries its directives exactly as written."""
        logger = Mock()
        lines = located_lines(Mock(), ["image: python:3.12  # update-time: ignore[update] ignore[stale]"])
        update_references_in_lines(lines, _REGEXP, get_new_version=new_version_getter("3.15"), logger=logger)
        logger.recognised_marker.assert_called_once()
        marker = cast("Marker", logger.recognised_marker.call_args.args[1])
        self.assertEqual(str(marker), "ignore[update] ignore[stale]")
