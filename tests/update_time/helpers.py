"""Shared test helpers."""

import importlib
import pkgutil
import unittest
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import ANY, Mock, patch

import update_time
from update_time.domain.version import DependencyVersion, NewVersionGetter, VersionString
from update_time.io.log import Logger
from update_time.sources.docker_hub import api_headers as docker_hub_headers
from update_time.sources.github import _list_releases as github_list_release
from update_time.sources.npmjs import get_changes as npmjs_get_changes
from update_time.sources.npmjs import get_publication_datetime as npmjs_get_publication_datetime
from update_time.sources.oci import _get_tag as oci_get_tag
from update_time.sources.oci import _registry_token as oci_registry_token
from update_time.sources.oci import _tag_names as oci_tag_names
from update_time.sources.pypi import project_versions as pypi_project_versions
from update_time.sources.pypi import release_metadata as pypi_release_metadata
from update_time.updaters.update_github_action import get_latest_version as github_get_latest_version

from tests.update_time.assertions import assert_success
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2, DIGEST3

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
        docker_hub_headers,
        oci_get_tag,
        oci_registry_token,
        oci_tag_names,
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

    def assert_ignored_logged(self, dependency: str, path: Path) -> None:
        """Assert that ignoring a reference (via the update-time: ignore marker) was logged at debug level."""
        message = "Ignoring updates for %s in %s (update-time: ignore)"
        self.mock_debug.assert_called_with(message, dependency, self._relative(path), stacklevel=ANY)

    def assert_skipped_logged(self, path: Path, reason: str) -> None:
        """Assert that deliberately skipping a file was logged at info level with the given reason."""
        self.mock_info.assert_called_once_with("Skipping %s: %s", self._relative(path), reason, stacklevel=ANY)

    def assert_unsupported_package_manager_logged(self, path: Path, manager: str, supported: str) -> None:
        """Assert that an unsupported package manager was logged as a warning for the file."""
        message = "Skipping %s: %s is not supported, only %s"
        self.mock_warning.assert_called_once_with(message, self._relative(path), manager, supported, stacklevel=ANY)

    @staticmethod
    def _relative(path: Path) -> Path:
        """Make the file path relative to the working directory, the same way the logger renders it."""
        return path.relative_to(Path.cwd())

    def assert_no_warnings_logged(self) -> None:
        """Assert that no warnings were logged."""
        self.mock_warning.assert_not_called()


class RegistryRequestsMixin(unittest.TestCase):
    """Mix in to patch requests.get and requests.head with one shared mock, exposed as `self.requests`.

    The OCI client resolves a digest with HEAD and does everything else (auth probe, token, tag listing, push date)
    with GET. Routing both verbs through a single mock lets a test install one `mock_docker_registry` dispatcher via
    `self.requests.side_effect` and assert against one `self.requests.call_args_list`.
    """

    def setUp(self) -> None:
        """Patch requests.get and requests.head with a single shared mock for the duration of the test."""
        super().setUp()
        self.requests = Mock()
        for target in ("requests.get", "requests.head"):
            patcher = patch(target, self.requests)
            self.addCleanup(patcher.stop)
            patcher.start()


def new_version_getter(version: VersionString, sha: str = "") -> NewVersionGetter:
    """Return a new-version-getter."""
    return lambda *_args: DependencyVersion(version=version, sha=sha)


def mock_response(json: Mapping | list | None = None, **kwargs: object) -> Mock:
    """Return a mock requests Response whose .json() returns the given value.

    Extra response attributes (text, status_code, headers, ...) can be set via keyword arguments.
    """
    response = Mock(json=Mock(return_value=json))
    response.configure_mock(**kwargs)
    return response


# Reusable class decorator that mocks the Docker Hub auth token request made by sources.docker_hub.api_headers
# when DOCKER_HUB_USERNAME/DOCKER_HUB_TOKEN are set, so the image updater tests never make a real network call.
mock_docker_hub_auth = patch("requests.post", Mock(return_value=mock_response({"access_token": "token"})))  # nosec[B105]


def mock_path(content: str, parent: Path | None = None) -> Mock:
    """Return a mock Path with the given text content, an optional parent, and a no-op relative_to()."""
    path = Mock(relative_to=Mock(return_value=Mock(parts=[])), read_text=Mock(return_value=content))
    if parent is not None:
        path.parent = parent
    return path


def release_json(tag_name: str, **extra: object) -> dict[str, object]:
    """Return a GitHub release API result for the tag, eligible (not a draft or prerelease) unless overridden."""
    return {"draft": False, "prerelease": False, "tag_name": tag_name, **extra}


def docker_tag(name: str, digest: str = "", **extra: object) -> dict[str, object]:
    """Return a single-tag Docker Hub API result for the tag, with an optional digest and extra fields."""
    return {"name": name, **({"digest": digest} if digest else {}), **extra}


