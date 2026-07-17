"""Logger unit tests."""

import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import ANY, Mock, patch

from packaging.specifiers import SpecifierSet
from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text

from update_time.domain.marker import Marker
from update_time.domain.version import DependencyVersion, Redundancy, VersionFilter
from update_time.io import filesystem
from update_time.io.log import DEPENDENCY_DELIMITER, Logger, LogHighlighter, get_logger

from tests.update_time.helpers import new_version_getter


class GetLoggerTests(TestCase):
    """Unit tests for how get_logger configures the root logger."""

    def test_diagnostics_are_sent_to_stderr(self):
        """Test that the root logger sends all diagnostics to stderr, keeping stdout clean for --version/--help."""
        get_logger("stderr")  # Ensure the root logger has been configured.
        rich_handlers = [handler for handler in logging.getLogger().handlers if isinstance(handler, RichHandler)]
        self.assertTrue(rich_handlers)
        self.assertTrue(all(handler.console.stderr for handler in rich_handlers))


class LoggerTests(TestCase):
    """Unit tests for the logger class."""

    @patch("logging.Logger.info")
    def test_suppress_repeated_changelog(self, mock_info: Mock):
        """Test that a repeated changelog is suppressed."""
        logger = Logger("suppress changelog")
        path = Path.cwd() / "pyproject.toml"
        message = Logger.MESSAGE_NEW_VERSION
        logger.new_version("dependency", DependencyVersion("1.0", "Changelog"), path)
        mock_info.assert_called_once_with(
            message,
            "dependency",
            Path("pyproject.toml"),
            "1.0",
            "Changelog",
            stacklevel=ANY,
        )
        logger.new_version("dependency", DependencyVersion("1.0", "Changelog"), path)
        mock_info.assert_called_with(
            message,
            "dependency",
            Path("pyproject.toml"),
            "1.0",
            Logger._SUPPRESSING_CHANGELOG,
            stacklevel=ANY,
        )

    @patch("logging.Logger.info")
    def test_new_version_without_publication_date(self, mock_info: Mock):
        """Test that the version is logged without a publication date when it is unknown."""
        Logger("no date").new_version("dependency", DependencyVersion("1.0", "Changelog"), Path.cwd() / "a.txt")
        mock_info.assert_called_once_with(
            Logger.MESSAGE_NEW_VERSION,
            "dependency",
            Path("a.txt"),
            "1.0",
            "Changelog",
            stacklevel=ANY,
        )

    @patch("logging.Logger.info")
    def test_pinned(self, mock_info: Mock):
        """Test that pinning a previously unpinned reference to a digest is logged."""
        sha = f"sha256:{'a' * 64}"
        Logger("pin").pinned("dependency", DependencyVersion("1.0", sha=sha), Path.cwd() / "Dockerfile")
        mock_info.assert_called_once_with(
            Logger._MESSAGE_PINNED,
            "dependency",
            Path("Dockerfile"),
            "1.0",
            sha,
            stacklevel=ANY,
        )

    @patch("logging.Logger.warning")
    def test_digest_drift(self, mock_warning: Mock):
        """Test that a re-pushed tag whose digest changed under an unchanged pin is warned about at warning level."""
        old_sha, new_sha = f"sha256:{'a' * 64}", f"sha256:{'b' * 64}"
        Logger("drift").digest_drift("dependency", "3.14", old_sha, new_sha, Path.cwd() / "Dockerfile")
        mock_warning.assert_called_once_with(
            Logger._MESSAGE_DIGEST_DRIFT, "dependency", "3.14", Path("Dockerfile"), old_sha, new_sha, stacklevel=ANY
        )

    @patch("logging.Logger.info")
    def test_adopted_drift(self, mock_info: Mock):
        """Test that adopting a re-pushed tag's new digest is logged at info level, naming the opt-in that caused it."""
        old_sha, new_sha = f"sha256:{'a' * 64}", f"sha256:{'b' * 64}"
        cause = "update-time: allow[digest-drift]"
        Logger("adopt").adopted_drift("dependency", "3.14", old_sha, new_sha, Path.cwd() / "Dockerfile", cause)
        message = Logger._MESSAGE_ADOPTED_DIGEST_DRIFT
        mock_info.assert_called_once_with(
            message, "dependency", "3.14", Path("Dockerfile"), old_sha, new_sha, cause, stacklevel=ANY
        )

    @patch("logging.Logger.warning")
    def test_warn_if_stale(self, mock_warning: Mock):
        """Test that a dependency whose newest release is old is warned about at warning level."""
        published = datetime.now(UTC) - timedelta(days=512, hours=1)
        version = DependencyVersion("4.15.0", newest_published=published)
        Logger("stale").warn_if_stale("humanize", version, Path.cwd() / "requirements.txt")
        mock_warning.assert_called_once_with(
            Logger._MESSAGE_STALE, "humanize", Path("requirements.txt"), "4.15.0", 512, 365, stacklevel=ANY
        )

    @patch("logging.Logger.warning")
    def test_warn_if_stale_does_nothing_when_not_stale(self, mock_warning: Mock):
        """Test that nothing is logged when the newest release date is recent or unknown."""
        recent = DependencyVersion("4.15.0", newest_published=datetime.now(UTC) - timedelta(days=1))
        undated = DependencyVersion("4.15.0")
        logger = Logger("stale")
        logger.warn_if_stale("humanize", recent, Path.cwd() / "requirements.txt")
        logger.warn_if_stale("humanize", undated, Path.cwd() / "requirements.txt")
        mock_warning.assert_not_called()

    @patch("logging.Logger.warning")
    def test_invalid_specifier(self, mock_warning: Mock):
        """Test that an unparsable version bound specifier is warned about at warning level."""
        Logger("bound").invalid_specifier("python", "@@@", Path.cwd() / "Dockerfile")
        mock_warning.assert_called_once_with(
            Logger._MESSAGE_INVALID_SPECIFIER, "@@@", "python", Path("Dockerfile"), stacklevel=ANY
        )

    @patch("logging.Logger.warning")
    def test_warn_if_redundant_bound(self, mock_warning: Mock):
        """Test that a redundant bound is warned about at warning level, showing the bound and how it is redundant."""
        version_filter = VersionFilter(SpecifierSet(">=3.12"), allow=True)  # never has an effect on a 3.12 pin
        Logger("bound").warn_if_redundant_bound("python", version_filter, "3.12", Path.cwd() / "Dockerfile")
        mock_warning.assert_called_once_with(
            Logger._MESSAGE_REDUNDANT_BOUND,
            version_filter,
            "python",
            "3.12",
            Path("Dockerfile"),
            Redundancy.NO_EFFECT.value,
            stacklevel=ANY,
        )

    @patch("logging.Logger.warning")
    def test_warn_if_redundant_bound_does_nothing_when_live(self, mock_warning: Mock):
        """Test that nothing is logged when the bound is live (a genuine ceiling or floor)."""
        version_filter = VersionFilter(SpecifierSet("<3.13"), allow=True)  # a live ceiling on a 3.12 pin
        Logger("bound").warn_if_redundant_bound("python", version_filter, "3.12", Path.cwd() / "Dockerfile")
        mock_warning.assert_not_called()

    @patch("logging.Logger.debug")
    def test_applying_marker(self, mock_debug: Mock):
        """Test that a reference's marker is logged at debug level as the directive list it expresses."""
        bound = VersionFilter(SpecifierSet("<3.13"), allow=True)
        marker = Marker(ignore_stale=True, allow_drift=True, version_filter=bound)
        Logger("marker").applying_marker("python", marker, Path.cwd() / "Dockerfile")
        mock_debug.assert_called_once_with(
            Logger._MESSAGE_APPLYING_MARKER,
            "ignore[stale] allow[update<3.13] allow[digest-drift]",
            "python",
            Path("Dockerfile"),
            stacklevel=ANY,
        )

    @patch("logging.Logger.debug")
    def test_applying_marker_does_nothing_without_marker(self, mock_debug: Mock):
        """Test that nothing is logged for a reference without a marker."""
        Logger("marker").applying_marker("python", Marker(), Path.cwd() / "Dockerfile")
        mock_debug.assert_not_called()

    @patch("logging.Logger.debug")
    def test_path_logged_at_debug(self, mock_debug: Mock):
        """Test that the per-file 'checking for updates' progress is logged at debug level."""
        Logger("path").path(Path.cwd() / "config.yml")
        mock_debug.assert_called_once_with(Logger._MESSAGE_CHECKING_PATH, Path("config.yml"), stacklevel=ANY)

    @patch("logging.Logger.info")
    def test_configured_uv_cooldown(self, mock_info: Mock):
        """Test that writing the cooldown into a project's uv config is logged, relative to the working directory."""
        Logger("cooldown").configured_uv_cooldown(Path.cwd() / "pyproject.toml", "7 days")
        mock_info.assert_called_once_with(Logger._MESSAGE_UV_COOLDOWN, "7 days", Path("pyproject.toml"), stacklevel=ANY)

    @patch("logging.Logger.info")
    def test_configured_uv_cooldown_outside_working_directory(self, mock_info: Mock):
        """Test that a workspace root outside the working directory is logged as its absolute path, not crashing."""
        outside = Path("/elsewhere/pyproject.toml")
        Logger("cooldown").configured_uv_cooldown(outside, "7 days")
        mock_info.assert_called_once_with(Logger._MESSAGE_UV_COOLDOWN, "7 days", outside, stacklevel=ANY)

    @patch("logging.Logger.warning")
    def test_invalid_pyproject_toml(self, mock_warning: Mock):
        """Test that an unparsable pyproject.toml is logged as a warning."""
        Logger("toml").invalid_pyproject_toml(Path.cwd() / "pyproject.toml")
        mock_warning.assert_called_once_with(Logger._MESSAGE_INVALID_TOML, Path("pyproject.toml"), stacklevel=ANY)

    @patch("logging.Logger.debug")
    def test_excluded_path_logged_at_debug(self, mock_debug: Mock):
        """Test that a directory held back by --exclude-path is logged at debug level."""
        Logger("exclude").excluded_path(Path("vendor"))
        mock_debug.assert_called_once_with(Logger._MESSAGE_EXCLUDING_PATH, Path("vendor"), stacklevel=ANY)

    @patch("logging.Logger.warning")
    def test_missing_excluded_path_logged_at_warning(self, mock_warning: Mock):
        """Test that a non-existing --exclude-path directory is logged as a warning, not an error."""
        Logger("exclude").missing_excluded_path(Path("vendor"))
        message = Logger._MESSAGE_PATH_TO_EXCLUDE_DOES_NOT_EXIST
        mock_warning.assert_called_once_with(message, Path("vendor"), stacklevel=ANY)

    @patch("logging.Logger.warning")
    def test_forced_outside_git_repository_logged_at_warning(self, mock_warning: Mock):
        """Test that running outside a git repository because of --force is logged as a warning, with the scan root."""
        Logger("git").forced_outside_git_repository(Path("/home/user/project"))
        mock_warning.assert_called_once_with(
            Logger._MESSAGE_FORCED_OUTSIDE_GIT_REPOSITORY, Path("/home/user/project"), stacklevel=ANY
        )

    def test_no_message_contains_a_full_stop(self):
        """Test that no log message contains a full stop, keeping the style consistent (commas and semicolons).

        Every string attribute of `Logger` is a message template (or a fragment substituted into one), so introspect
        them all rather than listing them by hand and risk missing a future message.
        """
        messages = [
            value for name, value in vars(Logger).items() if isinstance(value, str) and not name.startswith("__")
        ]
        self.assertGreater(len(messages), 1)  # Guard against the introspection silently finding nothing to check.
        for message in messages:
            with self.subTest(message=message):
                self.assertNotIn(".", message)

    @patch("logging.Logger.info")
    def test_new_version_with_publication_date(self, mock_info: Mock):
        """Test that the publication date is appended to the version when it is known."""
        published = datetime(2026, 5, 29, 13, 54, tzinfo=UTC)
        version = DependencyVersion("1.0", "Changelog", published=published)
        Logger("date").new_version("dependency", version, Path.cwd() / "a.txt")
        message = Logger.MESSAGE_NEW_VERSION
        mock_info.assert_called_once_with(
            message,
            "dependency",
            Path("a.txt"),
            "1.0, published: 2026-05-29 13:54",
            "Changelog",
            stacklevel=ANY,
        )

    @patch("logging.Logger.info")
    def test_publication_date_is_logged_in_utc(self, mock_info: Mock):
        """Test that a non-UTC publication date is converted to UTC before logging."""
        published = datetime(2026, 5, 29, 15, 54, tzinfo=timezone(timedelta(hours=2)))
        Logger("utc").new_version("dependency", DependencyVersion("1.0", published=published), Path.cwd() / "a.txt")
        message = Logger.MESSAGE_NEW_VERSION
        changelog = Logger.NO_CHANGELOG
        mock_info.assert_called_once_with(
            message,
            "dependency",
            Path("a.txt"),
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
        text = Text(Logger.MESSAGE_NEW_VERSION % ("actions/checkout", "a.txt", "1.1", "Changelog for 1.1"))
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
    """Tests that log records are attributed to the originating updater, not the logging or filesystem helpers."""

    def test_direct_call_is_attributed_to_the_caller(self):
        """Test that a log method called directly reports the calling line as its origin."""
        logger = Logger("origin direct")
        with self.assertLogs(logger.log, level="DEBUG") as captured:
            logger.path(Path.cwd())
        self.assertEqual(Path(captured.records[0].pathname).name, "test_log.py")

    def test_filesystem_helper_call_is_attributed_to_the_caller(self):
        """Test that logs emitted via the filesystem helper report the helper's caller, not filesystem.py, as origin."""
        logger = Logger("origin helper")
        with TemporaryDirectory() as directory:
            (Path(directory) / "config.yml").write_text("dependency: 1.0\n")
            with (
                patch("pathlib.Path.cwd", Mock(return_value=Path(directory))),
                self.assertLogs(logger.log, level="DEBUG") as captured,
            ):
                filesystem.update_files(
                    "*.yml",
                    regexp=r"(?P<dependency>dependency): (?P<version>[\d.]+)",
                    get_new_version=new_version_getter("2.0"),
                    logger=logger,
                    start=Path(directory),
                )
        origins = {Path(record.pathname).name for record in captured.records}
        self.assertEqual(origins, {"test_log.py"})
