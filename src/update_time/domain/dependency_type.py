"""The kinds of dependency Update-time updates, and the files that declare one.

The command-line help names these files.
"""

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

from update_time.domain import file_type

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True)
class DependencyType:
    """A kind of dependency Update-time updates, such as Docker images or Python dependencies."""

    name: str  # As the README's dependency type tables label it
    file_types: tuple[file_type.FileType, ...]


@dataclass(frozen=True)
class _DependencyTypes:
    """Every dependency type Update-time updates, in the order the README's tables list them in."""

    python_dependencies: DependencyType
    npm_dependencies: DependencyType
    node_engine_version: DependencyType
    python_version: DependencyType
    docker_images: DependencyType
    github_actions: DependencyType
    pre_commit_hooks: DependencyType
    jsdelivr_npm_urls: DependencyType

    def __iter__(self) -> Iterator[DependencyType]:
        """Yield each dependency type, in the order this class declares them."""
        return (getattr(self, field.name) for field in fields(self))


DEPENDENCY_TYPES = _DependencyTypes(
    python_dependencies=DependencyType(
        "Python dependencies",
        (file_type.PYPROJECT_TOML, file_type.REQUIREMENTS_TXT, file_type.INLINE_SCRIPT_METADATA),
    ),
    npm_dependencies=DependencyType("npm and pnpm dependencies", (file_type.PACKAGE_JSON,)),
    node_engine_version=DependencyType("Node engine version", (file_type.PACKAGE_JSON,)),
    python_version=DependencyType("Python version", (file_type.PYTHON_VERSION_FILE,)),
    docker_images=DependencyType(
        "Docker images",
        (
            file_type.DOCKERFILES,
            file_type.CIRCLE_CI_CONFIGS,
            file_type.GITLAB_CI_CONFIG,
            file_type.DOCKER_COMPOSE_FILES,
            file_type.HELM_CHARTS,
            file_type.DEVCONTAINER_CONFIGS,
        ),
    ),
    github_actions=DependencyType("GitHub Actions", (file_type.GITHUB_WORKFLOWS,)),
    pre_commit_hooks=DependencyType("Pre-commit hooks", (file_type.PRE_COMMIT_CONFIG,)),
    jsdelivr_npm_urls=DependencyType("jsDelivr npm URLs", (file_type.SPHINX_CONFIG,)),
)
