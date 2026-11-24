"""Logger unit tests."""

import contextlib
import logging
import re
import signal
from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest import TestCase
from unittest.mock import ANY, Mock, patch

import requests
from requests import Response
from rich.logging import RichHandler

from mutation_time.mutation import kills
from update_time.domain.bound import BoundRedundancy, Verb
from update_time.domain.dependency import (
    Archival,
    ArchivedSubject,
    Changes,
    DependencyVersion,
    FloatingPin,
    ImageNotLookedUp,
    Release,
    Yank,
)
from update_time.domain.directive import Reason
from update_time.domain.marker import Marker, Scope
from update_time.domain.reference import DriftedPin, DriftKind, Reference, RefKind
from update_time.io import log as log_module
from update_time.io.console import (
    CHANGES,
    NOTE,
)
from update_time.io.log import (
    FileContent,
    Logger,
    ToolTask,
    _LogMessage,
    get_logger,
)
from update_time.markers.parser import parse_directives
from update_time.primitives.location import Location
from update_time.primitives.lookup import LookedUp
from update_time.primitives.timestamp import utc_date_and_time

from tests.update_time.fixtures import (
    COMMIT_SHA1,
    COMMIT_SHA2,
    DIGEST,
    DIGEST1,
    DIGEST2,
    STALE_AFTER_90,
)
from tests.update_time.helpers import (
    adopted_drift_report,
    bound,
    drift_warning,
    logged_extra,
    message_of,
    published_version,
    reference,
    resolved_reference,
    vulnerability,
)
from tests.update_time.io.helpers import at, create_location, dependency

if TYPE_CHECKING:
    from collections.abc import Generator


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

    MESSAGE = _LogMessage(logging.WARNING, "Stale dependency %(dependency)s", "stale-dependency")

    def test_a_message_renders_as_its_format_string(self):
        """Test that a message renders as its format string, so logging can interpolate the arguments into it."""
        self.assertEqual(str(self.MESSAGE), "Stale dependency %(dependency)s")

    def test_a_message_reprs_as_its_format_string_and_its_name(self):
        """Test that a message reprs as its format string and its name, so a failing assertion names the message."""
        self.assertEqual(repr(self.MESSAGE), "'Stale dependency %(dependency)s' (stale-dependency)")

    def test_no_message_contains_a_full_stop(self):
        """Test that no log message contains a full stop, keeping the style consistent (commas and semicolons).

        Each log method holds its message templates. Each string attribute of `Logger` is a fragment substituted
        into one, and so is each word a kind of drift fills in.
        """
        fragments = [
            value for name, value in vars(Logger).items() if isinstance(value, str) and not name.startswith("__")
        ]
        messages = [str(message) for message in Logger.messages()] + fragments
        # Four enums carry message fragments of their own, so the fragments are read off the enums rather than off
        # the logger: the `Reason` a warning reports, the `BoundRedundancy` a bound is reported with, the
        # `FileContent` a file was read for, and the `ToolTask` a tool was run for.
        messages += [str(member) for enum in (Reason, BoundRedundancy, FileContent, ToolTask) for member in enum]
        messages += [word for move in log_module._REF_MOVES.values() for word in (move.moved, move.advice)]
        # Guard against the introspection silently covering the fragments alone, as it did when the messages stopped
        # being plain strings:
        self.assertIn(str(message_of(Logger.repository_not_served)), messages)
        self.assertIn(str(Reason.NO_YANK_CONCEPT), messages)
        self.assertIn(log_module._REF_MOVES[DriftKind.TAG].advice, messages)
        for message in messages:
            with self.subTest(message=message):
                self.assertNotIn(".", message)

    def test_every_message_names_its_holes(self):
        """Test that every message interpolates by name, so its log method hands the logger named fields."""
        messages = Logger.messages()
        names = [message.name for message in messages]
        self.assertIn("repository-not-served", names)  # The decorated methods' messages are read
        self.assertIn("digest-drift", names)  # And each of a method's stacked messages
        self.assertIn("ref-drift", names)
        for message in messages:
            with self.subTest(message=message.name):
                self.assertEqual(re.findall(r"%(?!\()", message.format), [])

    def test_no_two_messages_share_a_name(self):
        """Test that no two log messages share a name."""
        names = Counter(message.name for message in Logger.messages())
        self.assertIn("drift-push-date-unknown", names)  # Each of a method's stacked messages is read
        self.assertEqual([name for name, count in names.items() if count > 1], [])


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

    def test_several_locations_render_as_a_list(self):
        """Test that several locations render each wrapped, as a sentence lists them."""
        locations = (Location(Path.cwd() / "a.in"), Location(Path.cwd() / "b.in"))
        self.assertEqual(Logger._render_field("sources", locations), f"{at('a.in')} and {at('b.in')}")

    def test_an_empty_tuple_renders_as_it_is(self):
        """Test that an empty tuple is passed through as it is, since there is no location in it to list."""
        self.assertEqual(Logger._render_field("sources", ()), ())

    @patch("logging.Logger.log")
    def test_a_location_field_is_wrapped_and_a_plain_field_is_not(self, mock_log: Mock):
        """Test that a location passed as a named field is wrapped, while a plain field is passed through as it is."""
        message = _LogMessage(logging.INFO, "Skipping %(location)s: %(reason)s", "skipped")
        _new_logger()._emit(message, {"location": create_location("Dockerfile", 1), "reason": "it is compiled"}, {})
        mock_log.assert_called_once_with(
            message.level, message, {"location": at("Dockerfile:1"), "reason": "it is compiled"}, extra=ANY
        )

    @patch("logging.Logger.log")
    def test_the_dependency_field_is_wrapped_in_its_delimiter(self, mock_log: Mock):
        """Test that the field named `dependency` is wrapped, so no log method has to render one itself."""
        message = _LogMessage(logging.WARNING, "Could not find where %(dependency)s is declared", "not-found")
        _new_logger()._emit(message, {"dependency": "actions/checkout"}, {})
        mock_log.assert_called_once_with(
            message.level, message, {"dependency": dependency("actions/checkout")}, extra=ANY
        )


