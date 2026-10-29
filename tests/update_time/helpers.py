"""Shared test helpers."""

import ast
import contextlib
import importlib
import inspect
import pathlib
import pkgutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from functools import cache
from logging import DEBUG, ERROR, INFO, WARNING
from typing import TYPE_CHECKING, Protocol
from unittest.mock import ANY, Mock, call, patch

import update_time
from update_time.domain.archival import IGNORE_ARCHIVED
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import ArchivedSubject, DependencyVersion, FloatingPin, ImageNotLookedUp, tag_of
from update_time.domain.reference import Reference, RefKind
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.vulnerability import VULNERABILITY_LEVEL, Vulnerability
from update_time.io.console import CHANGES, NOTE
from update_time.io.log import Logger, LogMessage
from update_time.manifests.python_dependency_toml import Declaration
from update_time.markers.bound import parse_bound
from update_time.markers.directive import Reason
from update_time.markers.drift import ALLOW_HASH_DRIFT
from update_time.markers.floating import ALLOW_FLOATING_PIN
from update_time.markers.marker import Marker
from update_time.markers.reference import SteeredResolvedReference
from update_time.primitives.location import Location
from update_time.primitives.lookup import LookedUp
from update_time.primitives.timestamp import utc_date_and_time

from tests.helpers import patch_environ
from tests.update_time.fixtures import FRESH_DATE, PAST_COOLDOWN_DATE

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path
    from types import ModuleType
    from unittest.mock import _Call

    from update_time.domain.bound import Verb, VersionBound
    from update_time.domain.reference import DriftedPin
    from update_time.io.log import Drift
    from update_time.markers.directive import RedundancyReason


def declaration(dependency: str, version: str, path: Mock, line: int, position: int) -> Declaration:
    """Return the declaration a file makes at the line, of a dependency PyPI serves a release for."""
    return Declaration(dependency, version, Location(path, line), uv_sourced=False, direct_url=False, position=position)


@cache
def syntax_tree(source: str) -> ast.Module:
    """Return the syntax tree of the source, parsing each source once."""
    return ast.parse(source)


@cache
def syntax_nodes(source: str) -> tuple[ast.AST, ...]:
    """Return every node of the source's syntax tree, walking each source once."""
    return tuple(ast.walk(syntax_tree(source)))


def module_level_assignments(tree: ast.Module) -> Iterator[tuple[list[ast.expr], ast.expr | None]]:
    """Yield the targets and assigned value of each module-level assignment, annotated or not.

    An annotation without a value (`NAME: int`) assigns nothing, so its value is None.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            yield node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            yield [node.target], node.value


@contextlib.contextmanager
def project(files: dict[str, str]) -> Generator[str]:
    """Yield the path of a directory holding the given files, so a rule or a check has a project to scan."""
    with tempfile.TemporaryDirectory() as directory:
        for name, source in files.items():
            path = pathlib.Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
        yield directory


class _Cached(Protocol):
    """A function whose results are cached, as `functools.cache` returns one, wrapping the function it caches."""

    __wrapped__: object

    def cache_clear(self) -> None:
        """Empty the cache, so what one test cached is not served to the next."""


@cache
def _package_modules(package: ModuleType) -> tuple[ModuleType, ...]:
    """Return the package and every module within it, walked once and remembered.

    What each module holds is read afresh every time, so a cache added to one is still found. This cache survives the
    clearing itself as long as the module defining it sits outside `package`.
    """
    prefix = f"{package.__name__}."
    return (
        package,
        *(importlib.import_module(found.name) for found in pkgutil.walk_packages(package.__path__, prefix)),
    )


def _all_cached_functions(package: ModuleType) -> list[_Cached]:
    """Return every cached function the package's modules define, the package's own included.

    Every module is scanned because `_cached_functions` attributes each cache to the module defining it, so a
    module left out takes its caches with it.
    """
    return [function for module in _package_modules(package) for function in _cached_functions(module)]


def _cached_functions(module: ModuleType) -> list[_Cached]:
    """Return the cached functions the module defines, recognised by the `cache_clear` their decorator adds."""
    return [
        value
        for value in vars(module).values()
        if hasattr(value, "cache_clear") and getattr(value, "__module__", None) == module.__name__
    ]


def _all_loggers(package: ModuleType) -> list[Logger]:
    """Return the loggers the package's modules hold as module constants."""
    return [
        value for module in _package_modules(package) for value in vars(module).values() if isinstance(value, Logger)
    ]


