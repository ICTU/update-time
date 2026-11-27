"""Unit tests for the reference-rewriting engine."""

from dataclasses import replace
from typing import TYPE_CHECKING, Final

from update_time.domain.bound import NO_BOUND, Verb
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion, FloatingPin, PinnedDependency, Yank
from update_time.domain.directive import Reason
from update_time.domain.marker import NO_MARKER, Marker, Scope, Threshold
from update_time.domain.reference import DriftedPin, DriftKind, Reference
from update_time.domain.source import Source
from update_time.primitives.line import located_lines
from update_time.primitives.location import Location
from update_time.references.rewrite import update_references_in_lines

from tests.update_time.base_test_cases import MockLoggerTestCase
from tests.update_time.domain_objects import bound
from tests.update_time.fixtures import COMMIT_SHA1, COMMIT_SHA2, DIGEST, DIGEST1, DIGEST2, IMAGE_REGEXP, STALE_PROJECT

from .getters import mock_new_version_getter, mock_project_getter, new_version_getter

if TYPE_CHECKING:
    from unittest.mock import Mock

    from update_time.domain.bound import NewVersionGetter, VersionBound
    from update_time.domain.dependency import ProjectGetter

_SHA_REGEXP: Final[str] = rf"{IMAGE_REGEXP}(?:@(?P<sha>sha256:[a-f0-9]{{64}}))?"