@patch("logging.Logger.log")
class LoggerTests(TestCase):
    """Unit tests for the logger class."""

    @contextlib.contextmanager
    def subTest(self, *args: object, **kwargs: object) -> Generator[None]:  # noqa: N802
        """Run the case on records of its own, so a table cannot forget to reset the log."""
        cast("Mock", logging.Logger.log).reset_mock()  # The class-level patch makes the method the mock
        with super().subTest(*args, **kwargs):
            yield

    def assert_message(self, mock_log: Mock, message: _LogMessage, level: int, rendered: str) -> None:
        """Assert the log method emitted the message once, at the level, reading as the given text."""
        mock_log.assert_called_once()
        self.assert_last_message(mock_log, message, level, rendered)

    def assert_last_message(self, mock_log: Mock, message: _LogMessage, level: int, rendered: str) -> None:
        """Assert the most recent record is the message at the level, carrying exactly the fields the message names."""
        logged_level, template, fields = mock_log.call_args.args
        self.assertEqual((logged_level, template), (level, message))
        self.assertEqual(set(fields), template.holes)
        self.assertEqual(str(template) % fields, rendered)

    def assert_changes(self, mock_log: Mock, changes: str) -> None:
        """Assert the most recent record carries a changelog's changes beside its fields."""
        self.assertEqual(logged_extra(mock_log.call_args.kwargs), {CHANGES: changes})

    def assert_note(self, mock_log: Mock, note: str) -> None:
        """Assert the most recent record carries Update-time's own note about the changes beside its fields."""
        self.assertEqual(logged_extra(mock_log.call_args.kwargs), {NOTE: note})

    def test_a_changelog_is_shown_again_once_the_shown_changelogs_are_forgotten(self, mock_log: Mock):
        """Test that forgetting the changelogs shown makes a logger show a changelog it has already shown."""
        message = message_of(Logger.new_version)
        location = create_location("pyproject.toml", 5)
        available = f"New version available for {dependency('dependency')} in {at('pyproject.toml:5')}: 1.0"
        logger = get_logger("reset suppression")
        logger.new_version(
            reference("dependency", location), DependencyVersion("1.0", Changes("Changelog", markdown=False, url=""))
        )
        logger.new_version(
            reference("dependency", location), DependencyVersion("1.0", Changes("Changelog", markdown=False, url=""))
        )
        self.assert_last_message(mock_log, message, logging.INFO, available)
        self.assert_note(mock_log, "Suppressing changelog already shown, see above")
        logger.forget_shown_changelogs()
        logger.new_version(
            reference("dependency", location), DependencyVersion("1.0", Changes("Changelog", markdown=False, url=""))
        )
        self.assert_last_message(mock_log, message, logging.INFO, available)
        self.assert_changes(mock_log, "Changelog")

    def test_new_version_without_publication_date(self, mock_log: Mock):
        """Test that the version is logged without a publication date when it is unknown."""
        location = create_location("a.txt", 3)
        _new_logger().new_version(
            reference("dependency", location), DependencyVersion("1.0", Changes("Changelog", markdown=False, url=""))
        )
        self.assert_message(
            mock_log,
            message_of(Logger.new_version),
            logging.INFO,
            f"New version available for {dependency('dependency')} in {at('a.txt:3')}: 1.0",
        )
        self.assert_changes(mock_log, "Changelog")

    def test_pinned(self, mock_log: Mock):
        """Test that pinning a previously unpinned reference to a digest is logged."""
        location = create_location("Dockerfile", 1)
        _new_logger().pinned(reference("dependency", location), DependencyVersion("1.0", sha=DIGEST))
        self.assert_message(
            mock_log,
            message_of(Logger.pinned),
            logging.INFO,
            f"Pinned {dependency('dependency')} in {at('Dockerfile:1')} to 1.0@{DIGEST}",
        )

    def test_unpinned_floating_tag(self, mock_log: Mock):
        """Test that a floating tag pinned to no version is reported with the reason it was left as it is."""
        location = create_location("docker-compose.yml", 7)
        release = DependencyVersion("dev")
        _new_logger().unpinned_floating_tag(reference("acme/api", location, "dev"), release, FloatingPin.NO_VERSION_TAG)
        self.assert_message(
            mock_log,
            message_of(Logger.unpinned_floating_tag),
            logging.DEBUG,
            f"Floating tag {dependency('acme/api')}:dev in {at('docker-compose.yml:7')} was left as it is: "
            "no tag naming a version serves the same image",
        )

    def test_unpinned_floating_tag_of_a_reference_that_names_no_tag(self, mock_log: Mock):
        """Test that a floating tag is reported by the image's name alone when the reference does not name a tag."""
        location = create_location("docker-compose.yml", 7)
        release = DependencyVersion("latest")
        _new_logger().unpinned_floating_tag(reference("acme/api", location), release, FloatingPin.NO_VERSION_TAG)
        self.assert_message(
            mock_log,
            message_of(Logger.unpinned_floating_tag),
            logging.DEBUG,
            f"Floating tag {dependency('acme/api')} in {at('docker-compose.yml:7')} was left as it is: "
            f"{FloatingPin.NO_VERSION_TAG}",
        )

    def test_unpinned_ref(self, mock_log: Mock):
        """Test that a branch or tag left unpinned is reported by its kind, with why its commit could not be fetched."""
        location = create_location(".github/workflows/ci.yml", 17)
        for kind, ref in ((RefKind.BRANCH, "branch"), (RefKind.TAG, "tag")):
            with self.subTest(ref):
                _new_logger().unpinned_ref(reference("actions/checkout", location, "main"), "HTTP 404, Not Found", kind)
                self.assert_message(
                    mock_log,
                    message_of(Logger.unpinned_ref),
                    logging.ERROR,
                    f"Could not fetch the commit of the {ref} {dependency('actions/checkout')}@main in "
                    f"{at('.github/workflows/ci.yml:17')}, so it was left as it is (HTTP 404, Not Found)",
                )

    def test_unchecked_branch_drift(self, mock_log: Mock):
        """Test that a branch drift that could not be checked names the check, why it failed, and the pin's fate."""
        location = create_location(".github/workflows/ci.yml", 17)
        branch = reference("actions/checkout", location, "main")
        _new_logger().unchecked_branch_drift(branch, COMMIT_SHA2, "HTTP 403, API rate limit exceeded")
        self.assert_message(
            mock_log,
            message_of(Logger.unchecked_branch_drift),
            logging.ERROR,
            f"Could not check branch drift for {dependency('actions/checkout')}@main in "
            f"{at('.github/workflows/ci.yml:17')}: GitHub did not compare the pinned commit with {COMMIT_SHA2}, the "
            "branch's newest commit older than the cooldown (HTTP 403, API rate limit exceeded); the pin was left "
            "unchanged",
        )

    def test_comment_naming_no_ref(self, mock_log: Mock):
        """Test that a commit SHA whose comment does not name a ref is reported with the comment and its location."""
        location = create_location(".github/workflows/ci.yml", 17)
        _new_logger().comment_naming_no_ref(reference("actions/checkout", location, "pinned"))
        self.assert_message(
            mock_log,
            message_of(Logger.comment_naming_no_ref),
            logging.DEBUG,
            f"Commit SHA of {dependency('actions/checkout')} in {at('.github/workflows/ci.yml:17')} was left as it "
            "is: its comment pinned does not name a branch or tag of the repository",
        )

    def test_unpinned_floating_tag_reason_names_the_tag_looked_up(self, mock_log: Mock):
        """Test that a reason about the tag names the tag looked up, which for a reference naming none is `latest`."""
        location = create_location(".circleci/config.yml", 1)
        cases = {
            FloatingPin.NOT_LISTED: "latest is not among the tags listed for the image",
            FloatingPin.NOT_AMONG_EXAMINED: "latest is not among the newest tags examined for the image",
            FloatingPin.NO_MANIFEST: (
                "the registry does not serve a manifest for latest, so what that tag serves is unknown"
            ),
        }
        for reason, explanation in cases.items():
            with self.subTest(reason.name):
                release = DependencyVersion("latest", floating=reason)
                _new_logger().unpinned_floating_tag(reference("default", location), release, reason)
                self.assert_message(
                    mock_log,
                    message_of(Logger.unpinned_floating_tag),
                    logging.DEBUG,
                    f"Floating tag {dependency('default')} in {at('.circleci/config.yml:1')} was left as it is: "
                    f"{explanation}",
                )

    @kills.replace(
        log_module.Logger,
        '"Reference %(dependency)s%(tag)s in %(location)s was left as it is: %(reason)s"',
        '"Reference %(dependency)s%(tag)s in %(location)s was left as it is"',
        "the line reports that a reference was left as it is without naming why the file does not pull its image",
    )
    def test_image_not_looked_up(self, mock_log: Mock):
        """Test that a reference whose image the file does not pull is reported with the reason it does not."""
        location = create_location("docker-compose.yml", 4)
        _new_logger().image_not_looked_up(reference("acme/api", location, "1.2.3"), ImageNotLookedUp.BUILT_IMAGE)
        self.assert_message(
            mock_log,
            message_of(Logger.image_not_looked_up),
            logging.DEBUG,
            f"Reference {dependency('acme/api')}:1.2.3 in {at('docker-compose.yml:4')} was left as it is: "
            "the Compose file builds this image",
        )

    @kills.delete(
        Logger,
        ': the registry does not serve the image\'s "\n        "repository',
        "the line reports that a reference was left as it is without naming that its repository is not served",
    )
    def test_repository_not_served(self, mock_log: Mock):
        """Test that a reference whose registry does not serve the image's repository is reported with that reason."""
        location = create_location("Dockerfile", 1)
        _new_logger().repository_not_served(reference("spark-rm-base", location, "3.5.1"))
        self.assert_message(
            mock_log,
            message_of(Logger.repository_not_served),
            logging.DEBUG,
            f"Reference {dependency('spark-rm-base')}:3.5.1 in {at('Dockerfile:1')} was left as it is: "
            "the registry does not serve the image's repository",
        )

    def test_keeping_a_floating_tag(self, mock_log: Mock):
        """Test that keeping a floating tag is reported with the tag it names and what it resolves to."""
        location = create_location("Dockerfile", 1)
        release = DependencyVersion("3.14.7", sha=DIGEST)
        directive = "allow[floating-pin]"
        _new_logger().keeping_floating_tag(reference("python", location, "latest"), release, directive)
        self.assert_message(
            mock_log,
            message_of(Logger.keeping_floating_tag),
            logging.DEBUG,
            f"Keeping the floating tag {dependency('python')}:latest in {at('Dockerfile:1')}: it resolves to "
            f"3.14.7@{DIGEST} (update-time: {directive})",
        )

    def test_keeping_a_ref(self, mock_log: Mock):
        """Test that keeping a branch or tag is reported by its kind, with the name it has and what it resolves to."""
        location = create_location(".github/workflows/ci.yml", 17)
        directive = "allow[floating-pin]"
        release = DependencyVersion("4.3.0", sha=COMMIT_SHA1)
        for kind, ref in ((RefKind.BRANCH, "branch"), (RefKind.TAG, "tag")):
            with self.subTest(ref):
                _new_logger().keeping_ref(reference("actions/checkout", location, "main"), release, directive, kind)
                self.assert_message(
                    mock_log,
                    message_of(Logger.keeping_ref),
                    logging.DEBUG,
                    f"Keeping the {ref} {dependency('actions/checkout')}@main in {at('.github/workflows/ci.yml:17')}: "
                    f"it resolves to 4.3.0@{COMMIT_SHA1} (update-time: {directive})",
                )

    def test_keeping_a_reference_that_names_no_tag(self, mock_log: Mock):
        """Test that a reference naming no tag is reported by its name alone, there being no tag to name after it."""
        location = create_location("Dockerfile", 1)
        release = DependencyVersion("3.14.7", sha=DIGEST)
        directive = "allow[floating-pin]"
        _new_logger().keeping_floating_tag(reference("python", location), release, directive)
        self.assert_message(
            mock_log,
            message_of(Logger.keeping_floating_tag),
            logging.DEBUG,
            f"Keeping the floating tag {dependency('python')} in {at('Dockerfile:1')}: it resolves to "
            f"3.14.7@{DIGEST} (update-time: {directive})",
        )

    def test_drift(self, mock_log: Mock):
        """Test that a hash pin whose target changed under it is warned about, naming the kind of drift."""
        dockerfile, workflow = create_location("Dockerfile", 2), create_location(".github/workflows/ci.yml", 17)
        checkout, ci = dependency("actions/checkout"), at(".github/workflows/ci.yml:17")
        cases = {
            "digest": (
                DriftedPin("dependency", "3.14", dockerfile, DIGEST1, kind=DriftKind.DIGEST, new_sha=DIGEST2),
                (
                    f"Digest drift for {dependency('dependency')}:3.14 in {at('Dockerfile:2')}: pinned to "
                    f"{DIGEST1} but the registry now serves {DIGEST2}; the pin was left unchanged, verify the change "
                    "is expected before updating the pin"
                ),
            ),
            "tag": (
                DriftedPin("actions/checkout", "4.1.1", workflow, COMMIT_SHA1, kind=DriftKind.TAG, new_sha=COMMIT_SHA2),
                (
                    f"Tag drift for {checkout}@4.1.1 in {ci}: pinned to commit {COMMIT_SHA1} but the tag now "
                    f"points at {COMMIT_SHA2}; the pin was left unchanged, verify the tag was moved deliberately "
                    "before updating the pin"
                ),
            ),
            "branch": (
                DriftedPin(
                    "actions/checkout", "main", workflow, COMMIT_SHA1, kind=DriftKind.BRANCH, new_sha=COMMIT_SHA2
                ),
                (
                    f"Branch drift for {checkout}@main in {ci}: pinned to commit {COMMIT_SHA1} but the branch has "
                    f"moved on to {COMMIT_SHA2}; the pin was left unchanged, verify the branch moved to a commit you "
                    "trust before updating the pin"
                ),
            ),
            "ref": (
                DriftedPin(
                    "actions/checkout", "stable-2024", workflow, COMMIT_SHA1, kind=DriftKind.REF, new_sha=COMMIT_SHA2
                ),
                (
                    f"Ref drift for {checkout}@stable-2024 in {ci}: pinned to commit {COMMIT_SHA1} but the ref now "
                    f"points at {COMMIT_SHA2}; the pin was left unchanged, verify the ref moved to a commit you trust "
                    "before updating the pin"
                ),
            ),
        }
        for name, (drifted, rendered) in cases.items():
            with self.subTest(name):
                _new_logger().drift(drifted)
                message, _fields = drift_warning(drifted)
                self.assert_message(mock_log, message, logging.WARNING, rendered)

    def test_undated_candidate(self, mock_log: Mock):
        """Test that an undated candidate names the source, what it dates, and the version the reference stays on."""
        python, checkout = dependency("python"), dependency("actions/checkout")
        cases = {
            "image tag": (
                lambda log: log.undated_push("python", "3.15.0", "3.14.6", "HTTP 429"),
                message_of(Logger.undated_push),
                (
                    f"Could not determine from Docker Hub when {python}:3.15.0 was pushed (HTTP 429), so the cooldown "
                    f"can't be verified; {python} stays on 3.14.6 rather than trying the tags in between"
                ),
            ),
            "tagged commit": (
                lambda log: log.undated_commit("actions/checkout", "v4.4.0", "4.3.0", "HTTP 403"),
                message_of(Logger.undated_commit),
                (
                    f"Could not determine from GitHub when {checkout} v4.4.0 was committed (HTTP 403), so the cooldown "
                    f"can't be verified; {checkout} stays on 4.3.0 rather than trying the versions in between"
                ),
            ),
        }
        for name, (report, message, rendered) in cases.items():
            with self.subTest(name):
                report(_new_logger())
                self.assert_message(mock_log, message, logging.ERROR, rendered)

    def test_no_release_metadata(self, mock_log: Mock):
        """Test that unfetched release metadata names what it leaves unknown and the version the reference stays on."""
        _new_logger().no_release_metadata("humanize", "4.16.0", "4.15.0", "HTTP 503")
        humanize = dependency("humanize")
        self.assert_message(
            mock_log,
            message_of(Logger.no_release_metadata),
            logging.ERROR,
            f"Could not fetch from PyPI the metadata of {humanize} 4.16.0 (HTTP 503), so its yank state and "
            f"publication date are unknown; {humanize} stays on 4.15.0 rather than trying the versions in between",
        )

    def test_undated_commit(self, mock_log: Mock):
        """Test that the undated commit a moved branch points at is reported with the reference and its location."""
        location = create_location(".github/workflows/ci.yml", 17)
        drifted = DriftedPin(
            "actions/checkout", "main", location, COMMIT_SHA1, kind=DriftKind.BRANCH, new_sha=COMMIT_SHA2
        )
        _new_logger().undated_drift(drifted, "HTTP 403, API rate limit exceeded")
        self.assert_message(
            mock_log,
            message_of(Logger.undated_drift, "new_sha"),
            logging.ERROR,
            f"Could not determine from GitHub the date of commit {COMMIT_SHA2} that "
            f"{dependency('actions/checkout')}@main in {at('.github/workflows/ci.yml:17')} now points at (HTTP 403, "
            "API rate limit exceeded), so the cooldown can't be verified; not adopting the drift",
        )

    def test_undated_push(self, mock_log: Mock):
        """Test that a re-pushed tag that could not be dated is reported with the reference and its location."""
        drifted = DriftedPin(
            "python", "3.14", create_location("Dockerfile", 1), DIGEST1, kind=DriftKind.DIGEST, new_sha=DIGEST2
        )
        _new_logger().undated_drift(drifted, "HTTP 429")
        self.assert_message(
            mock_log,
            message_of(Logger.undated_drift, "dependency", "location", "tag", "reason"),
            logging.ERROR,
            f"Could not determine from Docker Hub when {dependency('python')}:3.14 in {at('Dockerfile:1')} was pushed "
            "(HTTP 429), so the cooldown can't be verified; not adopting the drift",
        )

    def test_digest_drift_of_a_reference_naming_no_tag(self, mock_log: Mock):
        """Test that a drifted digest is reported by the image's name alone when the reference does not name a tag."""
        location = create_location("Dockerfile", 1)
        drifted = DriftedPin("python", "", location, DIGEST1, kind=DriftKind.DIGEST, new_sha=DIGEST2)
        _new_logger().drift(drifted)
        message, _fields = drift_warning(drifted)
        self.assert_message(
            mock_log,
            message,
            logging.WARNING,
            f"Digest drift for {dependency('python')} in {at('Dockerfile:1')}: pinned to {DIGEST1} "
            f"but the registry now serves {DIGEST2}; the pin was left unchanged, verify the change is expected "
            "before updating the pin",
        )

    @kills.replace(
        log_module._locked_version,
        "if version:",
        "if True:",
        "a range rewritten without a locked version is reported as unchanged at an empty version",
        case="no locked version",
    )
    @kills.replace(
        log_module._locked_version,
        "if version:",
        "if False:",
        "a range rewritten with a locked version is reported as having none",
        case="a locked version",
    )
    def test_range_rewrite(self, mock_log: Mock):
        """Test that a rewritten range names the version the lock file held, and says so where it held none."""
        pin = reference("minimatch", create_location("package.json", 3), "^3.1.2")
        rewrite = f"npm rewrote the range of {dependency('minimatch')} in {at('package.json:3')} from ^3.1.2 to ^3.1.5"
        cases = {
            "a locked version": ("3.1.5", "the version in the lock file is unchanged at 3.1.5"),
            "no locked version": ("", "the lock file did not record a version before"),
        }
        for case, (version, locked) in cases.items():
            with self.subTest(case=case):
                _new_logger().range_rewrite("npm", pin, "^3.1.5", version)
                self.assert_message(mock_log, message_of(Logger.range_rewrite), logging.INFO, f"{rewrite}; {locked}")

    def test_adopted_drift(self, mock_log: Mock):
        """Test that adopted drift is logged at info level, naming the kind of drift and the opt-in that caused it."""
        directive = "allow[hash-drift]"
        dockerfile, workflow = create_location("Dockerfile", 2), create_location(".github/workflows/ci.yml", 17)
        checkout, ci = dependency("actions/checkout"), at(".github/workflows/ci.yml:17")
        cases = {
            "digest": (
                DriftedPin("dependency", "3.14", dockerfile, DIGEST1, kind=DriftKind.DIGEST, new_sha=DIGEST2),
                (
                    f"Adopted digest drift for {dependency('dependency')}:3.14 in {at('Dockerfile:2')}: "
                    f"re-pinned from {DIGEST1} to {DIGEST2} (update-time: {directive})"
                ),
            ),
            "tag": (
                DriftedPin("actions/checkout", "4.1.1", workflow, COMMIT_SHA1, kind=DriftKind.TAG, new_sha=COMMIT_SHA2),
                (
                    f"Adopted tag drift for {checkout}@4.1.1 in {ci}: "
                    f"re-pinned from {COMMIT_SHA1} to {COMMIT_SHA2} (update-time: {directive})"
                ),
            ),
            "branch": (
                DriftedPin(
                    "actions/checkout", "main", workflow, COMMIT_SHA1, kind=DriftKind.BRANCH, new_sha=COMMIT_SHA2
                ),
                (
                    f"Adopted branch drift for {checkout}@main in {ci}: "
                    f"re-pinned from {COMMIT_SHA1} to {COMMIT_SHA2} (update-time: {directive})"
                ),
            ),
            "ref": (
                DriftedPin(
                    "actions/checkout", "stable-2024", workflow, COMMIT_SHA1, kind=DriftKind.REF, new_sha=COMMIT_SHA2
                ),
                (
                    f"Adopted ref drift for {checkout}@stable-2024 in {ci}: "
                    f"re-pinned from {COMMIT_SHA1} to {COMMIT_SHA2} (update-time: {directive})"
                ),
            ),
        }
        for name, (drifted, rendered) in cases.items():
            with self.subTest(name):
                _new_logger().adopted_drift(drifted, directive)
                message, _fields = adopted_drift_report(drifted, directive)
                self.assert_message(mock_log, message, logging.INFO, rendered)

    def test_stale_dependency_warning(self, mock_log: Mock):
        """Test that an old newest release is warned about at warning level, naming the release and the threshold."""
        newest = Release("5.0.0", datetime.now(UTC) - timedelta(days=512, hours=1))
        _new_logger().stale_dependency(reference("humanize", create_location("requirements.txt", 9)), newest, 90)
        self.assert_message(
            mock_log,
            message_of(Logger.stale_dependency),
            logging.WARNING,
            f"Stale dependency {dependency('humanize')} in {at('requirements.txt:9')}: "
            "newest release 5.0.0 was published 512 days ago (> 90)",
        )

    @kills.replace(
        log_module,
        '"Incompatible Python version %(dependency)s in %(location)s: version %(version)s falls outside the "',
        '"%(dependency)s %(version)s in %(location)s falls outside the "',
        "the warning opens with the bare dependency name, unlike every other warning about a dependency",
    )
    def test_python_version_outside_requires_python_warning(self, mock_log: Mock):
        """Test that an entry outside the `requires-python` is warned about with a label, as a dependency is."""
        entry = reference("python", create_location(".python-version", 1), "3.13.2")
        requires_python = reference("python", create_location("pyproject.toml", 2), ">=3.10,<3.12")
        _new_logger().python_version_outside_requires_python(entry, requires_python)
        self.assert_message(
            mock_log,
            message_of(Logger.python_version_outside_requires_python),
            logging.WARNING,
            f"Incompatible Python version {dependency('python')} in {at('.python-version:1')}: version 3.13.2 "
            f"falls outside the requires-python >=3.10,<3.12 of {at('pyproject.toml:2')}, "
            "so uv refuses to run for the project",
        )

    def test_redundant_stale_threshold(self, mock_log: Mock):
        """Test that the warning names the threshold as written, the run's threshold, and the newest release's age.

        The run's threshold is named as the default where it is one, and by the option otherwise.
        """
        newest = Release("4.15.0", datetime.now(UTC) - timedelta(days=30, hours=1))
        location = create_location("requirements.txt", 9)
        threshold = STALE_AFTER_90.stale
        runs = {365: "the default of 365 days", 400: "--stale-after 400"}
        for run_threshold, run_words in runs.items():
            with self.subTest(run_threshold=run_threshold):
                _new_logger().redundant_stale_threshold(
                    reference("humanize", location), threshold, newest, run_threshold
                )
                self.assert_message(
                    mock_log,
                    message_of(Logger.redundant_stale_threshold),
                    logging.WARNING,
                    f"Redundant update-time directive set[stale-after=90] for {dependency('humanize')} in "
                    f"{at('requirements.txt:9')}: newest release 4.15.0 was published 30 days ago, which the "
                    f"threshold of 90 days and {run_words} judge alike",
                )

    def test_yanked_dependency_warning(self, mock_log: Mock):
        """Test that the warning quotes the maintainer's reason, and says so where they gave none."""
        message = message_of(Logger.yanked_dependency)
        location = create_location("requirements.txt", 9)
        for case, (reason, clause) in {
            "no reason": ("", "version 4.15.0 was yanked (reason not specified)"),
            "a reason": ("broke Python 3.10 support", 'version 4.15.0 was yanked ("broke Python 3.10 support")'),
        }.items():
            with self.subTest(case=case):
                version = DependencyVersion("4.15.0", yank=Yank(yanked=True, reason=reason))
                _new_logger().yanked_dependency(resolved_reference("humanize", location, version))
                self.assert_message(
                    mock_log,
                    message,
                    logging.WARNING,
                    f"Yanked dependency {dependency('humanize')} in {at('requirements.txt:9')}: {clause}",
                )

    def test_archived_dependency_warning(self, mock_log: Mock):
        """Test that the warning quotes the reason the source published, and ends where it published none."""
        message = message_of(Logger.archived_dependency)
        location = create_location("requirements.txt", 9)
        for case, (reason, clause) in {
            "no reason": ("", "the project was archived"),
            "a reason": ("superseded by humanize2", 'the project was archived ("superseded by humanize2")'),
        }.items():
            with self.subTest(case=case):
                archival = Archival(archived=True, reason=reason)
                _new_logger().archived_dependency(reference("humanize", location), archival)
                self.assert_message(
                    mock_log,
                    message,
                    logging.WARNING,
                    f"Archived dependency {dependency('humanize')} in {at('requirements.txt:9')}: {clause}",
                )

    def test_ignored_warning(self, mock_log: Mock):
        """Test that a warning a marker silenced reads with the warning's name and the directive that silenced it."""
        for scope, directive, advisory, name in (
            (Scope.STALE, "set[stale-after=0]", "", "staleness"),
            (Scope.YANKED, "ignore[yanked]", "", "yank"),
            (Scope.ARCHIVED, "ignore[archived]", "", "archival"),
            (
                Scope.VULNERABLE,
                "set[vulnerability-level=none]",
                "GHSA-2gwj-7jmv-h26r",
                "GHSA-2gwj-7jmv-h26r vulnerability",
            ),
        ):
            with self.subTest(scope=scope):
                _new_logger().ignored_warning(
                    reference("humanize", create_location("requirements.txt", 9)), scope, directive, advisory=advisory
                )
                self.assert_message(
                    mock_log,
                    message_of(Logger.ignored_warning),
                    logging.DEBUG,
                    f"Ignoring the {name} warning for {dependency('humanize')} in {at('requirements.txt:9')} "
                    f"(update-time: {directive})",
                )

    def test_an_invalid_item_is_warned_about_naming_the_item(self, mock_log: Mock):
        """Test that an item the marker language cannot read is warned about at warning level, naming it alone."""
        location = create_location("Dockerfile", 2)
        _new_logger().invalid_bracket_item(reference("python", location), "@@@")
        self.assert_message(
            mock_log,
            message_of(Logger.invalid_bracket_item),
            logging.WARNING,
            f"Invalid '@@@' in the update-time marker for {dependency('python')} in {at('Dockerfile:2')}",
        )

    def test_redundant_bound(self, mock_log: Mock):
        """Test that a bound deciding nothing for a 3.12 pin is warned about, naming it and how it is redundant."""
        location = create_location("Dockerfile", 6)
        for verb, item, redundancy in (
            (Verb.ALLOW, "major-update", BoundRedundancy.NO_EFFECT),
            (Verb.IGNORE, "patch-update", BoundRedundancy.BLOCKS_ALL),
        ):
            directive = f"{verb.value}[{item}]"
            with self.subTest(bound=directive):
                marker = Marker(version_bound=bound(verb, item))
                _new_logger().redundant_bound(Reference("python", "3.12", location, marker=marker), redundancy)
                self.assert_message(
                    mock_log,
                    message_of(Logger.redundant_bound),
                    logging.WARNING,
                    f"Redundant update-time directive {directive} for {dependency('python')} in {at('Dockerfile:6')}: "
                    f"this bound {redundancy} at version 3.12",
                )

    def test_recognised_marker(self, mock_log: Mock):
        """Test that a reference's marker is logged at debug level verbatim, exactly as the user wrote it."""
        # The raw text combines scopes and bracket items, so echoing it verbatim shows the log takes the user's marker.
        raw = "ignore[update] set[stale-after=0] allow[update<3.13, hash-drift]"
        marker = parse_directives(raw)
        location = create_location("Dockerfile", 6)
        _new_logger().recognised_marker(Reference("python", "", location, marker=marker))
        self.assert_message(
            mock_log,
            message_of(Logger.recognised_marker),
            logging.DEBUG,
            f"Recognised update-time marker {raw} for {dependency('python')} in {at('Dockerfile:6')}",
        )

    def test_ignored(self, mock_log: Mock):
        """Test that a held-back reference names the directive that held it back, not the ones written beside it."""
        marker = parse_directives("ignore[update] set[stale-after=0] allow[hash-drift]")
        location = create_location("Dockerfile", 6)
        _new_logger().ignored(Reference("python", "", location, marker=marker))
        self.assert_message(
            mock_log,
            message_of(Logger.ignored),
            logging.DEBUG,
            f"Ignoring updates for {dependency('python')} in {at('Dockerfile:6')} (update-time: ignore[update])",
        )

    def test_redundant_ignore(self, mock_log: Mock):
        """Test that an `ignore` silencing nothing reads with what it judged: the version, or the archived subject."""
        cases = {
            "yanked": (Scope.YANKED, "ignore[yanked]", "4.15.0", "version 4.15.0 was not yanked"),
            "vulnerable": (
                Scope.VULNERABLE,
                "ignore[vulnerable=CVE-2022-28346]",
                "4.15.0",
                "version 4.15.0 has no such vulnerability",
            ),
            "archived project": (
                Scope.ARCHIVED,
                "ignore[archived]",
                ArchivedSubject.PROJECT,
                "the project is not archived",
            ),
            "archived repository": (
                Scope.ARCHIVED,
                "ignore[archived]",
                ArchivedSubject.REPOSITORY,
                "the repository is not archived",
            ),
        }
        for case, (scope, directive, judged, finding) in cases.items():
            with self.subTest(case):
                location = create_location("requirements.txt", 9)
                _new_logger().redundant_ignore(reference("humanize", location), scope, directive, judged)
                self.assert_message(
                    mock_log,
                    message_of(Logger.redundant_ignore),
                    logging.WARNING,
                    f"Redundant update-time directive {directive} for {dependency('humanize')} in "
                    f"{at('requirements.txt:9')}: {finding}",
                )

    @kills.replace(
        log_module.Logger,
        '_ignoring("the %(advisory)s vulnerability warning", "--ignore-vulnerability %(identifiers)s")',
        '_ignoring("the %(identifiers)s vulnerability warning", "--ignore-vulnerability %(advisory)s")',
        "the run-wide line names what was passed in its subject and the advisory in its cause, swapping the two",
    )
    def test_globally_ignored_vulnerability(self, mock_log: Mock):
        """Test that the run-wide option's line reads with the advisory, and quotes the identifiers it was passed."""
        pin = reference("django", create_location("requirements.txt", 9), "3.2.0")
        reported = vulnerability("GHSA-2gwj-7jmv-h26r", "SQL Injection in Django", "critical")
        passed = frozenset({"CVE-2022-28346", "CVE-2021-31542"})
        _new_logger().globally_ignored_vulnerability(pin, reported, passed)
        self.assert_message(
            mock_log,
            message_of(Logger.globally_ignored_vulnerability),
            logging.DEBUG,
            f"Ignoring the GHSA-2gwj-7jmv-h26r vulnerability warning for {dependency('django')} "
            f"in {at('requirements.txt:9')} (--ignore-vulnerability CVE-2021-31542,CVE-2022-28346)",
        )

    def test_redundant_directive(self, mock_log: Mock):
        """Test that a directive holding nothing back is reported with the directive and the reason, as given."""
        cases = {
            "set[cooldown=30]": Reason.NO_COOLDOWN_DATES,
            "ignore[yanked]": Reason.NO_YANK_CONCEPT,
            "set[stale-after=90]": Reason.NO_STALENESS_DATES,
        }
        for directive, reason in cases.items():
            with self.subTest(directive=directive):
                location = create_location("Dockerfile", 2)
                _new_logger().redundant_directive(reference("python", location), directive, reason)
                self.assert_message(
                    mock_log,
                    message_of(Logger.redundant_directive),
                    logging.WARNING,
                    f"Redundant update-time directive {directive} for {dependency('python')} in "
                    f"{at('Dockerfile:2')}: {reason}",
                )

    def test_path_logged_at_debug(self, mock_log: Mock):
        """Test that the per-file 'checking for updates' progress is logged at debug level."""
        config_yml = Path.cwd() / "config.yml"
        _new_logger().path(config_yml)
        self.assert_message(
            mock_log,
            message_of(Logger.path),
            logging.DEBUG,
            f"Checking if there are updates for {at('config.yml')}",
        )

    def test_configured_uv_cooldown(self, mock_log: Mock):
        """Test that writing the cooldown into a project's uv config is logged, relative to the working directory."""
        path = Path.cwd() / "pyproject.toml"
        _new_logger().configured_uv_cooldown(path, "7 days")
        self.assert_message(
            mock_log,
            message_of(Logger.configured_uv_cooldown),
            logging.INFO,
            f"Set uv exclude-newer to '7 days' in {at('pyproject.toml')} to apply the cooldown",
        )

    def test_invalid_file(self, mock_log: Mock):
        """Test that a file that does not parse is warned about, naming the format given rather than its suffix."""
        for file_name, file_format in (("pom.xml", "XML"), ("compose.yml", "YAML")):
            with self.subTest(format=file_format):
                _new_logger().invalid_file(Path.cwd() / file_name, file_format)
                self.assert_message(
                    mock_log,
                    message_of(Logger.invalid_file),
                    logging.WARNING,
                    f"Skipping {at(file_name)}: it is not valid {file_format}",
                )

    def test_excluded_path_logged_at_debug(self, mock_log: Mock):
        """Test that a directory held back by --exclude-path is logged at debug level, with its path undelimited."""
        _new_logger().excluded_path(Path("vendor"))
        self.assert_message(
            mock_log, message_of(Logger.excluded_path), logging.DEBUG, "Excluding vendor from the scan (--exclude-path)"
        )

    def test_missing_excluded_path_logged_at_warning(self, mock_log: Mock):
        """Test that a non-existing --exclude-path directory is logged as a warning, not an error."""
        _new_logger().missing_excluded_path(Path("vendor"))
        self.assert_message(
            mock_log,
            message_of(Logger.missing_excluded_path),
            logging.WARNING,
            "Path vendor passed to --exclude-path does not exist",
        )

    def test_request_failed(self, mock_log: Mock):
        """Test that a failed request is warned about with its URL and why it failed, the HTTP error if it got one."""
        url = "https://pypi.org/pypi/humanize/json"
        response = Response()
        response.url, response.status_code, response.reason = url, 404, "Not Found"
        cases: dict[str, tuple[Response | Exception, str]] = {
            "an HTTP error": (response, "HTTP 404 Not Found"),
            "a timeout": (requests.exceptions.ReadTimeout("read timed out"), "the request timed out"),
            "a failed connection": (requests.exceptions.ConnectionError("connection refused"), "connection refused"),
        }
        for case, (failure, worded) in cases.items():
            with self.subTest(case):
                _new_logger().request_failed(url, failure)
                self.assert_message(
                    mock_log, message_of(Logger.request_failed), logging.WARNING, f"Could not fetch {url}: {worded}"
                )

    def test_running_logged_at_debug(self, mock_log: Mock):
        """Test that the version of Update-time, and the OS and Python version it runs on, are logged at debug level."""
        _new_logger().running("0.0.48", "macOS-26.7.1-arm64-arm-64bit-Mach-O", "3.14.7")
        self.assert_message(
            mock_log,
            message_of(Logger.running),
            logging.DEBUG,
            "Running Update-time v0.0.48 on macOS-26.7.1-arm64-arm-64bit-Mach-O with Python 3.14.7",
        )

    @kills.replace(
        log_module.Logger.arguments,
        "shlex",
        '" "',
        "an argument holding a space is logged as two arguments",
        case="argument with a space",
    )
    @kills.delete(
        log_module.Logger.arguments,
        ' or "none"',
        "a run without arguments logs nothing after the colon",
        case="no arguments",
    )
    def test_arguments_logged_at_debug(self, mock_log: Mock):
        """Test that the command-line arguments are logged at debug level, quoted as the shell reads them."""
        cases: tuple[tuple[str, list[str], str], ...] = (
            ("argument with a space", ["my project"], "'my project'"),
            ("no arguments", [], "none"),
        )
        for case, arguments, rendered in cases:
            with self.subTest(case):
                _new_logger().arguments(arguments)
                message = message_of(Logger.arguments)
                self.assert_message(mock_log, message, logging.DEBUG, f"Command-line arguments: {rendered}")

    def test_tool_version_logged_at_debug(self, mock_log: Mock):
        """Test that the version a tool reports in a directory is logged at debug level, every line of it."""
        maven = "Apache Maven 3.9.16 (2bdd9fd)\nMaven home: /opt/maven\nJava version: 27"
        for case, executable, directory, rendered, output in (
            ("a subdirectory", "npm", Path.cwd() / "frontend", "frontend", "11.19.1"),
            ("the scan root", "npm", Path.cwd(), ".", "11.19.1"),
            ("several lines", "./mvnw", Path.cwd(), ".", maven),
        ):
            with self.subTest(case):
                _new_logger().tool_version(executable, directory, output)
                expected = f"Version of {executable} in {at(rendered)}: {output}"
                self.assert_message(mock_log, message_of(Logger.tool_version), logging.DEBUG, expected)

    def test_credentials_logged_at_debug(self, mock_log: Mock):
        """Test that which credential variables are set is logged at debug level, naming each variable."""
        names = ("GITHUB_TOKEN", "DOCKER_HUB_USERNAME", "DOCKER_HUB_TOKEN")
        for case, set_variables, rendered in (
            ("all set", names, "GITHUB_TOKEN set, DOCKER_HUB_USERNAME set, DOCKER_HUB_TOKEN set"),
            ("none set", (), "GITHUB_TOKEN not set, DOCKER_HUB_USERNAME not set, DOCKER_HUB_TOKEN not set"),
            (
                "one of two set",
                ("DOCKER_HUB_USERNAME",),
                "GITHUB_TOKEN not set, DOCKER_HUB_USERNAME set, DOCKER_HUB_TOKEN not set",
            ),
        ):
            with self.subTest(case):
                _new_logger().credentials({name: name in set_variables for name in names})
                self.assert_message(mock_log, message_of(Logger.credentials), logging.DEBUG, f"Credentials: {rendered}")

    def test_network_settings_logged_at_debug(self, mock_log: Mock):
        """Test that the proxies, the hosts they skip, and the CA bundle are logged at debug level."""
        proxy = "proxy.acme.com:3128"
        for case, proxies, no_proxy, ca_bundle, rendered in (
            (
                "configured",
                {"http": proxy, "https": proxy},
                "localhost,.acme.internal",
                "/etc/ssl/acme.pem",
                (
                    f"proxies http={proxy}, https={proxy}, except for localhost,.acme.internal; "
                    "CA bundle /etc/ssl/acme.pem"
                ),
            ),
            ("proxies for every host", {"https": proxy}, "", "", f"proxies https={proxy}; the default CA bundle"),
            ("defaults", {}, "", "", "direct connections; the default CA bundle"),
        ):
            with self.subTest(case):
                _new_logger().network_settings(proxies, no_proxy, ca_bundle)
                self.assert_message(
                    mock_log, message_of(Logger.network_settings), logging.DEBUG, f"Network settings: {rendered}"
                )

    def test_used_up_github_rate_limit_logged_at_error(self, mock_log: Mock):
        """Test that a used-up GitHub rate limit is reported with its limit and reset, advising a missing token."""
        reset = datetime(2026, 10, 17, 15, 42, tzinfo=UTC)
        logger = _new_logger()
        for case, log, message, rendered in (
            (
                "anonymous",
                partial(logger.github_rate_limit_used_up, 60),
                message_of(Logger.github_rate_limit_used_up),
                (
                    f"GitHub's API rate limit of 60 requests is used up until {utc_date_and_time(reset)}; "
                    "set GITHUB_TOKEN to raise it"
                ),
            ),
            (
                "with a token",
                partial(logger.github_token_rate_limit_used_up, 5000),
                message_of(Logger.github_token_rate_limit_used_up),
                (
                    "GitHub's API rate limit of 5000 requests for GITHUB_TOKEN is used up until "
                    f"{utc_date_and_time(reset)}"
                ),
            ),
        ):
            with self.subTest(case):
                log(reset)
                self.assert_message(mock_log, message, logging.ERROR, rendered)

    def test_unreadable_github_rate_limit_logged_at_debug(self, mock_log: Mock):
        """Test that rate-limit headers that cannot be read are logged at debug level, with the reason."""
        _new_logger().unreadable_github_rate_limit(ValueError("invalid literal for int() with base 10: 'soon'"))
        self.assert_message(
            mock_log,
            message_of(Logger.unreadable_github_rate_limit),
            logging.DEBUG,
            "Could not read GitHub's rate-limit headers: invalid literal for int() with base 10: 'soon'",
        )

    def test_update_a_signal_stopped_logged_at_error(self, mock_log: Mock):
        """Test that an update a signal stopped is logged as an error, naming the dependency type and the signal."""
        _new_logger().update_stopped("Maven dependencies", signal.SIGKILL)
        self.assert_message(
            mock_log,
            message_of(Logger.update_stopped),
            logging.ERROR,
            "Could not finish updating Maven dependencies: SIGKILL stopped it",
        )

    def test_stopped_run_logged_at_error(self, mock_log: Mock):
        """Test that a run a signal stopped is logged as an error, naming the signal."""
        for signal_number, name in (
            (signal.SIGINT, "SIGINT (Ctrl-C)"),
            (signal.SIGTERM, "SIGTERM"),
            (signal.SIGHUP, "SIGHUP"),
            (signal.SIGQUIT, "SIGQUIT (Ctrl-\\)"),
        ):
            with self.subTest(name):
                _new_logger().stopped(signal_number)
                self.assert_message(
                    mock_log,
                    message_of(Logger.stopped),
                    logging.ERROR,
                    f"{name} stopped Update-time before it finished; review the files it changed, since a file it "
                    "was writing may be incomplete",
                )

    def test_forced_outside_git_repository_logged_at_warning(self, mock_log: Mock):
        """Test that running outside a git repository because of --force is logged as a warning, with the scan root."""
        _new_logger().forced_outside_git_repository(Path("/home/user/project"))
        self.assert_message(
            mock_log,
            message_of(Logger.forced_outside_git_repository),
            logging.WARNING,
            "Running outside a git repository (/home/user/project) because --force was given; changes are made in "
            "place and cannot be reverted",
        )

    def test_new_version_with_publication_date(self, mock_log: Mock):
        """Test that the publication date is appended to the version when it is known."""
        published = datetime(2026, 5, 29, 13, 54, tzinfo=UTC)
        version = DependencyVersion(
            "1.0", Changes("Changelog", markdown=False, url=""), publication=LookedUp(published)
        )
        location = create_location("a.txt", 3)
        _new_logger().new_version(reference("dependency", location), version)
        self.assert_message(
            mock_log,
            message_of(Logger.new_version),
            logging.INFO,
            f"New version available for {dependency('dependency')} in {at('a.txt:3')}: "
            f"{published_version('1.0', published)}",
        )
        self.assert_changes(mock_log, "Changelog")

    def test_new_version_without_a_changelog_notes_its_absence(self, mock_log: Mock):
        """Test that a new version without a changelog is logged with a note saying so."""
        published = datetime(2026, 5, 29, 13, 54, tzinfo=UTC)
        location = create_location("a.txt", 3)
        _new_logger().new_version(
            reference("dependency", location), DependencyVersion("1.0", publication=LookedUp(published))
        )
        self.assert_message(
            mock_log,
            message_of(Logger.new_version),
            logging.INFO,
            f"New version available for {dependency('dependency')} in {at('a.txt:3')}: "
            f"{published_version('1.0', published)}",
        )
        self.assert_note(mock_log, "No changelog available!")
