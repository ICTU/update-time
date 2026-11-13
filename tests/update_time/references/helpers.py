"""Test helpers the reference tests share: the logger, and the getters a decision is handed."""

from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import Mock

from update_time.domain.dependency import DependencyVersion, Project
from update_time.io.log import Logger

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.dependency import VersionString


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
    """Return a mock callable answering with the answer."""
    return Mock(return_value=answer)


def mock_new_version_getter(release: DependencyVersion) -> Mock:
    """Return a mock new-version-getter answering with the release (see `_mock_callable`)."""
    return _mock_callable(release)


def mock_project_getter(*, declares_archival: bool = False) -> Mock:
    """Return a mock project getter answering with an empty project (see `_mock_callable`)."""
    get_project = _mock_callable(Project())
    get_project.declares_archival = declares_archival
    return get_project
