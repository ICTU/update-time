"""Shared test helpers."""

import ast
import contextlib
import importlib
import pathlib
import pkgutil
import tempfile
import unittest
from functools import cache
from http import HTTPStatus
from logging import DEBUG, ERROR, WARNING
from typing import TYPE_CHECKING, Protocol, cast
from unittest.mock import ANY, Mock, call, patch

import update_time
from update_time.domain.bound import NewVersionGetter, Verb, VersionBound, parse_bound
from update_time.domain.dependency import DependencyVersion, VersionString
from update_time.domain.reference import Reference, ResolvedReference
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.vulnerability import NO_RISK_LEVEL, WARN_VULNERABILITY_LEVEL, Vulnerability
from update_time.io.log import Logger, LogMessage, reset_changelog_suppression
from update_time.primitives.location import Location

from tests.helpers import mock_response, patch_environ
from tests.update_time.fixtures import COMMIT_SHA

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Mapping, Sequence
    from pathlib import Path
    from types import ModuleType
    from unittest.mock import _Call, _patch

    from update_time.domain.drift import DriftedPin


def _module_level_assignments(tree: ast.Module) -> Iterator[tuple[list[ast.expr], ast.expr | None]]:
    """Yield the targets and assigned value of each module-level assignment, annotated or not.

    An annotation without a value (`NAME: int`) assigns nothing, so its value is None.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            yield node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            yield [node.target], node.value


@contextlib.contextmanager
def _project(files: dict[str, str]) -> Generator[str]:
    """Yield the path of a directory holding the given files, so a rule or a check has a project to scan.

    Both are exercised against a project written for the purpose, since the tree they normally scan holds no
    violation to find. Each key is a path relative to the directory, and the folders it names are created.
    """
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

    Walking reads the filesystem, which is too slow to repeat before each of the hundreds of tests that clear the
    caches. What each module holds is read afresh every time, so a cache added to one is still found. This cache
    survives the clearing itself as long as the module defining it sits outside `package`.
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
    """Return the cached functions the module defines, recognised by the `cache_clear` their decorator adds.

    A cached function the module imported is left to the module defining it, so that scanning every module clears
    each cache once.
    """
    return [
        value
        for value in vars(module).values()
        if hasattr(value, "cache_clear") and getattr(value, "__module__", None) == module.__name__
    ]


class CacheClearingTestCase(unittest.TestCase):
    """Base test case that resets global state before each test to prevent cross-test leakage.

    This clears the functools caches and the loggers' changelog-suppression state. The caches are discovered by
    scanning the package, so adding a `@cache` to a source module needs nothing added here.
    """

    def setUp(self) -> None:
        """Clear all caches and logger state so each test gets fresh results."""
        super().setUp()
        for cached_function in _all_cached_functions(update_time):
            cached_function.cache_clear()
        reset_changelog_suppression()


class LoggingTestCase(CacheClearingTestCase):
    """Base test case for any test of code that logs.

    It mocks the logger's log method, exposed as the mock_log attribute, and offers the assert_*_logged helpers below.
    This spares tests from patching (and threading through method arguments) the log method, and from silencing
    expected diagnostics by hand. The mock lives for the whole test, so a `subTest` table whose cases each assert on
    the records of their own run resets it between them with `self.mock_log.reset_mock()`.
    """

    def setUp(self) -> None:
        """Start the logger patch and register its cleanup."""
        super().setUp()
        patcher = patch("logging.Logger.log")
        self.addCleanup(patcher.stop)
        self.mock_log = patcher.start()
        self._error_expected = False  # Set by assert_error_logged; tearDown fails on an error the test didn't expect.

    def tearDown(self) -> None:
        """Fail the test if an error was logged that it did not explicitly expect via assert_error_logged.

        An error log almost always means something genuinely broke, and only a handful of tests expect one, so
        'no error unless expected' is enforced by default here rather than left to each test to assert (warnings,
        which are common and often expected, stay opt-in via assert_no_warnings_logged).
        """
        super().tearDown()
        if not self._error_expected:
            self.assertEqual(self.records(ERROR), [])

    def records(self, level: int) -> list[_Call]:
        """Return the records logged at the level, as the arguments they were logged with, without the level itself.

        Every record goes through the one patched log method, so a test that cares about one level filters the calls
        by it. Dropping the level from each call lets the assertions below read as the message and its arguments.
        """
        return [call(*args[1:], **kwargs) for args, kwargs in self.mock_log.call_args_list if args[0] == level]

    @staticmethod
    def _expected_call(message: LogMessage, fields: dict[str, object]) -> _Call:
        """Return the call the logger makes for the message, with its fields passed by name.

        The fields are rendered the way the logger renders them, so an assertion here names the plain domain values
        — a `Location` rather than its delimiter-wrapped text — and which fields carry a delimiter is asserted where
        that rendering itself is tested, in the logger's unit tests.
        """
        return call(message, Logger._rendered(fields))

    def assert_logged(self, message: LogMessage, **fields: object) -> None:
        """Assert the message was the only record logged at its level, with the given fields."""
        self.assertEqual(self.records(message.level), [self._expected_call(message, fields)])

    def assert_last_logged(self, message: LogMessage, **fields: object) -> None:
        """Assert the message was the most recent record at its level, with the given fields."""
        self.assertEqual(self.records(message.level)[-1:], [self._expected_call(message, fields)])

    def assert_logged_among_others(self, message: LogMessage, **fields: object) -> None:
        """Assert the message was logged with the given fields, among the other records at its level."""
        self.assertIn(self._expected_call(message, fields), self.records(message.level))

    def assert_error_logged(self, message: LogMessage, **fields: object) -> None:
        """Assert an error was logged, and mark it expected so tearDown's strict no-error check passes."""
        self._error_expected = True
        self.assert_logged(message, **fields)

    @staticmethod
    def _new_version_fields(dependency: str, version: str, location: Location, changes: str) -> dict[str, object]:
        """Return the fields the logger logs an available new version with."""
        return {"dependency": dependency, "location": location, "version": version, "changes": changes}

    def assert_new_version_logged(
        self, dependency: str, version: str, location: Location, changes: str = Logger._NO_CHANGELOG
    ) -> None:
        """Assert that a new version was the only record logged for the dependency in the file."""
        fields = self._new_version_fields(dependency, version, location, changes)
        self.assert_logged(Logger._MESSAGE_NEW_VERSION, **fields)

    def assert_last_new_version_logged(
        self, dependency: str, version: str, location: Location, changes: str = Logger._NO_CHANGELOG
    ) -> None:
        """Assert that a new version was the most recent record, for a run that logs several."""
        fields = self._new_version_fields(dependency, version, location, changes)
        self.assert_last_logged(Logger._MESSAGE_NEW_VERSION, **fields)

    def assert_new_version_logged_among_others(
        self, dependency: str, version: str, location: Location, changes: str = Logger._NO_CHANGELOG
    ) -> None:
        """Assert that a new version was logged, among the other records of a run that logs several."""
        fields = self._new_version_fields(dependency, version, location, changes)
        self.assert_logged_among_others(Logger._MESSAGE_NEW_VERSION, **fields)

    def records_of(self, message: LogMessage) -> list[_Call]:
        """Return the records logged for the message, ignoring the other records at its level."""
        return [record for record in self.records(message.level) if record.args[:1] == (message,)]

    def new_version_records(self) -> list[_Call]:
        """Return the records reporting an available new version, ignoring the other records at their level."""
        return self.records_of(Logger._MESSAGE_NEW_VERSION)

    def assert_none_logged(self, message: LogMessage, what: str) -> None:
        """Assert the message was not logged at all, whatever else was logged at its level."""
        self.assertEqual(self.records_of(message), [], f"Expected no {what} to be logged")

    def assert_no_new_version_logged(self) -> None:
        """Assert that no new version was logged (other records at the same level are allowed)."""
        self.assert_none_logged(Logger._MESSAGE_NEW_VERSION, "new version")

    def assert_pinned_logged(self, dependency: str, version: str, sha: str, location: Location) -> None:
        """Assert that pinning a previously unpinned reference to a digest was logged for the file."""
        self.assert_last_logged(
            Logger._MESSAGE_PINNED, dependency=dependency, location=location, version=version, sha=sha
        )

    def assert_cannot_pin_logged(self, dependency: str, location: Location) -> None:
        """Assert that a reference with nowhere to hold a hash was reported as one that cannot be pinned."""
        self.assert_logged(Logger._MESSAGE_CANNOT_PIN, dependency=dependency, location=location)

    def assert_digest_drift_logged(self, drifted: DriftedPin) -> None:
        """Assert that a re-pushed tag's digest drift was logged as a single warning for the file."""
        self.assert_logged(Logger._MESSAGE_DIGEST_DRIFT, **Logger._drift_fields(drifted))

    def assert_tag_drift_logged(self, drifted: DriftedPin) -> None:
        """Assert that a moved tag's commit drift was logged as a single warning for the file."""
        self.assert_logged(Logger._MESSAGE_TAG_DRIFT, **Logger._drift_fields(drifted))

    def assert_adopted_tag_drift_logged(self, drifted: DriftedPin, cause: object = ANY) -> None:
        """Assert that adopting a moved tag's new commit was logged once for the file."""
        self.assert_logged(Logger._MESSAGE_ADOPTED_TAG_DRIFT, **Logger._drift_fields(drifted), cause=cause)

    def assert_adopted_digest_drift_logged(self, drifted: DriftedPin, cause: object = ANY) -> None:
        """Assert that adopting a re-pushed tag's new digest was logged once for the file."""
        self.assert_logged(Logger._MESSAGE_ADOPTED_DIGEST_DRIFT, **Logger._drift_fields(drifted), cause=cause)

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
            Logger._MESSAGE_HASH_MISMATCH,
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
        expected = [
            self._expected_call(Logger._MESSAGE_STALE, fields | {"location": location}) for location in locations
        ]
        stale_warnings = self.records_of(Logger._MESSAGE_STALE)
        self.assertEqual(stale_warnings if among_others else self.records(Logger._MESSAGE_STALE.level), expected)

    def assert_yanked_dependency_logged(
        self, dependency: str, version: str, location: Location, reason: object = ANY
    ) -> None:
        """Assert that a pin left on a yanked version was warned about once for the file.

        The warning carries the version's `Yank`, which most callers don't care about, so it defaults to matching any.
        """
        self.assert_logged(
            Logger._MESSAGE_YANKED, dependency=dependency, location=location, version=version, reason=reason
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
        assert_logged = self.assert_logged_among_others if among_others else self.assert_logged
        reference = Reference(dependency, version, location)
        assert_logged(Logger._MESSAGE_VULNERABLE_DEPENDENCY, **Logger._vulnerability_fields(reference, vulnerability))

    def assert_inverted_vulnerable_item_logged(self, dependency: str, item: str, location: Location) -> None:
        """Assert that a `vulnerable` item comparing the wrong way round was warned about, among the other records."""
        self.assert_logged_among_others(
            Logger._MESSAGE_INVERTED_VULNERABLE_ITEM, item=item, dependency=dependency, location=location
        )

    @staticmethod
    def _redundant_suppression_fields(
        dependency: str, version: str, location: Location, directive: object
    ) -> dict[str, object]:
        """Return the fields every redundant vulnerability suppression is warned with."""
        return {"directive": directive, "dependency": dependency, "location": location, "version": version}

    def assert_redundant_vulnerable_scope_logged(
        self, dependency: str, version: str, location: Location, directive: object = ANY
    ) -> None:
        """Assert that a vulnerability scope with nothing left to hold back was warned about once for the file."""
        self.assert_logged(
            Logger._MESSAGE_REDUNDANT_VULNERABLE_SCOPE,
            **self._redundant_suppression_fields(dependency, version, location, directive),
        )

    def assert_redundant_vulnerable_advisory_logged(
        self, dependency: str, version: str, location: Location, directive: object = ANY
    ) -> None:
        """Assert that a suppression naming an advisory the version does not have was warned about, among the others."""
        self.assert_logged_among_others(
            Logger._MESSAGE_REDUNDANT_VULNERABLE_ADVISORY,
            **self._redundant_suppression_fields(dependency, version, location, directive),
        )

    def assert_redundant_vulnerable_level_logged(
        self, dependency: str, version: str, level: str, location: Location, directive: object = ANY
    ) -> None:
        """Assert that a risk level no vulnerability fell below was warned about, among the other records."""
        self.assert_logged_among_others(
            Logger._MESSAGE_REDUNDANT_VULNERABLE_LEVEL,
            **self._redundant_suppression_fields(dependency, version, location, directive),
            level=level,
        )

    def assert_redundant_vulnerable_source_logged(
        self, dependency: str, location: Location, directive: object = ANY
    ) -> None:
        """Assert that a vulnerability scope the dependency's source can never honour was warned about for the file."""
        self.assert_logged(
            Logger._MESSAGE_REDUNDANT_VULNERABLE_SOURCE,
            directive=directive,
            dependency=dependency,
            location=location,
        )

    def assert_no_redundant_suppression_logged(self) -> None:
        """Assert that no vulnerability suppression was reported as holding nothing back, whatever else was logged."""
        for message in (
            Logger._MESSAGE_REDUNDANT_VULNERABLE_SCOPE,
            Logger._MESSAGE_REDUNDANT_VULNERABLE_ADVISORY,
            Logger._MESSAGE_REDUNDANT_VULNERABLE_LEVEL,
            Logger._MESSAGE_REDUNDANT_VULNERABLE_SOURCE,
        ):
            with self.subTest(message=message):
                self.assert_none_logged(message, "redundant vulnerability suppression")

    def assert_redundant_yank_scope_logged(self, dependency: str, location: Location, directive: object = ANY) -> None:
        """Assert that a yank scope the dependency's source can never honour was warned about once for the file."""
        self.assert_logged(
            Logger._MESSAGE_REDUNDANT_YANK_SCOPE, directive=directive, dependency=dependency, location=location
        )

    def assert_redundant_cooldown_item_logged(
        self, dependency: str, location: Location, directive: object = ANY
    ) -> None:
        """Assert that a cooldown the dependency's source cannot measure was warned about once for the file."""
        self.assert_logged(
            Logger._MESSAGE_REDUNDANT_COOLDOWN_ITEM, directive=directive, dependency=dependency, location=location
        )

    def assert_redundant_stale_source_logged(
        self, dependency: str, location: Location, directive: object = ANY
    ) -> None:
        """Assert that a `stale` directive the dependency's source cannot answer was warned about once for the file."""
        self.assert_logged(
            Logger._MESSAGE_REDUNDANT_STALE_SOURCE, directive=directive, dependency=dependency, location=location
        )

    def assert_path_logged(self, path: Path) -> None:
        """Assert that the path being checked for updates was logged."""
        self.assert_last_logged(Logger._MESSAGE_CHECKING_PATH, location=Location(path))

    def assert_no_path_logged(self) -> None:
        """Assert that no path being checked for updates was logged (nothing logged at debug level)."""
        self.assertEqual(self.records(DEBUG), [])

    def assert_ignored_logged(self, dependency: str, location: Location, directive: object = ANY) -> None:
        """Assert that ignoring a reference (via an update-time: ignore directive) was logged."""
        self.assert_last_logged(Logger._MESSAGE_IGNORED, dependency=dependency, location=location, directive=directive)

    def assert_ignored_staleness_logged(self, dependency: str, location: Location, directive: object = ANY) -> None:
        """Assert that a staleness warning held back by a marker was logged, among the other records."""
        self.assert_logged_among_others(
            Logger._MESSAGE_IGNORED_STALENESS, dependency=dependency, location=location, directive=directive
        )

    def assert_no_ignored_vulnerability_logged(self) -> None:
        """Assert that no vulnerability warning was reported as held back, whatever else was logged at that level."""
        self.assert_none_logged(Logger._MESSAGE_IGNORED_VULNERABILITY, "vulnerability warning held back by a marker")

    def assert_ignored_vulnerability_logged(self, dependency: str, location: Location, directive: object = ANY) -> None:
        """Assert that a vulnerability warning held back by a marker was logged, among the other records."""
        self.assert_logged_among_others(
            Logger._MESSAGE_IGNORED_VULNERABILITY, dependency=dependency, location=location, directive=directive
        )

    def assert_no_globally_ignored_vulnerability_logged(self) -> None:
        """Assert that no vulnerability warning was reported as held back run-wide, whatever else was logged."""
        self.assert_none_logged(
            Logger._MESSAGE_GLOBALLY_IGNORED_VULNERABILITY, "vulnerability warning held back run-wide"
        )

    def assert_globally_ignored_vulnerability_logged(
        self, dependency: str, location: Location, advisory: object = ANY
    ) -> None:
        """Assert that a vulnerability warning held back run-wide was logged, among the other records at its level."""
        self.assert_logged_among_others(
            Logger._MESSAGE_GLOBALLY_IGNORED_VULNERABILITY,
            dependency=dependency,
            advisory=advisory,
            location=location,
        )

    def assert_ignored_yank_logged(self, dependency: str, location: Location, directive: object = ANY) -> None:
        """Assert that a yank warning held back by a marker was logged, among the other records."""
        self.assert_logged_among_others(
            Logger._MESSAGE_IGNORED_YANK, dependency=dependency, location=location, directive=directive
        )

    def assert_skipped_logged(self, path: Path, reason: str) -> None:
        """Assert that deliberately skipping a file was logged with the given reason."""
        self.assert_logged(Logger._MESSAGE_SKIP_PATH, location=Location(path), reason=reason)

    def assert_unsupported_package_manager_logged(self, path: Path, manager: str, supported: str) -> None:
        """Assert that an unsupported package manager was logged for the file."""
        self.assert_logged(
            Logger._MESSAGE_SKIP_UNSUPPORTED, location=Location(path), manager=manager, supported=supported
        )

    def assert_invalid_pyproject_toml_logged(self, path: Path) -> None:
        """Assert that an unparsable pyproject.toml was logged for the file."""
        self.assert_logged(Logger._MESSAGE_INVALID_TOML, location=Location(path))

    def assert_could_not_fetch_logged(self, url: object = ANY, status: object = ANY, reason: object = ANY) -> None:
        """Assert that a single 'could not fetch' warning was logged, optionally for a given URL and status/reason."""
        self.assert_logged(Logger._MESSAGE_NOT_OK_RESPONSE, url=url, status=status, reason=reason)

    def assert_command_stderr_logged(self, command: object = ANY, stderr: object = ANY) -> None:
        """Assert that a single 'command wrote to stderr' warning was logged, optionally for a given command/stderr."""
        self.assert_logged(Logger._MESSAGE_COMMAND_STDERR, command=command, stderr=stderr)

    def assert_no_warnings_logged(self) -> None:
        """Assert that no warnings were logged."""
        self.assertEqual(self.records(WARNING), [])


def reference(dependency: str, location: Location, version: str = "") -> Reference:
    """Return the reference a log method is handed, for the dependency pinned at the location.

    The version defaults to empty because most messages render only the dependency and its location.
    """
    return Reference(dependency, version, location)


def resolved_reference(
    dependency: str, location: Location, release: DependencyVersion, version: str = ""
) -> ResolvedReference:
    """Return the resolved reference a check is handed: the reference at the location, and its release."""
    return ResolvedReference(dependency, version, location, release=release)


def new_version_getter(version: VersionString, sha: str = "") -> NewVersionGetter:
    """Return a new-version-getter."""
    return lambda *_args: DependencyVersion(version=version, sha=sha)


def bound(verb: Verb, item: str) -> VersionBound:
    """Return the version bound the marker item expresses, for tests that need a bound as input.

    Wraps `parse_bound` to guarantee a bound — a test only passes an invalid item by mistake — so the result can
    flow into positions typed `VersionBound` without an Optional check in every test.
    """
    version_bound = parse_bound(verb, item)
    if version_bound is None:
        message = f"Not a version bound item: {item!r}"
        raise ValueError(message)
    return version_bound


# Reusable class decorator that mocks the Docker Hub auth token request made by sources.docker_hub.api_headers
# when DOCKER_HUB_USERNAME/DOCKER_HUB_TOKEN are set, so the image updater tests never make a real network call.
mock_docker_hub_auth = patch("requests.post", Mock(return_value=mock_response({"access_token": "token"})))  # nosec[B105]


# Reusable decorator that disables the staleness check, for update tests that focus on the update flow and would
# otherwise trigger the staleness pass's own registry requests. The staleness pass has its own dedicated tests.
staleness_disabled = patch_environ({STALE_AFTER.name: "0"})


def pyproject(*specs: str) -> str:
    """Return a minimal valid pyproject.toml pinning the given dependencies, in one dependencies array."""
    dependencies = ", ".join(f'"{spec}"' for spec in specs)
    return f"[project]\ndependencies = [{dependencies}]\n"


def github_release_json(tag_name: str, **extra: object) -> dict[str, object]:
    """Return a GitHub release API result for the tag, eligible (not a draft or prerelease) unless overridden."""
    return {"draft": False, "prerelease": False, "tag_name": tag_name, "body": None, "published_at": None, **extra}


def github_tag_json(name: str, sha: str = COMMIT_SHA) -> dict[str, object]:
    """Return a GitHub tags API result for the tag, carrying the tagged commit's SHA."""
    return {"name": name, "commit": {"sha": sha}}


def github_commits_json(sha: str = COMMIT_SHA, date: str = "") -> dict[str, object]:
    """Return a GitHub commits API result carrying a tag's commit SHA and, when given, its committer date."""
    return {"sha": sha, **({"commit": {"committer": {"date": date}}} if date else {})}


def _github_api(
    releases: list | None = None, tags: list | None = None, commit: Mapping | Mock | Exception | None = None
) -> Mock:
    """Return a requests.get mock that serves the GitHub releases, tags, and commits endpoints from the arguments.

    Routing by URL keeps tests independent of the order in which the source hits the endpoints. An endpoint given
    as None fails (a non-OK response), so tests can exercise unreachable endpoints. The commits endpoint serves the
    same commit for every ref; pass a Mock to serve a full response instead of JSON (e.g. a non-OK response with an
    error body), or an exception to make the request itself fail.
    """

    def serve(url: str, **_kwargs: object) -> Mock:
        json: Mapping | list | None
        if "/commits/" in url:
            if isinstance(commit, Exception):
                raise commit
            if isinstance(commit, Mock):
                return commit
            json = commit
        else:
            json = releases if "/releases" in url else tags
        return mock_response(json, ok=json is not None, status_code=200 if json is not None else 404, url=url)

    return Mock(side_effect=serve)


def patch_github(
    releases: list | None = None, tags: list | None = None, commit: Mapping | Mock | Exception | None = None
) -> _patch:
    """Patch requests.get to serve the GitHub API endpoints from the given values (see `_github_api`)."""
    return patch("requests.get", _github_api(releases, tags, commit))


def jsdelivr_versions(*version_strings: str) -> Mock:
    """Return a mock jsDelivr package API response listing the given versions (newest first)."""
    return mock_response({"versions": [{"version": version} for version in version_strings]})


def osv_advisory(
    advisory: str,
    summary: str,
    level: str = "",
    aliases: list[str] | None = None,
    vectors: dict[str, str] | None = None,
) -> dict[str, object]:
    """Return an OSV advisory record, carrying the risk level a GitHub-reviewed advisory reports when given.

    An advisory nobody reviewed for severity carries no `database_specific` section at all, which is the shape OSV
    returns for the PYSEC records among others; it states a CVSS vector at best, which `vectors` gives per CVSS
    version. `aliases` are the identifiers the other databases name the vulnerability by.
    """
    reviewed = {"database_specific": {"severity": level}} if level else {}
    named = {"aliases": aliases} if aliases else {}
    severities = [{"type": version, "score": vector} for version, vector in (vectors or {}).items()]
    scored = {"severity": severities} if severities else {}
    return {"id": advisory, "summary": summary, **reviewed, **named, **scored}


def vulnerability(advisory: str, summary: str, level: str, aliases: list[str] | None = None) -> Vulnerability:
    """Return the vulnerability Update-time reads an advisory as, known by the aliases where it has them."""
    return Vulnerability(advisory, summary, level, f"https://osv.dev/{advisory}", frozenset(aliases or []))


def osv_vulnerability(advisory: str, summary: str, level: str) -> tuple[dict[str, object], Vulnerability]:
    """Return an OSV advisory record and the vulnerability Update-time reads it as.

    Returned as a pair because a test that needs one needs the other: the record is what the mocked API serves, and
    the vulnerability is what the warning is asserted to carry. `level` is spelled the lower-case way Update-time
    reads it, since OSV states it upper-case.
    """
    return osv_advisory(advisory, summary, level.upper()), vulnerability(advisory, summary, level)


# The advisory the updater tests pin django to a vulnerable version for, and what Update-time reads it as. Shared,
# since the requirements.txt, pyproject.toml, and inline-script tests all check the same pin against the same answer.
DJANGO_ADVISORY, DJANGO_VULNERABILITY = osv_vulnerability("GHSA-2gwj-7jmv-h26r", "SQL Injection in Django", "critical")


def _osv_response(*advisories: dict[str, object]) -> Mock:
    """Return a mock OSV response listing the advisories that affect a version."""
    return mock_response({"vulns": list(advisories)})


def osv_api(*advisories: dict[str, object]) -> Mock:
    """Return a requests.post mock serving both OSV endpoints, reporting the advisories for every version queried.

    Routing by URL keeps tests independent of how many of each request the source makes: the batch endpoint answers
    which of the queried versions are affected, and the details endpoint answers with the advisories in full.
    """

    def serve(url: str, **kwargs: object) -> Mock:
        if not url.endswith("/querybatch"):
            return _osv_response(*advisories)
        queries = cast("dict[str, list]", kwargs["json"])["queries"]
        affected = {"vulns": [{"id": advisory["id"]} for advisory in advisories]} if advisories else {}
        return mock_response({"results": [affected for _query in queries]})

    return Mock(side_effect=serve)


def osv(*advisories: dict[str, object]) -> _patch:
    """Return a patch answering OSV with the advisories affecting a version, and with none when given none."""
    return patch("requests.post", osv_api(*advisories))


def unreachable_osv() -> _patch:
    """Return a patch failing every OSV request, as an OSV a run cannot reach does."""
    status = HTTPStatus.SERVICE_UNAVAILABLE
    unreachable = mock_response(ok=False, status_code=status, reason=status.phrase, url="https://api.osv.dev")
    return patch("requests.post", Mock(return_value=unreachable))


# Reusable class decorator that answers OSV with no advisories, for update tests that focus on the update flow.
# Without it their pins are looked up at OSV for real, since nothing else in those tests patches `requests.post`.
no_vulnerabilities = osv()


# Reusable decorator that switches the vulnerability check off, for update tests that focus on the update flow and
# would otherwise trigger the vulnerability pass's own OSV request.
vulnerability_check_disabled = patch_environ({WARN_VULNERABILITY_LEVEL.name: NO_RISK_LEVEL})


def npm_registry(published: dict[str, str], deprecated: dict[str, str] | None = None) -> Mock:
    """Return a mock npm registry response mapping versions to publish times and optional deprecation messages."""
    versions = {version: {"deprecated": reason} for version, reason in (deprecated or {}).items()}
    return mock_response({"time": published, "versions": versions})


PYPI_OLD_UPLOAD = "2020-01-01T00:00:00.000000Z"  # A distribution upload time well outside the cooldown window.


def pypi_index(*versions: str, files: Sequence[Mapping[str, str | bool]] | None = None) -> Mock:
    """Return a mock PyPI Index (Simple) API response listing the versions and, when given, distribution files."""
    body: dict[str, object] = {"versions": list(versions)}
    if files is not None:
        body["files"] = files
    return mock_response(body)


def yanked_file(filename: str, *, reason: str | bool = True) -> dict[str, str | bool]:
    """Return a mock Index API distribution file entry marked as yanked (a reason string, or True for none)."""
    return {"filename": filename, "yanked": reason}


def pypi_release(upload_time: str = PYPI_OLD_UPLOAD, *, yanked: bool = False, description: str = "") -> Mock:
    """Return a mock PyPI per-version metadata response with the given upload time, yank state, and description.

    An empty `upload_time` models a release with no distribution files (an empty `urls` list).
    """
    urls = [{"upload_time_iso_8601": upload_time}] if upload_time else []
    return mock_response({"info": {"description": description, "yanked": yanked}, "urls": urls})


def docker_tag(name: str, digest: str = "", **extra: object) -> dict[str, object]:
    """Return a single-tag Docker Hub API result for the tag, with an optional digest and extra fields."""
    return {"name": name, **({"digest": digest} if digest else {}), **extra}
