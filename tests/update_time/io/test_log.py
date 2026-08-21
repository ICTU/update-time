"""Logger unit tests."""

import inspect
import logging
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import ANY, Mock, patch

from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text

from update_time.domain.bound import Redundancy, Verb
from update_time.domain.dependency import DependencyVersion, FloatingPin, Yank
from update_time.domain.directive import Reason
from update_time.domain.drift import DriftedPin
from update_time.domain.marker import Marker, Scope
from update_time.io import log as log_module
from update_time.io.log import (
    DEPENDENCY_DELIMITER,
    LOCATION_DELIMITER,
    Logger,
    LogHighlighter,
    LogMessage,
    get_logger,
    reset_changelog_suppression,
)
from update_time.primitives.location import Location

from tests.mutation import Mutation, kills
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2
from tests.update_time.helpers import bound, reference, resolved_reference


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
        # The domain owns the fragments naming one of its own facts, so they are read from there rather than off the
        # logger: a `Reason` a warning reports, and the `Redundancy` a bound is reported with.
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


def _create_location(filename: str, line_number: int | None = None) -> Location:
    """Create a location in the current working directory."""
    return Location(Path.cwd() / filename, line_number)


def dependency(name: str) -> str:
    """Return the dependency name wrapped in its delimiter, as a log message carries it for the highlighter."""
    return f"{DEPENDENCY_DELIMITER}{name}{DEPENDENCY_DELIMITER}"


def _at(path_and_line: str) -> str:
    """Return the location wrapped in its delimiter, as a log message carries it for the highlighter."""
    return f"{LOCATION_DELIMITER}{path_and_line}{LOCATION_DELIMITER}"


