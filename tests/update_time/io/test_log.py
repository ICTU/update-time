"""Logger unit tests."""

import inspect
import logging
import re
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import ANY, Mock, patch

from rich.logging import RichHandler

from update_time.domain import dependency as dependency_module
from update_time.domain.bound import Redundancy, Verb
from update_time.domain.dependency import (
    AccountedFor,
    Archival,
    Changes,
    DependencyVersion,
    FloatingPin,
    Project,
    Release,
    Yank,
)
from update_time.domain.reference import DriftedPin
from update_time.io import log as log_module
from update_time.io.console import (
    CHANGES,
    NOTE,
)
from update_time.io.log import (
    Logger,
    LogMessage,
    get_logger,
    reset_changelog_suppression,
)
from update_time.markers import marker as marker_module
from update_time.markers.directive import Reason
from update_time.markers.marker import Marker, Scope, Threshold
from update_time.primitives.location import Location

from tests.mutation import Mutation, kills
from tests.update_time.fixtures import BARE_IGNORE, DIGEST, DIGEST1, DIGEST2
from tests.update_time.helpers import bound, reference, resolved_reference, vulnerability
from tests.update_time.io.helpers import at, create_location, dependency


class GetLoggerTests(TestCase):
    """Unit tests for how get_logger configures the root logger."""

    def test_diagnostics_are_sent_to_stderr(self):
        """Test that the root logger sends all diagnostics to stderr, keeping stdout clean for --version/--help."""
        get_logger("stderr")  # Ensure the root logger has been configured.
        rich_handlers = [handler for handler in logging.getLogger().handlers if isinstance(handler, RichHandler)]
        self.assertTrue(rich_handlers)
        self.assertTrue(all(handler.console.stderr for handler in rich_handlers))


class LogMessageTests(TestCase):
    """Unit tests for the log message type and the messages declared with it."""

    MESSAGE = LogMessage(logging.WARNING, "Stale dependency %(dependency)s")

    def test_a_message_renders_as_its_format_string(self):
        """Test that a message renders as its format string, so logging can interpolate the arguments into it."""
        self.assertEqual(str(self.MESSAGE), "Stale dependency %(dependency)s")

    def test_a_message_reprs_as_its_format_string(self):
        """Test that a message reprs as its format string, so a failing assertion reads as the message itself."""
        self.assertEqual(repr(self.MESSAGE), "'Stale dependency %(dependency)s'")

    def test_no_message_contains_a_full_stop(self):
        """Test that no log message contains a full stop, keeping the style consistent (commas and semicolons).

        Every message attribute of `Logger` is a message template, and every string attribute a fragment
        substituted into one.
        """
        messages = [
            str(value)
            for name, value in vars(Logger).items()
            if isinstance(value, LogMessage | str) and not name.startswith("__")
        ]
        # Two enums carry message fragments of their own, so the fragments are read off the enums rather than
        # off the logger: the `Reason` a warning reports, and the `Redundancy` a bound is reported with.
        messages += [str(member) for enum in (Reason, Redundancy) for member in enum]
        # Guard against the introspection silently covering the fragments alone, as it did when the messages stopped
        # being plain strings:
        self.assertIn(str(Logger._MESSAGE_NEW_VERSION), messages)
        self.assertIn(str(Reason.NO_YANK_CONCEPT), messages)
        for message in messages:
            with self.subTest(message=message):
                self.assertNotIn(".", message)

    def test_every_message_names_its_holes(self):
        """Test that every message interpolates by name, so its log method hands the logger named fields."""
        for name, message in vars(Logger).items():
            if isinstance(message, LogMessage):
                with self.subTest(message=name):
                    self.assertEqual(re.findall(r"%(?!\()", message.format), [])


def _new_logger() -> Logger:
    """Return a logger of its own, so what one test suppressed is not still suppressed for the next."""
    return Logger("test")


class RenderTests(TestCase):
    """Unit tests for how the logger renders a dependency and a location for the highlighter to pick up."""

    def test_render_wraps_the_relative_path_and_line_in_the_delimiter(self):
        """Test that a location renders as the delimiter-wrapped relative path, with the line appended when present."""
        path = Path.cwd() / "docs" / "requirements.txt"
        self.assertEqual(Logger._render_field("location", Location(path, 42)), at("docs/requirements.txt:42"))
        self.assertEqual(Logger._render_field("location", Location(path)), at("docs/requirements.txt"))

    @patch("logging.Logger.log")
    def test_a_location_field_is_wrapped_and_a_plain_field_is_not(self, mock_log: Mock):
        """Test that a location passed as a named field is wrapped, while a plain field is passed through as it is."""
        message = LogMessage(logging.INFO, "Skipping %(location)s: %(reason)s")
        _new_logger()._log(message, location=create_location("Dockerfile", 1), reason="it is compiled")
        mock_log.assert_called_once_with(
            message.level, message, {"location": at("Dockerfile:1"), "reason": "it is compiled"}
        )

    @patch("logging.Logger.log")
    def test_the_dependency_field_is_wrapped_in_its_delimiter(self, mock_log: Mock):
        """Test that the field named `dependency` is wrapped, so no log method has to render one itself."""
        message = LogMessage(logging.ERROR, "No valid version found for %(dependency)s")
        _new_logger()._log(message, dependency="actions/checkout")
        mock_log.assert_called_once_with(message.level, message, {"dependency": dependency("actions/checkout")})