class CacheClearingTestCase(unittest.TestCase):
    """Base test case that resets global state before each test to prevent cross-test leakage.

    The caches are discovered by scanning the package, so adding a `@cache` to a source module needs nothing added here.
    """

    def setUp(self) -> None:
        """Start each test as a fresh run."""
        super().setUp()
        self._start_new_run()

    def _start_new_run(self) -> None:
        """Forget what the previous run fetched and which changelogs it suppressed, so the next run starts afresh."""
        for cached_function in _all_cached_functions(update_time):
            cached_function.cache_clear()
        for logger in _all_loggers(update_time):
            logger.forget_shown_changelogs()


class LoggingTestCase(CacheClearingTestCase):
    """Base test case for any test of code that logs.

    It mocks the logger's log method, exposed as the mock_log attribute, and offers the assert_*_logged helpers below.
    """

    def setUp(self) -> None:
        """Start the logger patch and register its cleanup, before the test starts as a fresh run."""
        patcher = patch("logging.Logger.log")
        self.addCleanup(patcher.stop)
        self.mock_log = patcher.start()
        super().setUp()

    def tearDown(self) -> None:
        """Fail the test on an error its last run logged unexpectedly."""
        super().tearDown()
        self._assert_no_unexpected_error()

    @contextlib.contextmanager
    def subTest(self, *args: object, **kwargs: object) -> Generator[None]:  # noqa: N802
        """Run the case as a run of its own, so a table cannot forget to start one."""
        self._start_new_run()
        with super().subTest(*args, **kwargs):
            yield
            self._assert_no_unexpected_error()
        self._start_new_run()  # The case checked its own run, so tearDown has nothing of it left to check again

    def _start_new_run(self) -> None:
        """Forget what the previous run logged and expected as well, so the next run starts afresh."""
        super()._start_new_run()
        self.mock_log.reset_mock()
        self._error_expected = False  # Set by assert_error_logged; the run fails on an error the test didn't expect.

    def _assert_no_unexpected_error(self) -> None:
        """Fail if the run logged an error that it did not explicitly expect via assert_error_logged."""
        if not self._error_expected:
            self.assertEqual(self.records(ERROR), [])

    def records(self, level: int) -> list[_Call]:
        """Return the records logged at the level, each as the message and the fields it carries, changes folded in."""
        return [
            call(args[1], args[2] | kwargs.get("extra", {}))
            for args, kwargs in self.mock_log.call_args_list
            if args[0] == level
        ]

    @staticmethod
    def _expected_call(message: LogMessage, fields: dict[str, object]) -> _Call:
        """Return the call the logger makes for the message, with its fields passed by name.

        The fields are rendered the way the logger renders them, so an assertion here names the plain domain values
        — a `Location` rather than its delimiter-wrapped text.
        """
        return call(message, Logger._rendered(fields))

    def assert_logged(self, message: LogMessage, **fields: object) -> None:
        """Assert the message was the only record logged at its level, with the given fields."""
        self.assertEqual(self.records(message.level), [self._expected_call(message, fields)])

    def _assert_last_logged(self, message: LogMessage, **fields: object) -> None:
        """Assert the message was the most recent record at its level, with the given fields."""
        self.assertEqual(self.records(message.level)[-1:], [self._expected_call(message, fields)])

    def assert_logged_among_others(self, message: LogMessage, **fields: object) -> None:
        """Assert the message was logged with the given fields, among the other records at its level."""
        self.assertIn(self._expected_call(message, fields), self.records(message.level))

    def _assert_reported(self, message: LogMessage, *, among_others: bool, **fields: object) -> None:
        """Assert the message was logged with the given fields, as the only record at its level unless among others."""
        assert_logged = self.assert_logged_among_others if among_others else self.assert_logged
        assert_logged(message, **fields)

    def assert_error_logged(self, message: LogMessage, **fields: object) -> None:
        """Assert an error was logged, and mark it expected so tearDown's strict no-error check passes."""
        self.assert_errors_logged((message, fields))

    def assert_errors_logged(self, *errors: tuple[LogMessage, dict[str, object]]) -> None:
        """Assert the errors were the run's only errors, in this order, and mark them expected."""
        self._error_expected = True
        expected = [self._expected_call(message, fields) for message, fields in errors]
        self.assertEqual(self.records(ERROR), expected)

    @staticmethod
    def _new_version_fields(
        dependency: str, version: str, location: Location, note: str = Logger._NO_CHANGELOG, changes: object = ""
    ) -> dict[str, object]:
        """Return the fields the logger logs an available new version with: the changes, or else the note, below it."""
        below = {CHANGES: changes} if changes else {NOTE: note}
        return {"dependency": dependency, "location": location, "version": version, **below}

    def assert_new_version_logged(
        self,
        dependency: str,
        version: str,
        location: Location,
        note: str = Logger._NO_CHANGELOG,
        *,
        changes: object = "",
    ) -> None:
        """Assert that a new version was the only record logged for the dependency in the file."""
        fields = self._new_version_fields(dependency, version, location, note, changes)
        self.assert_logged(message_of(Logger.new_version), **fields)

    def assert_last_new_version_logged(
        self, dependency: str, version: str, location: Location, note: str = Logger._NO_CHANGELOG
    ) -> None:
        """Assert that a new version was the most recent record, for a run that logs several."""
        fields = self._new_version_fields(dependency, version, location, note)
        self._assert_last_logged(message_of(Logger.new_version), **fields)

    def assert_new_version_logged_among_others(
        self,
        dependency: str,
        version: str,
        location: Location,
        note: str = Logger._NO_CHANGELOG,
        *,
        changes: object = "",
    ) -> None:
        """Assert that a new version was logged, among the other records of a run that logs several."""
        fields = self._new_version_fields(dependency, version, location, note, changes)
        self.assert_logged_among_others(message_of(Logger.new_version), **fields)

    def assert_new_versions_logged(self, version: str, location: Location, *dependencies: str) -> None:
        """Assert that the dependencies, and no others, were reported at the location with the new version, in order."""
        expected = [
            self._expected_call(message_of(Logger.new_version), self._new_version_fields(dependency, version, location))
            for dependency in dependencies
        ]
        self.assertEqual(self.new_version_records(), expected)

    def records_of(self, message: LogMessage, **fields: object) -> list[_Call]:
        """Return the records logged for the message, ignoring the other records at its level.

        The fields narrow it further, so a message one field parameterises — a redundancy warning's reason — is
        matched by the value it carries rather than by the message alone.
        """
        rendered = Logger._rendered(fields)
        return [
            record
            for record in self.records(message.level)
            if record.args[:1] == (message,) and rendered.items() <= record.args[1].items()
        ]

    def new_version_records(self) -> list[_Call]:
        """Return the records reporting an available new version, ignoring the other records at their level."""
        return self.records_of(message_of(Logger.new_version))

    def assert_none_logged(self, message: LogMessage, what: str, **fields: object) -> None:
        """Assert the message was not logged at all, whatever else was logged at its level."""
        self.assertEqual(self.records_of(message, **fields), [], f"Expected no {what} to be logged")

    def assert_no_new_version_logged(self) -> None:
        """Assert that no new version was logged (other records at the same level are allowed)."""
        self.assert_none_logged(message_of(Logger.new_version), "new version")

    def assert_no_unpinned_floating_tag_logged(self) -> None:
        """Assert that the run did not report a floating tag left as it is (other records are allowed)."""
        self.assert_none_logged(message_of(Logger.unpinned_floating_tag), "unpinned floating tag")

    def assert_pinned_logged(
        self, dependency: str, version: str, sha: str, location: Location, *, among_others: bool = False
    ) -> None:
        """Assert that the reference was logged as pinned to a version and its hash, as the only record by default."""
        self._assert_reported(
            message_of(Logger.pinned),
            among_others=among_others,
            dependency=dependency,
            location=location,
            version=version,
            sha=sha,
        )

    def assert_unpinned_ref_logged(
        self, dependency: str, name: str, location: Location, reason: str, *, ref: RefKind = RefKind.BRANCH
    ) -> None:
        """Assert that a ref left unpinned was reported, with the reason, as the run's only error."""
        self.assert_error_logged(
            message_of(Logger.unpinned_ref), dependency=dependency, ref=ref, name=name, location=location, reason=reason
        )

    def assert_unpinned_floating_tag_logged(
        self, dependency: str, version: str, location: Location, reason: FloatingPin, looked_up: str = ""
    ) -> None:
        """Assert that a floating tag pinned to no version was reported, with its reason, among the other records.

        The reason names the tag looked up, which is the version unless the reference names none.
        """
        self.assert_logged_among_others(
            message_of(Logger.unpinned_floating_tag),
            dependency=dependency,
            tag=tag_of(version),
            location=location,
            reason=reason.explained(looked_up or version),
        )

    def assert_kept_floating_logged(
        self, dependency: str, version: str, release: DependencyVersion, location: Location, cause: object = ANY
    ) -> None:
        """Assert that a floating tag left as it is was reported, naming the release it resolves to."""
        self.assert_logged_among_others(
            message_of(Logger.keeping_floating_tag),
            dependency=dependency,
            tag=tag_of(version),
            resolved=release.version,
            sha=release.sha,
            location=location,
            cause=cause,
        )

    def assert_comment_naming_no_ref_logged(self, dependency: str, name: str, location: Location) -> None:
        """Assert that a commit SHA whose comment does not name a ref was reported as left as it is."""
        self.assert_logged_among_others(
            message_of(Logger.comment_naming_no_ref), dependency=dependency, name=name, location=location
        )

    def assert_kept_ref_logged(  # noqa: PLR0913 — the fields the message carries
        self,
        dependency: str,
        name: str,
        release: DependencyVersion,
        location: Location,
        cause: str,
        *,
        ref: RefKind = RefKind.BRANCH,
    ) -> None:
        """Assert that a branch or tag reference left as it is was reported, naming the release it resolves to."""
        self.assert_logged_among_others(
            message_of(Logger.keeping_ref),
            dependency=dependency,
            ref=ref,
            name=name,
            resolved=release.version,
            sha=release.sha,
            location=location,
            cause=cause,
        )

    def assert_image_not_looked_up_logged(
        self, dependency: str, tag: str, location: Location, reason: ImageNotLookedUp
    ) -> None:
        """Assert that a reference Update-time does not look up was reported, with its reason, among others."""
        self.assert_logged_among_others(
            message_of(Logger.image_not_looked_up),
            dependency=dependency,
            tag=tag_of(tag),
            location=location,
            reason=reason,
        )

    def assert_cannot_pin_logged(self, dependency: str, location: Location) -> None:
        """Assert that a reference with nowhere to hold a hash was reported as one that cannot be pinned."""
        self.assert_logged(message_of(Logger.cannot_pin), dependency=dependency, location=location)

    def assert_drift_logged(self, kind: Drift, drifted: DriftedPin, *, among_others: bool = False) -> None:
        """Assert that the drift was logged, as the file's only warning by default."""
        message, fields = drift_warning(kind, drifted)
        self._assert_reported(message, among_others=among_others, **fields)

    def assert_adopted_drift_logged(
        self, kind: Drift, drifted: DriftedPin, cause: object = ANY, *, among_others: bool = False
    ) -> None:
        """Assert that the adopted drift was logged, as the file's only record at its level by default."""
        message, fields = adopted_drift_report(kind, drifted, cause)
        self._assert_reported(message, among_others=among_others, **fields)

    def assert_undated_commit_logged(self, kind: Drift, drifted: DriftedPin, reason: str) -> None:
        """Assert that a commit whose date could not be determined was logged as the file's only error."""
        fields = Logger._drifted_reference_fields(kind, drifted, new_sha=drifted.new_sha, reason=reason)
        self.assert_error_logged(message_of(Logger.no_commit_date), **fields)

    def assert_undated_push_logged(self, drifted: DriftedPin, reason: str) -> None:
        """Assert that a re-pushed digest that could not be dated was logged as the file's only error."""
        fields = Logger._drifted_reference_fields(Logger.DIGEST_DRIFT, drifted, reason=reason)
        self.assert_error_logged(message_of(Logger.no_push_date), **fields)

    def assert_hash_mismatch_logged(
        self,
        dependency: str,
        version: str,
        declared_hash: str,
        served_hash: str,
        location: Location,
    ) -> None:
        """Assert that a declared integrity hash disagreeing with the served one was logged once for the file."""
        self.assert_logged(
            message_of(Logger.hash_mismatch),
            dependency=dependency,
            version=version,
            location=location,
            declared_hash=declared_hash,
            served_hash=served_hash,
        )

    def assert_stale_dependency_logged(
        self, dependency: str, version: str, *locations: Location, among_others: bool = False
    ) -> None:
        """Assert that a stale dependency was warned about at each of the locations, and by default nowhere else.

        The exact age and threshold vary with the wall clock, so they are matched with ANY. A run that warns about
        something else as well — a marker item that decides nothing — asserts `among_others`, which leaves the other
        warnings out of the comparison while still holding every staleness warning to the locations given.
        """
        fields: dict[str, object] = {"dependency": dependency, "version": version, "days": ANY, "threshold": ANY}
        message = message_of(Logger.report_staleness, "days")
        expected = [self._expected_call(message, fields | {"location": location}) for location in locations]
        stale_warnings = self.records_of(message)
        self.assertEqual(stale_warnings if among_others else self.records(message.level), expected)

    def assert_yanked_dependency_logged(
        self, dependency: str, version: str, location: Location, reason: object = ANY
    ) -> None:
        """Assert that a pin left on a yanked version was warned about once for the file."""
        self.assert_logged(
            message_of(Logger.report_yank, "reason"),
            dependency=dependency,
            location=location,
            version=version,
            reason=reason,
        )

    def assert_archived_dependency_logged(
        self,
        dependency: str,
        location: Location,
        reason: object = ANY,
        *,
        subject: ArchivedSubject = ArchivedSubject.PROJECT,
        among_others: bool = False,
    ) -> None:
        """Assert that an archived dependency was warned about, as the file's only warning by default."""
        self._assert_reported(
            message_of(Logger.report_archival, "reason"),
            among_others=among_others,
            dependency=dependency,
            location=location,
            subject=subject,
            reason=reason,
        )

    def assert_archived_repository_logged(self, dependency: str, location: Location) -> None:
        """Assert that a repository GitHub declares archived was warned about, GitHub publishing no reason."""
        self.assert_archived_dependency_logged(dependency, location, reason="", subject=ArchivedSubject.REPOSITORY)

    def assert_vulnerable_dependency_logged(
        self,
        dependency: str,
        version: str,
        vulnerability: Vulnerability,
        location: Location,
        *,
        among_others: bool = False,
    ) -> None:
        """Assert that a pin left on a vulnerable version was warned about, as the file's only warning by default."""
        fields = Logger._vulnerability_fields(reference(dependency, location, version), vulnerability)
        self._assert_reported(message_of(Logger.vulnerable_dependency), among_others=among_others, **fields)

    @staticmethod
    def _redundant_suppression_fields(
        dependency: str, version: str, location: Location, directive: object
    ) -> dict[str, object]:
        """Return the fields every redundant vulnerability suppression is warned with."""
        return {"directive": directive, "dependency": dependency, "location": location, "version": version}

    def assert_redundant_vulnerable_advisory_logged(
        self, dependency: str, version: str, location: Location, directive: object = ANY
    ) -> None:
        """Assert that a suppression naming an advisory the version does not have was warned about, among the others."""
        self.assert_logged_among_others(
            message_of(Logger.redundant_vulnerable_advisory),
            **self._redundant_suppression_fields(dependency, version, location, directive),
        )

    def assert_redundant_vulnerable_level_logged(  # noqa: PLR0913 — the fields of the warning
        self,
        dependency: str,
        version: str,
        level: str,
        location: Location,
        directive: object = ANY,
        *,
        run_level: str | None = None,
    ) -> None:
        """Assert that a risk level judging the vulnerabilities as the run's does was warned about, among others."""
        run_fields = {"default_level": VULNERABILITY_LEVEL.default} if run_level is None else {"run_level": run_level}
        self.assert_logged_among_others(
            message_of(Logger.redundant_vulnerable_level, *run_fields),
            **self._redundant_suppression_fields(dependency, version, location, directive),
            level=level,
            **run_fields,
        )

    def assert_redundant_directive_logged(
        self, reason: RedundancyReason, dependency: str, location: Location, directive: object = ANY
    ) -> None:
        """Assert that the run's only warning reports the directive as redundant, saying why."""
        self.assert_logged(
            message_of(Logger.redundant_directive),
            directive=directive,
            dependency=dependency,
            location=location,
            reason=reason,
        )

    def assert_no_redundant_vulnerable_level_logged(self) -> None:
        """Assert that no risk level was warned about as redundant, whichever way the warning names the run's level."""
        what = "redundant risk level"
        self.assert_none_logged(message_of(Logger.redundant_vulnerable_level, "run_level"), what)
        self.assert_none_logged(message_of(Logger.redundant_vulnerable_level, "default_level"), what)

    def assert_no_redundant_suppression_logged(self) -> None:
        """Assert that no vulnerability suppression was reported as silencing nothing, whatever else was logged."""
        what = "redundant vulnerability suppression"
        self.assert_none_logged(message_of(Logger.redundant_vulnerable_advisory), what)
        self.assert_no_redundant_vulnerable_level_logged()
        self.assert_none_logged(message_of(Logger.redundant_directive), what, reason=Reason.NO_VULNERABILITY_REPORTS)

    def assert_no_redundant_directive_logged(self) -> None:
        """Assert that no directive was reported as redundant, whatever else was logged."""
        self.assert_none_logged(message_of(Logger.redundant_directive), "redundant directive")
        what = "redundant staleness threshold"
        self.assert_none_logged(message_of(Logger.redundant_stale_threshold, "run_threshold"), what)
        self.assert_none_logged(message_of(Logger.redundant_stale_threshold, "default_threshold"), what)

    def assert_path_logged(self, path: Path) -> None:
        """Assert that the path being checked for updates was logged."""
        self._assert_last_logged(message_of(Logger.path), location=Location(path))

    def assert_no_debug_logged(self) -> None:
        """Assert that nothing was logged at DEBUG."""
        self.assertEqual(self.records(DEBUG), [])

    def assert_recognised_marker_logged(self, dependency: str, location: Location, marker: Marker) -> None:
        """Assert that a reference's marker was reported as recognised, among the other records at its level."""
        self.assert_logged_among_others(
            message_of(Logger.recognised_marker), directives=marker, dependency=dependency, location=location
        )

    def assert_invalid_bracket_item_logged(
        self, dependency: str, location: object, item: str, *, among_others: bool = False
    ) -> None:
        """Assert that an item the marker language cannot read was warned about, as the only warning by default."""
        self._assert_reported(
            message_of(Logger.invalid_bracket_item),
            among_others=among_others,
            bracket_item=item,
            dependency=dependency,
            location=location,
        )

    def assert_ignored_logged(
        self, dependency: str, location: Location, directive: object = ANY, *, among_others: bool = False
    ) -> None:
        """Assert that ignoring a reference's update was logged, by default as the last record."""
        assert_logged = self.assert_logged_among_others if among_others else self._assert_last_logged
        assert_logged(message_of(Logger.ignored), dependency=dependency, location=location, directive=directive)

    def assert_held_back_by_cooldown_logged(
        self, dependency: str, version: str, location: Location, *, days: int = COOLDOWN.default, directive: str = ""
    ) -> None:
        """Assert that the version published at `FRESH_DATE` was logged as held back, among the other records.

        A `directive` is the marker's directive that sets the cooldown, which the line names.
        """
        fields = {
            "dependency": dependency,
            "location": location,
            "version": version,
            "published": utc_date_and_time(FRESH_DATE),
            "days": days,
            "until": utc_date_and_time(FRESH_DATE + timedelta(days=days)),
            **({"directive": directive} if directive else {}),
        }
        self.assert_logged_among_others(message_of(Logger.held_back_by_cooldown, *fields), **fields)

    def assert_no_staleness_warning_logged(self, dependency: str, location: Location) -> None:
        """Assert that the dependency at the location was not warned about as stale."""
        message = message_of(Logger.report_staleness, "days")
        self.assert_none_logged(message, "staleness warning", dependency=dependency, location=location)

    def assert_no_ignored_staleness_logged(self) -> None:
        """Assert that no staleness warning was reported as silenced, whatever else was logged at that level."""
        self.assert_none_logged(message_of(Logger.ignored_staleness), "staleness warning silenced by a marker")

    def assert_no_ignored_vulnerability_logged(self) -> None:
        """Assert that no vulnerability warning was reported as silenced, whatever else was logged at that level."""
        self.assert_none_logged(message_of(Logger.ignored_vulnerability), "vulnerability warning silenced by a marker")

    def assert_ignored_vulnerability_logged(
        self, dependency: str, location: Location, advisory: str, directive: str
    ) -> None:
        """Assert that a vulnerability warning silenced by a marker was logged, among the other records."""
        self.assert_logged_among_others(
            message_of(Logger.ignored_vulnerability),
            dependency=dependency,
            location=location,
            advisory=advisory,
            directive=directive,
        )

    def assert_no_globally_ignored_vulnerability_logged(self) -> None:
        """Assert that no vulnerability warning was reported as silenced run-wide, whatever else was logged."""
        self.assert_none_logged(
            message_of(Logger.globally_ignored_vulnerability), "vulnerability warning silenced run-wide"
        )

    def assert_globally_ignored_vulnerability_logged(
        self, dependency: str, location: Location, advisory: str, identifiers: str
    ) -> None:
        """Assert that a vulnerability warning silenced run-wide was logged, among the other records at its level."""
        self.assert_logged_among_others(
            message_of(Logger.globally_ignored_vulnerability),
            dependency=dependency,
            advisory=advisory,
            identifiers=identifiers,
            location=location,
        )

    def assert_ignored_staleness_logged(self, dependency: str, location: Location, directive: object = ANY) -> None:
        """Assert that a staleness warning silenced by a marker was logged, among the other records."""
        self.assert_logged_among_others(
            message_of(Logger.ignored_staleness),
            dependency=dependency,
            location=location,
            directive=directive,
        )

    def assert_ignored_yank_logged(self, dependency: str, location: Location, directive: object = ANY) -> None:
        """Assert that a yank warning silenced by a marker was logged, among the other records."""
        self.assert_logged_among_others(
            message_of(Logger.report_yank, "dependency", "location", "directive"),
            dependency=dependency,
            location=location,
            directive=directive,
        )

    def assert_ignored_archival_logged(self, dependency: str, location: Location, directive: object = ANY) -> None:
        """Assert that an archival warning silenced by a marker was logged, among the other records."""
        self.assert_logged_among_others(
            message_of(Logger.report_archival, "dependency", "location", "directive"),
            dependency=dependency,
            location=location,
            directive=directive,
        )

    def assert_skipped_logged(self, path: Path, reason: str) -> None:
        """Assert that deliberately skipping a file was logged with the given reason."""
        self.assert_logged(message_of(Logger.skipped), location=Location(path), reason=reason)

    def assert_unsupported_package_manager_logged(self, path: Path, manager: str, supported: str) -> None:
        """Assert that an unsupported package manager was logged for the file."""
        self.assert_logged(
            message_of(Logger.unsupported_package_manager),
            location=Location(path),
            manager=manager,
            supported=supported,
        )

    def assert_no_dependencies_logged(self, path: Path, manager: str, verb: str) -> None:
        """Assert that the package manager was logged as not running for a file that declares nothing to update."""
        self.assert_logged(message_of(Logger.no_dependencies), location=Location(path), manager=manager, verb=verb)

    def assert_invalid_file_logged(self, path: Path, file_format: str) -> None:
        """Assert that a file that does not parse was logged for the file, naming its format."""
        self.assert_logged(message_of(Logger.invalid_file), location=Location(path), format=file_format)

    def assert_could_not_fetch_logged(self, *urls: object, status: object = ANY, reason: object = ANY) -> None:
        """Assert that the run's only warnings say each of the URLs could not be fetched, a single one by default."""
        message = message_of(Logger.response)
        fields = [{"url": url, "status": status, "reason": reason} for url in urls or (ANY,)]
        self.assertEqual(self.records(message.level), [self._expected_call(message, each) for each in fields])

    def assert_command_failed_logged(self, command: object, output: object) -> None:
        """Assert that a single 'command failed' error was logged, for the given command and output."""
        self.assert_error_logged(message_of(Logger.command_failed), command=command, output=output)

    def assert_command_stderr_logged(self, command: object = ANY, stderr: object = ANY) -> None:
        """Assert that a single 'command wrote to stderr' warning was logged, optionally for a given command/stderr."""
        self.assert_logged(message_of(Logger.command_stderr), command=command, stderr=stderr)

    def assert_uv_dependencies_not_updated_logged(self, path: Path) -> None:
        """Assert that the file's dependencies were reported as not updated, because `uv tree` failed."""
        self.assert_error_logged(message_of(Logger.uv_dependencies_not_updated), location=Location(path))

    def assert_no_info_logged(self) -> None:
        """Assert that nothing was logged at INFO."""
        self.assertEqual(self.records(INFO), [])

    def assert_no_warnings_logged(self) -> None:
        """Assert that no warnings were logged."""
        self.assertEqual(self.records(WARNING), [])


