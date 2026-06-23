"""Shared test helpers."""

import importlib
import pkgutil
import unittest
from functools import cache
from typing import TYPE_CHECKING
from unittest.mock import Mock

import update_time
from update_time.domain.version import DependencyVersion
from update_time.io.log import Logger
from update_time.sources.docker import _docker_hub_headers as docker_hub_headers
from update_time.sources.docker import _get_available_tags as docker_hub_get_available_tags
from update_time.sources.github import _list_releases as github_list_release
from update_time.sources.npmjs import get_changes as npmjs_get_changes
from update_time.sources.npmjs import get_publication_datetime as npmjs_get_publication_datetime
from update_time.sources.pypi import release_metadata as pypi_release_metadata
from update_time.updaters.update_github_action import get_latest_version as github_get_latest_version

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


@cache
def _module_loggers() -> tuple[Logger, ...]:
    """Return every module-level ``LOG`` logger in the update_time package, discovered by walking it."""
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
        pypi_release_metadata,
    )

    def setUp(self) -> None:
        """Clear all caches and logger state so each test gets fresh results."""
        super().setUp()
        for cached_function in self.CACHES:
            cached_function.cache_clear()
        for logger in _module_loggers():
            logger.logged_changes.clear()


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
