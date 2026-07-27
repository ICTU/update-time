"""Shared test helpers."""

import importlib
import pkgutil
import unittest
from functools import cache, wraps
from logging import DEBUG, ERROR, WARNING
from typing import TYPE_CHECKING, TypeVar, cast
from unittest.mock import ANY, Mock, call, patch

import update_time
from update_time.domain.bound import NewVersionGetter, Verb, VersionBound, parse_bound
from update_time.domain.location import Location
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.version import DependencyVersion, VersionString
from update_time.io.log import Logger, LogMessage
from update_time.sources.docker_hub import api_headers as docker_hub_headers
from update_time.sources.github import _get_commit as github_get_commit
from update_time.sources.github import _list_releases as github_list_release
from update_time.sources.github import _list_tags as github_list_tags
from update_time.sources.github import get_latest_version as github_get_latest_version
from update_time.sources.npmjs import _package_metadata as npmjs_package_metadata
from update_time.sources.npmjs import get_changes as npmjs_get_changes
from update_time.sources.npmjs import get_publication_datetime as npmjs_get_publication_datetime
from update_time.sources.oci import _get_tag as oci_get_tag
from update_time.sources.oci import _registry_token as oci_registry_token
from update_time.sources.oci import _tag_names as oci_tag_names
from update_time.sources.pypi import project_metadata as pypi_project_metadata
from update_time.sources.pypi import release_metadata as pypi_release_metadata

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path
    from unittest.mock import _Call, _patch, _patch_dict

# A test method or class a patch decorator is applied to and returns unchanged (see `patch_pathlib_path`).
_Decorated = TypeVar("_Decorated", bound="Callable[..., object]")

# An assert method that receives its first arguments rendered by the decorators below. Its signature differs from the
# one its callers use — they pass a path and an optional line number where it declares a rendered location — so it is
# typed loosely, at the cost of type checking the calls.
type _AssertMethod = Callable[..., None]


def renders_location(method: _AssertMethod) -> _AssertMethod:
    """Pass the assert method the location, rendered the way the logger renders it in a log record, as first argument.

    Callers pass the file path as it was given to the updater; it is made relative to the working directory here, the
    same way the logger renders it. They can add a `line` keyword to assert the reference points at a specific line,
    or leave it out to match any (or no) line number.
    """

    @wraps(method)
    def render(test_case: object, path: Path, *args: object, line: int | None = None, **kwargs: object) -> None:
        method(test_case, Logger._render_location(Location(path, line)), *args, **kwargs)

    return render


def renders_dependency(method: _AssertMethod) -> _AssertMethod:
    """Pass the assert method the dependency, rendered the way the logger renders it, as its second argument.

    Stack this decorator under `renders_location`, which renders the first argument.
    """

    @wraps(method)
    def render(test_case: object, location: str, dependency: str, *args: object, **kwargs: object) -> None:
        method(test_case, location, Logger._render_dependency(dependency), *args, **kwargs)

    return render


@cache
def _module_loggers() -> tuple[Logger, ...]:
    """Return every module-level `LOG` logger in the update_time package, discovered by walking it."""
    loggers = []
    for module_info in pkgutil.walk_packages(update_time.__path__, f"{update_time.__name__}."):
        module = importlib.import_module(module_info.name)
        if isinstance(log := getattr(module, "LOG", None), Logger):
            loggers.append(log)
    return tuple(loggers)


class CacheClearingTestCase(unittest.TestCase):
    """Base test case that resets global state before each test to prevent cross-test leakage.

    This clears the functools caches and the loggers' changelog-suppression state. This is the single place
    where the cached functions need to be listed. Add new @cache'd functions here.
    """

    CACHES = (
        docker_hub_headers,
        oci_get_tag,
        oci_registry_token,
        oci_tag_names,
        github_get_commit,
        github_get_latest_version,
        github_list_release,
        github_list_tags,
        npmjs_get_changes,
        npmjs_get_publication_datetime,
        npmjs_package_metadata,
        pypi_project_metadata,
        pypi_release_metadata,
    )

    def setUp(self) -> None:
        """Clear all caches and logger state so each test gets fresh results."""
        super().setUp()
        for cached_function in self.CACHES:
            cached_function.cache_clear()
        for logger in _module_loggers():
            logger.logged_changes.clear()