def drift_warning(kind: Drift, drifted: DriftedPin) -> tuple[LogMessage, dict[str, object]]:
    """Return the warning about the drift, and the fields it carries."""
    fields = Logger._drift_fields(kind, drifted, **kind.warning_words)
    return message_of(Logger.drift, *fields), fields


def adopted_drift_report(kind: Drift, drifted: DriftedPin, cause: object) -> tuple[LogMessage, dict[str, object]]:
    """Return the report that the drift was adopted, and the fields it carries."""
    fields = Logger._drift_fields(kind, drifted, cause=cause, **kind.adopted_words)
    return message_of(Logger.adopted_drift, *fields), fields


def message_of(log_method: object, *holes: str) -> LogMessage:
    """Return the log method's message that has the holes, the one with these holes alone where several have them."""
    messages: list[LogMessage] = vars(log_method)["messages"]
    having = [message for message in messages if message.holes >= set(holes)]
    (message,) = having if len(having) == 1 else [message for message in having if message.holes == set(holes)]
    return message


def logged_messages() -> dict[str, LogMessage]:
    """Return the messages the log methods of `Logger` hold, by the method's name, numbered when stacked."""
    logged: dict[str, LogMessage] = {}
    for name, value in vars(Logger).items():
        stacked = vars(value).get("messages", []) if inspect.isfunction(value) else []
        for index, message in enumerate(stacked):
            logged[name if len(stacked) == 1 else f"{name}.{index}"] = message
    return logged


