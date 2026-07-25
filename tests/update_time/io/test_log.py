"""Logger unit tests."""

import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import ANY, Mock, patch

from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text

from update_time.domain.bound import Redundancy, Verb
from update_time.domain.location import Location
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
from update_time.references import file
from update_time.references.github import latest_pin
from update_time.references.resolve import latest_version

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

    MESSAGE = LogMessage(logging.WARNING, "Stale dependency %s")

    def test_a_message_renders_as_its_format_string(self):
        """Test that a message renders as its format string, so logging can interpolate the arguments into it."""
        self.assertEqual(str(self.MESSAGE), "Stale dependency %s")

    def test_a_message_reprs_as_its_format_string(self):
        """Test that a message reprs as its format string, so a failing assertion reads as the message itself."""
        self.assertEqual(repr(self.MESSAGE), "'Stale dependency %s'")

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
        self.assertIn(str(Logger.MESSAGE_NEW_VERSION), messages)
        for message in messages:
            with self.subTest(message=message):
                self.assertNotIn(".", message)


class RenderTests(TestCase):
    """Unit tests for how the logger renders a dependency and a location for the highlighter to pick up."""

    def test_render_wraps_the_relative_path_and_line_in_the_delimiter(self):
        """Test that a location renders as the delimiter-wrapped relative path, with the line appended when present."""
        path = Path.cwd() / "docs" / "requirements.txt"
        self.assertEqual(
            Logger._render_location(Location(path, 42)),
            f"{LOCATION_DELIMITER}docs/requirements.txt:42{LOCATION_DELIMITER}",
        )
        self.assertEqual(
            Logger._render_location(Location(path)), f"{LOCATION_DELIMITER}docs/requirements.txt{LOCATION_DELIMITER}"
        )