class LoggingTestCase(CacheClearingTestCase):
    """Base test case for any test of code that logs.

    It mocks the logger's log method, exposed as the mock_log attribute, and offers the assert_*_logged helpers below.
    This spares tests from patching (and threading through method arguments) the log method, and from silencing
    expected diagnostics by hand.
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

    def assert_logged(self, message: LogMessage, *args: object) -> None:
        """Assert the message was the only record logged at its level, with the given arguments."""
        self.assertEqual(self.records(message.level), [call(message, *args, stacklevel=ANY)])

    def assert_last_logged(self, message: LogMessage, *args: object) -> None:
        """Assert the message was the most recent record at its level, with the given arguments."""
        self.assertEqual(self.records(message.level)[-1:], [call(message, *args, stacklevel=ANY)])

    def assert_logged_among_others(self, message: LogMessage, *args: object) -> None:
        """Assert the message was logged with the given arguments, among the other records at its level."""
        self.assertIn(call(message, *args, stacklevel=ANY), self.records(message.level))

    def assert_error_logged(self, message: LogMessage, *args: object) -> None:
        """Assert an error was logged, and mark it expected so tearDown's strict no-error check passes."""
        self._error_expected = True
        self.assert_logged(message, *args)

    @renders_location
    @renders_dependency
    def assert_new_version_logged(
        self, location: str, dependency: str, version: str, changes: str = Logger.NO_CHANGELOG, *, once: bool = True
    ) -> None:
        """Assert that the availability of a new version was logged for the dependency in the file."""
        assert_logged = self.assert_logged if once else self.assert_last_logged
        assert_logged(Logger.MESSAGE_NEW_VERSION, dependency, location, version, changes)

    def new_version_records(self) -> list[_Call]:
        """Return the records reporting an available new version, ignoring the other records at their level."""
        message = Logger.MESSAGE_NEW_VERSION
        return [record for record in self.records(message.level) if record.args[:1] == (message,)]

    def assert_no_new_version_logged(self) -> None:
        """Assert that no new version was logged (other records at the same level are allowed)."""
        self.assertEqual(self.new_version_records(), [], "Expected no new version to be logged")

    @renders_location
    @renders_dependency
    def assert_pinned_logged(self, location: str, dependency: str, version: str, sha: str) -> None:
        """Assert that pinning a previously unpinned reference to a digest was logged for the file."""
        self.assert_last_logged(Logger._MESSAGE_PINNED, dependency, location, version, sha)

    @renders_location
    @renders_dependency
    def assert_cannot_pin_logged(self, location: str, dependency: str) -> None:
        """Assert that a reference with nowhere to hold a hash was reported as one that cannot be pinned."""
        self.assert_logged(Logger._MESSAGE_CANNOT_PIN, dependency, location)

    @renders_location
    @renders_dependency
    def assert_digest_drift_logged(
        self, location: str, dependency: str, version: str, current_sha: str, new_sha: str
    ) -> None:
        """Assert that a re-pushed tag's digest drift was logged as a single warning for the file."""
        self.assert_logged(Logger._MESSAGE_DIGEST_DRIFT, dependency, version, location, current_sha, new_sha)

    @renders_location
    @renders_dependency
    def assert_adopted_drift_logged(  # noqa: PLR0913
        self, location: str, dependency: str, version: str, current_sha: str, new_sha: str, cause: object = ANY
    ) -> None:
        """Assert that adopting a re-pushed tag's new digest was logged once for the file."""
        self.assert_logged(
            Logger._MESSAGE_ADOPTED_DIGEST_DRIFT, dependency, version, location, current_sha, new_sha, cause
        )

    @renders_location
    @renders_dependency
    def assert_stale_dependency_logged(self, location: str, dependency: str, version: str) -> None:
        """Assert that a stale dependency (its newest release too old) was warned about once for the file.

        The exact age and threshold vary with the wall clock, so they are matched with ANY.
        """
        self.assert_logged(Logger._MESSAGE_STALE, dependency, location, version, ANY, ANY)

    @renders_location
    @renders_dependency
    def assert_yanked_dependency_logged(
        self, location: str, dependency: str, version: str, reason: object = ANY
    ) -> None:
        """Assert that a pin left on a yanked version was warned about once for the file.

        The reason renders differently for a specified and an unspecified yank, so it defaults to matching any.
        """
        self.assert_logged(Logger._MESSAGE_YANKED, dependency, location, version, reason)

    @renders_location
    @renders_dependency
    def assert_redundant_yank_scope_logged(self, location: str, dependency: str, directive: object = ANY) -> None:
        """Assert that a yank scope the dependency's source can never honour was warned about once for the file."""
        self.assert_logged(Logger._MESSAGE_REDUNDANT_YANK_SCOPE, directive, dependency, location)

    @renders_location
    def assert_path_logged(self, location: str) -> None:
        """Assert that the path being checked for updates was logged."""
        self.assert_last_logged(Logger._MESSAGE_CHECKING_PATH, location)

    def assert_no_path_logged(self) -> None:
        """Assert that no path being checked for updates was logged (nothing logged at debug level)."""
        self.assertEqual(self.records(DEBUG), [])

    @renders_location
    @renders_dependency
    def assert_ignored_logged(self, location: str, dependency: str, directive: object = ANY) -> None:
        """Assert that ignoring a reference (via an update-time: ignore directive) was logged."""
        self.assert_last_logged(Logger._MESSAGE_IGNORED, dependency, location, directive)

    @renders_location
    @renders_dependency
    def assert_ignored_staleness_logged(self, location: str, dependency: str, directive: object = ANY) -> None:
        """Assert that a staleness warning held back by a marker was logged, among the other records."""
        self.assert_logged_among_others(Logger._MESSAGE_IGNORED_STALENESS, dependency, location, directive)

    @renders_location
    @renders_dependency
    def assert_ignored_yank_logged(self, location: str, dependency: str, directive: object = ANY) -> None:
        """Assert that a yank warning held back by a marker was logged, among the other records."""
        self.assert_logged_among_others(Logger._MESSAGE_IGNORED_YANK, dependency, location, directive)

    @renders_location
    def assert_skipped_logged(self, location: str, reason: str) -> None:
        """Assert that deliberately skipping a file was logged with the given reason."""
        self.assert_logged(Logger._MESSAGE_SKIP_PATH, location, reason)

    @renders_location
    def assert_unsupported_package_manager_logged(self, location: str, manager: str, supported: str) -> None:
        """Assert that an unsupported package manager was logged for the file."""
        self.assert_logged(Logger._MESSAGE_SKIP_UNSUPPORTED, location, manager, supported)

    @renders_location
    def assert_invalid_pyproject_toml_logged(self, location: str) -> None:
        """Assert that an unparsable pyproject.toml was logged for the file."""
        self.assert_logged(Logger._MESSAGE_INVALID_TOML, location)

    def assert_could_not_fetch_logged(self, url: object = ANY, status: object = ANY, reason: object = ANY) -> None:
        """Assert that a single 'could not fetch' warning was logged, optionally for a given URL and status/reason."""
        self.assert_logged(Logger._MESSAGE_NOT_OK_RESPONSE, url, status, reason)

    def assert_command_stderr_logged(self, command: object = ANY, stderr: object = ANY) -> None:
        """Assert that a single 'command wrote to stderr' warning was logged, optionally for a given command/stderr."""
        self.assert_logged(Logger._MESSAGE_COMMAND_STDERR, command, stderr)

    def assert_no_warnings_logged(self) -> None:
        """Assert that no warnings were logged."""
        self.assertEqual(self.records(WARNING), [])


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


