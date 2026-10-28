"""Test helpers the reference tests share: the logger, and the getters and resolvers a decision is handed."""

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import Mock

from update_time.domain.dependency import DependencyVersion, Project
from update_time.io.log import Logger

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.dependency import VersionString
    from update_time.domain.reference import ResolvedReference


def mock_logger() -> Mock:
    """Return a mock logger that reports a marker as a logger does, through the mock's own log methods."""
    logger = Mock()
    logger.report_marker.side_effect = partial(Logger.report_marker, logger)
    return logger


def new_version_getter(version: VersionString, sha: str = "") -> NewVersionGetter:
    """Return a new-version-getter, which ignores the run's archival setting as a source reporting none does."""
    return lambda *_args, **_kwargs: DependencyVersion(version=version, sha=sha)


def mock_project_getter() -> Mock:
    """Return a mock project getter that does not claim a source capability, as an unregistered getter does not.

    A bare `Mock` grows any attribute it is asked for, so it would claim every capability a caller reads off a
    getter (see `primitives.capability`). Specifying it against a callable keeps it callable and lets those reads
    answer as they do for a source that registers nothing, which the resolver below is specified for too.
    """
    return Mock(spec=Callable, return_value=Project())


def mock_reference_resolver(*resolved: ResolvedReference) -> Mock:
    """Return a mock reference resolver answering with the resolved references, claiming no source capability."""
    return Mock(spec=Callable, return_value=list(resolved))
