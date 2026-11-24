"""Test helpers the reference tests share: the getters a decision is handed."""

from typing import TYPE_CHECKING
from unittest.mock import Mock

from update_time.domain.dependency import NO_PROJECT, DependencyVersion, Project

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.dependency import VersionString


def new_version_getter(version: VersionString, sha: str = "") -> NewVersionGetter:
    """Return a new-version-getter, which ignores the run's archival setting as a source reporting none does."""
    return lambda *_args, **_kwargs: DependencyVersion(version=version, sha=sha)


def _mock_callable(answer: object) -> Mock:
    """Return a mock callable answering with the answer."""
    return Mock(return_value=answer)


def mock_new_version_getter(release: DependencyVersion) -> Mock:
    """Return a mock new-version-getter answering with the release (see `_mock_callable`)."""
    return _mock_callable(release)


def mock_project_getter(project: Project = NO_PROJECT) -> Mock:
    """Return a mock project getter answering with the project, whose archival it declares where it has one."""
    get_project = _mock_callable(project)
    get_project.declares_archival = Mock(return_value=project.archival is not None)
    return get_project