def reference(dependency: str, location: Location, version: str = "") -> Reference:
    """Return the reference a log method is handed, for the dependency pinned at the location."""
    return Reference(dependency, version, location)


def resolved_reference(
    dependency: str,
    location: Location,
    release: DependencyVersion,
    version: str = "",
    marker: Marker | None = None,
) -> SteeredResolvedReference:
    """Return the resolved reference a check is handed: the reference, its release, and its marker."""
    return SteeredResolvedReference(
        dependency, version, location, release=release, marker=Marker() if marker is None else marker
    )


def bound(verb: Verb, item: str) -> VersionBound:
    """Return the version bound the marker item expresses, for tests that need a bound as input."""
    version_bound = parse_bound(verb, item)
    if version_bound is None:
        message = f"Not a version bound item: {item!r}"
        raise ValueError(message)
    return version_bound


# Reusable decorator that disables the staleness check, for update tests that focus on the update flow and would
# otherwise trigger the staleness pass's own registry requests. The staleness pass has its own dedicated tests.
staleness_disabled = patch_environ({STALE_AFTER.name: "0"})

# Reusable decorator that switches the archival check off run-wide, as --ignore-archived does, for the tests
# of what a run then leaves unasked and unreported.
archival_check_disabled = patch_environ({IGNORE_ARCHIVED.name: "1"})