class RenderTests(TestCase):
    """Unit tests for how the logger renders a dependency and a location for the highlighter to pick up."""

    def test_render_wraps_the_relative_path_and_line_in_the_delimiter(self):
        """Test that a location renders as the delimiter-wrapped relative path, with the line appended when present."""
        path = Path.cwd() / "docs" / "requirements.txt"
        self.assertEqual(Logger._render_location(Location(path, 42)), _at("docs/requirements.txt:42"))
        self.assertEqual(Logger._render_location(Location(path)), _at("docs/requirements.txt"))

    @patch("logging.Logger.log")
    def test_a_location_field_is_wrapped_and_a_plain_field_is_not(self, mock_log: Mock):
        """Test that a location passed as a named field is wrapped, while a plain field is passed through as it is."""
        message = LogMessage(logging.INFO, "Skipping %(location)s: %(reason)s")
        Logger("fields")._log(message, location=_create_location("Dockerfile", 1), reason="it is compiled")
        mock_log.assert_called_once_with(
            message.level, message, {"location": _at("Dockerfile:1"), "reason": "it is compiled"}
        )

    @patch("logging.Logger.log")
    def test_the_dependency_field_is_wrapped_in_its_delimiter(self, mock_log: Mock):
        """Test that the field named `dependency` is wrapped, so no log method has to render one itself."""
        message = LogMessage(logging.ERROR, "No valid version found for %(dependency)s")
        Logger("fields")._log(message, dependency="actions/checkout")
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
        mock_log.assert_called_with(message.level, message, ANY)
        _level, template, fields = mock_log.call_args.args
        self.assertEqual(sorted(fields), sorted(re.findall(r"%\((\w+)\)", str(template))))
        self.assertEqual(str(template) % fields, rendered)

    def test_suppress_repeated_changelog(self, mock_log: Mock):
        """Test that a repeated changelog is suppressed."""
        logger = Logger("suppress changelog")
        message = Logger._MESSAGE_NEW_VERSION
        location = _create_location("pyproject.toml", 5)
        available = f"New version available for {dependency('dependency')} in {_at('pyproject.toml:5')}: 1.0\n"
        logger.new_version(reference("dependency", location), DependencyVersion("1.0", "Changelog"))
        self.assert_message(mock_log, message, available + "Changelog")
        logger.new_version(reference("dependency", location), DependencyVersion("1.0", "Changelog"))
        self.assert_last_message(mock_log, message, available + "Suppressing changelog already shown, see above")

    def test_reset_changelog_suppression(self, mock_log: Mock):
        """Test that resetting the suppression makes a logger show a changelog it has already shown."""
        message = Logger._MESSAGE_NEW_VERSION
        location = _create_location("pyproject.toml", 5)
        available = f"New version available for {dependency('dependency')} in {_at('pyproject.toml:5')}: 1.0\n"
        logger = get_logger("reset suppression")
        logger.new_version(reference("dependency", location), DependencyVersion("1.0", "Changelog"))
        logger.new_version(reference("dependency", location), DependencyVersion("1.0", "Changelog"))
        self.assert_last_message(mock_log, message, available + "Suppressing changelog already shown, see above")
        reset_changelog_suppression()
        logger.new_version(reference("dependency", location), DependencyVersion("1.0", "Changelog"))
        self.assert_last_message(mock_log, message, available + "Changelog")

    def test_new_version_without_publication_date(self, mock_log: Mock):
        """Test that the version is logged without a publication date when it is unknown."""
        location = _create_location("a.txt", 3)
        Logger("no date").new_version(reference("dependency", location), DependencyVersion("1.0", "Changelog"))
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NEW_VERSION,
            f"New version available for {dependency('dependency')} in {_at('a.txt:3')}: 1.0\nChangelog",
        )

    def test_pinned(self, mock_log: Mock):
        """Test that pinning a previously unpinned reference to a digest is logged."""
        location = _create_location("Dockerfile", 1)
        Logger("pin").pinned(reference("dependency", location), DependencyVersion("1.0", sha=DIGEST))
        self.assert_message(
            mock_log,
            Logger._MESSAGE_PINNED,
            f"Pinned {dependency('dependency')} in {_at('Dockerfile:1')} to 1.0@{DIGEST}",
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
        location = _create_location("docker-compose.yml", 7)
        release = DependencyVersion("dev")
        Logger("floating").unpinned_floating_tag(
            reference("acme/api", location, "dev"), release, FloatingPin.NO_VERSION_TAG
        )
        self.assert_message(
            mock_log,
            Logger._MESSAGE_UNPINNED_FLOATING_TAG,
            f"Floating tag {dependency('acme/api')}:dev in {_at('docker-compose.yml:7')} was left as it is: "
            "no tag naming a version serves the same image",
        )

    def test_keeping_a_floating_tag(self, mock_log: Mock):
        """Test that a floating tag left as it is is reported with the tag it names and what it resolves to."""
        location = _create_location("Dockerfile", 1)
        release = DependencyVersion("3.14.7", sha=DIGEST)
        cause = "update-time: allow[floating-pin]"
        Logger("floating").keeping_floating_tag(reference("python", location, "latest"), release, cause)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_KEEPING_FLOATING_TAG,
            f"Keeping the floating tag {dependency('python')}:latest in {_at('Dockerfile:1')}: it resolves to "
            f"3.14.7@{DIGEST} ({cause})",
        )

    @kills(
        Mutation(
            log_module,
            '        return f":{version}" if version else ""',
            '        return f":{version}"',
            "a reference naming no tag is reported with a colon that names nothing after it",
        )
    )
    def test_keeping_a_reference_that_names_no_tag(self, mock_log: Mock):
        """Test that a reference naming no tag is reported by its name alone, there being no tag to name after it."""
        location = _create_location("Dockerfile", 1)
        release = DependencyVersion("3.14.7", sha=DIGEST)
        cause = "--allow-floating-pin"
        Logger("floating").keeping_floating_tag(reference("python", location), release, cause)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_KEEPING_FLOATING_TAG,
            f"Keeping the floating tag {dependency('python')} in {_at('Dockerfile:1')}: it resolves to "
            f"3.14.7@{DIGEST} ({cause})",
        )

    def test_digest_drift(self, mock_log: Mock):
        """Test that a re-pushed tag whose digest changed under an unchanged pin is warned about at warning level."""
        location = _create_location("Dockerfile", 2)
        Logger("drift").digest_drift(DriftedPin("dependency", "3.14", location, DIGEST1, new_sha=DIGEST2))
        self.assert_message(
            mock_log,
            Logger._MESSAGE_DIGEST_DRIFT,
            f"Digest drift for {dependency('dependency')}:3.14 in {_at('Dockerfile:2')}: pinned to {DIGEST1} "
            f"but the registry now serves {DIGEST2}; the pin was left unchanged, verify the change is expected "
            "before updating the pin",
        )

    def test_adopted_drift(self, mock_log: Mock):
        """Test that adopting a re-pushed tag's new digest is logged at info level, naming the opt-in that caused it."""
        cause = "update-time: allow[hash-drift]"
        location = _create_location("Dockerfile", 2)
        Logger("adopt").adopted_drift(DriftedPin("dependency", "3.14", location, DIGEST1, new_sha=DIGEST2), cause)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_ADOPTED_DIGEST_DRIFT,
            f"Adopted digest drift for {dependency('dependency')}:3.14 in {_at('Dockerfile:2')}: "
            f"re-pinned from {DIGEST1} to {DIGEST2} ({cause})",
        )

    def test_warn_if_stale(self, mock_log: Mock):
        """Test that an old newest release is warned about at warning level, against the threshold passed in.

        The 90 differs from the global default, so the reported `(> 90)` can only have come from the argument.
        """
        published = datetime.now(UTC) - timedelta(days=512, hours=1)
        version = DependencyVersion("4.15.0", newest_published=published)
        location = _create_location("requirements.txt", 9)
        Logger("stale").warn_if_stale(resolved_reference("humanize", location, version), 90)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_STALE,
            f"Stale dependency {dependency('humanize')} in {_at('requirements.txt:9')}: "
            "newest release 4.15.0 was published 512 days ago (> 90)",
        )

    def test_report_staleness(self, mock_log: Mock):
        """Test that staleness is reported as a warning, or as the hold-back of a marker that silences it.

        The release is 100 days old, which is stale against the 90 passed in and not against the global default, so
        either line is logged only when the given threshold is the one applied.
        """
        published = datetime.now(UTC) - timedelta(days=100, hours=1)
        version = DependencyVersion("4.15.0", newest_published=published)
        resolved = resolved_reference("humanize", _create_location("requirements.txt", 9), version)
        Logger("stale").report_staleness(resolved, Marker(), 90)
        mock_log.assert_called_once_with(Logger._MESSAGE_STALE.level, Logger._MESSAGE_STALE, ANY)
        mock_log.reset_mock()  # Judge the marker that silences it on the records of its own run.
        Logger("stale").report_staleness(
            resolved, Marker(ignored_scopes=Scope.STALE, raw="ignore[stale] allow[hash-drift]"), 90
        )
        self.assert_message(
            mock_log,
            Logger._MESSAGE_IGNORED_STALENESS,
            f"Ignoring the staleness warning for {dependency('humanize')} in {_at('requirements.txt:9')} "
            "(update-time: ignore[stale])",
        )

    def test_warn_if_stale_does_nothing_when_not_stale(self, mock_log: Mock):
        """Test that nothing is logged when the newest release date is recent or unknown."""
        recent = DependencyVersion("4.15.0", newest_published=datetime.now(UTC) - timedelta(days=1))
        undated = DependencyVersion("4.15.0")
        logger = Logger("stale")
        location = _create_location("requirements.txt", 9)
        logger.warn_if_stale(resolved_reference("humanize", location, recent), 90)
        logger.warn_if_stale(resolved_reference("humanize", location, undated), 90)
        mock_log.assert_not_called()

    def test_warn_if_yanked_without_reason(self, mock_log: Mock):
        """Test that a yanked pin with no maintainer reason reports that the reason was not specified."""
        version = DependencyVersion("4.15.0", yank=Yank(yanked=True))
        Logger("yanked").warn_if_yanked(
            resolved_reference("humanize", _create_location("requirements.txt", 9), version)
        )
        self.assert_message(
            mock_log,
            Logger._MESSAGE_YANKED,
            f"Yanked dependency {dependency('humanize')} in {_at('requirements.txt:9')}: "
            "version 4.15.0 was yanked (reason not specified)",
        )

    def test_warn_if_yanked_with_reason(self, mock_log: Mock):
        """Test that the warning renders the maintainer's yank reason in parentheses."""
        yank = Yank(yanked=True, reason="broke Python 3.10 support")
        location = _create_location("requirements.txt", 9)
        Logger("yanked").warn_if_yanked(
            resolved_reference("humanize", location, DependencyVersion("4.15.0", yank=yank))
        )
        self.assert_message(
            mock_log,
            Logger._MESSAGE_YANKED,
            f"Yanked dependency {dependency('humanize')} in {_at('requirements.txt:9')}: "
            'version 4.15.0 was yanked ("broke Python 3.10 support")',
        )

    def test_warn_if_yanked_does_nothing_when_not_yanked(self, mock_log: Mock):
        """Test that nothing is logged when the version was not yanked."""
        version = DependencyVersion("4.15.0")
        Logger("yanked").warn_if_yanked(
            resolved_reference("humanize", _create_location("requirements.txt", 9), version)
        )
        mock_log.assert_not_called()

    def test_invalid_specifier(self, mock_log: Mock):
        """Test that an unparsable version bound specifier is warned about at warning level."""
        location = _create_location("Dockerfile", 2)
        Logger("bound").invalid_bracket_item("python", "@@@", location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_INVALID_BRACKET_ITEM,
            f"Invalid '@@@' in the update-time marker for {dependency('python')} in {_at('Dockerfile:2')}; "
            "leaving the reference unchanged",
        )

    def test_inverted_stale_item(self, mock_log: Mock):
        """Test that a `stale` item comparing the wrong way round is warned about at warning level."""
        location = _create_location("Dockerfile", 2)
        Logger("stale").inverted_stale_item(reference("python", location), "stale>=90")
        self.assert_message(
            mock_log,
            Logger._MESSAGE_INVERTED_STALE_ITEM,
            f"Incorrect 'stale>=90' in the update-time marker for {dependency('python')} in {_at('Dockerfile:2')}: "
            "this comparison warns while a release is fresh and goes quiet once it is old, so it sets no threshold",
        )

    def test_inverted_cooldown_item(self, mock_log: Mock):
        """Test that a `cooldown` item comparing the wrong way round is warned about at warning level."""
        location = _create_location("Dockerfile", 2)
        Logger("cooldown").inverted_cooldown_item(reference("python", location), "cooldown>=30")
        self.assert_message(
            mock_log,
            Logger._MESSAGE_INVERTED_COOLDOWN_ITEM,
            f"Incorrect 'cooldown>=30' in the update-time marker for {dependency('python')} in {_at('Dockerfile:2')}: "
            "this comparison adopts a release only while it is fresh and holds it back once it is old, "
            "so it sets no cooldown",
        )

    def test_inverted_vulnerable_item(self, mock_log: Mock):
        """Test that a `vulnerable` item comparing the wrong way round is warned about at warning level."""
        location = _create_location("Dockerfile", 2)
        Logger("vulnerable").inverted_vulnerable_item(reference("python", location), "vulnerable>=high")
        self.assert_message(
            mock_log,
            Logger._MESSAGE_INVERTED_VULNERABLE_ITEM,
            f"Incorrect 'vulnerable>=high' in the update-time marker for {dependency('python')} in "
            f"{_at('Dockerfile:2')}: this comparison warns about the mild vulnerabilities and stays quiet about the "
            "severe ones, so it sets no risk level",
        )

    def test_warn_if_redundant_bound(self, mock_log: Mock):
        """Test that a redundant bound is warned about at warning level, showing the bound and how it is redundant."""
        version_bound = bound(Verb.ALLOW, "update>=3.12")  # never has an effect on a 3.12 pin
        marker = Marker(version_bound=version_bound)
        location = _create_location("Dockerfile", 6)
        Logger("bound").warn_if_redundant_bound(reference("python", location, "3.12"), marker)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_REDUNDANT_BOUND,
            f"Redundant update bound allow[update>=3.12] on {dependency('python')} 3.12 in {_at('Dockerfile:6')}: "
            "it never has an effect",
        )

    def test_warn_if_redundant_level_bound(self, mock_log: Mock):
        """Test that a level bound that blocks every update is warned about, rendered in its level form."""
        version_bound = bound(Verb.IGNORE, "patch-update")  # ignore[patch-update] blocks every update
        marker = Marker(version_bound=version_bound)
        location = _create_location("Dockerfile", 6)
        Logger("bound").warn_if_redundant_bound(reference("python", location, "3.12"), marker)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_REDUNDANT_BOUND,
            f"Redundant update bound ignore[patch-update] on {dependency('python')} 3.12 in {_at('Dockerfile:6')}: "
            "it blocks every update",
        )

    def test_warn_if_redundant_keep_all_level_bound(self, mock_log: Mock):
        """Test that a level bound that allows every update is warned about, unlike the implicit NO_BOUND default."""
        version_bound = bound(Verb.ALLOW, "major-update")  # allow[major-update] allows every update
        marker = Marker(version_bound=version_bound)
        location = _create_location("Dockerfile", 6)
        Logger("bound").warn_if_redundant_bound(reference("python", location, "3.12"), marker)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_REDUNDANT_BOUND,
            f"Redundant update bound allow[major-update] on {dependency('python')} 3.12 in {_at('Dockerfile:6')}: "
            "it never has an effect",
        )

    def test_warn_if_redundant_bound_does_nothing_when_live(self, mock_log: Mock):
        """Test that nothing is logged when the bound is live (a genuine ceiling or floor)."""
        version_bound = bound(Verb.ALLOW, "update<3.13")  # a live ceiling on a 3.12 pin
        marker = Marker(version_bound=version_bound)
        Logger("bound").warn_if_redundant_bound(reference("python", _create_location("Dockerfile", 6), "3.12"), marker)
        mock_log.assert_not_called()

    def test_warn_if_redundant_bound_does_nothing_when_level_bound_is_live(self, mock_log: Mock):
        """Test that nothing is logged for a level bound between the extremes: it always leaves room above the pin."""
        marker = Marker(version_bound=bound(Verb.IGNORE, "minor-update"))
        Logger("bound").warn_if_redundant_bound(reference("python", _create_location("Dockerfile", 6), "3.12"), marker)
        mock_log.assert_not_called()

    def test_warn_if_redundant_bound_does_nothing_for_no_bound(self, mock_log: Mock):
        """Test that nothing is logged for the keep-all NO_BOUND: the unmarked default is not a bound to report on."""
        Logger("bound").warn_if_redundant_bound(
            reference("python", _create_location("Dockerfile", 6), "3.12"), Marker()
        )
        mock_log.assert_not_called()

    def test_recognised_marker(self, mock_log: Mock):
        """Test that a reference's marker is logged at debug level verbatim, exactly as the user wrote it."""
        # The raw text combines scopes and bracket items, so echoing it verbatim shows the log takes the user's marker.
        raw = "ignore[update] ignore[stale] allow[update<3.13, hash-drift]"
        marker = Marker(
            ignored_scopes=Scope.STALE,
            allow_drift=True,
            version_bound=bound(Verb.ALLOW, "update<3.13"),
            raw=raw,
        )
        location = _create_location("Dockerfile", 6)
        Logger("marker").recognised_marker("python", marker, location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_RECOGNISED_MARKER,
            f"Recognised update-time marker {raw} for {dependency('python')} in {_at('Dockerfile:6')}",
        )

    def test_recognised_marker_does_nothing_without_marker(self, mock_log: Mock):
        """Test that nothing is logged for a reference without a marker."""
        Logger("marker").recognised_marker("python", Marker(), _create_location("Dockerfile", 6))
        mock_log.assert_not_called()

    def test_ignored(self, mock_log: Mock):
        """Test that a held-back reference logs its `ignore` directive verbatim, exactly as the user spelled it."""
        # `ignored` names the `ignore` directives from the verbatim `raw` text, each scope as the user spelled it.
        marker = Marker(ignored_scopes=Scope.UPDATE, raw="ignore[update] ignore[stale] allow[hash-drift]")
        location = _create_location("Dockerfile", 6)
        Logger("marker").ignored("python", marker, location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_IGNORED,
            f"Ignoring updates for {dependency('python')} in {_at('Dockerfile:6')} "
            "(update-time: ignore[update] ignore[stale])",
        )

    def test_report_staleness_does_nothing_when_not_stale(self, mock_log: Mock):
        """Test that a marker holding back a staleness warning that would not be given reports nothing either."""
        recent = DependencyVersion("4.15.0", newest_published=datetime.now(UTC) - timedelta(days=1))
        undated = DependencyVersion("4.15.0")
        logger = Logger("stale")
        marker = Marker(ignored_scopes=Scope.STALE, raw="ignore[stale]")
        location = _create_location("requirements.txt", 9)
        logger.report_staleness(resolved_reference("humanize", location, recent), marker, 90)
        logger.report_staleness(resolved_reference("humanize", location, undated), marker, 90)
        mock_log.assert_not_called()

    def test_report_yank(self, mock_log: Mock):
        """Test that a yank is reported as a warning, or as the hold-back of a marker that silences it."""
        version = DependencyVersion("4.15.0", yank=Yank(yanked=True, reason="broke Python 3.10 support"))
        resolved = resolved_reference("humanize", _create_location("requirements.txt", 9), version)
        Logger("yanked").report_yank(resolved, Marker())
        mock_log.assert_called_once_with(Logger._MESSAGE_YANKED.level, Logger._MESSAGE_YANKED, ANY)
        mock_log.reset_mock()  # Judge the marker that silences it on the records of its own run.
        Logger("yanked").report_yank(
            resolved, Marker(ignored_scopes=Scope.YANKED, raw="ignore[yanked] allow[hash-drift]")
        )
        self.assert_message(
            mock_log,
            Logger._MESSAGE_IGNORED_YANK,
            f"Ignoring the yank warning for {dependency('humanize')} in {_at('requirements.txt:9')} "
            "(update-time: ignore[yanked])",
        )

    def test_report_yank_does_nothing_when_not_yanked(self, mock_log: Mock):
        """Test that nothing is logged for a version that was not yanked, marker or no marker."""
        resolved = resolved_reference("humanize", _create_location("requirements.txt", 9), DependencyVersion("4.15.0"))
        Logger("yanked").report_yank(resolved, Marker())
        Logger("yanked").report_yank(resolved, Marker(ignored_scopes=Scope.YANKED, raw="ignore[yanked]"))
        mock_log.assert_not_called()

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
                location = _create_location("Dockerfile", 2)
                Logger("redundant").redundant_directive(reference("python", location), directive, reason)
                self.assert_message(
                    mock_log,
                    Logger._MESSAGE_REDUNDANT_DIRECTIVE,
                    f"Redundant update-time directive {directive} for {dependency('python')} in "
                    f"{_at('Dockerfile:2')}: {reason}",
                )

    def test_path_logged_at_debug(self, mock_log: Mock):
        """Test that the per-file 'checking for updates' progress is logged at debug level."""
        config_yml = Path.cwd() / "config.yml"
        Logger("path").path(config_yml)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_CHECKING_PATH,
            f"Checking if there are updates for {_at('config.yml')}",
        )

    def test_configured_uv_cooldown(self, mock_log: Mock):
        """Test that writing the cooldown into a project's uv config is logged, relative to the working directory."""
        path = Path.cwd() / "pyproject.toml"
        Logger("cooldown").configured_uv_cooldown(path, "7 days")
        self.assert_message(
            mock_log,
            Logger._MESSAGE_UV_COOLDOWN,
            f"Set uv exclude-newer to '7 days' in {_at('pyproject.toml')} to apply the cooldown",
        )

    def test_configured_uv_cooldown_outside_working_directory(self, mock_log: Mock):
        """Test that a workspace root outside the working directory is logged as its absolute path."""
        outside = Path("/elsewhere/pyproject.toml")
        Logger("cooldown").configured_uv_cooldown(outside, "7 days")
        self.assert_message(
            mock_log,
            Logger._MESSAGE_UV_COOLDOWN,
            f"Set uv exclude-newer to '7 days' in {_at('/elsewhere/pyproject.toml')} to apply the cooldown",
        )

    def test_invalid_pyproject_toml(self, mock_log: Mock):
        """Test that an unparsable pyproject.toml is logged as a warning."""
        path = Path.cwd() / "pyproject.toml"
        Logger("toml").invalid_pyproject_toml(path)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_INVALID_TOML,
            f"Skipping {_at('pyproject.toml')}: it is not valid TOML",
        )

    def test_excluded_path_logged_at_debug(self, mock_log: Mock):
        """Test that a directory held back by --exclude-path is logged at debug level, with its path undelimited."""
        Logger("exclude").excluded_path(Path("vendor"))
        self.assert_message(mock_log, Logger._MESSAGE_EXCLUDING_PATH, "Excluding vendor from the scan (--exclude-path)")

    def test_missing_excluded_path_logged_at_warning(self, mock_log: Mock):
        """Test that a non-existing --exclude-path directory is logged as a warning, not an error."""
        Logger("exclude").missing_excluded_path(Path("vendor"))
        self.assert_message(
            mock_log,
            Logger._MESSAGE_PATH_TO_EXCLUDE_DOES_NOT_EXIST,
            "Path vendor passed to --exclude-path does not exist",
        )

    def test_non_numeric_node_base_image(self, mock_log: Mock):
        """Test that a non-numeric Node base image tag is warned about, reporting its Dockerfile as a location."""
        dockerfile = Path.cwd() / "docker" / "Dockerfile"
        Logger("node").non_numeric_node_base_image(dockerfile, "lts")
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NON_NUMERIC_NODE_BASE_IMAGE_TAG,
            "Cannot derive the Node engine version from the non-numeric base image tag 'node:lts' in "
            f"{_at('docker/Dockerfile')}",
        )

    def test_response(self, mock_log: Mock):
        """Test that a response that is not OK is warned about, with its URL, status code, and reason phrase."""
        response = Mock(url="https://pypi.org/pypi/humanize/json", status_code=404, reason="Not Found")
        Logger("http").response(response)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NOT_OK_RESPONSE,
            "Could not fetch https://pypi.org/pypi/humanize/json: HTTP 404 Not Found",
        )

    def test_forced_outside_git_repository_logged_at_warning(self, mock_log: Mock):
        """Test that running outside a git repository because of --force is logged as a warning, with the scan root."""
        Logger("git").forced_outside_git_repository(Path("/home/user/project"))
        self.assert_message(
            mock_log,
            Logger._MESSAGE_FORCED_OUTSIDE_GIT_REPOSITORY,
            "Running outside a git repository (/home/user/project) because --force was given; changes are made in "
            "place and cannot be reverted",
        )

    def test_new_version_with_publication_date(self, mock_log: Mock):
        """Test that the publication date is appended to the version when it is known."""
        published = datetime(2026, 5, 29, 13, 54, tzinfo=UTC)
        version = DependencyVersion("1.0", "Changelog", published=published)
        location = _create_location("a.txt", 3)
        Logger("date").new_version(reference("dependency", location), version)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NEW_VERSION,
            f"New version available for {dependency('dependency')} in {_at('a.txt:3')}: "
            "1.0, published: 2026-05-29 13:54\nChangelog",
        )

    def test_publication_date_is_logged_in_utc(self, mock_log: Mock):
        """Test that a non-UTC publication date is converted to UTC before logging."""
        published = datetime(2026, 5, 29, 15, 54, tzinfo=timezone(timedelta(hours=2)))
        location = _create_location("a.txt", 3)
        Logger("utc").new_version(reference("dependency", location), DependencyVersion("1.0", published=published))
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NEW_VERSION,
            f"New version available for {dependency('dependency')} in {_at('a.txt:3')}: "
            "1.0, published: 2026-05-29 13:54\n"
            "No changelog available!",
        )


