"""Logger unit tests."""

import logging
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import ANY, Mock, patch

from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text

from update_time.domain.bound import Verb
from update_time.domain.drift import DriftedPin
from update_time.domain.marker import Marker
from update_time.domain.version import DependencyVersion, Reference, Yank
from update_time.io.log import (
    DEPENDENCY_DELIMITER,
    LOCATION_DELIMITER,
    Logger,
    LogHighlighter,
    LogMessage,
    get_logger,
)
from update_time.primitives.location import Location
from update_time.references import file
from update_time.references.github import latest_pin
from update_time.references.resolve import latest_version

from tests.update_time.fixtures import COMMIT_SHA, DIGEST, DIGEST1, DIGEST2
from tests.update_time.helpers import bound, new_version_getter


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

        Every message attribute of `Logger` is a message template, and every string attribute a fragment substituted
        into one, so introspect them all rather than listing them by hand and risk missing a future message.
        """
        messages = [
            str(value)
            for name, value in vars(Logger).items()
            if isinstance(value, LogMessage | str) and not name.startswith("__")
        ]
        # Guard against the introspection silently covering the fragments alone, as it did when the messages stopped
        # being plain strings:
        self.assertIn(str(Logger._MESSAGE_NEW_VERSION), messages)
        for message in messages:
            with self.subTest(message=message):
                self.assertNotIn(".", message)

    def test_every_message_names_its_holes(self):
        """Test that every message interpolates by name, so its log method hands the logger named fields.

        A `%(location)s` hole is filled from the field of that name, which is what lets the logger render a location
        and a dependency itself; a bare `%s` would be filled by position, leaving the rendering to the log method.
        """
        for name, message in vars(Logger).items():
            if isinstance(message, LogMessage):
                with self.subTest(message=name):
                    self.assertEqual(re.findall(r"%(?!\()", message.format), [])


def create_location(filename: str, line_number: int | None = None) -> Location:
    """Create a location in the current working directory."""
    return Location(Path.cwd() / filename, line_number)


def dependency(name: str) -> str:
    """Return the dependency name wrapped in its delimiter, as a log message carries it for the highlighter."""
    return f"{DEPENDENCY_DELIMITER}{name}{DEPENDENCY_DELIMITER}"


def at(path_and_line: str) -> str:
    """Return the location wrapped in its delimiter, as a log message carries it for the highlighter."""
    return f"{LOCATION_DELIMITER}{path_and_line}{LOCATION_DELIMITER}"


class RenderTests(TestCase):
    """Unit tests for how the logger renders a dependency and a location for the highlighter to pick up."""

    def test_render_wraps_the_relative_path_and_line_in_the_delimiter(self):
        """Test that a location renders as the delimiter-wrapped relative path, with the line appended when present."""
        path = Path.cwd() / "docs" / "requirements.txt"
        self.assertEqual(Logger._render_location(Location(path, 42)), at("docs/requirements.txt:42"))
        self.assertEqual(Logger._render_location(Location(path)), at("docs/requirements.txt"))

    @patch("logging.Logger.log")
    def test_a_location_field_is_wrapped_and_a_plain_field_is_not(self, mock_log: Mock):
        """Test that a location passed as a named field is wrapped, while a plain field is passed through as it is.

        Wrapping it here is what spares every log method from rendering its own location.
        """
        message = LogMessage(logging.INFO, "Skipping %(location)s: %(reason)s")
        Logger("fields")._log(message, location=create_location("Dockerfile", 1), reason="it is compiled")
        mock_log.assert_called_once_with(
            message.level,
            message,
            {"location": at("Dockerfile:1"), "reason": "it is compiled"},
            stacklevel=ANY,
        )

    @patch("logging.Logger.log")
    def test_the_dependency_field_is_wrapped_in_its_delimiter(self, mock_log: Mock):
        """Test that the field named `dependency` is wrapped, so no log method has to render one itself.

        The field is recognised by its name because a dependency name has no fixed shape a value could be recognised
        by, unlike a location.
        """
        message = LogMessage(logging.ERROR, "No valid version found for %(dependency)s")
        Logger("fields")._log(message, dependency="actions/checkout")
        mock_log.assert_called_once_with(
            message.level,
            message,
            {"dependency": dependency("actions/checkout")},
            stacklevel=ANY,
        )


@patch("logging.Logger.log")
class LoggerTests(TestCase):
    """Unit tests for the logger class."""

    def assert_message(self, mock_log: Mock, message: LogMessage, rendered: str) -> None:
        """Assert the log method emitted the message once, at its own level, reading as the given text."""
        mock_log.assert_called_once()
        self.assert_last_message(mock_log, message, rendered)

    def assert_last_message(self, mock_log: Mock, message: LogMessage, rendered: str) -> None:
        """Assert the most recent record reads as the given text, carrying exactly the fields the message names.

        Records emitted before the most recent one are ignored.
        """
        mock_log.assert_called_with(message.level, message, ANY, stacklevel=ANY)
        _level, template, fields = mock_log.call_args.args
        self.assertEqual(sorted(fields), sorted(re.findall(r"%\((\w+)\)", str(template))))
        self.assertEqual(str(template) % fields, rendered)

    def test_suppress_repeated_changelog(self, mock_log: Mock):
        """Test that a repeated changelog is suppressed."""
        logger = Logger("suppress changelog")
        message = Logger._MESSAGE_NEW_VERSION
        location = create_location("pyproject.toml", 5)
        available = f"New version available for {dependency('dependency')} in {at('pyproject.toml:5')}: 1.0\n"
        logger.new_version("dependency", DependencyVersion("1.0", "Changelog"), location)
        self.assert_message(mock_log, message, available + "Changelog")
        logger.new_version("dependency", DependencyVersion("1.0", "Changelog"), location)
        self.assert_last_message(mock_log, message, available + "Suppressing changelog already shown, see above")

    def test_new_version_without_publication_date(self, mock_log: Mock):
        """Test that the version is logged without a publication date when it is unknown."""
        location = create_location("a.txt", 3)
        Logger("no date").new_version("dependency", DependencyVersion("1.0", "Changelog"), location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NEW_VERSION,
            f"New version available for {dependency('dependency')} in {at('a.txt:3')}: 1.0\nChangelog",
        )

    def test_pinned(self, mock_log: Mock):
        """Test that pinning a previously unpinned reference to a digest is logged."""
        location = create_location("Dockerfile", 1)
        Logger("pin").pinned("dependency", DependencyVersion("1.0", sha=DIGEST), location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_PINNED,
            f"Pinned {dependency('dependency')} in {at('Dockerfile:1')} to 1.0@{DIGEST}",
        )

    def test_digest_drift(self, mock_log: Mock):
        """Test that a re-pushed tag whose digest changed under an unchanged pin is warned about at warning level."""
        location = create_location("Dockerfile", 2)
        Logger("drift").digest_drift(DriftedPin(Reference("dependency", "3.14", DIGEST1), DIGEST2, location))
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
        Logger("adopt").adopted_drift(DriftedPin(Reference("dependency", "3.14", DIGEST1), DIGEST2, location), cause)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_ADOPTED_DIGEST_DRIFT,
            f"Adopted digest drift for {dependency('dependency')}:3.14 in {at('Dockerfile:2')}: "
            f"re-pinned from {DIGEST1} to {DIGEST2} ({cause})",
        )

    def test_warn_if_stale(self, mock_log: Mock):
        """Test that an old newest release is warned about at warning level."""
        published = datetime.now(UTC) - timedelta(days=512, hours=1)
        version = DependencyVersion("4.15.0", newest_published=published)
        location = create_location("requirements.txt", 9)
        Logger("stale").warn_if_stale("humanize", version, location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_STALE,
            f"Stale dependency {dependency('humanize')} in {at('requirements.txt:9')}: "
            "newest release 4.15.0 was published 512 days ago (> 365)",
        )

    def test_warn_if_stale_does_nothing_when_not_stale(self, mock_log: Mock):
        """Test that nothing is logged when the newest release date is recent or unknown."""
        recent = DependencyVersion("4.15.0", newest_published=datetime.now(UTC) - timedelta(days=1))
        undated = DependencyVersion("4.15.0")
        logger = Logger("stale")
        location = create_location("requirements.txt", 9)
        logger.warn_if_stale("humanize", recent, location)
        logger.warn_if_stale("humanize", undated, location)
        mock_log.assert_not_called()

    def test_warn_if_yanked_without_reason(self, mock_log: Mock):
        """Test that a yanked pin with no maintainer reason reports that the reason was not specified."""
        version = DependencyVersion("4.15.0", yank=Yank(yanked=True))
        Logger("yanked").warn_if_yanked("humanize", version, create_location("requirements.txt", 9))
        self.assert_message(
            mock_log,
            Logger._MESSAGE_YANKED,
            f"Yanked dependency {dependency('humanize')} in {at('requirements.txt:9')}: "
            "version 4.15.0 was yanked (reason not specified)",
        )

    def test_warn_if_yanked_with_reason(self, mock_log: Mock):
        """Test that the warning renders the maintainer's yank reason in parentheses."""
        yank = Yank(yanked=True, reason="broke Python 3.10 support")
        location = create_location("requirements.txt", 9)
        Logger("yanked").warn_if_yanked("humanize", DependencyVersion("4.15.0", yank=yank), location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_YANKED,
            f"Yanked dependency {dependency('humanize')} in {at('requirements.txt:9')}: "
            'version 4.15.0 was yanked ("broke Python 3.10 support")',
        )

    def test_warn_if_yanked_does_nothing_when_not_yanked(self, mock_log: Mock):
        """Test that nothing is logged when the version was not yanked."""
        version = DependencyVersion("4.15.0")
        Logger("yanked").warn_if_yanked("humanize", version, create_location("requirements.txt", 9))
        mock_log.assert_not_called()

    def test_invalid_specifier(self, mock_log: Mock):
        """Test that an unparsable version bound specifier is warned about at warning level."""
        location = create_location("Dockerfile", 2)
        Logger("bound").invalid_specifier("python", "@@@", location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_INVALID_SPECIFIER,
            f"Invalid '@@@' in the update-time marker for {dependency('python')} in {at('Dockerfile:2')}; "
            "leaving the reference unchanged",
        )

    def test_warn_if_redundant_bound(self, mock_log: Mock):
        """Test that a redundant bound is warned about at warning level, showing the bound and how it is redundant."""
        version_bound = bound(Verb.ALLOW, "update>=3.12")  # never has an effect on a 3.12 pin
        marker = Marker(version_bound=version_bound)
        location = create_location("Dockerfile", 6)
        Logger("bound").warn_if_redundant_bound("python", marker, "3.12", location)
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
        Logger("bound").warn_if_redundant_bound("python", marker, "3.12", location)
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
        Logger("bound").warn_if_redundant_bound("python", marker, "3.12", location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_REDUNDANT_BOUND,
            f"Redundant update bound allow[major-update] on {dependency('python')} 3.12 in {at('Dockerfile:6')}: "
            "it never has an effect",
        )

    def test_warn_if_redundant_bound_does_nothing_when_live(self, mock_log: Mock):
        """Test that nothing is logged when the bound is live (a genuine ceiling or floor)."""
        version_bound = bound(Verb.ALLOW, "update<3.13")  # a live ceiling on a 3.12 pin
        marker = Marker(version_bound=version_bound)
        Logger("bound").warn_if_redundant_bound("python", marker, "3.12", create_location("Dockerfile", 6))
        mock_log.assert_not_called()

    def test_warn_if_redundant_bound_does_nothing_when_level_bound_is_live(self, mock_log: Mock):
        """Test that nothing is logged for a level bound between the extremes: it always leaves room above the pin."""
        marker = Marker(version_bound=bound(Verb.IGNORE, "minor-update"))
        Logger("bound").warn_if_redundant_bound("python", marker, "3.12", create_location("Dockerfile", 6))
        mock_log.assert_not_called()

    def test_warn_if_redundant_bound_does_nothing_for_no_bound(self, mock_log: Mock):
        """Test that nothing is logged for the keep-all NO_BOUND: the unmarked default is not a bound to report on."""
        Logger("bound").warn_if_redundant_bound("python", Marker(), "3.12", create_location("Dockerfile", 6))
        mock_log.assert_not_called()

    def test_recognised_marker(self, mock_log: Mock):
        """Test that a reference's marker is logged at debug level verbatim, exactly as the user wrote it."""
        # The raw text combines scopes and bracket items, so echoing it verbatim shows the log takes the user's marker.
        raw = "ignore[update] ignore[stale] allow[update<3.13, hash-drift]"
        marker = Marker(ignore_stale=True, allow_drift=True, version_bound=bound(Verb.ALLOW, "update<3.13"), raw=raw)
        location = create_location("Dockerfile", 6)
        Logger("marker").recognised_marker("python", marker, location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_RECOGNISED_MARKER,
            f"Recognised update-time marker {raw} for {dependency('python')} in {at('Dockerfile:6')}",
        )

    def test_recognised_marker_does_nothing_without_marker(self, mock_log: Mock):
        """Test that nothing is logged for a reference without a marker."""
        Logger("marker").recognised_marker("python", Marker(), create_location("Dockerfile", 6))
        mock_log.assert_not_called()

    def test_ignored(self, mock_log: Mock):
        """Test that a held-back reference logs its `ignore` directive verbatim, exactly as the user spelled it."""
        # `ignored` names the `ignore` directives from the verbatim `raw` text, each scope as the user spelled it.
        marker = Marker(ignore_update=True, raw="ignore[update] ignore[stale] allow[hash-drift]")
        location = create_location("Dockerfile", 6)
        Logger("marker").ignored("python", marker, location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_IGNORED,
            f"Ignoring updates for {dependency('python')} in {at('Dockerfile:6')} "
            "(update-time: ignore[update] ignore[stale])",
        )

    def test_ignored_staleness(self, mock_log: Mock):
        """Test that a held-back staleness warning is logged at debug level, with the `ignore` directive as written."""
        published = datetime.now(UTC) - timedelta(days=512, hours=1)
        version = DependencyVersion("4.15.0", newest_published=published)
        marker = Marker(ignore_stale=True, raw="ignore[stale] allow[hash-drift]")
        location = create_location("requirements.txt", 9)
        Logger("stale").ignored_staleness("humanize", version, marker, location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_IGNORED_STALENESS,
            f"Ignoring the staleness warning for {dependency('humanize')} in {at('requirements.txt:9')} "
            "(update-time: ignore[stale])",
        )

    def test_ignored_staleness_does_nothing_when_not_stale(self, mock_log: Mock):
        """Test that nothing is logged when the marker holds back a staleness warning that would not be given."""
        recent = DependencyVersion("4.15.0", newest_published=datetime.now(UTC) - timedelta(days=1))
        undated = DependencyVersion("4.15.0")
        logger = Logger("stale")
        marker = Marker(ignore_stale=True, raw="ignore[stale]")
        location = create_location("requirements.txt", 9)
        logger.ignored_staleness("humanize", recent, marker, location)
        logger.ignored_staleness("humanize", undated, marker, location)
        mock_log.assert_not_called()

    def test_ignored_yank(self, mock_log: Mock):
        """Test that a held-back yank warning is logged at debug level, with the `ignore` directive as written."""
        version = DependencyVersion("4.15.0", yank=Yank(yanked=True, reason="broke Python 3.10 support"))
        marker = Marker(ignore_yanked=True, raw="ignore[yanked] allow[hash-drift]")
        location = create_location("requirements.txt", 9)
        Logger("yanked").ignored_yank("humanize", version, marker, location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_IGNORED_YANK,
            f"Ignoring the yank warning for {dependency('humanize')} in {at('requirements.txt:9')} "
            "(update-time: ignore[yanked])",
        )

    def test_ignored_yank_does_nothing_when_not_yanked(self, mock_log: Mock):
        """Test that nothing is logged when the marker holds back a yank warning that would not be given."""
        version = DependencyVersion("4.15.0")
        marker = Marker(ignore_yanked=True, raw="ignore[yanked]")
        Logger("yanked").ignored_yank("humanize", version, marker, create_location("requirements.txt", 9))
        mock_log.assert_not_called()

    def test_redundant_yank_scope(self, mock_log: Mock):
        """Test that an inert yank scope is warned about, with the `ignore` directive as the user wrote it."""
        marker = Marker(ignore_yanked=True, raw="ignore[yanked] allow[hash-drift]")
        location = create_location("Dockerfile", 2)
        Logger("yanked").redundant_yank_scope("python", marker, location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_REDUNDANT_YANK_SCOPE,
            f"Redundant update-time marker ignore[yanked] for {dependency('python')} in {at('Dockerfile:2')}: "
            "this dependency's source has no yank concept, so the marker holds nothing back",
        )

    def test_path_logged_at_debug(self, mock_log: Mock):
        """Test that the per-file 'checking for updates' progress is logged at debug level."""
        config_yml = Path.cwd() / "config.yml"
        Logger("path").path(config_yml)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_CHECKING_PATH,
            f"Checking if there are updates for {at('config.yml')}",
        )

    def test_configured_uv_cooldown(self, mock_log: Mock):
        """Test that writing the cooldown into a project's uv config is logged, relative to the working directory."""
        path = Path.cwd() / "pyproject.toml"
        Logger("cooldown").configured_uv_cooldown(path, "7 days")
        self.assert_message(
            mock_log,
            Logger._MESSAGE_UV_COOLDOWN,
            f"Set uv exclude-newer to '7 days' in {at('pyproject.toml')} to apply the cooldown",
        )

    def test_configured_uv_cooldown_outside_working_directory(self, mock_log: Mock):
        """Test that a workspace root outside the working directory is logged as its absolute path."""
        outside = Path("/elsewhere/pyproject.toml")
        Logger("cooldown").configured_uv_cooldown(outside, "7 days")
        self.assert_message(
            mock_log,
            Logger._MESSAGE_UV_COOLDOWN,
            f"Set uv exclude-newer to '7 days' in {at('/elsewhere/pyproject.toml')} to apply the cooldown",
        )

    def test_invalid_pyproject_toml(self, mock_log: Mock):
        """Test that an unparsable pyproject.toml is logged as a warning."""
        path = Path.cwd() / "pyproject.toml"
        Logger("toml").invalid_pyproject_toml(path)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_INVALID_TOML,
            f"Skipping {at('pyproject.toml')}: it is not valid TOML",
        )

    def test_excluded_path_logged_at_debug(self, mock_log: Mock):
        """Test that a directory held back by --exclude-path is logged at debug level, with its path undelimited.

        A scan root is not a reference's location, so it carries no delimiter for the highlighter to style it by.
        """
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
        """Test that a non-numeric Node base image tag is warned about, reporting its Dockerfile as a location.

        The Dockerfile is a file the scan found, like the ones every other message points at, so it is reported
        relative to the working directory and delimited for the highlighter rather than as a bare path.
        """
        dockerfile = Path.cwd() / "docker" / "Dockerfile"
        Logger("node").non_numeric_node_base_image(dockerfile, "lts")
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NON_NUMERIC_NODE_BASE_IMAGE_TAG,
            "Cannot derive the Node engine version from the non-numeric base image tag 'node:lts' in "
            f"{at('docker/Dockerfile')}",
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
        location = create_location("a.txt", 3)
        Logger("date").new_version("dependency", version, location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NEW_VERSION,
            f"New version available for {dependency('dependency')} in {at('a.txt:3')}: "
            "1.0, published: 2026-05-29 13:54\nChangelog",
        )

    def test_publication_date_is_logged_in_utc(self, mock_log: Mock):
        """Test that a non-UTC publication date is converted to UTC before logging."""
        published = datetime(2026, 5, 29, 15, 54, tzinfo=timezone(timedelta(hours=2)))
        location = create_location("a.txt", 3)
        Logger("utc").new_version("dependency", DependencyVersion("1.0", published=published), location)
        self.assert_message(
            mock_log,
            Logger._MESSAGE_NEW_VERSION,
            f"New version available for {dependency('dependency')} in {at('a.txt:3')}: "
            "1.0, published: 2026-05-29 13:54\n"
            "No changelog available!",
        )


