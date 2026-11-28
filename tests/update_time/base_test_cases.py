"""Base test cases that start each test as a fresh run, check what it logs, and put every executable on the path."""

import contextlib
import importlib
import pkgutil
import unittest
from datetime import timedelta
from functools import cache
from logging import DEBUG, ERROR, INFO, WARNING
from typing import TYPE_CHECKING, Protocol, override
from unittest.mock import ANY, Mock, call, patch

import update_time
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import (
    ArchivedSubject,
    DependencyVersion,
    FloatingPin,
    ImageNotLookedUp,
    Unserved,
    tag_of,
)
from update_time.domain.marker import Marker, Scope
from update_time.domain.reference import RefKind
from update_time.domain.vulnerability import VULNERABILITY_LEVEL, Vulnerability
from update_time.io.console import CHANGES, NOTE
from update_time.io.log import (
    _WARNINGS,
    Logger,
    ToolFailure,
    ToolTask,
    _Fields,  # simplint: ignore[private-import-path] # annotates with it
    _FieldValue,  # simplint: ignore[private-import-path] # annotates with it
    _finding,
    _LogMessage,  # simplint: ignore[private-import-path] # annotates with it
    _risk_level_reason,
    _unnamed_ref_reason,
    _unsupported_manager_reason,
)
from update_time.primitives.environment import EnvVar
from update_time.primitives.location import Location
from update_time.primitives.timestamp import utc_date_and_time

from .domain_objects import reference
from .fixtures import FRESH_DATE
from .io.log_messages import adopted_drift_report, drift_warning, logged_extra, message_of

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Generator, Mapping
    from pathlib import Path
    from types import ModuleType
    from unittest.mock import _Call  # simplint: ignore[private-import-path] # no public call type

    from update_time.domain.dependency import Yank
    from update_time.domain.directive import RedundancyReason
    from update_time.domain.reference import DriftedPin
    from update_time.io.log import FileContent
    from update_time.primitives.command import Command


class _Cached(Protocol):
    """A function whose results are cached, as `functools.cache` returns one."""

    def cache_clear(self) -> None:
        """Empty the cache, so what one test cached is not served to the next."""


def setting_names(package: ModuleType) -> set[str]:
    """Return the environment variable of each setting the package's modules hold: what a run is configured with."""
    return {value.name for module in _package_modules(package) for value in module_values(module, EnvVar)}


@cache
def _package_modules(package: ModuleType) -> tuple[ModuleType, ...]:
    """Return every module within the package, walked once and remembered.

    What each module holds is read afresh every time, so a cache added to one is still found. This cache survives the
    clearing itself as long as the module defining it sits outside `package`.
    """
    prefix = f"{package.__name__}."
    return tuple(importlib.import_module(found.name) for found in pkgutil.walk_packages(package.__path__, prefix))


def module_values[Value](module: ModuleType, kind: type[Value]) -> list[Value]:
    """Return the values of the kind the module holds."""
    return [value for value in module.__dict__.values() if isinstance(value, kind)]


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
        for logger in all_loggers(update_time):
            logger.forget_shown_changelogs()


def _all_cached_functions(package: ModuleType) -> list[_Cached]:
    """Return every cached function the package's modules hold, recognised by the `cache_clear` their decorator adds."""
    return [
        value
        for module in _package_modules(package)
        for value in module.__dict__.values()
        if hasattr(value, "cache_clear")
    ]


@cache
def all_loggers(package: ModuleType) -> tuple[Logger, ...]:
    """Return the loggers the package's modules hold, as module constants or as attributes of their classes.

    A module creates its loggers when it is imported, so the package's modules are scanned for them once.
    """
    values = [value for module in _package_modules(package) for value in module.__dict__.values()]
    attributes = [attribute for value in values if isinstance(value, type) for attribute in value.__dict__.values()]
    return tuple(value for value in values + attributes if isinstance(value, Logger))


