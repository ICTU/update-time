"""Logger unit tests."""

import inspect
import logging
import re
import signal
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final
from unittest import TestCase
from unittest.mock import ANY, Mock, patch

import requests
from requests import Response
from rich.logging import RichHandler

from mutation_time.mutation import kills
from update_time.domain.bound import BoundRedundancy, Verb
from update_time.domain.dependency import (
    NO_CHANGES,
    Archival,
    ArchivedSubject,
    Changes,
    DependencyVersion,
    FloatingPin,
    ImageNotLookedUp,
    Release,
    Unserved,
    Yank,
)
from update_time.domain.directive import Reason
from update_time.domain.marker import Marker, Scope
from update_time.domain.reference import DriftedPin, DriftKind, Reference, RefKind
from update_time.io import log
from update_time.io.console import CHANGES, NOTE
from update_time.io.log import (
    _DRIFT_MOVES,
    FileContent,
    Logger,
    NotRunCause,
    ReactorCause,
    ToolFailure,
    ToolTask,
    _binary_lock_reason,
    _locked_version,
    _LogMessage,
    _node_managers_reason,
    _not_run_reason,
    _old_yarn_reason,
    _python_managers_reason,
    _reactor_clauses,
    _risk_level_reason,
    _unsupported_manager_reason,
    get_logger,
)
from update_time.markers.parser import parse_directives
from update_time.primitives.location import Location
from update_time.primitives.lookup import LookedUp
from update_time.primitives.timestamp import utc_date_and_time

from tests.update_time.base_test_cases import LoggingTestCase
from tests.update_time.domain_objects import bound, reference, resolved_reference, vulnerability
from tests.update_time.fixtures import COMMIT_SHA1, COMMIT_SHA2, DIGEST, DIGEST1, DIGEST2, STALE_AFTER_90

from .log_fields import at, create_location, dependency, published_version
from .log_messages import adopted_drift_report, logged_extra, message_of

if TYPE_CHECKING:
    from collections.abc import Callable


class GetLoggerTests(TestCase):
    """Unit tests for how get_logger configures the root logger."""

    def test_diagnostics_are_sent_to_stderr(self):
        """Test that the root logger sends all diagnostics to stderr, keeping stdout clean for --version/--help."""
        get_logger("stderr")  # Ensure the root logger has been configured.
        rich_handlers = [handler for handler in logging.getLogger().handlers if isinstance(handler, RichHandler)]
        self.assertTrue(rich_handlers)
        self.assertTrue(all(handler.console.stderr for handler in rich_handlers))


_TEST_EVERY_MESSAGE_NAMES_ITS_HOLES_PATTERN: Final[re.Pattern[str]] = re.compile(r"%(?!\()")


class LogMessageTests(TestCase):
    """Unit tests for the log message type and the messages declared with it."""

    MESSAGE = _LogMessage(level=logging.WARNING, format="Stale dependency %(dependency)s", name="stale-dependency")

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
            value
            for name, value in inspect.getmembers_static(Logger)
            if isinstance(value, str) and not name.startswith("__")
        ]
        messages = [str(message) for message in Logger.messages()] + fragments
        # These enums carry message fragments of their own, so the fragments are read off the enums rather than off
        # the logger.
        enums = (
            Reason,
            BoundRedundancy,
            FileContent,
            ToolTask,
            ToolFailure,
            ReactorCause,
            NotRunCause,
            ImageNotLookedUp,
            Unserved,
        )
        messages += [str(member) for enum in enums for member in enum]
        messages += [word for move in _DRIFT_MOVES.values() for word in (move.mover, move.moved, move.advice)]
        # Guard against the introspection silently covering the fragments alone, as it did when the messages stopped
        # being plain strings:
        self.assertIn(str(message_of(Logger.reference_left_as_is)), messages)
        self.assertIn(str(Reason.NO_YANK_CONCEPT), messages)
        self.assertIn(_DRIFT_MOVES[DriftKind.TAG].advice, messages)
        for message in messages:
            with self.subTest(message=message):
                self.assertNotIn(".", message)

    def test_every_message_names_its_holes(self):
        """Test that every message interpolates by name, so its log method hands the logger named fields."""
        messages = Logger.messages()
        self.assertIn("reference-left-as-is", [message.name for message in messages])  # The methods' messages are read
        for message in messages:
            with self.subTest(message=message.name):
                self.assertEqual(_TEST_EVERY_MESSAGE_NAMES_ITS_HOLES_PATTERN.findall(message.format), [])

    def test_no_two_messages_share_a_name(self):
        """Test that no two log messages share a name."""
        names = Counter(message.name for message in Logger.messages())
        self.assertIn("drift-date-unknown", names)  # The decorated methods' messages are read
        self.assertEqual([name for name, count in names.items() if count > 1], [])


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
        message = _LogMessage(level=logging.INFO, format="Skipping %(location)s: %(reason)s", name="skipped")
        _new_logger()._emit(message, {"location": create_location("Dockerfile", 1), "reason": "it is compiled"}, {})
        mock_log.assert_called_once_with(
            message.level, message, {"location": at("Dockerfile:1"), "reason": "it is compiled"}, extra=ANY
        )

    @patch("logging.Logger.log")
    def test_the_dependency_field_is_wrapped_in_its_delimiter(self, mock_log: Mock):
        """Test that the field named `dependency` is wrapped, so no log method has to render one itself."""
        message = _LogMessage(
            level=logging.WARNING, format="Could not find where %(dependency)s is declared", name="not-found"
        )
        _new_logger()._emit(message, {"dependency": "actions/checkout"}, {})
        mock_log.assert_called_once_with(
            message.level, message, {"dependency": dependency("actions/checkout")}, extra=ANY
        )


def _new_logger() -> Logger:
    """Return a logger of its own, so what one test suppressed is not still suppressed for the next."""
    return Logger("test")