@patch("logging.Logger.log")
class LoggerTests(TestCase):
    """Unit tests for the logger class."""

    def assert_message(self, mock_log: Mock, message: LogMessage, rendered: str) -> None:
        """Assert the log method emitted the message once, at its own level, reading as the given text."""
        mock_log.assert_called_once()
        self.assert_last_message(mock_log, message, rendered)

    def assert_last_message(self, mock_log: Mock, message: LogMessage, rendered: str) -> None:
        """Assert the most recent record reads as the given text, carrying exactly the fields the message names."""
        level, template, fields = mock_log.call_args.args
        self.assertEqual((level, template), (message.level, message))
        self.assertEqual(sorted(fields), sorted(re.findall(r"%\((\w+)\)", str(template))))
        self.assertEqual(str(template) % fields, rendered)

    def assert_changes(self, mock_log: Mock, changes: str) -> None:
        """Assert the most recent record carries a changelog's changes beside its fields."""
        self.assertEqual(mock_log.call_args.kwargs, {"extra": {CHANGES: changes}})

    def assert_note(self, mock_log: Mock, note: str) -> None:
        """Assert the most recent record carries Update-time's own note about the changes beside its fields."""
        self.assertEqual(mock_log.call_args.kwargs, {"extra": {NOTE: note}})

    def test_suppress_repeated_changelog(self, mock_log: Mock):
        """Test that a repeated changelog is suppressed."""
        logger = _new_logger()
        message = Logger._MESSAGE_NEW_VERSION
        location = create_location("pyproject.toml", 5)
        available = f"New version available for {dependency('dependency')} in {at('pyproject.toml:5')}: 1.0"
        logger.new_version(
            reference("dependency", location), DependencyVersion("1.0", Changes("Changelog", markdown=False))
        )
        self.assert_message(mock_log, message, available)
        self.assert_changes(mock_log, "Changelog")
        logger.new_version(
            reference("dependency", location), DependencyVersion("1.0", Changes("Changelog", markdown=False))
        )
        self.assert_last_message(mock_log, message, available)
        self.assert_note(mock_log, "Suppressing changelog already shown, see above")

    def test_reset_changelog_suppression(self, mock_log: Mock):
        """Test that resetting the suppression makes a logger show a changelog it has already shown."""
        message = Logger._MESSAGE_NEW_VERSION
        location = create_location("pyproject.toml", 5)
        available = f"New version available for {dependency('dependency')} in {at('pyproject.toml:5')}: 1.0"
        logger = get_logger("reset suppression")
        logger.new_version(
            reference("dependency", location), DependencyVersion("1.0", Changes("Changelog", markdown=False))
        )
        logger.new_version(
            reference("dependency", location), DependencyVersion("1.0", Changes("Changelog", markdown=False))
        )
        self.assert_last_message(mock_log, message, available)
        self.assert_note(mock_log, "Suppressing changelog already shown, see above")
        reset_changelog_suppression()
        logger.new_version(
            reference("dependency", location), DependencyVersion("1.0", Changes("Changelog", markdown=False))
        )
        self.assert_last_message(mock_log, message, available)
        self.assert_changes(mock_log, "Changelog")

    def test_new_version_without_publication_date(self, mock_log: Mock):
        """Test that the version is logged without a publication date when it is unknown."""
        location = create_location("a.txt", 3)
        _new_logger().new_version(
            reference("dependency", location), DependencyVersion("1.0", Changes("Changelog", markdown=False))
        )
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NEW_VERSION,
            f"New version available for {dependency('dependency')} in {at('a.txt:3')}: 1.0",
        )
        self.assert_changes(mock_log, "Changelog")

    def test_pinned(self, mock_log: Mock):
        """Test that pinning a previously unpinned reference to a digest is logged."""
        location = create_location("Dockerfile", 1)
        _new_logger().pinned(reference("dependency", location), DependencyVersion("1.0", sha=DIGEST))
        self.assert_message(
            mock_log,
            Logger._MESSAGE_PINNED,
            f"Pinned {dependency('dependency')} in {at('Dockerfile:1')} to 1.0@{DIGEST}",
        )

    @kills(
        Mutation(
            log_module,
            '        "Floating tag %(dependency)s%(tag)s in %(location)s was left as it is: %(reason)s",',
            '        "Floating tag %(dependency)s%(tag)s in %(location)s was left as it is",',
            "the line reports that a tag was left as it is without naming the reason it was left",
        )
    )
    def test_unpinned_floating_tag(self, mock_log: Mock):
        """Test that a floating tag pinned to no version is reported with the reason it was left as it is."""
        location = create_location("docker-compose.yml", 7)
        release = DependencyVersion("dev")
        _new_logger().unpinned_floating_tag(reference("acme/api", location, "dev"), release, FloatingPin.NO_VERSION_TAG)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_UNPINNED_FLOATING_TAG,
            f"Floating tag {dependency('acme/api')}:dev in {at('docker-compose.yml:7')} was left as it is: "
            "no tag naming a version serves the same image",
        )

    @kills(
        Mutation(
            log_module,
            '        "Reference %(dependency)s%(tag)s in %(location)s was left as it is: %(reason)s",\n',
            '        "Reference %(dependency)s%(tag)s in %(location)s was left as it is",\n',
            "the line reports that a reference was left as it is without naming what accounts for it",
        )
    )
    def test_accounted_for_reference(self, mock_log: Mock):
        """Test that a reference the file itself accounts for is reported with what accounts for it."""
        location = create_location("docker-compose.yml", 4)
        _new_logger().accounted_for_reference(reference("acme/api", location, "1.2.3"), AccountedFor.BUILT_IMAGE)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_ACCOUNTED_FOR_REFERENCE,
            f"Reference {dependency('acme/api')}:1.2.3 in {at('docker-compose.yml:4')} was left as it is: "
            "the Compose file builds this image",
        )

    def test_keeping_a_floating_tag(self, mock_log: Mock):
        """Test that a floating tag left as it is is reported with the tag it names and what it resolves to."""
        location = create_location("Dockerfile", 1)
        release = DependencyVersion("3.14.7", sha=DIGEST)
        cause = "update-time: allow[floating-pin]"
        _new_logger().keeping_floating_tag(reference("python", location, "latest"), release, cause)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_KEEPING_FLOATING_TAG,
            f"Keeping the floating tag {dependency('python')}:latest in {at('Dockerfile:1')}: it resolves to "
            f"3.14.7@{DIGEST} ({cause})",
        )

    @kills(
        Mutation(
            dependency_module,
            '    return f":{version}" if version else ""',
            '    return f":{version}"',
            "a reference naming no tag is reported with a colon that names nothing after it",
        )
    )
    def test_keeping_a_reference_that_names_no_tag(self, mock_log: Mock):
        """Test that a reference naming no tag is reported by its name alone, there being no tag to name after it."""
        location = create_location("Dockerfile", 1)
        release = DependencyVersion("3.14.7", sha=DIGEST)
        cause = "--allow-floating-pin"
        _new_logger().keeping_floating_tag(reference("python", location), release, cause)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_KEEPING_FLOATING_TAG,
            f"Keeping the floating tag {dependency('python')} in {at('Dockerfile:1')}: it resolves to "
            f"3.14.7@{DIGEST} ({cause})",
        )

    def test_digest_drift(self, mock_log: Mock):
        """Test that a re-pushed tag whose digest changed under an unchanged pin is warned about at warning level."""
        location = create_location("Dockerfile", 2)
        _new_logger().drift(Logger.DIGEST_DRIFT, DriftedPin("dependency", "3.14", location, DIGEST1, new_sha=DIGEST2))
        self.assert_message(
            mock_log,
            Logger._MESSAGE_DIGEST_DRIFT,
            f"Digest drift for {dependency('dependency')}:3.14 in {at('Dockerfile:2')}: pinned to {DIGEST1} "
            f"but the registry now serves {DIGEST2}; the pin was left unchanged, verify the change is expected "
            "before updating the pin",
        )

    def test_adopted_drift(self, mock_log: Mock):
        """Test that adopting a re-pushed tag's new digest is logged at info level, naming the opt-in that caused it."""
        cause = "update-time: allow[hash-drift]"
        location = create_location("Dockerfile", 2)
        _new_logger().adopted_drift(
            Logger.DIGEST_DRIFT, DriftedPin("dependency", "3.14", location, DIGEST1, new_sha=DIGEST2), cause
        )
        self.assert_message(
            mock_log,
            Logger._MESSAGE_ADOPTED_DIGEST_DRIFT,
            f"Adopted digest drift for {dependency('dependency')}:3.14 in {at('Dockerfile:2')}: "
            f"re-pinned from {DIGEST1} to {DIGEST2} ({cause})",
        )

    def test_stale_dependency_warning(self, mock_log: Mock):
        """Test that an old newest release is warned about at warning level, naming the release that was measured.

        The reference was left on 4.15.0 while the dependency's newest release is 5.0.0, so the version in the
        message can only be the one whose date was measured. The 90 differs from the global default, so the
        reported `(> 90)` can only have come from the argument.
        """
        published = datetime.now(UTC) - timedelta(days=512, hours=1)
        version = DependencyVersion("4.15.0", project=Project(newest=Release("5.0.0", published)))
        location = create_location("requirements.txt", 9)
        _new_logger().report_staleness(resolved_reference("humanize", location, version), Marker(), 90)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_STALE,
            f"Stale dependency {dependency('humanize')} in {at('requirements.txt:9')}: "
            "newest release 5.0.0 was published 512 days ago (> 90)",
        )

    def test_report_staleness(self, mock_log: Mock):
        """Test that staleness is reported as a warning, or as the marker that silences it.

        The release is 100 days old, which is stale against the 90 passed in and not against the global default, so
        either line is logged only when the given threshold is the one applied.
        """
        published = datetime.now(UTC) - timedelta(days=100, hours=1)
        version = DependencyVersion("4.15.0", project=Project(newest=Release("4.15.0", published)))
        resolved = resolved_reference("humanize", create_location("requirements.txt", 9), version)
        _new_logger().report_staleness(resolved, Marker(), 90)
        mock_log.assert_called_once_with(Logger._MESSAGE_STALE.level, Logger._MESSAGE_STALE, ANY)
        mock_log.reset_mock()  # Judge the marker that silences it on the records of its own run.
        _new_logger().report_staleness(
            resolved,
            Marker(
                ignored_scopes=Scope.UPDATE | Scope.STALE,
                written_scopes=Scope.UPDATE | Scope.STALE,
                raw="ignore[update] ignore[stale] allow[hash-drift]",
            ),
            90,
        )
        self.assert_message(
            mock_log,
            Logger._MESSAGE_IGNORED_STALENESS,
            f"Ignoring the staleness warning for {dependency('humanize')} in {at('requirements.txt:9')} "
            "(update-time: ignore[stale])",
        )

    def test_yanked_dependency_warning(self, mock_log: Mock):
        """Test that the warning quotes the maintainer's reason, and says so where they gave none."""
        for case, (reason, clause) in {
            "no reason": ("", "version 4.15.0 was yanked (reason not specified)"),
            "a reason": ("broke Python 3.10 support", 'version 4.15.0 was yanked ("broke Python 3.10 support")'),
        }.items():
            with self.subTest(case=case):
                mock_log.reset_mock()
                version = DependencyVersion("4.15.0", yank=Yank(yanked=True, reason=reason))
                _new_logger().report_yank(
                    resolved_reference("humanize", create_location("requirements.txt", 9), version), Marker()
                )
                self.assert_message(
                    mock_log,
                    Logger._MESSAGE_YANKED,
                    f"Yanked dependency {dependency('humanize')} in {at('requirements.txt:9')}: {clause}",
                )

    @kills(
        Mutation(
            log_module,
            '        reason = f\' ("{archival.reason}")\' if archival.reason else ""',
            '        reason = ""',
            "the reason the source published is left out of the warning",
        )
    )
    def test_archived_dependency_warning(self, mock_log: Mock):
        """Test that the warning quotes the reason the source published, and ends where it published none."""
        for case, (reason, clause) in {
            "no reason": ("", "the project was archived"),
            "a reason": ("superseded by humanize2", 'the project was archived ("superseded by humanize2")'),
        }.items():
            with self.subTest(case=case):
                mock_log.reset_mock()
                archival = Archival(archived=True, reason=reason)
                version = DependencyVersion("4.15.0", project=Project(archival=archival))
                _new_logger().report_archival(
                    resolved_reference("humanize", create_location("requirements.txt", 9), version), Marker()
                )
                self.assert_message(
                    mock_log,
                    Logger._MESSAGE_ARCHIVED,
                    f"Archived dependency {dependency('humanize')} in {at('requirements.txt:9')}: {clause}",
                )

    def test_invalid_specifier(self, mock_log: Mock):
        """Test that an unparsable version bound specifier is warned about at warning level."""
        location = create_location("Dockerfile", 2)
        _new_logger().invalid_bracket_item("python", "@@@", location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_INVALID_BRACKET_ITEM,
            f"Invalid '@@@' in the update-time marker for {dependency('python')} in {at('Dockerfile:2')}; "
            "leaving the reference unchanged",
        )

    def test_inverted_stale_item(self, mock_log: Mock):
        """Test that a `stale` item comparing the wrong way round is warned about at warning level."""
        location = create_location("Dockerfile", 2)
        marker = Marker(stale=Threshold(inverted_item="stale>=90"))
        _new_logger().report_inverted_items(reference("python", location), marker)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_INVERTED_STALE_ITEM,
            f"Incorrect 'stale>=90' in the update-time marker for {dependency('python')} in {at('Dockerfile:2')}: "
            "this comparison warns while a release is fresh and goes quiet once it is old, so it sets no threshold",
        )

    def test_inverted_cooldown_item(self, mock_log: Mock):
        """Test that a `cooldown` item comparing the wrong way round is warned about at warning level."""
        location = create_location("Dockerfile", 2)
        marker = Marker(cooldown=Threshold(inverted_item="cooldown>=30"))
        _new_logger().report_inverted_items(reference("python", location), marker)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_INVERTED_COOLDOWN_ITEM,
            f"Incorrect 'cooldown>=30' in the update-time marker for {dependency('python')} in {at('Dockerfile:2')}: "
            "this comparison adopts a release only while it is fresh and holds it back once it is old, "
            "so it sets no cooldown",
        )

    def test_inverted_vulnerable_item(self, mock_log: Mock):
        """Test that a `vulnerable` item comparing the wrong way round is warned about at warning level."""
        location = create_location("Dockerfile", 2)
        marker = Marker(vulnerable=Threshold(inverted_item="vulnerable>=high"))
        _new_logger().report_inverted_items(reference("python", location), marker)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_INVERTED_VULNERABLE_ITEM,
            f"Incorrect 'vulnerable>=high' in the update-time marker for {dependency('python')} in "
            f"{at('Dockerfile:2')}: this comparison warns about the mild vulnerabilities and stays quiet about the "
            "severe ones, so it sets no risk level",
        )

    def test_warn_if_redundant_bound(self, mock_log: Mock):
        """Test that a redundant bound is warned about at warning level, showing the bound and how it is redundant."""
        version_bound = bound(Verb.ALLOW, "update>=3.12")  # never has an effect on a 3.12 pin
        marker = Marker(version_bound=version_bound)
        location = create_location("Dockerfile", 6)
        _new_logger().warn_if_redundant_bound(reference("python", location, "3.12"), marker)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_REDUNDANT_BOUND,
            f"Redundant update bound allow[update>=3.12] on {dependency('python')} 3.12 in {at('Dockerfile:6')}: "
            "it never has an effect",
        )

    def test_warn_if_redundant_level_bound(self, mock_log: Mock):
        """Test that a level bound that blocks every update is warned about, rendered in its level form."""
        version_bound = bound(Verb.IGNORE, "patch-update")  # ignore[patch-update] blocks every update
        marker = Marker(version_bound=version_bound)
        location = create_location("Dockerfile", 6)
        _new_logger().warn_if_redundant_bound(reference("python", location, "3.12"), marker)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_REDUNDANT_BOUND,
            f"Redundant update bound ignore[patch-update] on {dependency('python')} 3.12 in {at('Dockerfile:6')}: "
            "it blocks every update",
        )

    def test_warn_if_redundant_keep_all_level_bound(self, mock_log: Mock):
        """Test that a level bound that allows every update is warned about, unlike the implicit NO_BOUND default."""
        version_bound = bound(Verb.ALLOW, "major-update")  # allow[major-update] allows every update
        marker = Marker(version_bound=version_bound)
        location = create_location("Dockerfile", 6)
        _new_logger().warn_if_redundant_bound(reference("python", location, "3.12"), marker)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_REDUNDANT_BOUND,
            f"Redundant update bound allow[major-update] on {dependency('python')} 3.12 in {at('Dockerfile:6')}: "
            "it never has an effect",
        )

    def test_warn_if_redundant_bound_does_nothing_for_a_bound_that_decides(self, mock_log: Mock):
        """Test that nothing is logged for a live bound, at either level, nor for the unmarked default."""
        markers = {
            "a live ceiling on a 3.12 pin": Marker(version_bound=bound(Verb.ALLOW, "update<3.13")),
            "a level bound between the extremes": Marker(version_bound=bound(Verb.IGNORE, "minor-update")),
            "no bound at all": Marker(),
        }
        for case, marker in markers.items():
            with self.subTest(case=case):
                mock_log.reset_mock()
                location = create_location("Dockerfile", 6)
                _new_logger().warn_if_redundant_bound(reference("python", location, "3.12"), marker)
                mock_log.assert_not_called()

    def test_recognised_marker(self, mock_log: Mock):
        """Test that a reference's marker is logged at debug level verbatim, exactly as the user wrote it."""
        # The raw text combines scopes and bracket items, so echoing it verbatim shows the log takes the user's marker.
        raw = "ignore[update] ignore[stale] allow[update<3.13, hash-drift]"
        marker = Marker(
            ignored_scopes=Scope.STALE,
            allowed_scopes=Scope.HASH_DRIFT,
            version_bound=bound(Verb.ALLOW, "update<3.13"),
            raw=raw,
        )
        location = create_location("Dockerfile", 6)
        _new_logger().recognised_marker("python", marker, location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_RECOGNISED_MARKER,
            f"Recognised update-time marker {raw} for {dependency('python')} in {at('Dockerfile:6')}",
        )

    def test_recognised_marker_does_nothing_without_marker(self, mock_log: Mock):
        """Test that nothing is logged for a reference without a marker."""
        _new_logger().recognised_marker("python", Marker(), create_location("Dockerfile", 6))
        mock_log.assert_not_called()

    def test_ignored(self, mock_log: Mock):
        """Test that a held-back reference names the directive that held it back, not the ones written beside it."""
        marker = Marker(
            ignored_scopes=Scope.UPDATE,
            written_scopes=Scope.UPDATE | Scope.STALE,
            raw="ignore[update] ignore[stale] allow[hash-drift]",
        )
        location = create_location("Dockerfile", 6)
        _new_logger().ignored("python", marker, location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_IGNORED,
            f"Ignoring updates for {dependency('python')} in {at('Dockerfile:6')} (update-time: ignore[update])",
        )

    @kills(
        Mutation(
            marker_module,
            "        return self.as_written.directive_for(scope) or self.raw_directives(Verb.IGNORE)",
            "        return self.as_written.directive_for(scope)",
            "a bare `ignore` names nothing at all, instead of echoing itself",
        )
    )
    def test_ignored_by_a_bare_marker(self, mock_log: Mock):
        """Test that a bare `ignore` names itself, rather than a scoped directive the user never wrote."""
        marker = Marker(ignored_scopes=BARE_IGNORE.ignored_scopes, raw="ignore")
        location = create_location("Dockerfile", 6)
        _new_logger().ignored("python", marker, location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_IGNORED,
            f"Ignoring updates for {dependency('python')} in {at('Dockerfile:6')} (update-time: ignore)",
        )

    def test_report_staleness_does_nothing_when_not_stale(self, mock_log: Mock):
        """Test that a release that is recent or undated is reported by nothing, marker or no marker."""
        newest = Release("4.15.0", datetime.now(UTC) - timedelta(days=1))
        recent = DependencyVersion("4.15.0", project=Project(newest=newest))
        undated = DependencyVersion("4.15.0")
        logger = _new_logger()
        location = create_location("requirements.txt", 9)
        for marker in (Marker(), Marker(ignored_scopes=Scope.STALE, raw="ignore[stale]")):
            with self.subTest(marker=marker.raw or "no marker"):
                mock_log.reset_mock()
                logger.report_staleness(resolved_reference("humanize", location, recent), marker, 90)
                logger.report_staleness(resolved_reference("humanize", location, undated), marker, 90)
                mock_log.assert_not_called()

    def test_report_yank(self, mock_log: Mock):
        """Test that a yank is reported as a warning, or as the marker that silences it."""
        version = DependencyVersion("4.15.0", yank=Yank(yanked=True, reason="broke Python 3.10 support"))
        resolved = resolved_reference("humanize", create_location("requirements.txt", 9), version)
        _new_logger().report_yank(resolved, Marker())
        mock_log.assert_called_once_with(Logger._MESSAGE_YANKED.level, Logger._MESSAGE_YANKED, ANY)
        mock_log.reset_mock()  # Judge the marker that silences it on the records of its own run.
        _new_logger().report_yank(
            resolved,
            Marker(
                ignored_scopes=Scope.UPDATE | Scope.YANKED,
                written_scopes=Scope.UPDATE | Scope.YANKED,
                raw="ignore[update] ignore[yanked] allow[hash-drift]",
            ),
        )
        self.assert_message(
            mock_log,
            Logger._MESSAGE_IGNORED_YANK,
            f"Ignoring the yank warning for {dependency('humanize')} in {at('requirements.txt:9')} "
            "(update-time: ignore[yanked])",
        )

    def test_report_yank_does_nothing_when_not_yanked(self, mock_log: Mock):
        """Test that nothing is logged for a version that was not yanked, marker or no marker."""
        resolved = resolved_reference("humanize", create_location("requirements.txt", 9), DependencyVersion("4.15.0"))
        _new_logger().report_yank(resolved, Marker())
        _new_logger().report_yank(resolved, Marker(ignored_scopes=Scope.YANKED, raw="ignore[yanked]"))
        mock_log.assert_not_called()

    @kills(
        Mutation(
            log_module,
            '    _MESSAGE_IGNORED_VULNERABILITY = LogMessage(DEBUG, _ignoring("the %(advisory)s '
            'vulnerability warning"))',
            '    _MESSAGE_IGNORED_VULNERABILITY = LogMessage(DEBUG, _ignoring("the %(advisories)s '
            'vulnerability warning"))',
            "the silenced line names a field the log call does not supply, so the line is lost at render time",
        )
    )
    def test_ignored_vulnerability(self, mock_log: Mock):
        """Test that a vulnerability warning a marker silenced reads with the advisory it silenced."""
        marker = Marker(ignored_scopes=Scope.VULNERABLE, written_scopes=Scope.VULNERABLE, raw="ignore[vulnerable]")
        pin = reference("django", create_location("requirements.txt", 9), "3.2.0")
        reported = vulnerability("GHSA-2gwj-7jmv-h26r", "SQL Injection in Django", "critical")
        _new_logger().ignored_vulnerability(pin, reported, marker)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_IGNORED_VULNERABILITY,
            f"Ignoring the GHSA-2gwj-7jmv-h26r vulnerability warning for {dependency('django')} "
            f"in {at('requirements.txt:9')} (update-time: ignore[vulnerable])",
        )

    @kills(
        Mutation(
            log_module,
            '        DEBUG, _ignoring("the %(advisory)s vulnerability warning", '
            '"--ignore-vulnerability %(identifiers)s")',
            '        DEBUG, _ignoring("the %(identifiers)s vulnerability warning", '
            '"--ignore-vulnerability %(advisory)s")',
            "the run-wide line names what was passed in its subject and the advisory in its cause, swapping the two",
        )
    )
    def test_globally_ignored_vulnerability(self, mock_log: Mock):
        """Test that the run-wide option's line reads with the advisory, and quotes the identifiers it was passed."""
        pin = reference("django", create_location("requirements.txt", 9), "3.2.0")
        reported = vulnerability("GHSA-2gwj-7jmv-h26r", "SQL Injection in Django", "critical")
        passed = frozenset({"CVE-2022-28346", "CVE-2021-31542"})
        _new_logger().globally_ignored_vulnerability(pin, reported, passed)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_GLOBALLY_IGNORED_VULNERABILITY,
            f"Ignoring the GHSA-2gwj-7jmv-h26r vulnerability warning for {dependency('django')} "
            f"in {at('requirements.txt:9')} (--ignore-vulnerability CVE-2021-31542,CVE-2022-28346)",
        )

    def test_redundant_directive(self, mock_log: Mock):
        """Test that a directive holding nothing back is reported with the directive and the reason, as given."""
        cases = {
            "ignore[cooldown<30]": Reason.NO_COOLDOWN_DATES,
            "ignore[yanked]": Reason.NO_YANK_CONCEPT,
            "allow[stale>=90]": Reason.NO_STALENESS_DATES,
        }
        for directive, reason in cases.items():
            with self.subTest(directive=directive):
                mock_log.reset_mock()  # Judge each case on the records of its own run.
                location = create_location("Dockerfile", 2)
                _new_logger().redundant_directive(reference("python", location), directive, reason)
                self.assert_message(
                    mock_log,
                    Logger._MESSAGE_REDUNDANT_DIRECTIVE,
                    f"Redundant update-time directive {directive} for {dependency('python')} in "
                    f"{at('Dockerfile:2')}: {reason}",
                )

    def test_path_logged_at_debug(self, mock_log: Mock):
        """Test that the per-file 'checking for updates' progress is logged at debug level."""
        config_yml = Path.cwd() / "config.yml"
        _new_logger().path(config_yml)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_CHECKING_PATH,
            f"Checking if there are updates for {at('config.yml')}",
        )

    def test_configured_uv_cooldown(self, mock_log: Mock):
        """Test that writing the cooldown into a project's uv config is logged, relative to the working directory."""
        path = Path.cwd() / "pyproject.toml"
        _new_logger().configured_uv_cooldown(path, "7 days")
        self.assert_message(
            mock_log,
            Logger._MESSAGE_UV_COOLDOWN,
            f"Set uv exclude-newer to '7 days' in {at('pyproject.toml')} to apply the cooldown",
        )

    def test_configured_uv_cooldown_outside_working_directory(self, mock_log: Mock):
        """Test that a workspace root outside the working directory is logged as its absolute path."""
        outside = Path("/elsewhere/pyproject.toml")
        _new_logger().configured_uv_cooldown(outside, "7 days")
        self.assert_message(
            mock_log,
            Logger._MESSAGE_UV_COOLDOWN,
            f"Set uv exclude-newer to '7 days' in {at('/elsewhere/pyproject.toml')} to apply the cooldown",
        )

    def test_invalid_file(self, mock_log: Mock):
        """Test that a file that does not parse is warned about, naming the format given rather than its suffix."""
        for file_name, file_format in (("pom.xml", "XML"), ("compose.yml", "YAML")):
            with self.subTest(format=file_format):
                mock_log.reset_mock()
                _new_logger().invalid_file(Path.cwd() / file_name, file_format)
                self.assert_message(
                    mock_log,
                    Logger._MESSAGE_INVALID_FILE,
                    f"Skipping {at(file_name)}: it is not valid {file_format}",
                )

    def test_excluded_path_logged_at_debug(self, mock_log: Mock):
        """Test that a directory held back by --exclude-path is logged at debug level, with its path undelimited."""
        _new_logger().excluded_path(Path("vendor"))
        self.assert_message(mock_log, Logger._MESSAGE_EXCLUDING_PATH, "Excluding vendor from the scan (--exclude-path)")

    def test_missing_excluded_path_logged_at_warning(self, mock_log: Mock):
        """Test that a non-existing --exclude-path directory is logged as a warning, not an error."""
        _new_logger().missing_excluded_path(Path("vendor"))
        self.assert_message(
            mock_log,
            Logger._MESSAGE_PATH_TO_EXCLUDE_DOES_NOT_EXIST,
            "Path vendor passed to --exclude-path does not exist",
        )

    def test_non_numeric_node_base_image(self, mock_log: Mock):
        """Test that a non-numeric Node base image tag is warned about, reporting its Dockerfile as a location."""
        dockerfile = Path.cwd() / "docker" / "Dockerfile"
        _new_logger().non_numeric_node_base_image(dockerfile, "lts")
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NON_NUMERIC_NODE_BASE_IMAGE_TAG,
            "Cannot derive the Node engine version from the non-numeric base image tag 'node:lts' in "
            f"{at('docker/Dockerfile')}",
        )

    def test_response(self, mock_log: Mock):
        """Test that a response that is not OK is warned about, with its URL, status code, and reason phrase."""
        response = Mock(url="https://pypi.org/pypi/humanize/json", status_code=404, reason="Not Found")
        _new_logger().response(response)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NOT_OK_RESPONSE,
            "Could not fetch https://pypi.org/pypi/humanize/json: HTTP 404 Not Found",
        )

    def test_forced_outside_git_repository_logged_at_warning(self, mock_log: Mock):
        """Test that running outside a git repository because of --force is logged as a warning, with the scan root."""
        _new_logger().forced_outside_git_repository(Path("/home/user/project"))
        self.assert_message(
            mock_log,
            Logger._MESSAGE_FORCED_OUTSIDE_GIT_REPOSITORY,
            "Running outside a git repository (/home/user/project) because --force was given; changes are made in "
            "place and cannot be reverted",
        )

    def test_new_version_with_publication_date(self, mock_log: Mock):
        """Test that the publication date is appended to the version when it is known."""
        published = datetime(2026, 5, 29, 13, 54, tzinfo=UTC)
        version = DependencyVersion("1.0", Changes("Changelog", markdown=False), published=published)
        location = create_location("a.txt", 3)
        _new_logger().new_version(reference("dependency", location), version)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NEW_VERSION,
            f"New version available for {dependency('dependency')} in {at('a.txt:3')}: "
            "1.0, published: 2026-05-29 13:54",
        )
        self.assert_changes(mock_log, "Changelog")

    def test_publication_date_is_logged_in_utc(self, mock_log: Mock):
        """Test that a non-UTC publication date is converted to UTC before logging."""
        published = datetime(2026, 5, 29, 15, 54, tzinfo=timezone(timedelta(hours=2)))
        location = create_location("a.txt", 3)
        _new_logger().new_version(reference("dependency", location), DependencyVersion("1.0", published=published))
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NEW_VERSION,
            f"New version available for {dependency('dependency')} in {at('a.txt:3')}: "
            "1.0, published: 2026-05-29 13:54",
        )
        self.assert_note(mock_log, "No changelog available!")