class LoggingTestCase(CacheClearingTestCase):
    """Base test case for any test of code that logs, with the logger's log method mocked as `self.mock_log`."""

    @override
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

    def _assert_no_unexpected_error(self) -> None:
        """Fail if the run logged an error that it did not explicitly expect via assert_error_logged."""
        if not self._error_expected:
            self.assertEqual(self.records(ERROR), [])

    def records(self, level: int) -> list[_Call]:
        """Return the records logged at the level, each as the message and the fields it carries, changes folded in."""
        return [record for record_level, record in self._records_in_order() if record_level == level]

    def _records_in_order(self) -> list[tuple[int, _Call]]:
        """Return each record logged, in order, with its level, as the message and the fields it carries."""
        return [
            (args[0], call(args[1], args[2] | logged_extra(kwargs))) for args, kwargs in self.mock_log.call_args_list
        ]

    @override
    @contextlib.contextmanager
    def subTest(self, *args: object, **kwargs: object) -> Generator[None]:
        """Run the case as a run of its own, so a table cannot forget to start one."""
        self._start_new_run()
        with super().subTest(*args, **kwargs):
            yield
            self._assert_no_unexpected_error()
        self._start_new_run()  # The case checked its own run, so tearDown has nothing of it left to check again

    @override
    def _start_new_run(self) -> None:
        """Forget what the previous run logged and expected as well, so the next run starts afresh."""
        super()._start_new_run()
        self.mock_log.reset_mock()
        self._error_expected = False  # Set by assert_error_logged; the run fails on an error the test didn't expect.

    def assert_path_logged_first(self, path: Path) -> None:
        """Assert that the first record at the path's location says it is checked, and is the only one saying so."""
        checked_line = self._checked_call(path)
        location = checked_line.args[1]["location"]
        records = [record for _, record in self._records_in_order() if record.args[1].get("location") == location]
        self.assertEqual(records[:1], [checked_line])
        self.assertEqual(records.count(checked_line), 1)

    def _checked_call(self, path: Path) -> _Call:
        """Return the call the logger makes for the line saying the path is checked."""
        return self._expected_call(message_of(Logger.path), {"location": Location(path)})

    @staticmethod
    def _expected_call(message: _LogMessage, fields: _Fields) -> _Call:
        """Return the call the logger makes for the message, with its fields passed by name.

        The fields are rendered the way the logger renders them, so an assertion here names the plain domain values
        — a `Location` rather than its delimiter-wrapped text.
        """
        return call(message, Logger._rendered(fields))

    def assert_debug_logged_after_path(self, path: Path, message: _LogMessage, **fields: _FieldValue) -> None:
        """Assert that the path was logged as checked, and then the message about it, as the only records at DEBUG."""
        message_record = self._expected_call(message, {"location": Location(path), **fields})
        self.assertEqual(self.records(DEBUG), [self._checked_call(path), message_record])

    def assert_warnings_logged(self, *warnings: tuple[_LogMessage, Mapping[str, _FieldValue]]) -> None:
        """Assert the warnings were the run's only warnings, in this order."""
        expected = [self._expected_call(message, dict(fields)) for message, fields in warnings]
        self.assertEqual(self.records(WARNING), expected)

    def assert_new_version_logged(
        self, dependency: str, version: str, location: Location, note: str = Logger._NO_CHANGELOG, *, changes: str = ""
    ) -> None:
        """Assert that a new version was the only record logged for the dependency in the file."""
        fields = self._new_version_fields(dependency, version, location, note, changes)
        self.assert_logged(message_of(Logger.new_version), **fields)

    @staticmethod
    def _new_version_fields(
        dependency: str, version: str, location: Location, note: str = Logger._NO_CHANGELOG, changes: str = ""
    ) -> _Fields:
        """Return the fields the logger logs an available new version with: the changes, or else the note, below it."""
        below = {CHANGES: changes} if changes else {NOTE: note}
        return {"dependency": dependency, "location": location, "version": version, **below}

    def assert_logged(self, message: _LogMessage, **fields: _FieldValue) -> None:
        """Assert the message was the only record logged at its level, with the given fields."""
        self.assertEqual(self.records(message.level), [self._expected_call(message, fields)])

    def assert_last_new_version_logged(
        self, dependency: str, version: str, location: Location, note: str = Logger._NO_CHANGELOG
    ) -> None:
        """Assert that a new version was the most recent record, for a run that logs several."""
        fields = self._new_version_fields(dependency, version, location, note)
        self._assert_last_logged(message_of(Logger.new_version), **fields)

    def _assert_last_logged(self, message: _LogMessage, **fields: _FieldValue) -> None:
        """Assert the message was the most recent record at its level, with the given fields."""
        self.assertEqual(self.records(message.level)[-1:], [self._expected_call(message, fields)])

    def assert_new_version_logged_among_others(
        self, dependency: str, version: str, location: Location, note: str = Logger._NO_CHANGELOG, *, changes: str = ""
    ) -> None:
        """Assert that a new version was logged, among the other records of a run that logs several."""
        fields = self._new_version_fields(dependency, version, location, note, changes)
        self.assert_logged_among_others(message_of(Logger.new_version), **fields)

    def assert_logged_among_others(self, message: _LogMessage, **fields: _FieldValue) -> None:
        """Assert the message was logged with the given fields, among the other records at its level."""
        self.assertIn(self._expected_call(message, fields), self.records(message.level))

    def assert_new_versions_logged(self, version: str, location: Location, *dependencies: str) -> None:
        """Assert that the dependencies, and no others, were reported at the location with the new version, in order."""
        expected = [
            self._expected_call(message_of(Logger.new_version), self._new_version_fields(dependency, version, location))
            for dependency in dependencies
        ]
        self.assertEqual(self.new_version_records(), expected)

    def new_version_records(self) -> list[_Call]:
        """Return the records reporting an available new version, ignoring the other records at their level."""
        return self.records_of(message_of(Logger.new_version))

    def records_of(self, message: _LogMessage, **fields: _FieldValue) -> list[_Call]:
        """Return the records logged for the message that carry the fields, ignoring the other records at its level."""
        rendered = Logger._rendered(fields)
        return [
            record
            for record in self.records(message.level)
            if record.args[:1] == (message,) and rendered.items() <= record.args[1].items()
        ]

    def assert_moved_back_logged(
        self, manager: str, dependency: str, version: str, new_version: str, *locations: Location
    ) -> None:
        """Assert that the manager moving the version back, at each location, was all the run logged at its level."""
        message = message_of(Logger.version_moved_back)
        fields = {"manager": manager, "dependency": dependency, "version": version, "new_version": new_version}
        expected = [self._expected_call(message, {**fields, "location": location}) for location in locations]
        self.assertEqual(self.records(message.level), expected)

    def assert_project_cooldown_logged(self, path: pathlib.Path, setting: str) -> None:
        """Assert that the setting is logged as holding the cooldown of the file, rather than the run's cooldown."""
        self.assert_logged_among_others(
            message_of(Logger.project_cooldown), location=Location(path), days=COOLDOWN.default, setting=setting
        )

    def assert_no_new_version_logged(self) -> None:
        """Assert that no new version was logged (other records at the same level are allowed)."""
        self.assert_none_logged(message_of(Logger.new_version), "new version")

    def assert_none_logged(self, message: _LogMessage, what: str, **fields: _FieldValue) -> None:
        """Assert the message was not logged at all, whatever else was logged at its level."""
        self.assertEqual(self.records_of(message, **fields), [], f"Expected no {what} to be logged")

    def assert_no_reference_left_as_is_logged(self) -> None:
        """Assert that the run did not report a reference left as it is (other records are allowed)."""
        self.assert_none_logged(message_of(Logger.reference_left_as_is), "reference left as it is")

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
            hash=f"@{sha}",
        )

    def _assert_reported(self, message: _LogMessage, *, among_others: bool, **fields: _FieldValue) -> None:
        """Assert the message was logged with the given fields, as the only record at its level unless among others."""
        (self.assert_logged_among_others if among_others else self.assert_logged)(message, **fields)

    def assert_unpinned_ref_logged(
        self, dependency: str, name: str, location: Location, reason: str, *, ref: RefKind = RefKind.BRANCH
    ) -> None:
        """Assert that a ref left unpinned was reported, with the reason, as the run's only error."""
        self.assert_error_logged(
            message_of(Logger.unpinned_ref), dependency=dependency, ref=ref, name=name, location=location, reason=reason
        )

    def assert_error_logged(self, message: _LogMessage, **fields: _FieldValue) -> None:
        """Assert an error was logged, and mark it expected so tearDown's strict no-error check passes."""
        self.assert_errors_logged((message, fields))

    def assert_errors_logged(self, *errors: tuple[_LogMessage, _Fields]) -> None:
        """Assert the errors were the run's only errors, in this order, and mark them expected."""
        self._error_expected = True
        self.assertEqual(self.records(ERROR), [self._expected_call(message, fields) for message, fields in errors])

    def assert_invalid_json_logged(self, url: str, *, among_others: bool = False) -> None:
        """Assert that an answer that is not JSON was reported, naming the URL, as the run's only warning by default."""
        self._assert_reported(message_of(Logger.invalid_response), among_others=among_others, url=url, format="JSON")

    def assert_unpinned_floating_tag_logged(
        self, dependency: str, version: str, location: Location, reason: FloatingPin, looked_up: str = ""
    ) -> None:
        """Assert that a floating tag pinned to no version was reported, with its reason, among the other records.

        The reason names the tag looked up, which is the version unless the reference names none.
        """
        self.assert_logged_among_others(
            message_of(Logger.reference_left_as_is),
            dependency=dependency,
            tag=tag_of(version),
            location=location,
            reason=reason.explained(looked_up or version),
        )

    def assert_kept_floating_logged(  # noqa: PLR0913 — the fields the message carries
        self,
        dependency: str,
        version: str,
        release: DependencyVersion,
        location: Location,
        directive: str = ANY,
        *,
        ref: RefKind | None = None,
    ) -> None:
        """Assert that a floating pin left as it is was reported: a floating image tag, or else a ref of that kind."""
        self.assert_logged_among_others(
            message_of(Logger.keeping_floating_pin),
            dependency=dependency,
            pin="floating tag" if ref is None else ref,
            name=tag_of(version, ":" if ref is None else "@"),
            resolved=release.version,
            sha=release.sha,
            location=location,
            directive=directive,
        )

    def assert_comment_naming_no_ref_logged(self, dependency: str, name: str, location: Location) -> None:
        """Assert that a commit SHA whose comment does not name a ref was reported as left as it is."""
        self.assert_logged_among_others(
            message_of(Logger.reference_left_as_is),
            dependency=dependency,
            tag="",
            location=location,
            reason=_unnamed_ref_reason(name),
        )

    def assert_left_as_is_logged(
        self, dependency: str, tag: str, location: Location, reason: ImageNotLookedUp | Unserved
    ) -> None:
        """Assert that a reference left as it is was reported, with its reason, among others."""
        self.assert_logged_among_others(
            message_of(Logger.reference_left_as_is),
            dependency=dependency,
            tag=tag_of(tag),
            location=location,
            reason=reason,
        )

    def assert_cannot_pin_logged(self, dependency: str, location: Location) -> None:
        """Assert that a reference with nowhere to hold a hash was reported as one that cannot be pinned."""
        self.assert_logged(message_of(Logger.cannot_pin), dependency=dependency, location=location)

    def assert_drift_logged(self, drifted: DriftedPin, *, among_others: bool = False) -> None:
        """Assert that the drift was logged, as the file's only warning by default."""
        message, fields = drift_warning(drifted)
        self._assert_reported(message, among_others=among_others, **fields)

    def assert_adopted_drift_logged(
        self, drifted: DriftedPin, directive: str = ANY, *, among_others: bool = False
    ) -> None:
        """Assert that the adopted drift was logged, as the file's only record at its level by default."""
        message, fields = adopted_drift_report(drifted, directive)
        self._assert_reported(message, among_others=among_others, **fields)

    def assert_undated_drift_logged(self, drifted: DriftedPin, reason: str) -> None:
        """Assert that what the drifted pin now points at could not be dated, logged as the file's only error."""
        self.assert_error_logged(message_of(Logger.undated_drift), **Logger._undated_drift_fields(drifted, reason))

    def assert_hash_mismatch_logged(
        self, dependency: str, version: str, declared_hash: str, served_hash: str, location: Location
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
        fields: _Fields = {"dependency": dependency, "version": version, "days": ANY, "threshold": ANY}
        message = message_of(Logger.stale_dependency)
        expected = [self._expected_call(message, fields | {"location": location}) for location in locations]
        stale_warnings = self.records_of(message)
        self.assertEqual(stale_warnings if among_others else self.records(message.level), expected)

    def assert_yanked_dependency_logged(
        self, dependency: str, version: str, location: Location, reason: Yank = ANY
    ) -> None:
        """Assert that a pin left on a yanked version was warned about once for the file."""
        self.assert_logged(
            message_of(Logger.yanked_dependency),
            dependency=dependency,
            location=location,
            version=version,
            reason=reason,
        )

    def assert_archived_repository_logged(self, dependency: str, location: Location) -> None:
        """Assert that a repository GitHub or Docker Hub declares archived was warned about, neither giving a reason."""
        self.assert_archived_dependency_logged(dependency, location, reason="", subject=ArchivedSubject.REPOSITORY)

    def assert_archived_dependency_logged(
        self,
        dependency: str,
        location: Location,
        reason: str = ANY,
        *,
        subject: ArchivedSubject = ArchivedSubject.PROJECT,
        among_others: bool = False,
    ) -> None:
        """Assert that an archived dependency was warned about, as the file's only warning by default."""
        self._assert_reported(
            message_of(Logger.archived_dependency),
            among_others=among_others,
            dependency=dependency,
            location=location,
            subject=subject,
            reason=reason,
        )

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

    def assert_redundant_vulnerable_advisory_logged(
        self, dependency: str, version: str, location: Location, directive: str = ANY
    ) -> None:
        """Assert that a suppression naming an advisory the version does not have was warned about, among the others."""
        reason = _finding(Scope.VULNERABLE, version)
        self.assert_redundant_directive_logged(reason, dependency, location, directive, among_others=True)

    def assert_redundant_directive_logged(
        self,
        reason: RedundancyReason | str,
        dependency: str,
        location: Location,
        directive: str = ANY,
        *,
        among_others: bool = False,
    ) -> None:
        """Assert that a warning reports the directive as redundant, saying why, as the only warning by default."""
        self._assert_reported(
            message_of(Logger.redundant_directive),
            among_others=among_others,
            directive=directive,
            dependency=dependency,
            location=location,
            reason=reason,
        )

    def assert_redundant_vulnerable_level_logged(  # noqa: PLR0913 — the fields of the warning
        self,
        dependency: str,
        version: str,
        level: str,
        location: Location,
        directive: str = ANY,
        *,
        run_level: str | None = None,
    ) -> None:
        """Assert that a risk level judging the vulnerabilities as the run's does was warned about, among others."""
        reason = _risk_level_reason(level, run_level or VULNERABILITY_LEVEL.default, version)
        self.assert_redundant_directive_logged(reason, dependency, location, directive, among_others=True)

    def assert_no_redundant_suppression_logged(self) -> None:
        """Assert that no vulnerability suppression was reported as silencing nothing, whatever else was logged."""
        self.assertEqual(
            self._redundant_directives_naming("vulnerab"), [], "A vulnerability suppression was reported as redundant"
        )

    def _redundant_directives_naming(self, word: str) -> list[_Call]:
        """Return the records reporting a directive as redundant whose directive names the word."""
        records = self.records_of(message_of(Logger.redundant_directive))
        return [record for record in records if word in str(record.args[1]["directive"])]

    def assert_no_redundant_vulnerable_level_logged(self) -> None:
        """Assert that no risk level was warned about as redundant."""
        self.assertEqual(
            self._redundant_directives_naming("vulnerability-level"), [], "A risk level was reported as redundant"
        )

    def assert_no_redundant_directive_logged(self) -> None:
        """Assert that no directive was reported as redundant, whatever else was logged."""
        self.assert_none_logged(message_of(Logger.redundant_directive), "redundant directive")

    def assert_path_logged(self, path: Path) -> None:
        """Assert that the path being checked for updates was logged."""
        self.assertEqual(self.records(DEBUG)[-1:], [self._checked_call(path)])

    def assert_no_debug_logged(self) -> None:
        """Assert that nothing was logged at DEBUG."""
        self.assertEqual(self.records(DEBUG), [])

    def assert_recognised_marker_logged(self, dependency: str, location: Location, marker: Marker) -> None:
        """Assert that a reference's marker was reported as recognised, among the other records at its level."""
        self.assert_logged_among_others(
            message_of(Logger.recognised_marker), directives=marker, dependency=dependency, location=location
        )

    def assert_recognised_marker_spelled(self, *spellings: str) -> None:
        """Assert that the markers reported as recognised are spelled as given, in that order."""
        records = self.records_of(message_of(Logger.recognised_marker))
        self.assertEqual([str(record.args[1]["directives"]) for record in records], list(spellings))

    def assert_invalid_bracket_item_logged(
        self, dependency: str, location: Location, item: str, *, among_others: bool = False
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
        self, dependency: str, location: Location, directive: str = ANY, *, among_others: bool = False
    ) -> None:
        """Assert that ignoring a reference's update was logged, by default as the last record."""
        assert_logged = self.assert_logged_among_others if among_others else self._assert_last_logged
        fields: _Fields = {
            "dependency": dependency,
            "location": location,
            "versions": "every version",
            "directive": directive,
        }
        assert_logged(message_of(Logger.held_back), **fields)

    def assert_no_ignored_logged(self) -> None:
        """Assert that no update was reported as held back by a marker, whatever else was logged at that level."""
        self.assert_none_logged(message_of(Logger.held_back), "update held back by a marker", versions="every version")

    def assert_held_back_by_cooldown_logged(
        self, dependency: str, version: str, location: Location, *, days: int = COOLDOWN.default, directive: str = ""
    ) -> None:
        """Assert that the version published at `FRESH_DATE` was logged as held back, among the other records."""
        fields: _Fields = {
            "dependency": dependency,
            "location": location,
            "version": version,
            "published": utc_date_and_time(FRESH_DATE),
            "days": days,
            "until": utc_date_and_time(FRESH_DATE + timedelta(days=days)),
            "set_by": f" (update-time: {directive})" if directive else "",
        }
        self.assert_logged_among_others(message_of(Logger.held_back_by_cooldown), **fields)

    def assert_no_staleness_warning_logged(self, dependency: str, location: Location) -> None:
        """Assert that the dependency at the location was not warned about as stale."""
        message = message_of(Logger.stale_dependency)
        self.assert_none_logged(message, "staleness warning", dependency=dependency, location=location)

    def assert_no_ignored_staleness_logged(self) -> None:
        """Assert that no staleness warning was reported as silenced, whatever else was logged at that level."""
        self.assert_none_logged(
            message_of(Logger.silenced_warning), "staleness warning silenced by a marker", warning="staleness"
        )

    def assert_no_ignored_vulnerability_logged(self) -> None:
        """Assert that no vulnerability warning was reported as silenced, whatever else was logged at that level."""
        silenced = self._silenced_vulnerabilities("update-time:")
        self.assertEqual(silenced, [], "Expected that a marker did not silence a vulnerability warning")

    def _silenced_vulnerabilities(self, cause: str) -> list[_Call]:
        """Return the records reporting a silenced vulnerability warning, whose cause starts with the given text."""
        silenced = self.records_of(message_of(Logger.silenced_warning))
        return [
            record
            for record in silenced
            if record.args[1]["warning"].endswith(" vulnerability") and record.args[1]["cause"].startswith(cause)
        ]

    def assert_ignored_vulnerability_logged(
        self, dependency: str, location: Location, advisory: str, directive: str
    ) -> None:
        """Assert that a vulnerability warning silenced by a marker was logged, among the other records."""
        self.assert_logged_among_others(
            message_of(Logger.silenced_warning),
            dependency=dependency,
            location=location,
            warning=_WARNINGS[Scope.VULNERABLE].format(advisory=advisory),
            cause=f"update-time: {directive}",
        )

    def assert_no_globally_ignored_vulnerability_logged(self) -> None:
        """Assert that no vulnerability warning was reported as silenced run-wide, whatever else was logged."""
        silenced = self._silenced_vulnerabilities("--ignore-vulnerability")
        self.assertEqual(silenced, [], "Expected that the option did not silence a vulnerability warning")

    def assert_globally_ignored_vulnerability_logged(
        self, dependency: str, location: Location, advisory: str, identifiers: str
    ) -> None:
        """Assert that a vulnerability warning silenced run-wide was logged, among the other records at its level."""
        self.assert_logged_among_others(
            message_of(Logger.silenced_warning),
            dependency=dependency,
            location=location,
            warning=_WARNINGS[Scope.VULNERABLE].format(advisory=advisory),
            cause=f"--ignore-vulnerability {identifiers}",
        )

    def assert_ignored_warning_logged(self, scope: Scope, dependency: str, location: Location, directive: str) -> None:
        """Assert that the warning the scope steers was logged as silenced by a marker, among the other records."""
        self.assert_logged_among_others(
            message_of(Logger.silenced_warning),
            dependency=dependency,
            location=location,
            warning=_WARNINGS[scope],
            cause=f"update-time: {directive}",
        )

    def assert_redundant_ignore_archived_logged(self, dependency: str, location: Location) -> None:
        """Assert that an `ignore[archived]` on a repository that is not archived was warned about as redundant."""
        reason = _finding(Scope.ARCHIVED, ArchivedSubject.REPOSITORY)
        self.assert_redundant_directive_logged(reason, dependency, location, "ignore[archived]")

    def assert_unsupported_package_manager_logged(self, path: Path, manager: str, supported: str) -> None:
        """Assert that an unsupported package manager was logged for the file."""
        self.assert_logged(
            message_of(Logger.manager_cannot_run),
            location=Location(path),
            manager=manager,
            reason=_unsupported_manager_reason(supported),
        )

    def assert_invalid_file_logged(self, path: Path, file_format: str) -> None:
        """Assert that a file that does not parse was logged for the file, naming its format."""
        self.assert_logged(message_of(Logger.invalid_file), location=Location(path), format=file_format)

    def assert_unreadable_file_logged(self, path: Path, content: FileContent, error: str | Exception) -> None:
        """Assert that a file that could not be read was logged, naming what the run read it for and the error."""
        self.assert_logged(
            message_of(Logger.unreadable_file), content=content, location=Location(path), error=str(error)
        )

    def assert_tool_not_on_path_logged(self, path: Path, executable: str) -> None:
        """Assert that an executable missing from the path was logged at error, for the update of the file."""
        self.assert_error_logged(
            message_of(Logger.tool_failed),
            task=ToolTask.UPDATE,
            location=Location(path),
            tool=executable,
            failure=ToolFailure.NOT_ON_PATH,
        )

    def assert_could_not_fetch_logged(self, *urls: str, status: int = ANY, reason: str = ANY) -> None:
        """Assert that the run's only warnings say each of the URLs could not be fetched, a single one by default."""
        message = message_of(Logger.request_failed)
        failure = ANY if status is ANY else _StartingWith(f"HTTP {status} {'' if reason is ANY else str(reason)}")
        fields: list[_Fields] = [{"url": url, "failure": failure} for url in urls or (ANY,)]
        self.assertEqual(self.records(message.level), [self._expected_call(message, each) for each in fields])

    def assert_command_failed_logged(self, command: Command, output: str) -> None:
        """Assert that a single 'command failed' error was logged, for the given command and output."""
        self.assert_error_logged(message_of(Logger.command_failed), command=command, output=output)

    def assert_command_stderr_logged(self, command: Command = ANY, stderr: str = ANY) -> None:
        """Assert that a single 'command wrote to stderr' warning was logged, optionally for a given command/stderr."""
        self.assert_logged(message_of(Logger.command_stderr), command=command, stderr=stderr)

    def assert_uv_dependencies_not_updated_logged(self, path: Path, tool: str) -> None:
        """Assert that the file's dependencies were reported as not updated, because the uv command failed."""
        self.assert_error_logged(
            message_of(Logger.tool_failed),
            location=Location(path),
            task=ToolTask.UPDATE,
            tool=tool,
            failure=ToolFailure.FAILED,
        )

    def assert_no_info_logged(self) -> None:
        """Assert that nothing was logged at INFO."""
        self.assertEqual(self.records(INFO), [])

    def assert_no_warnings_logged(self) -> None:
        """Assert that no warnings were logged."""
        self.assertEqual(self.records(WARNING), [])


class _StartingWith(str):
    """A string that equals any string starting with it, so an assertion can leave the end of a field open."""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        """Return whether the other string starts with this one."""
        return isinstance(other, str) and other.startswith(str(self))

    __hash__ = str.__hash__


class ExecutablesOnPathTestCase(LoggingTestCase):
    """A test case whose path holds every executable, so the tests run the same whatever the machine installed."""

    @override
    def setUp(self) -> None:
        """Find every executable on the path."""
        super().setUp()
        self.which = self.enterContext(patch("shutil.which", side_effect=lambda executable: f"/bin/{executable}"))

    def leave_off_the_path(self, *missing: str) -> None:
        """Find every executable on the path except the missing ones."""
        self.which.side_effect = lambda executable: None if executable in missing else f"/bin/{executable}"


class MockLoggerTestCase(unittest.TestCase):
    """Base for the tests that assert what the code under test logs to a mock logger."""

    @override
    def setUp(self) -> None:
        """Start each test with a fresh mock logger and path."""
        super().setUp()
        self.logger = Mock()
        self.path = Mock()

    @override
    @contextlib.contextmanager
    def subTest(self, *args: object, **kwargs: object) -> Generator[None]:
        """Run the case on calls of its own, so a table cannot forget to reset the logger."""
        self.logger.reset_mock()
        with super().subTest(*args, **kwargs):
            yield
