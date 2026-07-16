"""Shared test helpers."""

import importlib
import pkgutil
import unittest
from functools import cache
from typing import TYPE_CHECKING
from unittest.mock import ANY, Mock, patch

import update_time
from update_time.domain.staleness import STALE_AFTER_DAYS_ENV_VAR
from update_time.domain.version import DependencyVersion, NewVersionGetter, VersionString
from update_time.io.log import Logger
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
    from collections.abc import Mapping
    from pathlib import Path
    from unittest.mock import _patch, _patch_dict


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

    It mocks the logger's methods, exposed as mock_debug/info/warning/error attributes, and offers the assert_*_logged
    helpers below. This spares tests from patching (and threading through method arguments) the log methods, and from
    silencing expected diagnostics by hand.
    """

    def setUp(self) -> None:
        """Start the logger patches and register their cleanup."""
        super().setUp()
        self.mock_debug = self._patch_logger("debug")
        self.mock_info = self._patch_logger("info")
        self.mock_warning = self._patch_logger("warning")
        self.mock_error = self._patch_logger("error")
        self._error_expected = False  # Set by assert_error_logged; tearDown fails on an error the test didn't expect.

    def tearDown(self) -> None:
        """Fail the test if an error was logged that it did not explicitly expect via assert_error_logged.

        An error log almost always means something genuinely broke, and only a handful of tests expect one, so
        'no error unless expected' is enforced by default here rather than left to each test to assert (warnings,
        which are common and often expected, stay opt-in via assert_no_warnings_logged).
        """
        super().tearDown()
        if not self._error_expected:
            self.mock_error.assert_not_called()

    def _patch_logger(self, method: str) -> Mock:
        """Patch a Logger method for the duration of the test and return the mock."""
        patcher = patch(f"logging.Logger.{method}")
        self.addCleanup(patcher.stop)
        return patcher.start()

    def assert_error_logged(self, message: str, *args: object) -> None:
        """Assert an error was logged, and mark it expected so tearDown's strict no-error check passes."""
        self._error_expected = True
        self.mock_error.assert_called_once_with(message, *args, stacklevel=ANY)

    def assert_new_version_logged(
        self, path: Path, dependency: str, version: str, changes: str = Logger.NO_CHANGELOG, *, once: bool = True
    ) -> None:
        """Assert that the availability of a new version was logged at info level for the dependency in the file.

        Pass the file `path` as it was given to the updater; it is made relative to the working directory here, the
        same way the logger renders it.
        """
        assert_called = self.mock_info.assert_called_once_with if once else self.mock_info.assert_called_with
        assert_called(Logger.MESSAGE_NEW_VERSION, dependency, Logger._relative(path), version, changes, stacklevel=ANY)

    def assert_no_new_version_logged(self) -> None:
        """Assert that no new version was logged at info level (other info-level messages are allowed)."""
        new_version_calls = [
            call for call in self.mock_info.call_args_list if call.args[:1] == (Logger.MESSAGE_NEW_VERSION,)
        ]
        self.assertEqual(new_version_calls, [], "Expected no new version to be logged")

    def assert_pinned_logged(self, path: Path, dependency: str, version: str, sha: str) -> None:
        """Assert that pinning a previously unpinned reference to a digest was logged at info level for the file."""
        message = Logger._MESSAGE_PINNED
        self.mock_info.assert_called_with(message, dependency, Logger._relative(path), version, sha, stacklevel=ANY)

    def assert_digest_drift_logged(
        self, path: Path, dependency: str, version: str, current_sha: str, new_sha: str
    ) -> None:
        """Assert that a re-pushed tag's digest drift was logged as a single warning for the file."""
        message = Logger._MESSAGE_DIGEST_DRIFT
        self.mock_warning.assert_called_once_with(
            message, dependency, version, Logger._relative(path), current_sha, new_sha, stacklevel=ANY
        )

    def assert_adopted_drift_logged(  # noqa: PLR0913
        self, path: Path, dependency: str, version: str, current_sha: str, new_sha: str, cause: object = ANY
    ) -> None:
        """Assert that adopting a re-pushed tag's new digest was logged once at info level for the file."""
        message = Logger._MESSAGE_ADOPTED_DIGEST_DRIFT
        self.mock_info.assert_called_once_with(
            message, dependency, version, Logger._relative(path), current_sha, new_sha, cause, stacklevel=ANY
        )

    def assert_stale_dependency_logged(self, path: Path, dependency: str, version: str) -> None:
        """Assert that a stale dependency (its newest release too old) was warned about once for the file.

        The exact age and threshold vary with the wall clock, so they are matched with ANY.
        """
        message = Logger._MESSAGE_STALE
        self.mock_warning.assert_called_once_with(
            message, dependency, Logger._relative(path), version, ANY, ANY, stacklevel=ANY
        )

    def assert_path_logged(self, path: Path) -> None:
        """Assert that the path being checked for updates was logged at debug level."""
        self.mock_debug.assert_called_with(Logger._MESSAGE_CHECKING_PATH, Logger._relative(path), stacklevel=ANY)

    def assert_no_path_logged(self) -> None:
        """Assert that no path being checked for updates was logged (nothing logged at debug level)."""
        self.mock_debug.assert_not_called()

    def assert_ignored_logged(self, dependency: str, path: Path, directive: object = ANY) -> None:
        """Assert that ignoring a reference (via an update-time: ignore directive) was logged at debug level."""
        self.mock_debug.assert_called_with(
            Logger._MESSAGE_IGNORED, dependency, Logger._relative(path), directive, stacklevel=ANY
        )

    def assert_skipped_logged(self, path: Path, reason: str) -> None:
        """Assert that deliberately skipping a file was logged at info level with the given reason."""
        self.mock_info.assert_called_once_with(
            Logger._MESSAGE_SKIP_PATH, Logger._relative(path), reason, stacklevel=ANY
        )

    def assert_unsupported_package_manager_logged(self, path: Path, manager: str, supported: str) -> None:
        """Assert that an unsupported package manager was logged as a warning for the file."""
        message = Logger._MESSAGE_SKIP_UNSUPPORTED
        self.mock_warning.assert_called_once_with(message, Logger._relative(path), manager, supported, stacklevel=ANY)

    def assert_invalid_pyproject_toml_logged(self, path: Path) -> None:
        """Assert that an unparsable pyproject.toml was logged as a warning for the file."""
        self.mock_warning.assert_called_once_with(Logger._MESSAGE_INVALID_TOML, Logger._relative(path), stacklevel=ANY)

    def assert_could_not_fetch_logged(self, url: object = ANY, detail: object = ANY) -> None:
        """Assert that a single 'could not fetch' warning was logged, optionally for a given URL and status/error."""
        self.mock_warning.assert_called_once_with(Logger._MESSAGE_COULD_NOT_FETCH, url, detail, stacklevel=ANY)

    def assert_command_stderr_logged(self, command: object = ANY, stderr: object = ANY) -> None:
        """Assert that a single 'command wrote to stderr' warning was logged, optionally for a given command/stderr."""
        self.mock_warning.assert_called_once_with(Logger._MESSAGE_COMMAND_STDERR, command, stderr, stacklevel=ANY)

    def assert_no_warnings_logged(self) -> None:
        """Assert that no warnings were logged."""
        self.mock_warning.assert_not_called()


