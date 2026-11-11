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
    """Return a mock logger that reports a marker and a redundant opt-in as a logger does, through its own methods."""
    logger = Mock()
    logger.report_marker.side_effect = partial(Logger.report_marker, logger)
    logger.warn_if_the_opt_in_is_redundant.side_effect = partial(Logger.warn_if_the_opt_in_is_redundant, logger)
    return logger


def new_version_getter(version: VersionString, sha: str = "") -> NewVersionGetter:
    """Return a new-version-getter, which ignores the run's archival setting as a source reporting none does."""
    return lambda *_args, **_kwargs: DependencyVersion(version=version, sha=sha)


def _mock_callable(answer: object) -> Mock:
    """Return a mock callable answering with the answer, which records nothing a function does not.

    A bare `Mock` grows any attribute it is asked for, so it would claim every capability a caller reads off a
    getter (see `primitives.capability`), and say why it does not look an image up (see `domain.image_lookup`).
    Specifying it against a callable keeps it callable and lets those reads answer as they do for a function that
    records nothing.
    """
    return Mock(spec=Callable, return_value=answer)


def mock_new_version_getter(release: DependencyVersion) -> Mock:
    """Return a mock new-version-getter answering with the release (see `_mock_callable`)."""
    return _mock_callable(release)


def mock_project_getter() -> Mock:
    """Return a mock project getter answering with an empty project (see `_mock_callable`)."""
    return _mock_callable(Project())


def mock_reference_resolver(*resolved: ResolvedReference) -> Mock:
    """Return a mock reference resolver answering with the resolved references (see `_mock_callable`)."""
    return _mock_callable(list(resolved))