class LogHighlighterTests(TestCase):
    """Tests that a whole sha256 digest is highlighted as one token, not fragmented by Rich's built-in rules."""

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
        message = f"New version available in {at('docs/requirements.txt:42')}: 4.15.0"
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


class LogOriginTests(TestCase):
    """Tests that log records are attributed to the originating updater, not to the shared machinery in between."""

    def assert_origin_is_this_test(self, records: list[logging.LogRecord]) -> None:
        """Assert that every record names this test file, the caller of the shared machinery, as its origin."""
        self.assertEqual({Path(record.pathname).name for record in records}, {"test_log.py"})

    def test_direct_call_is_attributed_to_the_caller(self):
        """Test that a log method called directly reports the calling line as its origin."""
        logger = Logger("origin direct")
        with self.assertLogs(logger.log, level="DEBUG") as captured:
            logger.path(Path.cwd())
        self.assert_origin_is_this_test(captured.records)

    def test_file_rewrite_is_attributed_to_the_caller(self):
        """Test that logs emitted while rewriting a file report the rewriting's caller as their origin."""
        logger = Logger("origin rewrite")
        with TemporaryDirectory() as directory:
            (Path(directory) / "config.yml").write_text("dependency: 1.0\n")
            with (
                patch("pathlib.Path.cwd", Mock(return_value=Path(directory))),
                self.assertLogs(logger.log, level="DEBUG") as captured,
            ):
                file.update_files(
                    "*.yml",
                    regexp=r"(?P<dependency>dependency): (?P<version>[\d.]+)",
                    get_new_version=new_version_getter("2.0"),
                    logger=logger,
                    start=Path(directory),
                )
        self.assert_origin_is_this_test(captured.records)

    def test_version_decision_is_attributed_to_the_caller(self):
        """Test that a warning from the shared version decision reports its caller, not the decision, as origin."""
        logger = Logger("origin decision")
        stale = DependencyVersion("2.0", newest_published=datetime.now(UTC) - timedelta(days=1000))
        reference = Reference("dependency", "1.0")
        with self.assertLogs(logger.log, level="DEBUG") as captured:
            latest_version(reference, lambda *_args: stale, Marker(), Location(Path.cwd()), logger)
        self.assert_origin_is_this_test(captured.records)

    def test_github_pin_decision_is_attributed_to_the_caller(self):
        """Test that a pin from the shared GitHub decision reports its caller, not the decision, as origin."""
        logger = Logger("origin github")
        latest = DependencyVersion("2.0", sha=COMMIT_SHA)
        reference = Reference("owner/action", "1.0")
        with (
            patch("update_time.references.github.get_latest_version", Mock(return_value=latest)),
            self.assertLogs(logger.log, level="DEBUG") as captured,
        ):
            latest_pin(reference, Marker(), Location(Path.cwd()), logger)
        self.assert_origin_is_this_test(captured.records)