class LoggerMessageTest(TestCase):
    """Test that Logger's message templates pair one-to-one with the log methods and constants that own them.

    Each `MESSAGE_` template sits directly above its owner, which nothing but convention enforces. That owner is
    the log method emitting it, or, for a pair of messages one dispatch reports through, the constant holding both.
    """

    @staticmethod
    def _templates() -> set[str]:
        """Return the names of the message templates on Logger."""
        return {name for name in vars(Logger) if name.removeprefix("_").startswith("MESSAGE_")}

    @classmethod
    def methods_by_template(cls) -> dict[str, set[str]]:
        """Return, for each message template on Logger, the names of the log methods that reference it."""
        templates = cls._templates()
        references: dict[str, set[str]] = {template: set() for template in templates}
        for name in vars(Logger):
            if name.startswith("__") or not inspect.isfunction(function := getattr(Logger, name)):
                continue
            for template in templates & set(function.__code__.co_names):
                references[template].add(name)
        return references

    @classmethod
    def holders_by_template(cls) -> dict[str, set[str]]:
        """Return, for each message template on Logger, the names of the constants that hold it.

        A constant pairing messages — a check's warning and its silencing, a drift's warning and its adoption —
        has them emitted by the dispatch the pair shares, so neither is named by a log method of its own. The
        templates are matched by identity, since the constant holds the message rather than its name.
        """
        template_of = {id(getattr(Logger, template)): template for template in cls._templates()}
        holders: dict[str, set[str]] = {template: set() for template in cls._templates()}
        for name, value in vars(Logger).items():
            if not is_dataclass(value) or isinstance(value, type):
                continue
            for field in fields(value):
                if isinstance(message := getattr(value, field.name), LogMessage):
                    holders[template_of[id(message)]].add(name)
        return holders

    @kills(
        Mutation(
            log_module,
            '    _MESSAGE_NO_VERSION = LogMessage(ERROR, "No valid version found for %(dependency)s")',
            '    _MESSAGE_ORPHANED = LogMessage(ERROR, "Nothing emits this")\n\n'
            '    _MESSAGE_NO_VERSION = LogMessage(ERROR, "No valid version found for %(dependency)s")',
            "a message template that neither a log method nor a holder emits goes unnoticed",
        )
    )
    def test_each_template_belongs_to_exactly_one_owner(self):
        """Test that each message template is owned by exactly one log method or constant: no orphans, no sharing."""
        methods, holders = self.methods_by_template(), self.holders_by_template()
        owners = {template: names | holders[template] for template, names in methods.items()}
        self.assertEqual({template: names for template, names in owners.items() if len(names) != 1}, {})

    def test_each_method_references_at_most_one_template(self):
        """Test that no log method references more than one message template."""
        template_counts: dict[str, int] = {}
        for methods in self.methods_by_template().values():
            for method in methods:
                template_counts[method] = template_counts.get(method, 0) + 1
        self.assertEqual({method: count for method, count in template_counts.items() if count > 1}, {})