class LogHighlighterTests(TestCase):
    """Unit tests for how the log output is highlighted."""

    def test_digest_highlighted_as_one_token(self):
        """Test that the full digest gets a single `repr.digest` span and no leftover fragment sub-spans inside it."""
        digest = f"sha256:{'a4fde3b2' + 'c' * 56}"  # a realistic 64-hex-character digest
        text = Text(f"pinned to {digest} but the registry now serves {DIGEST2}")
        LogHighlighter().highlight(text)
        start = text.plain.index(digest)
        spans_in_digest = [span for span in text.spans if span.start >= start and span.end <= start + len(digest)]
        self.assertEqual(spans_in_digest, [(start, start + len(digest), "repr.digest")])

    def test_version_numbers_still_highlighted(self):
        """Test that ordinary highlighting (e.g. of a version number) is preserved for messages without a digest."""
        text = Text("New version available: 3.14")
        LogHighlighter().highlight(text)
        self.assertIn("repr.number", [span.style for span in text.spans])

    def test_dependency_name_highlighted_and_markers_removed(self):
        """Test that a marker-wrapped dependency name is styled as `repr.dependency` and the markers leave no trace."""
        dependency = Logger._render_dependency("actions/checkout")
        fields = {"dependency": dependency, "location": "a.txt", "version": "1.1", "changes": "Changelog for 1.1"}
        text = Text(Logger._MESSAGE_NEW_VERSION.format % fields)
        LogHighlighter().highlight(text)
        self.assertEqual(text.plain, "New version available for actions/checkout in a.txt: 1.1\nChangelog for 1.1")
        dependency_spans = [(text.plain[span.start : span.end], span.style) for span in text.spans]
        self.assertIn(("actions/checkout", "repr.dependency"), dependency_spans)

    def test_dependency_names_and_digest_together(self):
        """Test that several dependency names and a digest in one message are each styled without interfering."""
        message = f"Pinned {dependency('ghcr.io/astral-sh/uv')} in Dockerfile to {DIGEST}"
        text = Text(message)
        LogHighlighter().highlight(text)
        self.assertEqual(text.plain, f"Pinned ghcr.io/astral-sh/uv in Dockerfile to {DIGEST}")
        styled = {(text.plain[span.start : span.end], span.style) for span in text.spans}
        self.assertIn(("ghcr.io/astral-sh/uv", "repr.dependency"), styled)
        self.assertIn((DIGEST, "repr.digest"), styled)

    def test_location_highlighted_as_one_token(self):
        """Test that a delimited path:line is one `repr.filename` span, delimiters stripped and no stray number span."""
        message = f"New version available in {_at('docs/requirements.txt:42')}: 4.15.0"
        text = Text(message)
        LogHighlighter().highlight(text)
        self.assertEqual(text.plain, "New version available in docs/requirements.txt:42: 4.15.0")
        styled = [(text.plain[span.start : span.end], span.style) for span in text.spans]
        self.assertIn(("docs/requirements.txt:42", "repr.filename"), styled)
        # The line number is part of the single location token, not highlighted as a separate number.
        self.assertNotIn(("42", "repr.number"), styled)

    def test_no_colour_output_is_plain_text(self):
        """Test that with colour disabled the styled name renders as the same plain text, markers and all removed."""
        highlighted = LogHighlighter()(Text(f"Pinned {dependency('python')} in Dockerfile"))
        console = Console(no_color=True, force_terminal=False)
        with console.capture() as capture:
            console.print(highlighted, end="")
        self.assertEqual(capture.get(), "Pinned python in Dockerfile")

    def test_dependency_style_is_bold_white(self):
        """Test that get_logger wires `repr.dependency` to bold white in the handler's console theme."""
        get_logger("theme")  # Ensure the root logger, and its themed RichHandler console, have been configured.
        handler = next(h for h in logging.getLogger().handlers if isinstance(h, RichHandler))
        self.assertEqual(str(handler.console.get_style("repr.dependency")), "bold white")