class LoggerTests(LoggingTestCase):
    """Unit tests for the logger class."""

    def test_a_changelog_is_shown_again_once_the_shown_changelogs_are_forgotten(self):
        """Test that forgetting the changelogs shown makes a logger show a changelog it has already shown."""
        message = message_of(Logger.new_version)
        pin = reference("dependency", create_location("pyproject.toml", 5))
        version = DependencyVersion("1.0", Changes("Changelog", markdown=False, url=""))
        available = f"New version available for {dependency('dependency')} in {at('pyproject.toml:5')}: 1.0"
        logger = get_logger("reset suppression")
        logger.new_version(pin, version)
        logger.new_version(pin, version)
        self.assert_last_message(message, logging.INFO, available)
        self.assert_note("Suppressing changelog already shown, see above")
        logger.forget_shown_changelogs()
        logger.new_version(pin, version)
        self.assert_last_message(message, logging.INFO, available)
        self.assert_changes("Changelog")

    def assert_last_message(self, message: _LogMessage, level: int, rendered: str) -> None:
        """Assert the most recent record is the message at the level with exactly its fields, an error as expected."""
        logged_level, template, fields = self.mock_log.call_args.args
        self.assertEqual((logged_level, template), (level, message))
        self.assertEqual(set(fields), template.holes)
        self.assertEqual(str(template) % fields, rendered)
        self._error_expected |= level == logging.ERROR

    def assert_note(self, note: str) -> None:
        """Assert the most recent record carries Update-time's own note about the changes beside its fields."""
        self.assertEqual(logged_extra(self.mock_log.call_args.kwargs), {NOTE: note})

    def assert_changes(self, changes: str) -> None:
        """Assert the most recent record carries a changelog's changes beside its fields."""
        self.assertEqual(logged_extra(self.mock_log.call_args.kwargs), {CHANGES: changes})

    def test_new_version_without_publication_date(self):
        """Test that the version is logged without a publication date when it is unknown."""
        location = create_location("a.txt", 3)
        _new_logger().new_version(
            reference("dependency", location), DependencyVersion("1.0", Changes("Changelog", markdown=False, url=""))
        )
        self.assert_message(
            message_of(Logger.new_version),
            logging.INFO,
            f"New version available for {dependency('dependency')} in {at('a.txt:3')}: 1.0",
        )
        self.assert_changes("Changelog")

    def assert_message(self, message: _LogMessage, level: int, rendered: str) -> None:
        """Assert the log method emitted the message once, at the level, reading as the given text."""
        self.mock_log.assert_called_once()
        self.assert_last_message(message, level, rendered)

    def test_pinned(self):
        """Test that pinning a previously unpinned reference to a digest is logged."""
        location = create_location("Dockerfile", 1)
        _new_logger().pinned(reference("dependency", location), DependencyVersion("1.0", sha=DIGEST))
        self.assert_message(
            message_of(Logger.pinned),
            logging.INFO,
            f"Pinned {dependency('dependency')} in {at('Dockerfile:1')} to 1.0@{DIGEST}",
        )

    @kills.delete(Logger.pinned, ' if version.sha else ""', "a pin without a hash ends in a bare @")
    def test_pinned_without_a_hash(self):
        """Test that pinning a reference to a version without a hash is logged without one."""
        _new_logger().pinned(reference("python", create_location("Dockerfile", 2)), DependencyVersion("3.14.7"))
        self.assert_message(
            message_of(Logger.pinned), logging.INFO, f"Pinned {dependency('python')} in {at('Dockerfile:2')} to 3.14.7"
        )

    def test_unpinned_floating_tag(self):
        """Test that a floating tag pinned to no version is reported with the reason it was left as it is."""
        location = create_location("docker-compose.yml", 7)
        cases = {"a floating tag": ("dev", "dev", ":dev"), "a tagless reference": ("", "latest", "")}
        for case, (tag, release, rendered_tag) in cases.items():
            with self.subTest(case):
                floating = reference("acme/api", location, tag)
                _new_logger().unpinned_floating_tag(floating, DependencyVersion(release), FloatingPin.NO_VERSION_TAG)
                self.assert_message(
                    message_of(Logger.reference_left_as_is),
                    logging.DEBUG,
                    f"Reference {dependency('acme/api')}{rendered_tag} in {at('docker-compose.yml:7')} was left as "
                    f"it is: {FloatingPin.NO_VERSION_TAG}",
                )

    @kills.delete(
        Logger.held_back_by_cooldown,
        'f" (update-time: {directive})" if directive else ',
        "a line about a marker's cooldown leaves out the directive that set it",
        case="marker's cooldown",
    )
    @kills.delete(
        Logger.held_back_by_cooldown,
        ' if directive else ""',
        "a line about the run's cooldown names an empty directive",
        case="run's cooldown",
    )
    def test_held_back_by_cooldown(self):
        """Test that a version held back by the cooldown is reported, naming the directive that set the cooldown."""
        fresh = Release("3.15.0", datetime(2026, 10, 8, 13, 1, tzinfo=UTC))
        location = create_location("Dockerfile", 2)
        for case, (days, directive, until, set_by) in {
            "run's cooldown": (7, "", "2026-10-15 13:01 UTC", ""),
            "marker's cooldown": (30, "set[cooldown=30]", "2026-11-07 13:01 UTC", " (update-time: set[cooldown=30])"),
        }.items():
            with self.subTest(case):
                _new_logger().held_back_by_cooldown(reference("python", location), fresh, days, directive)
                self.assert_message(
                    message_of(Logger.held_back_by_cooldown),
                    logging.DEBUG,
                    f"Holding back version 3.15.0 of {dependency('python')} in {at('Dockerfile:2')}: published "
                    f"2026-10-08 13:01 UTC, within the cooldown of {days} days until {until}{set_by}",
                )

    def test_unpinned_ref(self):
        """Test that a branch or tag left unpinned is reported by its kind, with why its commit could not be fetched."""
        location = create_location(".github/workflows/ci.yml", 17)
        for kind, ref in ((RefKind.BRANCH, "branch"), (RefKind.TAG, "tag")):
            with self.subTest(ref):
                _new_logger().unpinned_ref(reference("actions/checkout", location, "main"), "HTTP 404, Not Found", kind)
                self.assert_message(
                    message_of(Logger.unpinned_ref),
                    logging.ERROR,
                    f"Could not fetch the commit of the {ref} {dependency('actions/checkout')}@main in "
                    f"{at('.github/workflows/ci.yml:17')}, so it was left as it is (HTTP 404, Not Found)",
                )

    @kills.delete(
        _reactor_clauses,
        ", ReactorCause.UNPARSABLE_POM",
        "a reactor holding a pom that does not parse counts the poms left rather than its other poms",
        case="unparsable pom",
    )
    @kills.delete(
        _reactor_clauses,
        "ReactorCause.AS_FOUND, ",
        "a reactor unreadable as the scan found it counts the poms left rather than its other poms",
        case="as found",
    )
    @kills.delete(
        _reactor_clauses,
        ' if cause is ReactorCause.UPDATED_POM else ""',
        "every error ends in the advice to revert a pom",
        case="read earlier",
    )
    @kills.replace(
        _reactor_clauses,
        "delimit_location(Location(pom))",
        "str(pom)",
        "the cause styles its pom as plain text",
        case="updated pom",
    )
    @kills.delete(
        _reactor_clauses,
        ' if poms else ""',
        "an update that broke the reactor in its run over the last pom counts zero poms left",
        case="updated last pom",
    )
    def test_unreadable_reactor(self):
        """Test that a reactor Maven cannot read names the cause, the poms Maven does not run over, and any fix."""
        reactor = f"Maven cannot read the reactor in {at('.')}"
        updated, unparsable = at("bom/pom.xml"), at("sub/pom.xml")
        not_run = "so Update-time does not run Maven over"
        fix = "revert that pom and hold the update that broke the reactor back with a marker, or fix what Maven reports"
        cases = {
            "read earlier": (ReactorCause.READ_EARLIER, 50, None, f"{reactor}, {not_run} the 50 poms left in it"),
            "as found": (
                ReactorCause.AS_FOUND,
                118,
                None,
                f"{reactor} as the scan found it, {not_run} its 118 other poms",
            ),
            "updated pom": (
                ReactorCause.UPDATED_POM,
                1,
                Path("bom/pom.xml"),
                f"{reactor} since Update-time updated {updated}, {not_run} the 1 pom left in it; {fix}",
            ),
            "updated last pom": (
                ReactorCause.UPDATED_POM,
                0,
                Path("bom/pom.xml"),
                f"{reactor} since Update-time updated {updated}; {fix}",
            ),
            "unparsable pom": (
                ReactorCause.UNPARSABLE_POM,
                118,
                Path("sub/pom.xml"),
                f"{reactor} since {unparsable} does not parse, {not_run} its 118 other poms",
            ),
        }
        for case, (cause, poms, pom, rendered) in cases.items():
            with self.subTest(case):
                _new_logger().unreadable_reactor(Path(), cause, poms, pom)
                self.assert_message(message_of(Logger.unreadable_reactor), logging.ERROR, rendered)

    @kills.replace(
        NotRunCause, "to install", "to resolve", "npm is said to resolve dependencies", case="nothing to install"
    )
    @kills.replace(
        NotRunCause, "to resolve", "to install", "uv is said to install dependencies", case="nothing to resolve"
    )
    @kills.delete(
        NotRunCause,
        ", and %(manager)s would write %(file)s",
        "the reason leaves out which lock the package manager would write",
        case="no lock file",
    )
    @kills.delete(
        NotRunCause,
        ", and %(file)s does not exist",
        "the reason leaves out which uv.lock is missing",
        case="not a uv project",
    )
    @kills.delete(
        NotRunCause, " of %(file)s", "the reason leaves out whose table sets managed = false", case="marked unmanaged"
    )
    @kills.replace(
        _not_run_reason,
        "_delimited(file)",
        "str(file)",
        "a reason styles its file as plain text",
        case="workspace root outside the scan",
    )
    @kills.delete(
        NotRunCause,
        " %(file)s says why",
        "the reason leaves out which workspace root the warning is about",
        case="left with the workspace root",
    )
    @kills.delete(
        NotRunCause,
        " this pom is part of",
        "the reason leaves out which reactor Maven cannot read",
        case="reactor unreadable",
    )
    def test_manager_not_run(self):
        """Test that a package manager not run for a file names the file, the manager, and the cause."""
        package_json, pyproject_toml = Path("app/package.json"), Path("app/pyproject.toml")
        cases = {
            "nothing to install": (
                package_json,
                "npm",
                NotRunCause.NO_DEPENDENCIES_TO_INSTALL,
                None,
                "it does not declare dependencies for npm to install",
            ),
            "nothing to resolve": (
                pyproject_toml,
                "uv",
                NotRunCause.NO_DEPENDENCIES_TO_RESOLVE,
                None,
                "it does not declare dependencies for uv to resolve",
            ),
            "no lock file": (
                package_json,
                "npm",
                NotRunCause.NO_LOCK_FILE,
                Path("app/package-lock.json"),
                f"it does not have a lock file, and npm would write {at('app/package-lock.json')}",
            ),
            "not a uv project": (
                pyproject_toml,
                "uv",
                NotRunCause.NOT_A_UV_PROJECT,
                Path("app/uv.lock"),
                f"it does not have a [tool.uv] table, and {at('app/uv.lock')} does not exist",
            ),
            "marked unmanaged": (
                pyproject_toml,
                "uv",
                NotRunCause.MARKED_UNMANAGED,
                Path("pyproject.toml"),
                f"the [tool.uv] table of {at('pyproject.toml')} sets managed = false",
            ),
            "workspace root outside the scan": (
                pyproject_toml,
                "uv",
                NotRunCause.WORKSPACE_ROOT_OUTSIDE_THE_SCAN,
                Path("pyproject.toml"),
                f"its uv workspace root {at('pyproject.toml')} lies outside the scan root",
            ),
            "left with the workspace root": (
                package_json,
                "yarn",
                NotRunCause.LEFT_WITH_THE_WORKSPACE_ROOT,
                Path("package.json"),
                f"the warning about its workspace root {at('package.json')} says why",
            ),
            "reactor unreadable": (
                Path("sub/pom.xml"),
                "Maven",
                NotRunCause.REACTOR_UNREADABLE,
                None,
                "Maven cannot read the reactor this pom is part of",
            ),
        }
        message = message_of(Logger.manager_not_run)
        for case, (path, manager, cause, other, reason) in cases.items():
            with self.subTest(case):
                _new_logger().manager_not_run(path, manager, cause, other)
                self.assert_message(message, logging.DEBUG, f"Not running {manager} for {at(str(path))}: {reason}")

    @kills.replace(ToolFailure, '"failed"', '"stopped"', "a tool that failed is said to have stopped", case="failed")
    @kills.replace(
        ToolFailure,
        '"is not on the path"',
        '"failed"',
        "a tool missing from the path is said to have failed, so the error does not say to install it",
        case="not on the path",
    )
    def test_tool_failed(self):
        """Test that a tool that did not do its task names the task, the file, the tool, and how it failed."""
        cases = {
            "failed": (ToolFailure.FAILED, "uv pip compile", "uv pip compile failed"),
            "not on the path": (ToolFailure.NOT_ON_PATH, "uv", "uv is not on the path"),
        }
        message = message_of(Logger.tool_failed)
        for case, (failure, tool, ending) in cases.items():
            with self.subTest(case):
                _new_logger().tool_failed(Path("requirements.txt"), ToolTask.RECOMPILE, tool, failure)
                self.assert_message(message, logging.ERROR, f"Could not recompile {at('requirements.txt')}: {ending}")

    def test_unchecked_branch_drift(self):
        """Test that a branch drift that could not be checked names the check, why it failed, and the pin's fate."""
        branch = reference("actions/checkout", create_location(".github/workflows/ci.yml", 17), "main")
        _new_logger().unchecked_branch_drift(branch, COMMIT_SHA2, "HTTP 403, API rate limit exceeded")
        self.assert_message(
            message_of(Logger.unchecked_branch_drift),
            logging.ERROR,
            f"Could not check branch drift for {dependency('actions/checkout')}@main in "
            f"{at('.github/workflows/ci.yml:17')}: GitHub did not compare the pinned commit with {COMMIT_SHA2}, the "
            "branch's newest commit older than the cooldown (HTTP 403, API rate limit exceeded); the pin was left "
            "unchanged",
        )

    @kills.replace(
        Logger.comment_naming_no_ref,
        'replace(reference, current_version="")',
        "reference",
        "the line names the comment beside the commit SHA as the reference's tag",
    )
    def test_comment_naming_no_ref(self):
        """Test that a commit SHA whose comment does not name a ref is reported with the comment and its location."""
        location = create_location(".github/workflows/ci.yml", 17)
        _new_logger().comment_naming_no_ref(reference("actions/checkout", location, "pinned"))
        self.assert_message(
            message_of(Logger.reference_left_as_is),
            logging.DEBUG,
            f"Reference {dependency('actions/checkout')} in {at('.github/workflows/ci.yml:17')} was left as it "
            "is: its comment pinned does not name a branch or tag of the repository",
        )

    def test_unpinned_floating_tag_reason_names_the_tag_looked_up(self):
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
                    message_of(Logger.reference_left_as_is),
                    logging.DEBUG,
                    f"Reference {dependency('default')} in {at('.circleci/config.yml:1')} was left as it is: "
                    f"{explanation}",
                )

    @kills.replace(
        Logger,
        '"Reference %(dependency)s%(tag)s in %(location)s was left as it is: %(reason)s"',
        '"Reference %(dependency)s%(tag)s in %(location)s was left as it is"',
        "the line reports that a reference was left as it is without naming why",
    )
    def test_reference_left_as_is(self):
        """Test that a reference left as it is is reported with the tag it names and the reason."""
        location = create_location("docker-compose.yml", 4)
        _new_logger().reference_left_as_is(reference("acme/api", location, "1.2.3"), ImageNotLookedUp.BUILT_IMAGE)
        self.assert_message(
            message_of(Logger.reference_left_as_is),
            logging.DEBUG,
            f"Reference {dependency('acme/api')}:1.2.3 in {at('docker-compose.yml:4')} was left as it is: "
            "the Compose file builds this image",
        )

    @kills.replace(
        Logger.keeping_floating_pin,
        '("floating tag", ":")',
        '("floating tag", "@")',
        "a floating tag is spelled as a ref",
        case="a floating tag",
    )
    @kills.replace(
        Logger.keeping_floating_pin,
        "tag_of(reference.current_version, separator)",
        "separator + reference.current_version",
        "a tagless reference ends in a bare separator",
        case="a tagless reference",
    )
    @kills.replace(
        Logger.keeping_floating_pin, '(ref, "@")', '(ref, ":")', "a ref is spelled as an image tag", case="a branch"
    )
    def test_keeping_a_floating_pin(self):
        """Test that keeping a floating pin is reported by its kind, with the name it has and what it resolves to."""
        location = create_location("ci.yml", 17)
        release = DependencyVersion("4.3.0", sha=COMMIT_SHA1)
        directive = "allow[floating-pin]"
        for case, (name, version, ref, pin, rendered_version) in {
            "a floating tag": ("python", "latest", None, "floating tag", ":latest"),
            "a tagless reference": ("python", "", None, "floating tag", ""),
            "a branch": ("actions/checkout", "main", RefKind.BRANCH, "branch", "@main"),
        }.items():
            with self.subTest(case):
                _new_logger().keeping_floating_pin(reference(name, location, version), release, directive, ref)
                self.assert_message(
                    message_of(Logger.keeping_floating_pin),
                    logging.DEBUG,
                    f"Keeping the {pin} {dependency(name)}{rendered_version} in {at('ci.yml:17')}: it resolves to "
                    f"4.3.0@{COMMIT_SHA1} (update-time: {directive})",
                )

    def test_drift(self):
        """Test that a hash pin whose target changed under it is warned about, naming the kind of drift."""
        checkout, ci = dependency("actions/checkout"), at(".github/workflows/ci.yml:17")
        cases = {
            "digest": (
                DriftKind.DIGEST,
                "3.14",
                (
                    f"Digest drift for {dependency('dependency')}:3.14 in {at('Dockerfile:2')}: pinned to "
                    f"{DIGEST1} but the registry now serves {DIGEST2}; the pin was left unchanged, verify the change "
                    "is expected before updating the pin"
                ),
            ),
            "tag": (
                DriftKind.TAG,
                "4.1.1",
                (
                    f"Tag drift for {checkout}@4.1.1 in {ci}: pinned to commit {COMMIT_SHA1} but the tag now "
                    f"points at {COMMIT_SHA2}; the pin was left unchanged, verify the tag was moved deliberately "
                    "before updating the pin"
                ),
            ),
            "branch": (
                DriftKind.BRANCH,
                "main",
                (
                    f"Branch drift for {checkout}@main in {ci}: pinned to commit {COMMIT_SHA1} but the branch has "
                    f"moved on to {COMMIT_SHA2}; the pin was left unchanged, verify the branch moved to a commit you "
                    "trust before updating the pin"
                ),
            ),
            "ref": (
                DriftKind.REF,
                "stable-2024",
                (
                    f"Ref drift for {checkout}@stable-2024 in {ci}: pinned to commit {COMMIT_SHA1} but the ref now "
                    f"points at {COMMIT_SHA2}; the pin was left unchanged, verify the ref moved to a commit you trust "
                    "before updating the pin"
                ),
            ),
            "digest of a tagless reference": (
                DriftKind.DIGEST,
                "",
                (
                    f"Digest drift for {dependency('dependency')} in {at('Dockerfile:2')}: pinned to {DIGEST1} but the "
                    f"registry now serves {DIGEST2}; the pin was left unchanged, verify the change is expected before "
                    "updating the pin"
                ),
            ),
        }
        for name, (kind, version, rendered) in cases.items():
            with self.subTest(name):
                _new_logger().drift(_drifted_pin(kind, version))
                self.assert_message(message_of(Logger.drift), logging.WARNING, rendered)

    def test_undated_candidate(self):
        """Test that a candidate the source does not date names the source, and the version the reference stays on."""
        python, checkout, humanize = dependency("python"), dependency("actions/checkout"), dependency("humanize")
        cases = {
            "image tag": (
                lambda log: log.undated_push("python", "3.15.0", "3.14.6", "HTTP 429"),
                (
                    f"Could not determine from Docker Hub when {python}:3.15.0 was pushed (HTTP 429), so the cooldown "
                    f"can't be verified; {python} stays on 3.14.6 rather than trying the tags in between"
                ),
            ),
            "tagged commit": (
                lambda log: log.undated_commit("actions/checkout", "v4.4.0", "4.3.0", "HTTP 403"),
                (
                    f"Could not determine from GitHub when {checkout} v4.4.0 was committed (HTTP 403), so the cooldown "
                    f"can't be verified; {checkout} stays on 4.3.0 rather than trying the versions in between"
                ),
            ),
            "release metadata": (
                lambda log: log.no_release_metadata("humanize", "4.16.0", "4.15.0", "HTTP 503"),
                (
                    f"Could not fetch from PyPI the metadata of {humanize} 4.16.0 (HTTP 503), so its yank state and "
                    f"publication date are unknown; {humanize} stays on 4.15.0 rather than trying the versions in "
                    "between"
                ),
            ),
        }
        for name, (report, rendered) in cases.items():
            with self.subTest(name):
                report(_new_logger())
                self.assert_message(message_of(Logger.unknown_version_date), logging.ERROR, rendered)

    def test_undated_commit(self):
        """Test that the undated commit a moved branch points at is reported with the reference and its location."""
        location = create_location(".github/workflows/ci.yml", 17)
        drifted = DriftedPin(
            "actions/checkout", "main", location, COMMIT_SHA1, kind=DriftKind.BRANCH, new_sha=COMMIT_SHA2
        )
        _new_logger().undated_drift(drifted, "HTTP 403, API rate limit exceeded")
        self.assert_message(
            message_of(Logger.undated_drift),
            logging.ERROR,
            f"Could not determine from GitHub the date of commit {COMMIT_SHA2} that "
            f"{dependency('actions/checkout')}@main in {at('.github/workflows/ci.yml:17')} now points at (HTTP 403, "
            "API rate limit exceeded), so the cooldown can't be verified; not adopting the drift",
        )

    def test_undated_push(self):
        """Test that a re-pushed tag that could not be dated is reported with the reference and its location."""
        drifted = DriftedPin(
            "python", "3.14", create_location("Dockerfile", 1), DIGEST1, kind=DriftKind.DIGEST, new_sha=DIGEST2
        )
        _new_logger().undated_drift(drifted, "HTTP 429")
        self.assert_message(
            message_of(Logger.undated_drift),
            logging.ERROR,
            f"Could not determine from Docker Hub when {dependency('python')}:3.14 in {at('Dockerfile:1')} was pushed "
            "(HTTP 429), so the cooldown can't be verified; not adopting the drift",
        )

    @kills.replace(
        _locked_version,
        "if version:",
        "if True:",
        "a range rewritten without a locked version is reported as unchanged at an empty version",
        case="no locked version",
    )
    @kills.replace(
        _locked_version,
        "if version:",
        "if False:",
        "a range rewritten with a locked version is reported as having none",
        case="a locked version",
    )
    def test_range_rewrite(self):
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
                self.assert_message(message_of(Logger.range_rewrite), logging.INFO, f"{rewrite}; {locked}")

    def test_adopted_drift(self):
        """Test that adopted drift is logged at info level, naming the kind of drift and the opt-in that caused it."""
        action, ci = dependency("actions/checkout"), at(".github/workflows/ci.yml:17")
        image, dockerfile = dependency("dependency"), at("Dockerfile:2")
        directive = "allow[hash-drift]"
        sha = f"re-pinned from {COMMIT_SHA1} to {COMMIT_SHA2} (update-time: {directive})"
        digest = f"re-pinned from {DIGEST1} to {DIGEST2} (update-time: {directive})"
        cases = {
            "digest": (DriftKind.DIGEST, "3.14", f"Adopted digest drift for {image}:3.14 in {dockerfile}: {digest}"),
            "tag": (DriftKind.TAG, "4.1.1", f"Adopted tag drift for {action}@4.1.1 in {ci}: {sha}"),
            "branch": (DriftKind.BRANCH, "main", f"Adopted branch drift for {action}@main in {ci}: {sha}"),
            "ref": (DriftKind.REF, "stable-2024", f"Adopted ref drift for {action}@stable-2024 in {ci}: {sha}"),
        }
        for name, (kind, version, rendered) in cases.items():
            with self.subTest(name):
                _new_logger().adopted_drift(drifted := _drifted_pin(kind, version), directive)
                message, _fields = adopted_drift_report(drifted, directive)
                self.assert_message(message, logging.INFO, rendered)

    def test_stale_dependency_warning(self):
        """Test that an old newest release is warned about at warning level, naming the release and the threshold."""
        newest = Release("5.0.0", datetime.now(UTC) - timedelta(days=512, hours=1))
        _new_logger().stale_dependency(reference("humanize", create_location("requirements.txt", 9)), newest, 90)
        self.assert_message(
            message_of(Logger.stale_dependency),
            logging.WARNING,
            f"Stale dependency {dependency('humanize')} in {at('requirements.txt:9')}: "
            "newest release 5.0.0 was published 512 days ago (> 90)",
        )

    @kills.replace(
        log,
        '"Incompatible Python version %(dependency)s in %(location)s: version %(version)s falls outside the "',
        '"%(dependency)s %(version)s in %(location)s falls outside the "',
        "the warning opens with the bare dependency name, unlike every other warning about a dependency",
    )
    def test_python_version_outside_requires_python_warning(self):
        """Test that an entry outside the `requires-python` is warned about with a label, as a dependency is."""
        entry = reference("python", create_location(".python-version", 1), "3.13.2")
        requires_python = reference("python", create_location("pyproject.toml", 2), ">=3.10,<3.12")
        _new_logger().python_version_outside_requires_python(entry, requires_python)
        self.assert_message(
            message_of(Logger.python_version_outside_requires_python),
            logging.WARNING,
            f"Incompatible Python version {dependency('python')} in {at('.python-version:1')}: version 3.13.2 "
            f"falls outside the requires-python >=3.10,<3.12 of {at('pyproject.toml:2')}, "
            "so uv refuses to run for the project",
        )

    def test_redundant_stale_threshold(self):
        """Test that the warning names the threshold as written, the run's threshold, and the newest release's age.

        The run's threshold is named as the default where it is one, and by the option otherwise.
        """
        newest = Release("4.15.0", datetime.now(UTC) - timedelta(days=30, hours=1))
        humanize = reference("humanize", create_location("requirements.txt", 9))
        threshold = STALE_AFTER_90.stale
        for run_threshold, run_words in {365: "the default of 365 days", 400: "--stale-after 400"}.items():
            with self.subTest(run_threshold=run_threshold):
                _new_logger().redundant_stale_threshold(humanize, threshold, newest, run_threshold)
                self.assert_message(
                    message_of(Logger.redundant_directive),
                    logging.WARNING,
                    f"Redundant update-time directive set[stale-after=90] for {dependency('humanize')} in "
                    f"{at('requirements.txt:9')}: newest release 4.15.0 was published 30 days ago, which the "
                    f"threshold of 90 days and {run_words} judge alike",
                )

    @kills.replace(
        _risk_level_reason,
        'report the same vulnerabilities in version {version}"',
        'report the same vulnerabilities"',
        "the warning about a risk level leaves out the version it judged",
    )
    def test_redundant_vulnerable_level(self):
        """Test that the warning names the level as written, the run's level, and the version judged."""
        marker = parse_directives("set[vulnerability-level=high]")
        _new_logger().redundant_vulnerable_level(
            Reference("django", "4.2.0", create_location("requirements.txt", 9), marker=marker), "low"
        )
        self.assert_message(
            message_of(Logger.redundant_directive),
            logging.WARNING,
            f"Redundant update-time directive set[vulnerability-level=high] for {dependency('django')} in "
            f"{at('requirements.txt:9')}: the risk level high and the default level low report the same "
            "vulnerabilities in version 4.2.0",
        )

    def test_yanked_dependency_warning(self):
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
                    message,
                    logging.WARNING,
                    f"Yanked dependency {dependency('humanize')} in {at('requirements.txt:9')}: {clause}",
                )

    def test_archived_dependency_warning(self):
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
                    message,
                    logging.WARNING,
                    f"Archived dependency {dependency('humanize')} in {at('requirements.txt:9')}: {clause}",
                )

    def test_ignored_warning(self):
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
                    message_of(Logger.silenced_warning),
                    logging.DEBUG,
                    f"Ignoring the {name} warning for {dependency('humanize')} in {at('requirements.txt:9')} "
                    f"(update-time: {directive})",
                )

    def test_an_invalid_item_is_warned_about_naming_the_item(self):
        """Test that an item the marker language cannot read is warned about at warning level, naming it alone."""
        _new_logger().invalid_bracket_item(reference("python", create_location("Dockerfile", 2)), "@@@")
        self.assert_message(
            message_of(Logger.invalid_bracket_item),
            logging.WARNING,
            f"Invalid '@@@' in the update-time marker for {dependency('python')} in {at('Dockerfile:2')}",
        )

    def test_redundant_bound(self):
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
                    message_of(Logger.redundant_directive),
                    logging.WARNING,
                    f"Redundant update-time directive {directive} for {dependency('python')} in {at('Dockerfile:6')}: "
                    f"this bound {redundancy} at version 3.12",
                )

    def test_recognised_marker(self):
        """Test that a reference's marker is logged at debug level verbatim, exactly as the user wrote it."""
        # The raw text combines scopes and bracket items, so echoing it verbatim shows the log takes the user's marker.
        raw = "ignore[update] set[stale-after=0] allow[update<3.13, hash-drift]"
        marker = parse_directives(raw)
        _new_logger().recognised_marker(Reference("python", "", create_location("Dockerfile", 6), marker=marker))
        self.assert_message(
            message_of(Logger.recognised_marker),
            logging.DEBUG,
            f"Recognised update-time marker {raw} for {dependency('python')} in {at('Dockerfile:6')}",
        )

    @kills.replace(
        Logger.ignored,
        '"every version", reference.marker.bound_directive)',
        '"every version", reference.marker.version_bound_directive or "ignore[update]")',
        "an `ignore[update]` beside a bound is reported under the bound's directive",
    )
    def test_ignored(self):
        """Test that a held-back reference names the directive that held it back, not the ones written beside it."""
        marker = parse_directives("ignore[update] set[stale-after=0] allow[update<3.13, hash-drift]")
        _new_logger().ignored(Reference("python", "", create_location("Dockerfile", 6), marker=marker))
        self.assert_message(
            message_of(Logger.held_back),
            logging.DEBUG,
            f"Holding back every version of {dependency('python')} in {at('Dockerfile:6')} "
            "(update-time: ignore[update])",
        )

    @kills.replace(
        Logger.holding_back_range,
        'f"the versions {maven_range}"',
        "maven_range",
        "the line names the range without saying it holds versions",
    )
    def test_holding_back_range(self):
        """Test that the versions a bound holds back are named as the range Maven reads, with the directive."""
        guava = reference("com.google.guava:guava", create_location("pom.xml", 12))
        _new_logger().holding_back_range(guava, "[34,)", "ignore[update>=34]")
        self.assert_message(
            message_of(Logger.held_back),
            logging.DEBUG,
            f"Holding back the versions [34,) of {dependency('com.google.guava:guava')} in {at('pom.xml:12')} "
            "(update-time: ignore[update>=34])",
        )

    def test_redundant_ignore(self):
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
                    message_of(Logger.redundant_directive),
                    logging.WARNING,
                    f"Redundant update-time directive {directive} for {dependency('humanize')} in "
                    f"{at('requirements.txt:9')}: {finding}",
                )

    @kills.replace(
        Logger.globally_ignored_vulnerability,
        "{','.join(sorted(passed))}",
        "{vulnerability.advisory}",
        "the run-wide line names the advisory as what the option was passed",
    )
    def test_globally_ignored_vulnerability(self):
        """Test that the run-wide option's line reads with the advisory, and quotes the identifiers it was passed."""
        pin = reference("django", create_location("requirements.txt", 9), "3.2.0")
        reported = vulnerability("GHSA-2gwj-7jmv-h26r", "SQL Injection in Django", "critical")
        _new_logger().globally_ignored_vulnerability(pin, reported, frozenset({"CVE-2022-28346", "CVE-2021-31542"}))
        self.assert_message(
            message_of(Logger.silenced_warning),
            logging.DEBUG,
            f"Ignoring the GHSA-2gwj-7jmv-h26r vulnerability warning for {dependency('django')} "
            f"in {at('requirements.txt:9')} (--ignore-vulnerability CVE-2021-31542,CVE-2022-28346)",
        )

    @kills.replace(
        _binary_lock_reason,
        "_delimited(lock)",
        "lock",
        "the warning styles the binary lock as plain text",
        case="binary bun lock",
    )
    @kills.replace(
        _old_yarn_reason,
        "runs in {_delimited(directory)}",
        "runs in {directory}",
        "the warning styles the directory the old yarn runs in as plain text",
        case="yarn without a cooldown",
    )
    @kills.replace(
        _node_managers_reason,
        "[_delimited(file) for file in files]",
        "[str(file) for file in files]",
        "the warning styles the files naming different managers as plain text",
        case="several node package managers",
    )
    @kills.replace(
        _python_managers_reason,
        "[_delimited(lock_file) for lock_file in lock_files]",
        "[str(lock_file) for lock_file in lock_files]",
        "the warning styles the lock files naming different managers as plain text",
        case="several python package managers",
    )
    @kills.replace(
        _unsupported_manager_reason,
        "supports only {supported}",
        "supports {supported}",
        "the warning leaves out that Update-time supports the named managers alone",
        case="unsupported package manager",
    )
    def test_manager_cannot_run(self):
        """Test that a package manager Update-time cannot run is warned about, naming the manager and the reason."""
        package_json, pyproject_toml, here = Path.cwd() / "package.json", Path.cwd() / "pyproject.toml", Path.cwd()
        node, python = at("package.json"), at("pyproject.toml")
        cases: dict[str, tuple[Callable[[Logger], None], str]] = {
            "binary bun lock": (
                lambda log: log.binary_lock(package_json, here / "bun.lockb", here / "bun.lock"),
                (
                    f"Not running bun for {node}: it locks its dependencies in the binary {at('bun.lockb')}, which "
                    f"Update-time cannot read; run bun install --save-text-lockfile to convert it to {at('bun.lock')}"
                ),
            ),
            "yarn without a cooldown": (
                lambda log: log.yarn_without_a_cooldown(package_json, here / "web", "1.22.22"),
                (
                    f"Not running yarn for {node}: the yarn that runs in {at('web')} is version 1.22.22, which cannot "
                    "apply a cooldown; run yarn set version stable to switch the project to a yarn that can"
                ),
            ),
            "several node package managers": (
                lambda log: log.files_of_more_than_one_manager(package_json, [here / "pnpm.yaml", here / "bun.lockb"]),
                (
                    f"Not running a package manager for {node}: {at('pnpm.yaml')} and {at('bun.lockb')} name different "
                    "ones; set the packageManager field to the one to run"
                ),
            ),
            "several python package managers": (
                lambda log: log.signals_of_more_than_one_manager(
                    pyproject_toml, [here / "uv.lock", here / "pdm.lock"], []
                ),
                (
                    f"Not running uv for {python}: {at('uv.lock')} and {at('pdm.lock')} name different package "
                    "managers; remove the lock file or the table of the one the project no longer uses"
                ),
            ),
            "unsupported package manager": (
                lambda log: log.unsupported_package_manager(pyproject_toml, "poetry", "uv"),
                f"Not running poetry for {python}: Update-time supports only uv",
            ),
        }
        for case, (log_message, rendered) in cases.items():
            with self.subTest(case):
                log_message(_new_logger())
                self.assert_message(message_of(Logger.manager_cannot_run), logging.WARNING, rendered)

    @kills.replace(
        _python_managers_reason,
        '[f"[tool.{table}]" for table in tables]',
        "list(tables)",
        "the warning about the tables of more than one manager names a table by its bare name",
    )
    def test_manager_cannot_run_names_the_tables_of_the_managers(self):
        """Test that the tables naming different package managers are named as the pyproject.toml writes them."""
        _new_logger().signals_of_more_than_one_manager(Path.cwd() / "pyproject.toml", [], ["poetry", "pdm"])
        self.assert_message(
            message_of(Logger.manager_cannot_run),
            logging.WARNING,
            f"Not running uv for {at('pyproject.toml')}: [tool.poetry] and [tool.pdm] name different package managers; "
            "remove the lock file or the table of the one the project no longer uses",
        )

    def test_redundant_directive(self):
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
                    message_of(Logger.redundant_directive),
                    logging.WARNING,
                    f"Redundant update-time directive {directive} for {dependency('python')} in "
                    f"{at('Dockerfile:2')}: {reason}",
                )

    def test_path_logged_at_debug(self):
        """Test that the per-file 'checking for updates' progress is logged at debug level."""
        _new_logger().path(Path.cwd() / "config.yml")
        self.assert_message(
            message_of(Logger.path), logging.DEBUG, f"Checking if there are updates for {at('config.yml')}"
        )

    @kills.replace(
        Logger.configured_cooldown,
        "self._file_fields(recorded_in, configured=Location(path),",
        "self._file_fields(path, configured=Location(recorded_in),",
        "the line names the file that records the cooldown as the file it is set for",
    )
    def test_configured_cooldown(self):
        """Test that a cooldown Update-time set is logged, naming the file it is set for and the file recording it."""
        compiled, source = Path.cwd() / "requirements.txt", Path.cwd() / "requirements.in"
        _new_logger().configured_cooldown(compiled, source, "7 days", "uv exclude-newer")
        self.assert_message(
            message_of(Logger.configured_cooldown),
            logging.INFO,
            f"Set uv exclude-newer to '7 days' for {at('requirements.txt')}, and recorded in {at('requirements.in')} "
            "that Update-time manages it",
        )

    def test_invalid_file(self):
        """Test that a file that does not parse is warned about, naming the format given rather than its suffix."""
        for file_name, file_format in (("pom.xml", "XML"), ("compose.yml", "YAML")):
            with self.subTest(format=file_format):
                _new_logger().invalid_file(Path.cwd() / file_name, file_format)
                self.assert_message(
                    message_of(Logger.invalid_file),
                    logging.WARNING,
                    f"Skipping {at(file_name)}: it is not valid {file_format}",
                )

    def test_excluded_path_logged_at_debug(self):
        """Test that a directory held back by --exclude-path is logged at debug level, with its path undelimited."""
        _new_logger().excluded_path(Path("vendor"))
        self.assert_message(
            message_of(Logger.excluded_path), logging.DEBUG, "Excluding vendor from the scan (--exclude-path)"
        )

    def test_missing_excluded_path_logged_at_warning(self):
        """Test that a non-existing --exclude-path directory is logged as a warning, not an error."""
        _new_logger().missing_excluded_path(Path("vendor"))
        self.assert_message(
            message_of(Logger.missing_excluded_path),
            logging.WARNING,
            "Path vendor passed to --exclude-path does not exist",
        )

    def test_request_failed(self):
        """Test that a failed request is warned about with its URL and why it failed, the HTTP error if it got one."""
        response = Response()
        url = "https://pypi.org/pypi/humanize/json"
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
                    message_of(Logger.request_failed), logging.WARNING, f"Could not fetch {url}: {worded}"
                )

    def test_running_logged_at_debug(self):
        """Test that the version of Update-time, and the OS and Python version it runs on, are logged at debug level."""
        _new_logger().running("0.0.48", "macOS-26.7.1-arm64-arm-64bit-Mach-O", "3.14.7")
        self.assert_message(
            message_of(Logger.running),
            logging.DEBUG,
            "Running Update-time v0.0.48 on macOS-26.7.1-arm64-arm-64bit-Mach-O with Python 3.14.7",
        )

    @kills.replace(
        Logger.arguments,
        "shlex",
        '" "',
        "an argument holding a space is logged as two arguments",
        case="argument with a space",
    )
    @kills.delete(
        Logger.arguments,
        ' or "none"',
        "a run without arguments logs nothing after the colon",
        case="no arguments",
    )
    def test_arguments_logged_at_debug(self):
        """Test that the command-line arguments are logged at debug level, quoted as the shell reads them."""
        cases: tuple[tuple[str, list[str], str], ...] = (
            ("argument with a space", ["my project"], "'my project'"),
            ("no arguments", [], "none"),
        )
        for case, arguments, rendered in cases:
            with self.subTest(case):
                _new_logger().arguments(arguments)
                self.assert_message(message_of(Logger.arguments), logging.DEBUG, f"Command-line arguments: {rendered}")

    def test_tool_version_logged_at_debug(self):
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
                self.assert_message(message_of(Logger.tool_version), logging.DEBUG, expected)

    def test_credentials_logged_at_debug(self):
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
                self.assert_message(message_of(Logger.credentials), logging.DEBUG, f"Credentials: {rendered}")

    def test_network_settings_logged_at_debug(self):
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
                self.assert_message(message_of(Logger.network_settings), logging.DEBUG, f"Network settings: {rendered}")

    @kills.replace(
        Logger.github_rate_limit_used_up,
        "if authenticated else",
        "if True else",
        "an anonymous run reports the limit of GITHUB_TOKEN",
        case="anonymous",
    )
    @kills.replace(
        Logger.github_rate_limit_used_up,
        "if authenticated else",
        "if False else",
        "an authenticated run is advised to set GITHUB_TOKEN",
        case="with a token",
    )
    def test_used_up_github_rate_limit_logged_at_error(self):
        """Test that a used-up GitHub rate limit is reported with its limit and reset, advising a missing token."""
        reset = datetime(2026, 10, 17, 15, 42, tzinfo=UTC)
        until = utc_date_and_time(reset)
        for case, (limit, authenticated, rendered) in {
            "anonymous": (60, False, f"60 requests is used up until {until}; set GITHUB_TOKEN to raise it"),
            "with a token": (5000, True, f"5000 requests for GITHUB_TOKEN is used up until {until}"),
        }.items():
            with self.subTest(case):
                _new_logger().github_rate_limit_used_up(limit, reset, authenticated=authenticated)
                self.assert_message(
                    message_of(Logger.github_rate_limit_used_up),
                    logging.ERROR,
                    f"GitHub's API rate limit of {rendered}",
                )

    def test_unreadable_github_rate_limit_logged_at_debug(self):
        """Test that rate-limit headers that cannot be read are logged at debug level, with the reason."""
        _new_logger().unreadable_github_rate_limit(ValueError("invalid literal for int() with base 10: 'soon'"))
        self.assert_message(
            message_of(Logger.unreadable_github_rate_limit),
            logging.DEBUG,
            "Could not read GitHub's rate-limit headers: invalid literal for int() with base 10: 'soon'",
        )

    def test_update_a_signal_stopped_logged_at_error(self):
        """Test that an update a signal stopped is logged as an error, naming the dependency type and the signal."""
        _new_logger().update_stopped("Maven dependencies", signal.SIGKILL)
        self.assert_message(
            message_of(Logger.update_stopped),
            logging.ERROR,
            "Could not finish updating Maven dependencies: SIGKILL stopped it",
        )

    def test_stopped_run_logged_at_error(self):
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
                    message_of(Logger.stopped),
                    logging.ERROR,
                    f"{name} stopped Update-time before it finished; review the files it changed, since a file it "
                    "was writing may be incomplete",
                )

    def test_forced_outside_git_repository_logged_at_warning(self):
        """Test that running outside a git repository because of --force is logged as a warning, with the scan root."""
        _new_logger().forced_outside_git_repository(Path("/home/user/project"))
        self.assert_message(
            message_of(Logger.forced_outside_git_repository),
            logging.WARNING,
            "Running outside a git repository (/home/user/project) because --force was given; changes are made in "
            "place and cannot be reverted",
        )

    def test_new_version_with_publication_date(self):
        """Test that the publication date is appended to the version when it is known, with or without a changelog."""
        published = datetime(2026, 5, 29, 13, 54, tzinfo=UTC)
        cases = {
            "a changelog": (Changes("Changelog", markdown=False, url=""), {CHANGES: "Changelog"}),
            "no changelog": (NO_CHANGES, {NOTE: "No changelog available!"}),
        }
        for case, (changes, extra) in cases.items():
            with self.subTest(case):
                version = DependencyVersion("1.0", changes, publication=LookedUp(published))
                _new_logger().new_version(reference("dependency", create_location("a.txt", 3)), version)
                self.assert_message(
                    message_of(Logger.new_version),
                    logging.INFO,
                    f"New version available for {dependency('dependency')} in {at('a.txt:3')}: "
                    f"{published_version('1.0', published)}",
                )
                self.assertEqual(logged_extra(self.mock_log.call_args.kwargs), extra)


def _drifted_pin(kind: DriftKind, version: str) -> DriftedPin:
    """Return a pin of an image or an action at the version, drifted from the first digest or commit to the second."""
    if kind == DriftKind.DIGEST:
        return DriftedPin("dependency", version, create_location("Dockerfile", 2), DIGEST1, kind=kind, new_sha=DIGEST2)
    workflow = create_location(".github/workflows/ci.yml", 17)
    return DriftedPin("actions/checkout", version, workflow, COMMIT_SHA1, kind=kind, new_sha=COMMIT_SHA2)
