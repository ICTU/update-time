"""The kinds of dependency Update-time updates, and the files that declare one."""

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Final

from .file_type import (
    CIRCLE_CI_CONFIGS,
    DEVCONTAINER_CONFIGS,
    DOCKER_COMPOSE_FILES,
    DOCKERFILES,
    GITHUB_WORKFLOWS,
    GITLAB_CI_CONFIG,
    HELM_CHARTS,
    INLINE_SCRIPT_METADATA,
    PACKAGE_JSON,
    PNPM_WORKSPACE_YAML,
    POM_XML,
    PRE_COMMIT_CONFIG,
    PYPROJECT_TOML,
    PYTHON_VERSION_FILE,
    REQUIREMENTS_TXT,
    SPHINX_CONFIG,
    YARNRC_YML,
    FileType,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True)
class DependencyType:
    """A kind of dependency Update-time updates, such as Docker images or Python dependencies."""

    name: str  # As the README's dependency type tables label it
    file_types: tuple[FileType, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class _DependencyTypes:
    """Every dependency type Update-time updates, in the order the README's tables list them in."""

    python_dependencies: DependencyType
    npm_dependencies: DependencyType
    maven_dependencies: DependencyType
    node_engine_version: DependencyType
    python_version: DependencyType
    docker_images: DependencyType
    github_actions: DependencyType
    pre_commit_hooks: DependencyType
    jsdelivr_npm_urls: DependencyType

    def __iter__(self) -> Iterator[DependencyType]:
        """Yield each dependency type, in the order this class declares them."""
        return (getattr(self, field.name) for field in fields(self))


DEPENDENCY_TYPES: Final[_DependencyTypes] = _DependencyTypes(
    python_dependencies=DependencyType(
        "Python dependencies", (PYPROJECT_TOML, REQUIREMENTS_TXT, INLINE_SCRIPT_METADATA)
    ),
    npm_dependencies=DependencyType(
        "npm, pnpm, yarn, and bun dependencies", (PACKAGE_JSON, PNPM_WORKSPACE_YAML, YARNRC_YML)
    ),
    maven_dependencies=DependencyType("Maven dependencies", (POM_XML,)),
    node_engine_version=DependencyType("Node engine version", (PACKAGE_JSON,)),
    python_version=DependencyType("Python version", (PYTHON_VERSION_FILE,)),
    docker_images=DependencyType(
        "Docker images",
        (DOCKERFILES, CIRCLE_CI_CONFIGS, GITLAB_CI_CONFIG, DOCKER_COMPOSE_FILES, HELM_CHARTS, DEVCONTAINER_CONFIGS),
    ),
    github_actions=DependencyType("GitHub Actions", (GITHUB_WORKFLOWS,)),
    pre_commit_hooks=DependencyType("Pre-commit hooks", (PRE_COMMIT_CONFIG,)),
    jsdelivr_npm_urls=DependencyType("jsDelivr npm URLs", (SPHINX_CONFIG,)),
)
