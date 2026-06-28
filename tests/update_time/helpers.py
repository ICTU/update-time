"""Shared test helpers."""

import importlib
import pkgutil
import unittest
from functools import cache
from typing import TYPE_CHECKING
from unittest.mock import ANY, Mock, patch

import update_time
from update_time.domain.version import DependencyVersion
from update_time.io.log import Logger
from update_time.sources.docker import _docker_hub_headers as docker_hub_headers
from update_time.sources.docker import _get_available_tags as docker_hub_get_available_tags
from update_time.sources.github import _list_releases as github_list_release
from update_time.sources.npmjs import get_changes as npmjs_get_changes
from update_time.sources.npmjs import get_publication_datetime as npmjs_get_publication_datetime
from update_time.sources.pypi import project_versions as pypi_project_versions
from update_time.sources.pypi import release_metadata as pypi_release_metadata
from update_time.updaters.update_github_action import get_latest_version as github_get_latest_version

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path


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
        docker_hub_get_available_tags,
        docker_hub_headers,
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

    NEW_VERSION_MESSAGE = "New version available for %s: %s\n%s"

    def assert_new_version_logged(
        self, dependency: str, version: str, changes: str = "No changelog available!", *, once: bool = False
    ) -> None:
        """Assert that the availability of a new version was logged at info level for the dependency."""
        assert_called = self.mock_info.assert_called_once_with if once else self.mock_info.assert_called_with
        assert_called(self.NEW_VERSION_MESSAGE, dependency, version, changes, stacklevel=ANY)

    def assert_no_new_version_logged(self) -> None:
        """Assert that no new version was logged at info level (other info-level messages are allowed)."""
        new_version_calls = [
            call for call in self.mock_info.call_args_list if call.args[:1] == (self.NEW_VERSION_MESSAGE,)
        ]
        self.assertEqual([], new_version_calls, "Expected no new version to be logged")

    def assert_pinned_logged(self, dependency: str, version: str, sha: str) -> None:
        """Assert that pinning a previously unpinned reference to a digest was logged at info level."""
        self.mock_info.assert_called_with("Pinned %s to %s@%s", dependency, version, sha, stacklevel=ANY)

    def assert_path_logged(self, path: Path) -> None:
        """Assert that the path being checked for updates was logged at debug level."""
        self.mock_debug.assert_called_with("Checking if there are updates for %s", path, stacklevel=ANY)

    def assert_no_path_logged(self) -> None:
        """Assert that no path being checked for updates was logged (nothing logged at debug level)."""
        self.mock_debug.assert_not_called()

    def assert_skipped_logged(self, path: Path, reason: str) -> None:
        """Assert that skipping a file was logged at info level with the given reason."""
        self.mock_info.assert_called_once_with("Skipping %s: %s", path, reason, stacklevel=ANY)

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
    """Return a Docker Hub tags endpoint result for the tag, with an optional digest and extra fields."""
    return {"name": name, **({"digest": digest} if digest else {}), **extra}


def docker_hub_response(*tags: dict[str, object], next_url: str | None = None, **kwargs: object) -> Mock:
    """Return a mock Docker Hub tags endpoint response containing the given tags, optionally paginated."""
    json: dict[str, object] = {"results": list(tags)}
    if next_url is not None:
        json["next"] = next_url
    return mock_response(json, **kwargs)