def mock_docker_registry(
    *tags: dict[str, object], names: list[str] | None = None, list_ok: bool = True
) -> Callable[..., Mock]:
    """Return a requests.get/.head side effect that mimics an OCI registry plus Docker Hub's per-tag metadata.

    It models the full flow: the `/v2/` probe answers the OCI auth challenge pointing at the registry's token
    endpoint, the token request returns a token, `tags/list` returns the tag names (the names of the given tags
    unless overridden), a manifest `HEAD` returns the tag's digest in the `Docker-Content-Digest` header (or a 404
    if unknown), and Docker Hub's proprietary per-tag request returns that tag's push date (or a 404 if unknown).
    The same callable is assigned to both `requests.get` and `requests.head`; it routes purely on the URL.
    """
    by_name = {cast("str", tag["name"]): tag for tag in tags}
    tag_names = list(by_name) if names is None else names

    def get(url: str, *_args: object, **_kwargs: object) -> Mock:
        if url.endswith("/v2/"):  # OCI auth challenge probe: point the client at the registry's token endpoint.
            host = url.removeprefix("https://").split("/", maxsplit=1)[0]
            realm = "https://auth.docker.io/token" if host == "registry-1.docker.io" else f"https://{host}/token"
            challenge = f'Bearer realm="{realm}",service="{host}"'
            return mock_response({}, ok=False, status_code=401, headers={"WWW-Authenticate": challenge})
        if "/token" in url and "/v2/" not in url:  # Token endpoint discovered from the challenge.
            return mock_response({"token": "token"})  # nosec[B105]
        if "/tags/list" in url:
            return mock_response(
                {"tags": tag_names}, ok=list_ok, status_code=200 if list_ok else 404, url=url, headers={}
            )
        if "/manifests/" in url:  # Manifest HEAD: the digest is returned in the Docker-Content-Digest header.
            tag = by_name.get(url.rsplit("/manifests/", maxsplit=1)[-1])
            if tag is None:
                return mock_response({}, ok=False, status_code=404, url=url)
            digest = cast("str", tag.get("digest", ""))
            return mock_response({}, headers={"Docker-Content-Digest": digest} if digest else {})
        # Docker Hub proprietary per-tag metadata (the push date); only reached for a tag that resolved a digest.
        return mock_response(by_name.get(url.rsplit("/tags/", maxsplit=1)[-1], {}))

    return get


class ImageUpdaterTestMixin(RegistryRequestsMixin, LoggingTestCase):
    """Shared tests for the updaters that rewrite `image:tag[@digest]` references via `update_file`/`get_latest_tag`.

    All these updaters do the same thing to a reference (leave it when already latest, bump the tag and digest, pin a
    tag-only reference); they differ only in how a reference is written in their file format and how the file is
    discovered. A concrete suite supplies those two through `reference` and `run_updater`, inherits the common cases
    below, and adds its own format-specific tests (stage aliases, machine images, features, variable substitution).
    """

    def reference(self, image: str) -> str:
        """Return `image` embedded in the file format the updater rewrites (e.g. `FROM {image}` and a newline)."""
        raise NotImplementedError

    def run_updater(self, mock_file: Mock) -> int:
        """Discover `mock_file`, run the updater, and return its exit code."""
        raise NotImplementedError

    def test_no_changes(self) -> None:
        """Test that an image already at the latest pinned tag is left unchanged."""
        self.requests.side_effect = mock_docker_registry()
        mock_file = mock_path(self.reference(f"python:3.14@{DIGEST}"))
        assert_success(self.run_updater(mock_file))
        mock_file.write_text.assert_not_called()
        self.assert_path_logged(mock_file)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_bumped(self) -> None:
        """Test that the image tag and digest are bumped when a newer version is available."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        mock_file = mock_path(self.reference(f"python:3.14@{DIGEST1}"))
        assert_success(self.run_updater(mock_file))
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.15@{DIGEST2}"))
        self.assert_new_version_logged(mock_file, "python", "3.15")
        self.assert_no_warnings_logged()

    def test_pin_unpinned_image(self) -> None:
        """Test that an image referenced by tag only is pinned with the latest tag and digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        mock_file = mock_path(self.reference("python:3.14"))
        assert_success(self.run_updater(mock_file))
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.15@{DIGEST2}"))
        self.assert_new_version_logged(mock_file, "python", "3.15")
        self.assert_no_warnings_logged()

    def test_pin_unpinned_image_already_at_latest(self) -> None:
        """Test that an unpinned image already at the latest version is still pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST3))
        mock_file = mock_path(self.reference("python:3.14"))
        assert_success(self.run_updater(mock_file))
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.14@{DIGEST3}"))
        self.assert_pinned_logged(mock_file, "python", "3.14", DIGEST3)
        self.assert_no_warnings_logged()