def new_version_getter(version: VersionString, sha: str = "") -> NewVersionGetter:
    """Return a new-version-getter."""
    return lambda *_args: DependencyVersion(version=version, sha=sha)


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


# Reusable decorator that disables the staleness check, for update tests that focus on the update flow and would
# otherwise trigger the staleness pass's own registry requests. The staleness pass has its own dedicated tests.
staleness_disabled = patch_environ({STALE_AFTER_DAYS_ENV_VAR: "0"})


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


def npm_registry(published: dict[str, str]) -> Mock:
    """Return a mock npm registry response mapping versions to their publication dates (its `time` map)."""
    return mock_response({"time": published})


PYPI_OLD_UPLOAD = "2020-01-01T00:00:00.000000Z"  # A distribution upload time well outside the cooldown window.


def pypi_index(*versions: str, files: list[dict[str, str]] | None = None) -> Mock:
    """Return a mock PyPI Index (Simple) API response listing the versions and, when given, distribution files."""
    body: dict[str, object] = {"versions": list(versions)}
    if files is not None:
        body["files"] = files
    return mock_response(body)


def pypi_release(upload_time: str = PYPI_OLD_UPLOAD, *, yanked: bool = False, description: str = "") -> Mock:
    """Return a mock PyPI per-version metadata response with the given upload time, yank state, and description.

    An empty `upload_time` models a release with no distribution files (an empty `urls` list).
    """
    urls = [{"upload_time_iso_8601": upload_time}] if upload_time else []
    return mock_response({"info": {"description": description, "yanked": yanked}, "urls": urls})


def docker_tag(name: str, digest: str = "", **extra: object) -> dict[str, object]:
    """Return a single-tag Docker Hub API result for the tag, with an optional digest and extra fields."""
    return {"name": name, **({"digest": digest} if digest else {}), **extra}