# Reusable decorator that switches the staleness and the archival check off run-wide, as --stale-after 0 and
# --ignore-archived do.
project_checks_disabled = patch_environ({STALE_AFTER.name: "0", IGNORE_ARCHIVED.name: "1"})

# Reusable context managers that make every reference in the run adopt hash drift or keep its pin floating, as
# --allow-hash-drift and --allow-floating-pin do.
hash_drift_allowed = patch_environ({ALLOW_HASH_DRIFT.name: "1"})
floating_pin_allowed = patch_environ({ALLOW_FLOATING_PIN.name: "1"})


def pyproject(*specs: str, marker: str = "") -> str:
    """Return a minimal valid pyproject.toml pinning the given dependencies, in one dependencies array.

    `marker` goes in an `# update-time:` comment on that array's line, so it steers every dependency the array
    declares.
    """
    dependencies = ", ".join(f'"{spec}"' for spec in specs)
    comment = f"  # update-time: {marker}" if marker else ""
    return f"[project]\ndependencies = [{dependencies}]{comment}\n"


def days_ago(days: int) -> datetime:
    """Return the instant the given number of days ago."""
    return datetime.now(UTC) - timedelta(days=days)


def published_version(version: str, published: str | datetime) -> str:
    """Return how the log renders the version published at the timestamp, given as a datetime or in ISO 8601."""
    timestamp = published if isinstance(published, datetime) else datetime.fromisoformat(published)
    return str(DependencyVersion(version, publication=LookedUp(timestamp)))