@patch("logging.Logger.log")
class LoggerTests(TestCase):
    """Unit tests for the logger class."""

    def test_suppress_repeated_changelog(self, mock_log: Mock):
        """Test that a repeated changelog is suppressed."""
        logger = Logger("suppress changelog")
        path = Path.cwd() / "pyproject.toml"
        message = Logger.MESSAGE_NEW_VERSION
        logger.new_version("dependency", DependencyVersion("1.0", "Changelog"), Location(path, 5))
        mock_log.assert_called_once_with(
            message.level,
            message,
            Logger._render_dependency("dependency"),
            Logger._render_location(Location(path, 5)),
            "1.0",
            "Changelog",
            stacklevel=ANY,
        )
        logger.new_version("dependency", DependencyVersion("1.0", "Changelog"), Location(path, 5))
        mock_log.assert_called_with(
            message.level,
            message,
            Logger._render_dependency("dependency"),
            Logger._render_location(Location(path, 5)),
            "1.0",
            Logger._SUPPRESSING_CHANGELOG,
            stacklevel=ANY,
        )

    def test_new_version_without_publication_date(self, mock_log: Mock):
        """Test that the version is logged without a publication date when it is unknown."""
        path = Path.cwd() / "a.txt"
        Logger("no date").new_version("dependency", DependencyVersion("1.0", "Changelog"), Location(path, 3))
        mock_log.assert_called_once_with(
            Logger.MESSAGE_NEW_VERSION.level,
            Logger.MESSAGE_NEW_VERSION,
            Logger._render_dependency("dependency"),
            Logger._render_location(Location(path, 3)),
            "1.0",
            "Changelog",
            stacklevel=ANY,
        )

    def test_pinned(self, mock_log: Mock):
        """Test that pinning a previously unpinned reference to a digest is logged."""
        sha = f"sha256:{'a' * 64}"
        path = Path.cwd() / "Dockerfile"
        Logger("pin").pinned("dependency", DependencyVersion("1.0", sha=sha), Location(path, 1))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_PINNED.level,
            Logger._MESSAGE_PINNED,
            Logger._render_dependency("dependency"),
            Logger._render_location(Location(path, 1)),
            "1.0",
            sha,
            stacklevel=ANY,
        )

    def test_digest_drift(self, mock_log: Mock):
        """Test that a re-pushed tag whose digest changed under an unchanged pin is warned about at warning level."""
        old_sha, new_sha = f"sha256:{'a' * 64}", f"sha256:{'b' * 64}"
        path = Path.cwd() / "Dockerfile"
        Logger("drift").digest_drift("dependency", "3.14", old_sha, new_sha, Location(path, 2))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_DIGEST_DRIFT.level,
            Logger._MESSAGE_DIGEST_DRIFT,
            Logger._render_dependency("dependency"),
            "3.14",
            Logger._render_location(Location(path, 2)),
            old_sha,
            new_sha,
            stacklevel=ANY,
        )

    def test_adopted_drift(self, mock_log: Mock):
        """Test that adopting a re-pushed tag's new digest is logged at info level, naming the opt-in that caused it."""
        old_sha, new_sha = f"sha256:{'a' * 64}", f"sha256:{'b' * 64}"
        cause = "update-time: allow[digest-drift]"
        path = Path.cwd() / "Dockerfile"
        Logger("adopt").adopted_drift("dependency", "3.14", old_sha, new_sha, Location(path, 2), cause)
        message = Logger._MESSAGE_ADOPTED_DIGEST_DRIFT
        mock_log.assert_called_once_with(
            message.level,
            message,
            Logger._render_dependency("dependency"),
            "3.14",
            Logger._render_location(Location(path, 2)),
            old_sha,
            new_sha,
            cause,
            stacklevel=ANY,
        )

    def test_warn_if_stale(self, mock_log: Mock):
        """Test that a dependency whose newest release is old is warned about at warning level."""
        published = datetime.now(UTC) - timedelta(days=512, hours=1)
        version = DependencyVersion("4.15.0", newest_published=published)
        path = Path.cwd() / "requirements.txt"
        Logger("stale").warn_if_stale("humanize", version, Location(path, 9))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_STALE.level,
            Logger._MESSAGE_STALE,
            Logger._render_dependency("humanize"),
            Logger._render_location(Location(path, 9)),
            "4.15.0",
            512,
            365,
            stacklevel=ANY,
        )

    def test_warn_if_stale_does_nothing_when_not_stale(self, mock_log: Mock):
        """Test that nothing is logged when the newest release date is recent or unknown."""
        recent = DependencyVersion("4.15.0", newest_published=datetime.now(UTC) - timedelta(days=1))
        undated = DependencyVersion("4.15.0")
        logger = Logger("stale")
        loc = Location(Path.cwd() / "requirements.txt", 9)
        logger.warn_if_stale("humanize", recent, loc)
        logger.warn_if_stale("humanize", undated, loc)
        mock_log.assert_not_called()

    @staticmethod
    def rendered_message(mock: Mock) -> str:
        """Return the logged message with its % arguments filled in, as it would appear once formatted."""
        _level, template, *args = mock.call_args.args
        return str(template) % tuple(args)

    def test_warn_if_yanked_without_reason(self, mock_log: Mock):
        """Test that a yanked pin with no maintainer reason reports that the reason was not specified."""
        version = DependencyVersion("4.15.0", yank=Yank(yanked=True))
        Logger("yanked").warn_if_yanked("humanize", version, Location(Path.cwd() / "requirements.txt", 9))
        mock_log.assert_called_once()
        self.assertIn("was yanked (reason not specified)", self.rendered_message(mock_log))

    def test_warn_if_yanked_with_reason(self, mock_log: Mock):
        """Test that the maintainer's yank reason is included in the warning, in parentheses."""
        version = DependencyVersion("4.15.0", yank=Yank(yanked=True, reason="broke Python 3.10 support"))
        Logger("yanked").warn_if_yanked("humanize", version, Location(Path.cwd() / "requirements.txt", 9))
        mock_log.assert_called_once()
        self.assertIn('was yanked ("broke Python 3.10 support")', self.rendered_message(mock_log))

    def test_warn_if_yanked_does_nothing_when_not_yanked(self, mock_log: Mock):
        """Test that nothing is logged when the version was not yanked."""
        version = DependencyVersion("4.15.0")
        Logger("yanked").warn_if_yanked("humanize", version, Location(Path.cwd() / "requirements.txt", 9))
        mock_log.assert_not_called()

    def test_invalid_specifier(self, mock_log: Mock):
        """Test that an unparsable version bound specifier is warned about at warning level."""
        path = Path.cwd() / "Dockerfile"
        Logger("bound").invalid_specifier("python", "@@@", Location(path, 2))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_INVALID_SPECIFIER.level,
            Logger._MESSAGE_INVALID_SPECIFIER,
            "@@@",
            Logger._render_dependency("python"),
            Logger._render_location(Location(path, 2)),
            stacklevel=ANY,
        )

    def test_warn_if_redundant_bound(self, mock_log: Mock):
        """Test that a redundant bound is warned about at warning level, showing the bound and how it is redundant."""
        version_bound = bound(Verb.ALLOW, "update>=3.12")  # never has an effect on a 3.12 pin
        marker = Marker(version_bound=version_bound)
        Logger("bound").warn_if_redundant_bound("python", marker, "3.12", Location(Path.cwd() / "Dockerfile", 6))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_REDUNDANT_BOUND.level,
            Logger._MESSAGE_REDUNDANT_BOUND,
            version_bound,
            Logger._render_dependency("python"),
            "3.12",
            Logger._render_location(Location(Path.cwd() / "Dockerfile", 6)),
            Redundancy.NO_EFFECT.value,
            stacklevel=ANY,
        )

    def test_warn_if_redundant_level_bound(self, mock_log: Mock):
        """Test that a level bound that blocks every update is warned about, rendered in its level form."""
        version_bound = bound(Verb.IGNORE, "patch-update")  # ignore[patch-update] blocks every update
        marker = Marker(version_bound=version_bound)
        Logger("bound").warn_if_redundant_bound("python", marker, "3.12", Location(Path.cwd() / "Dockerfile", 6))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_REDUNDANT_BOUND.level,
            Logger._MESSAGE_REDUNDANT_BOUND,
            version_bound,
            Logger._render_dependency("python"),
            "3.12",
            Logger._render_location(Location(Path.cwd() / "Dockerfile", 6)),
            Redundancy.BLOCKS_ALL.value,
            stacklevel=ANY,
        )

    def test_warn_if_redundant_keep_all_level_bound(self, mock_log: Mock):
        """Test that a level bound that allows every update is warned about, unlike the implicit NO_BOUND default."""
        version_bound = bound(Verb.ALLOW, "major-update")  # allow[major-update] allows every update
        marker = Marker(version_bound=version_bound)
        Logger("bound").warn_if_redundant_bound("python", marker, "3.12", Location(Path.cwd() / "Dockerfile", 6))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_REDUNDANT_BOUND.level,
            Logger._MESSAGE_REDUNDANT_BOUND,
            version_bound,
            Logger._render_dependency("python"),
            "3.12",
            Logger._render_location(Location(Path.cwd() / "Dockerfile", 6)),
            Redundancy.NO_EFFECT.value,
            stacklevel=ANY,
        )

    def test_warn_if_redundant_bound_does_nothing_when_live(self, mock_log: Mock):
        """Test that nothing is logged when the bound is live (a genuine ceiling or floor)."""
        version_bound = bound(Verb.ALLOW, "update<3.13")  # a live ceiling on a 3.12 pin
        marker = Marker(version_bound=version_bound)
        Logger("bound").warn_if_redundant_bound("python", marker, "3.12", Location(Path.cwd() / "Dockerfile", 6))
        mock_log.assert_not_called()

    def test_warn_if_redundant_bound_does_nothing_when_level_bound_is_live(self, mock_log: Mock):
        """Test that nothing is logged for a level bound between the extremes: it always leaves room above the pin."""
        marker = Marker(version_bound=bound(Verb.IGNORE, "minor-update"))
        Logger("bound").warn_if_redundant_bound("python", marker, "3.12", Location(Path.cwd() / "Dockerfile", 6))
        mock_log.assert_not_called()

    def test_warn_if_redundant_bound_does_nothing_for_no_bound(self, mock_log: Mock):
        """Test that nothing is logged for the keep-all NO_BOUND: the unmarked default is not a bound to report on."""
        Logger("bound").warn_if_redundant_bound("python", Marker(), "3.12", Location(Path.cwd() / "Dockerfile", 6))
        mock_log.assert_not_called()

    def test_recognised_marker(self, mock_log: Mock):
        """Test that a reference's marker is logged at debug level verbatim, exactly as the user wrote it."""
        # The raw text combines scopes and bracket items in a form the boolean fields alone could not produce, so
        # echoing it verbatim proves the log shows the user's own marker.
        raw = "ignore[update] ignore[stale] allow[update<3.13, digest-drift]"
        marker = Marker(ignore_stale=True, allow_drift=True, version_bound=bound(Verb.ALLOW, "update<3.13"), raw=raw)
        path = Path.cwd() / "Dockerfile"
        Logger("marker").recognised_marker("python", marker, Location(path, 6))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_RECOGNISED_MARKER.level,
            Logger._MESSAGE_RECOGNISED_MARKER,
            raw,
            Logger._render_dependency("python"),
            Logger._render_location(Location(path, 6)),
            stacklevel=ANY,
        )

    def test_recognised_marker_does_nothing_without_marker(self, mock_log: Mock):
        """Test that nothing is logged for a reference without a marker."""
        Logger("marker").recognised_marker("python", Marker(), Location(Path.cwd() / "Dockerfile", 6))
        mock_log.assert_not_called()

    def test_ignored(self, mock_log: Mock):
        """Test that a held-back reference logs its `ignore` directive verbatim, exactly as the user spelled it."""
        # `ignored` names just the `ignore` directives from the verbatim `raw` text: the combined scopes are kept
        # apart rather than shown as a bare `ignore`, and the `allow` alongside is left out, only the `ignore`.
        marker = Marker(ignore_update=True, raw="ignore[update] ignore[stale] allow[digest-drift]")
        path = Path.cwd() / "Dockerfile"
        Logger("marker").ignored("python", marker, Location(path, 6))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_IGNORED.level,
            Logger._MESSAGE_IGNORED,
            Logger._render_dependency("python"),
            Logger._render_location(Location(path, 6)),
            "ignore[update] ignore[stale]",
            stacklevel=ANY,
        )

    def test_ignored_staleness(self, mock_log: Mock):
        """Test that a held-back staleness warning is logged at debug level, with the `ignore` directive as written."""
        published = datetime.now(UTC) - timedelta(days=512, hours=1)
        version = DependencyVersion("4.15.0", newest_published=published)
        marker = Marker(ignore_stale=True, raw="ignore[stale] allow[digest-drift]")
        path = Path.cwd() / "requirements.txt"
        Logger("stale").ignored_staleness("humanize", version, marker, Location(path, 9))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_IGNORED_STALENESS.level,
            Logger._MESSAGE_IGNORED_STALENESS,
            Logger._render_dependency("humanize"),
            Logger._render_location(Location(path, 9)),
            "ignore[stale]",
            stacklevel=ANY,
        )

    def test_ignored_staleness_does_nothing_when_not_stale(self, mock_log: Mock):
        """Test that nothing is logged when the marker holds back a staleness warning that would not be given."""
        recent = DependencyVersion("4.15.0", newest_published=datetime.now(UTC) - timedelta(days=1))
        undated = DependencyVersion("4.15.0")
        logger = Logger("stale")
        marker = Marker(ignore_stale=True, raw="ignore[stale]")
        loc = Location(Path.cwd() / "requirements.txt", 9)
        logger.ignored_staleness("humanize", recent, marker, loc)
        logger.ignored_staleness("humanize", undated, marker, loc)
        mock_log.assert_not_called()

    def test_ignored_yank(self, mock_log: Mock):
        """Test that a held-back yank warning is logged at debug level, with the `ignore` directive as written."""
        version = DependencyVersion("4.15.0", yank=Yank(yanked=True, reason="broke Python 3.10 support"))
        marker = Marker(ignore_yanked=True, raw="ignore[yanked] allow[digest-drift]")
        path = Path.cwd() / "requirements.txt"
        Logger("yanked").ignored_yank("humanize", version, marker, Location(path, 9))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_IGNORED_YANK.level,
            Logger._MESSAGE_IGNORED_YANK,
            Logger._render_dependency("humanize"),
            Logger._render_location(Location(path, 9)),
            "ignore[yanked]",
            stacklevel=ANY,
        )

    def test_ignored_yank_does_nothing_when_not_yanked(self, mock_log: Mock):
        """Test that nothing is logged when the marker holds back a yank warning that would not be given."""
        version = DependencyVersion("4.15.0")
        marker = Marker(ignore_yanked=True, raw="ignore[yanked]")
        Logger("yanked").ignored_yank("humanize", version, marker, Location(Path.cwd() / "requirements.txt", 9))
        mock_log.assert_not_called()

    def test_redundant_yank_scope(self, mock_log: Mock):
        """Test that an inert yank scope is warned about, with the `ignore` directive as the user wrote it."""
        marker = Marker(ignore_yanked=True, raw="ignore[yanked] allow[digest-drift]")
        path = Path.cwd() / "Dockerfile"
        Logger("yanked").redundant_yank_scope("python", marker, Location(path, 2))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_REDUNDANT_YANK_SCOPE.level,
            Logger._MESSAGE_REDUNDANT_YANK_SCOPE,
            "ignore[yanked]",
            Logger._render_dependency("python"),
            Logger._render_location(Location(path, 2)),
            stacklevel=ANY,
        )

    def test_path_logged_at_debug(self, mock_log: Mock):
        """Test that the per-file 'checking for updates' progress is logged at debug level."""
        config_yml = Path.cwd() / "config.yml"
        Logger("path").path(config_yml)
        mock_log.assert_called_once_with(
            Logger._MESSAGE_CHECKING_PATH.level,
            Logger._MESSAGE_CHECKING_PATH,
            Logger._render_location(Location(config_yml)),
            stacklevel=ANY,
        )

    def test_configured_uv_cooldown(self, mock_log: Mock):
        """Test that writing the cooldown into a project's uv config is logged, relative to the working directory."""
        path = Path.cwd() / "pyproject.toml"
        Logger("cooldown").configured_uv_cooldown(path, "7 days")
        mock_log.assert_called_once_with(
            Logger._MESSAGE_UV_COOLDOWN.level,
            Logger._MESSAGE_UV_COOLDOWN,
            "7 days",
            Logger._render_location(Location(path)),
            stacklevel=ANY,
        )

    def test_configured_uv_cooldown_outside_working_directory(self, mock_log: Mock):
        """Test that a workspace root outside the working directory is logged as its absolute path, not crashing."""
        outside = Path("/elsewhere/pyproject.toml")
        Logger("cooldown").configured_uv_cooldown(outside, "7 days")
        mock_log.assert_called_once_with(
            Logger._MESSAGE_UV_COOLDOWN.level,
            Logger._MESSAGE_UV_COOLDOWN,
            "7 days",
            Logger._render_location(Location(outside)),
            stacklevel=ANY,
        )

    def test_invalid_pyproject_toml(self, mock_log: Mock):
        """Test that an unparsable pyproject.toml is logged as a warning."""
        path = Path.cwd() / "pyproject.toml"
        Logger("toml").invalid_pyproject_toml(path)
        mock_log.assert_called_once_with(
            Logger._MESSAGE_INVALID_TOML.level,
            Logger._MESSAGE_INVALID_TOML,
            Logger._render_location(Location(path)),
            stacklevel=ANY,
        )

    def test_excluded_path_logged_at_debug(self, mock_log: Mock):
        """Test that a directory held back by --exclude-path is logged at debug level."""
        Logger("exclude").excluded_path(Path("vendor"))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_EXCLUDING_PATH.level, Logger._MESSAGE_EXCLUDING_PATH, Path("vendor"), stacklevel=ANY
        )

    def test_missing_excluded_path_logged_at_warning(self, mock_log: Mock):
        """Test that a non-existing --exclude-path directory is logged as a warning, not an error."""
        Logger("exclude").missing_excluded_path(Path("vendor"))
        message = Logger._MESSAGE_PATH_TO_EXCLUDE_DOES_NOT_EXIST
        mock_log.assert_called_once_with(message.level, message, Path("vendor"), stacklevel=ANY)

    def test_forced_outside_git_repository_logged_at_warning(self, mock_log: Mock):
        """Test that running outside a git repository because of --force is logged as a warning, with the scan root."""
        Logger("git").forced_outside_git_repository(Path("/home/user/project"))
        mock_log.assert_called_once_with(
            Logger._MESSAGE_FORCED_OUTSIDE_GIT_REPOSITORY.level,
            Logger._MESSAGE_FORCED_OUTSIDE_GIT_REPOSITORY,
            Path("/home/user/project"),
            stacklevel=ANY,
        )

    def test_new_version_with_publication_date(self, mock_log: Mock):
        """Test that the publication date is appended to the version when it is known."""
        published = datetime(2026, 5, 29, 13, 54, tzinfo=UTC)
        version = DependencyVersion("1.0", "Changelog", published=published)
        path = Path.cwd() / "a.txt"
        Logger("date").new_version("dependency", version, Location(path, 3))
        message = Logger.MESSAGE_NEW_VERSION
        mock_log.assert_called_once_with(
            message.level,
            message,
            Logger._render_dependency("dependency"),
            Logger._render_location(Location(path, 3)),
            "1.0, published: 2026-05-29 13:54",
            "Changelog",
            stacklevel=ANY,
        )

    def test_publication_date_is_logged_in_utc(self, mock_log: Mock):
        """Test that a non-UTC publication date is converted to UTC before logging."""
        published = datetime(2026, 5, 29, 15, 54, tzinfo=timezone(timedelta(hours=2)))
        path = Path.cwd() / "a.txt"
        Logger("utc").new_version("dependency", DependencyVersion("1.0", published=published), Location(path, 3))
        message = Logger.MESSAGE_NEW_VERSION
        changelog = Logger.NO_CHANGELOG
        mock_log.assert_called_once_with(
            message.level,
            message,
            Logger._render_dependency("dependency"),
            Logger._render_location(Location(path, 3)),
            "1.0, published: 2026-05-29 13:54",
            changelog,
            stacklevel=ANY,
        )


