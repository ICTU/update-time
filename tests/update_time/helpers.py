"""Shared test helpers."""

import importlib
import pkgutil
import unittest
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import ANY, Mock, patch

import update_time
from update_time.domain.version import DependencyVersion
from update_time.io.log import Logger
from update_time.sources.docker import _docker_hub_headers as docker_hub_headers
from update_time.sources.docker import _get_tag as docker_get_tag
from update_time.sources.docker import _oci_token as docker_oci_token
from update_time.sources.docker import _tag_names as docker_tag_names
from update_time.sources.github import _list_releases as github_list_release
from update_time.sources.npmjs import get_changes as npmjs_get_changes
from update_time.sources.npmjs import get_publication_datetime as npmjs_get_publication_datetime
from update_time.sources.pypi import project_versions as pypi_project_versions
from update_time.sources.pypi import release_metadata as pypi_release_metadata
from update_time.updaters.update_github_action import get_latest_version as github_get_latest_version

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


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
        docker_get_tag,
        docker_hub_headers,
        docker_oci_token,
        docker_tag_names,
        github_get_latest_version,
        github_list_release,
        npmjs_get_changes,
        npmjs_get_publication_datetime,
        pypi_project_versions,
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
    """Base test case that mocks the logger's methods, exposed as mock_debug/info/warning/error attributes.

    This spares every updater test from patching (and threading through method arguments) the log methods.
    """

    def setUp(self) -> None:
        """Start the logger patches and register their cleanup."""
        super().setUp()
        self.mock_debug = self._patch_logger("debug")
        self.mock_info = self._patch_logger("info")
        self.mock_warning = self._patch_logger("warning")
        self.mock_error = self._patch_logger("error")

    def _patch_logger(self, method: str) -> Mock:
        """Patch a Logger method for the duration of the test and return the mock."""
        patcher = patch(f"logging.Logger.{method}")
        self.addCleanup(patcher.stop)
        return patcher.start()

    NEW_VERSION_MESSAGE = "New version available for %s in %s: %s\n%s"

    def assert_new_version_logged(
        self, path: Path, dependency: str, version: str, changes: str = "No changelog available!", *, once: bool = False
    ) -> None:
        """Assert that the availability of a new version was logged at info level for the dependency in the file.

        Pass the file `path` as it was given to the updater; it is made relative to the working directory here, the
        same way the logger renders it.
        """
        assert_called = self.mock_info.assert_called_once_with if once else self.mock_info.assert_called_with
        assert_called(self.NEW_VERSION_MESSAGE, dependency, self._relative(path), version, changes, stacklevel=ANY)

    def assert_no_new_version_logged(self) -> None:
        """Assert that no new version was logged at info level (other info-level messages are allowed)."""
        new_version_calls = [
            call for call in self.mock_info.call_args_list if call.args[:1] == (self.NEW_VERSION_MESSAGE,)
        ]
        self.assertEqual([], new_version_calls, "Expected no new version to be logged")

    def assert_pinned_logged(self, path: Path, dependency: str, version: str, sha: str) -> None:
        """Assert that pinning a previously unpinned reference to a digest was logged at info level for the file."""
        message = "Pinned %s in %s to %s@%s"
        self.mock_info.assert_called_with(message, dependency, self._relative(path), version, sha, stacklevel=ANY)

    def assert_path_logged(self, path: Path) -> None:
        """Assert that the path being checked for updates was logged at debug level."""
        self.mock_debug.assert_called_with("Checking if there are updates for %s", self._relative(path), stacklevel=ANY)

    def assert_no_path_logged(self) -> None:
        """Assert that no path being checked for updates was logged (nothing logged at debug level)."""
        self.mock_debug.assert_not_called()

    def assert_skipped_logged(self, path: Path, reason: str) -> None:
        """Assert that skipping a file was logged at info level with the given reason."""
        self.mock_info.assert_called_once_with("Skipping %s: %s", self._relative(path), reason, stacklevel=ANY)

    @staticmethod
    def _relative(path: Path) -> Path:
        """Make the file path relative to the working directory, the same way the logger renders it."""
        return path.relative_to(Path.cwd())

    def assert_no_warnings_logged(self) -> None:
        """Assert that no warnings were logged."""
        self.mock_warning.assert_not_called()


def new_version_getter(version: str, sha: str = "") -> Callable[[str, str], DependencyVersion]:
    """Return a new-version-getter."""
    return lambda *_args: DependencyVersion(version=version, sha=sha)


def mock_response(json: Mapping | list | None = None, **kwargs: object) -> Mock:
    """Return a mock requests Response whose .json() returns the given value.

    Extra response attributes (text, status_code, headers, ...) can be set via keyword arguments.
    """
    response = Mock(json=Mock(return_value=json))
    response.configure_mock(**kwargs)
    return response


# Reusable class decorator that mocks the Docker Hub auth token request made by sources.docker._docker_hub_headers
# when DOCKER_HUB_USERNAME/DOCKER_HUB_TOKEN are set, so the image updater tests never make a real network call.
mock_docker_hub_auth = patch("requests.post", Mock(return_value=mock_response({"access_token": "token"})))  # nosec[B105]


def mock_path(content: str) -> Mock:
    """Return a mock Path with the given text content and a no-op relative_to()."""
    return Mock(relative_to=Mock(return_value=Mock(parts=[])), read_text=Mock(return_value=content))


def release_json(tag_name: str, **extra: object) -> dict[str, object]:
    """Return a GitHub release API result for the tag, eligible (not a draft or prerelease) unless overridden."""
    return {"draft": False, "prerelease": False, "tag_name": tag_name, **extra}


def docker_tag(name: str, digest: str = "", **extra: object) -> dict[str, object]:
    """Return a single-tag Docker Hub API result for the tag, with an optional digest and extra fields."""
    return {"name": name, **({"digest": digest} if digest else {}), **extra}


def mock_docker_registry(
    *tags: dict[str, object], names: list[str] | None = None, list_ok: bool = True
) -> Callable[..., Mock]:
    """Return a requests.get side effect that mimics the OCI tag listing and Docker Hub per-tag metadata endpoints.

    The OCI anonymous token request returns a token, the OCI `tags/list` request returns the tag names (the names
    of the given tags unless overridden), and a per-tag request returns that tag's metadata (or a 404 if unknown).
    """
    by_name = {cast("str", tag["name"]): tag for tag in tags}
    tag_names = list(by_name) if names is None else names

    def get(url: str, *_args: object, **_kwargs: object) -> Mock:
        if "auth.docker.io" in url:
            return mock_response({"token": "token"})  # nosec[B105]
        if "/tags/list" in url:
            return mock_response(
                {"tags": tag_names}, ok=list_ok, status_code=200 if list_ok else 404, url=url, headers={}
            )
        name = url.rsplit("/tags/", maxsplit=1)[-1]
        if name in by_name:
            return mock_response(by_name[name])
        return mock_response({}, ok=False, status_code=404, url=url)

    return get