# The artefact most Maven tests declare.
GUAVA = "com.google.guava:guava"


def properties_element(values: dict[str, str]) -> str:
    """Return a `<properties>` element declaring the given names and values."""
    declared = "".join(f"    <{name}>{value}</{name}>\n" for name, value in values.items())
    return f"  <properties>\n{declared}  </properties>\n"


def vulnerability(advisory: str, summary: str, level: str, aliases: list[str] | None = None) -> Vulnerability:
    """Return the vulnerability Update-time reads an advisory as, known by the aliases where it has them."""
    return Vulnerability(advisory, summary, level, f"https://osv.dev/{advisory}", frozenset(aliases or []))


def docker_tag(
    name: str, digest: str = "", *, tag_last_pushed: datetime | None = PAST_COOLDOWN_DATE, **extra: object
) -> dict[str, object]:
    """Return a single-tag Docker Hub API result for the tag, with an optional digest, a push date, and extra fields."""
    pushed = {"tag_last_pushed": tag_last_pushed.isoformat()} if tag_last_pushed else {}
    return {"name": name, **({"digest": digest} if digest else {}), **pushed, **extra}


def docker_hub_version(version: str) -> str:
    """Return the version as a new-version line renders it for a tag `docker_tag` dates by default."""
    return published_version(version, PAST_COOLDOWN_DATE)