class UpdateReferencesTest(MockLoggerTestCase):
    """Unit tests for updating the references in a list of lines."""

    def test_no_reference(self):
        """Test that lines without a reference are returned unchanged."""
        lines = ["line1", "line2"]
        self.assertEqual(self.rewrite(lines, "regexp", new_version_getter("1.1")), lines)
        self.logger.new_version.assert_not_called()

    def rewrite(
        self, lines: list[str], regexp: str, get_new_version: NewVersionGetter, get_project: ProjectGetter | None = None
    ) -> list[str]:
        """Run the rewrite engine over the lines with the given regexp, new-version getter, and project getter."""
        source = Source(new_version=get_new_version, project=get_project)
        return update_references_in_lines(located_lines(self.path, lines), regexp, source=source, logger=self.logger)

    def test_empty_file(self):
        """Test that a file without any lines is returned unchanged."""
        self.assertEqual(self.rewrite([], IMAGE_REGEXP, new_version_getter("3.15")), [])
        self.logger.new_version.assert_not_called()

    def test_new_version(self):
        """Test that a reference is updated to the new version, logged at its own 1-based line."""
        new_lines = self.rewrite(["line1", "image: python:3.14"], IMAGE_REGEXP, new_version_getter("3.15"))
        self.assertEqual(new_lines, ["line1", "image: python:3.15"])
        # "line1" then the reference, so the reference is on line 2.
        self.logger.new_version.assert_called_with(self.reference(line=2), DependencyVersion(version="3.15"))

    def reference(
        self,
        dependency: str = "python",
        version: str = "3.14",
        line: int = 1,
        *,
        hash_pin: str | None = None,
        marker: Marker = NO_MARKER,
    ) -> Reference:
        """Return the reference the engine hands the logger, for the line it sits on, steered by the marker.

        `hash_pin` is the hash pin the reference records, empty where it records none. It is None where the regexp does
        not have a place for one.
        """
        location = Location(self.path, line)
        return Reference(dependency, version, location, hash_pin or "", hash_pin is not None, marker=marker)

    def test_reference_logged_at_its_own_line_not_the_marker_line(self):
        """Test that a reference governed by a standalone marker is logged at the reference's line, not the marker's."""
        lines = ["line1", "# update-time: set[stale-after=0]", "image: python:3.14"]
        self.rewrite(lines, IMAGE_REGEXP, new_version_getter("3.15"))
        # The marker sits on line 2 and applies to the reference on line 3; the reported line is the reference's.
        marker = Marker(stale=Threshold(0))
        self.logger.new_version.assert_called_with(
            self.reference(line=3, marker=marker), DependencyVersion(version="3.15")
        )

    def test_new_version_with_sha(self):
        """Test that both the version and the digest of an already-pinned reference are updated."""
        new_lines = self.rewrite(
            [f"uses: action/action@{COMMIT_SHA1} # v3.14"],
            r"uses: (?P<dependency>[\w\d\./-]+)@(?P<sha>[a-f0-9]{40}) # v?(?P<version>[\d\w\.\-]+)",
            new_version_getter("3.15", COMMIT_SHA2),
        )
        self.assertEqual(new_lines, [f"uses: action/action@{COMMIT_SHA2} # v3.15"])
        self.logger.new_version.assert_called_with(
            self.reference("action/action", "3.14", line=1, hash_pin=COMMIT_SHA1),
            DependencyVersion(version="3.15", sha=COMMIT_SHA2),
        )

    def test_unchanged_version(self):
        """Test that a reference already at the latest version is left unchanged."""
        lines = ["line1", "image: python:3.14"]
        self.assertEqual(self.rewrite(lines, IMAGE_REGEXP, new_version_getter("3.14")), lines)
        self.logger.new_version.assert_not_called()

    def test_pin_unpinned_at_latest_version(self):
        """Test that an unpinned reference at the latest version is pinned, logging a pin rather than a new version."""
        new_lines = self.rewrite(["line1", "image: python:3.14"], _SHA_REGEXP, new_version_getter("3.14", DIGEST))
        self.assertEqual(new_lines, ["line1", f"image: python:3.14@{DIGEST}"])
        self.logger.pinned.assert_called_with(
            self.reference(line=2, hash_pin=""), DependencyVersion(version="3.14", sha=DIGEST)
        )
        self.logger.new_version.assert_not_called()
        self.logger.drift.assert_not_called()  # An unpinned reference has no pinned digest to drift from.

    def test_digest_drift_warns_without_rewriting(self):
        """Test that a pinned reference whose digest changed at the registry is warned about, not rewritten."""
        lines = [f"image: python:3.14@{DIGEST1}"]
        self.assertEqual(self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.14", DIGEST2)), lines)
        self.logger.drift.assert_called_once_with(self.drifted())
        self.logger.new_version.assert_not_called()
        self.logger.pinned.assert_not_called()

    def drifted(self, line: int = 1, version: str = "3.14", marker: Marker = NO_MARKER) -> DriftedPin:
        """Return the drifted pin the engine hands the logger, for a re-pushed `python` reference and its marker."""
        return DriftedPin(
            "python",
            version,
            Location(self.path, line),
            DIGEST1,
            can_hold_a_hash_pin=True,
            marker=marker,
            kind=DriftKind.DIGEST,
            new_sha=DIGEST2,
        )

    def test_matching_digest_not_warned(self):
        """Test that a pinned reference whose digest is unchanged is left alone, without a drift warning."""
        lines = [f"image: python:3.14@{DIGEST}"]
        self.assertEqual(self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.14", DIGEST)), lines)
        self.logger.drift.assert_not_called()

    def test_pin_unpinned_with_new_version(self):
        """Test that an unpinned reference is pinned and bumped to the latest version at the same time."""
        new_lines = self.rewrite(["line1", "image: python:3.14"], _SHA_REGEXP, new_version_getter("3.15", DIGEST))
        self.assertEqual(new_lines, ["line1", f"image: python:3.15@{DIGEST}"])
        self.logger.new_version.assert_called_with(
            self.reference(line=2, hash_pin=""), DependencyVersion(version="3.15", sha=DIGEST)
        )

    def test_unpinned_left_alone_without_digest(self):
        """Test that an unpinned reference is not pinned when no digest is available."""
        lines = ["line1", "image: python:3.14"]
        self.assertEqual(self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.14")), lines)
        self.logger.new_version.assert_not_called()

    def test_new_version_sorting_lower_than_current(self):
        """Test the regression where a newer version sorts lexicographically lower than the current one."""
        new_lines = self.rewrite(["line1", "image: python:3.9"], IMAGE_REGEXP, new_version_getter("3.10"))
        self.assertEqual(new_lines, ["line1", "image: python:3.10"])
        self.logger.new_version.assert_called_with(
            self.reference("python", "3.9", line=2), DependencyVersion(version="3.10")
        )

    def test_version_from_source_applied_even_when_lower(self):
        """Test that any differing version the getter returns is applied, trusting the source."""
        new_lines = self.rewrite(["line1", "image: python:3.14"], IMAGE_REGEXP, new_version_getter("3.13"))
        self.assertEqual(new_lines, ["line1", "image: python:3.13"])
        self.logger.new_version.assert_called_with(self.reference(line=2), DependencyVersion(version="3.13"))

    def test_an_ignore_marker_leaves_its_reference_untouched(self):
        """Test that an `ignore[update]` after `#` or `//` holds back the reference inline or on the line below.

        The marker comment itself does not carry a reference, so only the reference it steers is logged as ignored.
        """
        cases = {
            "# above the line": (["# update-time: ignore[update]", "image: python:3.14"], 2),
            "// inline": (["image: python:3.14  // update-time: ignore[update]"], 1),
            "// above the line": (["// update-time: ignore[update]", "image: python:3.14"], 2),
        }
        for case, (lines, line) in cases.items():
            with self.subTest(case):
                self.assertEqual(self.rewrite(lines, IMAGE_REGEXP, new_version_getter("3.15")), lines)
                self.assert_update_held_back(line=line)

    def assert_update_held_back(self, dependency: str = "python", line: int = 1) -> None:
        """Assert the reference on the line was logged as held back by an `ignore[update]`."""
        marker = Marker(ignored_scopes=Scope.UPDATE)
        self.logger.ignored.assert_called_once_with(self.reference(dependency, line=line, marker=marker))

    def test_inline_marker_leaves_following_line_alone(self):
        """Test that an inline marker steers only a reference on its own line, never on the line below it."""
        lines = ["image: a:3.14  # update-time: ignore[update]", "image: b:3.14"]
        new_lines = self.rewrite(lines, IMAGE_REGEXP, new_version_getter("3.15"))
        self.assertEqual(new_lines, ["image: a:3.14  # update-time: ignore[update]", "image: b:3.15"])
        self.assert_update_held_back("a")

    def test_ignore_update_marker_skips_update_but_still_checks_staleness(self):
        """Test that `ignore[update]` leaves the version unchanged but still runs the staleness check."""
        lines = ["image: python:3.14  # update-time: ignore[update]"]
        new_lines = self.rewrite(lines, IMAGE_REGEXP, new_version_getter("3.15"), mock_project_getter(STALE_PROJECT))
        self.assertEqual(new_lines, lines)  # version left as-is
        self.logger.stale_dependency.assert_called_once()  # staleness still checked
        self.assert_update_held_back()

    def test_ignore_update_and_stale_after_zero_still_check_for_a_yank(self):
        """Test that a scope the marker leaves live keeps the reference queried, so its check still runs.

        `ignore[update]` and `set[stale-after=0]` leave the scopes beside them live; the yank check is not silenced, so
        the source is still queried for it rather than the reference being skipped outright.
        """
        yanked = mock_new_version_getter(DependencyVersion("3.14", yank=Yank(yanked=True)))
        lines = ["image: python:3.14  # update-time: ignore[update] set[stale-after=0]"]
        self.assertEqual(self.rewrite(lines, IMAGE_REGEXP, yanked), lines)  # version left as-is
        marker = Marker(ignored_scopes=Scope.UPDATE, stale=Threshold(value=0))
        # The source is queried for the yank, and the warning is handed the reference carrying the marker.
        self.assert_marker_reaches(self.logger.yanked_dependency, marker)

    def assert_marker_reaches(self, report: Mock, marker: Marker) -> None:
        """Assert the check reported once, on the reference carrying the marker."""
        report.assert_called_once()
        self.assertEqual(report.call_args.args[0].marker, marker)

    def test_stale_after_zero_still_updates_and_reports_with_the_marker(self):
        """Test that `set[stale-after=0]` applies the update and silences the staleness warning."""
        lines = ["image: python:3.14  # update-time: set[stale-after=0]"]
        new_lines = self.rewrite(lines, IMAGE_REGEXP, new_version_getter("3.15"), mock_project_getter(STALE_PROJECT))
        self.assertEqual(new_lines, ["image: python:3.15  # update-time: set[stale-after=0]"])  # version bumped
        self.assert_marker_reaches(self.logger.ignored_warning, Marker(stale=Threshold(value=0)))
        self.logger.ignored.assert_not_called()  # the update is not held back, so nothing is logged as ignored

    def test_allow_hash_drift_marker_adopts_new_digest(self):
        """Test that an inline `allow[hash-drift]` marker re-pins a re-pushed tag's digest instead of warning."""
        lines = [f"image: python:3.14@{DIGEST1}  # update-time: allow[hash-drift]"]
        new_lines = self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.14", DIGEST2))
        self.assertEqual(new_lines, [f"image: python:3.14@{DIGEST2}  # update-time: allow[hash-drift]"])
        self.logger.adopted_drift.assert_called_once_with(
            self.drifted(marker=Marker(allowed_scopes=Scope.HASH_DRIFT)), "allow[hash-drift]"
        )
        self.logger.drift.assert_not_called()

    def test_allow_hash_drift_marker_above_line_adopts(self):
        """Test that a standalone `allow[hash-drift]` comment opts the reference on the line below it in."""
        lines = ["# update-time: allow[hash-drift]", f"image: python:3.14@{DIGEST1}"]
        new_lines = self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.14", DIGEST2))
        self.assertEqual(new_lines, ["# update-time: allow[hash-drift]", f"image: python:3.14@{DIGEST2}"])
        self.logger.adopted_drift.assert_called_once_with(
            self.drifted(2, marker=Marker(allowed_scopes=Scope.HASH_DRIFT)), "allow[hash-drift]"
        )

    def test_allow_hash_drift_marker_is_noop_when_version_also_changed(self):
        """Test that when the version has moved too, the normal update path runs and the marker doesn't apply."""
        lines = [f"image: python:3.14@{DIGEST1}  # update-time: allow[hash-drift]"]
        new_lines = self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.15", DIGEST2))
        self.assertEqual(new_lines, [f"image: python:3.15@{DIGEST2}  # update-time: allow[hash-drift]"])
        self.logger.new_version.assert_called_once()  # a real version bump, not a drift adoption
        self.logger.adopted_drift.assert_not_called()

    def test_drift_is_reported_for_a_tag_kept_floating(self):
        """Test that a reference kept floating is warned about when its tag serves another digest than it records."""
        resolved = DependencyVersion(version="3.15", sha=DIGEST2, floating=FloatingPin.RESOLVED)
        lines = [f"image: python:latest@{DIGEST1}  # update-time: allow[floating-pin]"]
        self.assertEqual(self.rewrite(lines, _SHA_REGEXP, mock_new_version_getter(resolved)), lines)
        self.logger.drift.assert_called_once_with(
            self.drifted(version="latest", marker=Marker(allowed_scopes=Scope.FLOATING_PIN))
        )

    def test_drift_adopted_for_a_tag_kept_floating(self):
        """Test that a reference kept floating adopts the digest its tag serves now, keeping the tag itself."""
        resolved = DependencyVersion(version="3.15", sha=DIGEST2, floating=FloatingPin.RESOLVED)
        marker = "# update-time: allow[floating-pin, hash-drift]"
        new_lines = self.rewrite(
            [f"image: python:latest@{DIGEST1}  {marker}"], _SHA_REGEXP, mock_new_version_getter(resolved)
        )
        self.assertEqual(new_lines, [f"image: python:latest@{DIGEST2}  {marker}"])
        self.logger.adopted_drift.assert_called_once_with(
            self.drifted(version="latest", marker=Marker(allowed_scopes=Scope.FLOATING_PIN | Scope.HASH_DRIFT)),
            "allow[hash-drift]",
        )
        self.logger.keeping_floating_pin.assert_not_called()

    def test_kept_floating_tag_names_the_directive_that_kept_it(self):
        """Test that a tag kept floating names the directive keeping it, not the drift opt-in written beside it."""
        resolved = DependencyVersion(version="3.15", sha=DIGEST, floating=FloatingPin.RESOLVED)
        lines = ["image: python:latest  # update-time: allow[floating-pin, hash-drift]"]
        self.assertEqual(self.rewrite(lines, IMAGE_REGEXP, mock_new_version_getter(resolved)), lines)
        self.logger.keeping_floating_pin.assert_called_once_with(
            self.reference(version="latest", marker=Marker(allowed_scopes=Scope.FLOATING_PIN | Scope.HASH_DRIFT)),
            resolved,
            "allow[floating-pin]",
        )

    def test_ignore_wins_over_allow_hash_drift_marker(self):
        """Test that a reference marked both `ignore[update]` and `allow[hash-drift]` is left untouched."""
        get_new_version = new_version_getter("3.14", DIGEST2)
        lines = ["# update-time: allow[hash-drift]", f"image: python:3.14@{DIGEST1}  # update-time: ignore[update]"]
        self.assertEqual(self.rewrite(lines, _SHA_REGEXP, get_new_version), lines)
        self.logger.adopted_drift.assert_not_called()
        self.logger.drift.assert_not_called()

    def test_hash_drift_opt_in_on_a_version_tag_is_not_redundant(self):
        """Test that `allow[hash-drift]` on an image's version tag is not reported, with or without a digest yet."""
        cases = {
            "digest": (f"image: python:3.14@{DIGEST1}", f"image: python:3.14@{DIGEST1}"),
            "no digest yet": ("image: python:3.14", f"image: python:3.14@{DIGEST1}"),
        }
        marker = "  # update-time: allow[hash-drift]"
        for case, (line, rewritten) in cases.items():
            with self.subTest(case):
                new_lines = self.rewrite([line + marker], _SHA_REGEXP, new_version_getter("3.14", DIGEST1))
                self.assertEqual(new_lines, [rewritten + marker])
                self.logger.redundant_directive.assert_not_called()

    def test_hash_drift_opt_in_on_a_held_back_reference_is_redundant(self):
        """Test that `allow[hash-drift]` on a pinned image whose update is held back is reported as never adopted."""
        lines = [f"image: python:3.14@{DIGEST1}  # update-time: ignore[update] allow[hash-drift]"]
        self.assertEqual(self.rewrite(lines, _SHA_REGEXP, new_version_getter("3.14", DIGEST2)), lines)
        self.logger.redundant_directive.assert_called_once_with(
            self.reference(
                hash_pin=DIGEST1, marker=Marker(ignored_scopes=Scope.UPDATE, allowed_scopes=Scope.HASH_DRIFT)
            ),
            "allow[hash-drift]",
            Reason.DRIFT_NEVER_ADOPTED,
        )

    def test_a_marker_passes_its_bound_to_the_source(self):
        """Test that a marker inline or above the line passes its bound to the source, whose version is applied."""
        cases = {
            "allow[update<3.13]": ("3.12", "3.12.9", bound(Verb.ALLOW, "update<3.13")),
            "ignore[update>=3.13]": ("3.12", "3.12.9", bound(Verb.IGNORE, "update>=3.13")),
            "ignore[minor-update]": ("3.12.1", "3.12.9", bound(Verb.IGNORE, "minor-update")),
            "allow[minor-update]": ("3.12.1", "3.13.0", bound(Verb.ALLOW, "minor-update")),
            "ignore[major-update]": ("3.12", "3.12.9", bound(Verb.IGNORE, "major-update")),
            "allow[update<3.13] (pinned until the 3.13 migration)": (
                "3.12",
                "3.12.9",
                bound(Verb.ALLOW, "update<3.13"),
            ),
            "allow[update]": ("3.14", "3.15", NO_BOUND),
        }
        placements = {"inline": ["image: python:{}  {marker}"], "above": ["{marker}", "image: python:{}"]}
        for directive, (current, new, version_bound) in cases.items():
            for placement, lines in placements.items():
                with self.subTest(directive=directive, placement=placement):
                    marker = f"# update-time: {directive}"
                    get_new_version = mock_new_version_getter(DependencyVersion(version=new))
                    old_lines = [line.format(current, marker=marker) for line in lines]
                    new_lines = [line.format(new, marker=marker) for line in lines]
                    self.assertEqual(self.rewrite(old_lines, IMAGE_REGEXP, get_new_version), new_lines)
                    self.assert_resolved(get_new_version, f"python:{current}", version_bound)

    def assert_resolved(
        self, get_new_version: Mock, image: str, version_bound: VersionBound, cooldown: int = COOLDOWN.default
    ) -> None:
        """Assert the source was asked once about the image, written as the line names it, under bound and cooldown."""
        dependency, _, version = image.partition(":")
        get_new_version.assert_called_once_with(PinnedDependency(dependency, version), version_bound, cooldown)

    def test_ignore_update_wins_over_level_bound(self):
        """Test that a reference marked both `ignore[update]` and a level bound is left untouched."""
        get_new_version = new_version_getter("3.13")
        lines = ["# update-time: ignore[update]", "image: python:3.12  # update-time: allow[minor-update]"]
        self.assertEqual(self.rewrite(lines, IMAGE_REGEXP, get_new_version), lines)

    def test_a_bound_combines_with_hash_drift_in_one_bracket(self):
        """Test that an `allow` bracket combines a level or a specifier bound with the hash-drift opt-in."""
        for case, item in {"a level bound": "minor-update", "a specifier bound": "update<3.15"}.items():
            with self.subTest(case):
                get_new_version = mock_new_version_getter(DependencyVersion(version="3.14", sha=DIGEST2))
                lines = [f"image: python:3.14@{DIGEST1}  # update-time: allow[{item}, hash-drift]"]
                self.assertEqual(
                    self.rewrite(lines, _SHA_REGEXP, get_new_version), [lines[0].replace(DIGEST1, DIGEST2)]
                )
                self.assert_resolved(get_new_version, "python:3.14", bound(Verb.ALLOW, item))

    def test_directive_list_combines_bound_and_hash_drift(self):
        """Test that a bound and an `allow[hash-drift]` directive listed after one prefix both apply."""
        get_new_version = mock_new_version_getter(DependencyVersion(version="3.14", sha=DIGEST2))
        lines = [f"image: python:3.14@{DIGEST1}  # update-time: allow[update<3.15] allow[hash-drift]"]
        self.assertEqual(
            self.rewrite(lines, _SHA_REGEXP, get_new_version), [lines[0].replace(DIGEST1, DIGEST2)]
        )  # the drift opt-in is honoured
        self.assert_resolved(get_new_version, "python:3.14", bound(Verb.ALLOW, "update<3.15"))
        # The report names the directive that opted the reference in, not the bound written beside it.
        marker = Marker(allowed_scopes=Scope.HASH_DRIFT, version_bound=bound(Verb.ALLOW, "update<3.15"))
        self.logger.adopted_drift.assert_called_once_with(self.drifted(marker=marker), "allow[hash-drift]")

    def test_an_unreadable_item_warns_and_the_reference_updates(self):
        """Test that an item the language cannot read is warned about, and the reference updates."""
        self.assert_invalid_bracket_item(["image: python:3.12  # update-time: ignore[updaet]"], "updaet")

    def assert_invalid_bracket_item(self, lines: list[str], bracket_item: str, line: int = 1) -> None:
        """Assert that the bracket item is warned about, and that the reference on the line updates as usual."""
        new_lines = self.rewrite(lines, IMAGE_REGEXP, mock_new_version_getter(DependencyVersion(version="3.12.9")))
        self.assertEqual(new_lines, [each.replace("python:3.12", "python:3.12.9") for each in lines])
        reference = self.reference(version="3.12", line=line, marker=Marker(invalid_item=bracket_item))
        self.logger.invalid_bracket_item.assert_called_once_with(reference, bracket_item)
        self.logger.ignored.assert_not_called()

    def test_a_marker_with_an_unreadable_item_is_recognised_as_written(self):
        """Test that a marker holding an unreadable item is reported as recognised, spelled as the file spells it."""
        self.rewrite(
            ["image: python:3.12  # update-time: ignore[yanked, stlae]"], IMAGE_REGEXP, new_version_getter("3.12")
        )
        marker = Marker(ignored_scopes=Scope.YANKED, invalid_item="stlae")
        self.logger.recognised_marker.assert_called_once_with(self.reference(version="3.12", marker=marker))
        self.assertEqual(str(self.logger.recognised_marker.call_args.args[0].marker), "ignore[yanked, stlae]")

    def test_an_ignore_update_beside_an_unreadable_item_holds_the_update_back_and_says_so(self):
        """Test that an `ignore[update]` sharing a bracket with an unreadable item holds the update back as written."""
        lines = ["image: python:3.12  # update-time: ignore[update, stlae]"]
        self.assertEqual(self.rewrite(lines, IMAGE_REGEXP, new_version_getter("3.12.9")), lines)
        marker = Marker(ignored_scopes=Scope.UPDATE, invalid_item="stlae")
        self.logger.invalid_bracket_item.assert_called_once_with(self.reference(version="3.12", marker=marker), "stlae")
        self.logger.ignored.assert_called_once_with(self.reference(version="3.12", marker=marker))

    def test_an_unreadable_item_above_the_line_is_reported_for_the_reference_below(self):
        """Test that an unreadable item in a comment above the reference is reported for the reference below."""
        self.assert_invalid_bracket_item(["# update-time: allow[update@@@]", "image: python:3.12"], "@@@", line=2)

    def test_floating_tag_without_a_place_for_a_digest_is_pinned_to_its_version_alone(self):
        """Test that a floating tag is pinned by its version alone where the regexp does not hold a digest."""
        resolved = DependencyVersion(version="3.15", sha=DIGEST, floating=FloatingPin.RESOLVED)
        new_lines = self.rewrite(["image: python:latest"], IMAGE_REGEXP, mock_new_version_getter(resolved))
        self.assertEqual(new_lines, ["image: python:3.15"])
        self.logger.pinned.assert_called_once_with(self.reference(version="latest"), replace(resolved, sha=""))

    def test_ignore_floating_pin_is_a_noop(self):
        """Test that `ignore[floating-pin]` pins the floating tag, which is what happens without a marker."""
        resolved = DependencyVersion(version="3.15", sha=DIGEST, floating=FloatingPin.RESOLVED)
        get_new_version = mock_new_version_getter(resolved)
        lines = ["image: python:latest  # update-time: ignore[floating-pin]"]
        new_lines = self.rewrite(lines, _SHA_REGEXP, get_new_version)
        self.assertEqual(new_lines, [f"image: python:3.15@{DIGEST}  # update-time: ignore[floating-pin]"])
        self.logger.invalid_bracket_item.assert_not_called()

    def test_ignore_update_wins_over_allow_floating_pin(self):
        """Test that a reference whose update is held back is left alone, whatever the floating-pin directive asks."""
        resolved = DependencyVersion(version="3.15", sha=DIGEST, floating=FloatingPin.RESOLVED)
        get_new_version = mock_new_version_getter(resolved)
        lines = ["image: python:latest  # update-time: ignore[update] allow[floating-pin]"]
        self.assertEqual(self.rewrite(lines, IMAGE_REGEXP, get_new_version), lines)