def mock_response(json: Mapping | list | None = None, **kwargs: object) -> Mock:
    """Return a mock requests Response whose .json() returns the given value.

    Extra response attributes (text, status_code, headers, ...) can be set via keyword arguments.
    """
    response = Mock(json=Mock(return_value=json))
    response.links = {}  # requests parses the Link header into `.links`; default to none, override per test.
    response.configure_mock(**kwargs)
    return response


# Reusable class decorator that mocks the Docker Hub auth token request made by sources.docker_hub.api_headers
# when DOCKER_HUB_USERNAME/DOCKER_HUB_TOKEN are set, so the image updater tests never make a real network call.
mock_docker_hub_auth = patch("requests.post", Mock(return_value=mock_response({"access_token": "token"})))  # nosec[B105]


def patch_get(json: Mapping | list | None = None, **kwargs: object) -> _patch:
    """Patch requests.get to return a mock requests Response whose .json() returns the given value.

    Extra response attributes (text, status_code, headers, ...) can be set via keyword arguments.
    """
    return patch("requests.get", Mock(return_value=mock_response(json, **kwargs)))


def patch_environ(environment_variables: dict[str, str] | None = None, *, clear: bool | None = None) -> _patch_dict:
    """Mock os.environ with the given environment variables.

    If none are given, clear the environment, unless overridden by an explicit clear=True or clear=False.
    """
    clear = not environment_variables if clear is None else clear
    return patch.dict("os.environ", environment_variables or {}, clear=clear)


