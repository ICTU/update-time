"""Test helpers the reference tests share: the getters and resolvers a decision is handed."""

from collections.abc import Callable
from typing import TYPE_CHECKING
from unittest.mock import Mock

from update_time.domain.dependency import DependencyVersion, Project

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.dependency import VersionString
    from update_time.domain.reference import ResolvedReference


def new_version_getter(version: VersionString, sha: str = "") -> NewVersionGetter:
    """Return a new-version-getter."""
    return lambda *_args: DependencyVersion(version=version, sha=sha)


def mock_new_version_getter() -> Mock:
    """Return a mock new-version getter that claims no source capability, as an unregistered getter claims none.

    A bare `Mock` grows any attribute it is asked for, so it would claim every capability a caller reads off a
    getter (see `primitives.capability`). Specifying it against a callable keeps it callable and lets those reads
    answer as they do for a source that registers nothing, which the two getters below are specified for too.
    """
    return Mock(spec=Callable)


def mock_project_getter() -> Mock:
    """Return a mock project getter that claims no source capability, as `mock_new_version_getter` explains."""
    return Mock(spec=Callable, return_value=Project())


def mock_reference_resolver(*resolved: ResolvedReference) -> Mock:
    """Return a mock reference resolver answering with the resolved references, claiming no source capability."""
    return Mock(spec=Callable, return_value=list(resolved))