class LogHighlighterTests(TestCase):
    """Tests that a whole sha256 digest is highlighted as one token, not fragmented by Rich's built-in rules."""

    def test_digest_highlighted_as_one_token(self):
        """Test that the full digest gets a single `repr.digest` span and no leftover fragment sub-spans inside it."""
        digest = f"sha256:{'a4fde3b2' + 'c' * 56}"  # a realistic 64-hex-character digest
        text = Text(f"pinned to {digest} but the registry now serves sha256:{'b' * 64}")
        LogHighlighter().highlight(text)
        start = text.plain.index(digest)
        spans_in_digest = [span for span in text.spans if span.start >= start and span.end <= start + len(digest)]
        self.assertEqual([(start, start + len(digest), "repr.digest")], spans_in_digest)

    def test_version_numbers_still_highlighted(self):
        """Test that ordinary highlighting (e.g. of a version number) is preserved for messages without a digest."""
        text = Text("New version available: 3.14")
        LogHighlighter().highlight(text)
        self.assertIn("repr.number", [span.style for span in text.spans])

    def test_dependency_name_highlighted_and_markers_removed(self):
        """Test that a marker-wrapped dependency name is styled as `repr.dependency` and the markers leave no trace."""
        dependency = Logger._render_dependency("actions/checkout")
        text = Text(Logger.MESSAGE_NEW_VERSION.format % (dependency, "a.txt", "1.1", "Changelog for 1.1"))
        LogHighlighter().highlight(text)
        self.assertEqual(text.plain, "New version available for actions/checkout in a.txt: 1.1\nChangelog for 1.1")
        dependency_spans = [(text.plain[span.start : span.end], span.style) for span in text.spans]
        self.assertIn(("actions/checkout", "repr.dependency"), dependency_spans)

    def test_dependency_names_and_digest_together(self):
        """Test that several dependency names and a digest in one message are each styled without interfering."""
        digest = f"sha256:{'a' * 64}"
        message = f"Pinned {DEPENDENCY_DELIMITER}ghcr.io/astral-sh/uv{DEPENDENCY_DELIMITER} in Dockerfile to {digest}"
        text = Text(message)
        LogHighlighter().highlight(text)
        self.assertEqual(text.plain, f"Pinned ghcr.io/astral-sh/uv in Dockerfile to {digest}")
        styled = {(text.plain[span.start : span.end], span.style) for span in text.spans}
        self.assertIn(("ghcr.io/astral-sh/uv", "repr.dependency"), styled)
        self.assertIn((digest, "repr.digest"), styled)

    def test_location_highlighted_as_one_token(self):
        """Test that a delimited path:line is one `repr.filename` span, delimiters stripped and no stray number span."""
        message = f"New version available in {LOCATION_DELIMITER}docs/requirements.txt:42{LOCATION_DELIMITER}: 4.15.0"
        text = Text(message)
        LogHighlighter().highlight(text)
        self.assertEqual(text.plain, "New version available in docs/requirements.txt:42: 4.15.0")
        styled = [(text.plain[span.start : span.end], span.style) for span in text.spans]
        self.assertIn(("docs/requirements.txt:42", "repr.filename"), styled)
        # The line number is part of the single location token, not highlighted as a separate number.
        self.assertNotIn(("42", "repr.number"), styled)

    def test_no_colour_output_is_plain_text(self):
        """Test that with colour disabled the styled name renders as the same plain text, markers and all removed."""
        highlighted = LogHighlighter()(Text(f"Pinned {DEPENDENCY_DELIMITER}python{DEPENDENCY_DELIMITER} in Dockerfile"))
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
        latest = DependencyVersion("2.0", sha="a" * 40)
        reference = Reference("owner/action", "1.0")
        with (
            patch("update_time.references.github.get_latest_version", Mock(return_value=latest)),
            self.assertLogs(logger.log, level="DEBUG") as captured,
        ):
            latest_pin(reference, Marker(), Location(Path.cwd()), logger)
        self.assert_origin_is_this_test(captured.records)