def patch_pathlib_path(*methods: str, **methods_and_return_values: object) -> Callable[[_Decorated], _Decorated]:
    """Patch one or more pathlib.Path methods, each to return the given value, for the test's duration.

    Usable as a decorator on a test method or class (like `unittest.mock.patch`, by stacking one patch per method);
    it adds no mock argument. Each keyword names a pathlib.Path method and gives the value it should return, so
    several can be patched at once, for example `@patch_pathlib_path(exists=True, read_text="file contents")`.
    """

    def decorate(target: _Decorated) -> _Decorated:
        decorated: Callable[..., object] = target
        for method in methods:
            decorated = patch(f"pathlib.Path.{method}")(decorated)
        for method, return_value in methods_and_return_values.items():
            decorated = patch(f"pathlib.Path.{method}", Mock(return_value=return_value))(decorated)
        return cast("_Decorated", decorated)

    return decorate


# Reusable decorator that disables the staleness check, for update tests that focus on the update flow and would
# otherwise trigger the staleness pass's own registry requests. The staleness pass has its own dedicated tests.
staleness_disabled = patch_environ({STALE_AFTER.name: "0"})


def mock_path(content: str, parent: Path | None = None) -> Mock:
    """Return a mock Path with the given text content, an optional parent, and a no-op relative_to()."""
    path = Mock(relative_to=Mock(return_value=Mock(parts=[])), read_text=Mock(return_value=content))
    if parent is not None:
        path.parent = parent
    return path


def github_release_json(tag_name: str, **extra: object) -> dict[str, object]:
    """Return a GitHub release API result for the tag, eligible (not a draft or prerelease) unless overridden."""
    return {"draft": False, "prerelease": False, "tag_name": tag_name, "body": None, "published_at": None, **extra}


def github_tag_json(name: str, sha: str = "sha") -> dict[str, object]:
    """Return a GitHub tags API result for the tag, carrying the tagged commit's SHA."""
    return {"name": name, "commit": {"sha": sha}}


def github_commits_json(sha: str = "sha", date: str = "") -> dict[str, object]:
    """Return a GitHub commits API result carrying a tag's commit SHA and, when given, its committer date."""
    return {"sha": sha, **({"commit": {"committer": {"date": date}}} if date else {})}


def github_api(
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
    """Patch requests.get to serve the GitHub API endpoints from the given values (see `github_api`)."""
    return patch("requests.get", github_api(releases, tags, commit))


def jsdelivr_versions(*version_strings: str) -> Mock:
    """Return a mock jsDelivr package API response listing the given versions (newest first)."""
    return mock_response({"versions": [{"version": version} for version in version_strings]})


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