class LoggerMessageTest(TestCase):
    """Test that Logger's message templates and its log methods pair one-to-one.

    Each `MESSAGE_` template sits directly above the log method that emits it, which nothing but convention enforces.
    """

    @staticmethod
    def methods_by_template() -> dict[str, set[str]]:
        """Return, for each message template on Logger, the names of the log methods that reference it."""
        templates = {name for name in vars(Logger) if name.removeprefix("_").startswith("MESSAGE_")}
        references: dict[str, set[str]] = {template: set() for template in templates}
        for name in vars(Logger):
            if name.startswith("__") or not inspect.isfunction(function := getattr(Logger, name)):
                continue
            for template in templates & set(function.__code__.co_names):
                references[template].add(name)
        return references

    def test_each_template_belongs_to_exactly_one_method(self):
        """Test that each message template is referenced by exactly one log method: no orphans, no sharing."""
        unpaired = {template: methods for template, methods in self.methods_by_template().items() if len(methods) != 1}
        self.assertEqual(unpaired, {})

    def test_each_method_references_at_most_one_template(self):
        """Test that no log method references more than one message template."""
        template_counts: dict[str, int] = {}
        for methods in self.methods_by_template().values():
            for method in methods:
                template_counts[method] = template_counts.get(method, 0) + 1
        self.assertEqual({method: count for method, count in template_counts.items() if count > 1}, {})
