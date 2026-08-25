"""The kinds of file Update-time scans, and the glob patterns that find them."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FileType:
    """A kind of file Update-time scans, such as Dockerfiles or a Sphinx config."""

    name: str  # As the README's first table and the command-line help name it
    patterns: tuple[str, ...]  # The glob patterns that find these files, relative to the directory walked
    start: str = ""  # The directory to walk, relative to the scan root; the scan root itself when empty
    case_sensitive: bool | None = None  # Whether the patterns match case, or the platform's default when None
    recursive: bool = True  # Whether the subdirectories are searched too, or the directory alone


_YAML_PATTERNS = ("*.yml", "*.yaml")
# Dockerfiles are conventionally named `Dockerfile`, or `<purpose>.Dockerfile` / `Dockerfile.<purpose>` when a
# project has more than one (e.g. `python.Dockerfile`, `Dockerfile.dev`). The three patterns don't overlap for any
# realistic name, so a file is discovered once.
DOCKERFILE_NAME = "Dockerfile"
DOCKERFILE_GLOB_PATTERNS = (DOCKERFILE_NAME, f"*.{DOCKERFILE_NAME}", f"{DOCKERFILE_NAME}.*")

PYPROJECT_TOML = FileType("pyproject.toml", ("pyproject.toml",))
# A requirements file is named `requirements.txt`, `requirements-<purpose>.txt` or `<purpose>-requirements.txt`, or
# is any `.txt` file in a `requirements/` directory.
REQUIREMENTS_TXT = FileType(
    "requirements.txt",
    ("requirements.txt", "requirements-*.txt", "*-requirements.txt", "requirements/*.txt"),
    case_sensitive=True,
)
INLINE_SCRIPT_METADATA = FileType("PEP 723 inline script metadata", ("*.py",))
PACKAGE_JSON = FileType("package.json", ("package.json",))
PYTHON_VERSION_FILE = FileType(".python-version", (".python-version",))
DOCKERFILES = FileType("Dockerfiles", DOCKERFILE_GLOB_PATTERNS, case_sensitive=False)  # A `dockerfile` counts
CIRCLE_CI_CONFIGS = FileType("CircleCI configs", _YAML_PATTERNS, start=".circleci")
# GitLab CI reads one file at the repository root, so the scan root is read as it stands: walking it would also
# find a nested `.gitlab-ci.yml`, which GitLab itself never reads.
GITLAB_CI_CONFIG = FileType(".gitlab-ci.yml", (".gitlab-ci.yml",), recursive=False)
DOCKER_COMPOSE_FILES = FileType("Docker Compose files", ("docker-compose*.yml",))
HELM_CHARTS = FileType("Helm charts", _YAML_PATTERNS, start="helm")
DEVCONTAINER_CONFIGS = FileType(
    "devcontainer configs",
    (".devcontainer.json", ".devcontainer/devcontainer.json", ".devcontainer/*/devcontainer.json"),
)
GITHUB_WORKFLOWS = FileType("YAML files under .github/", _YAML_PATTERNS, start=".github")
PRE_COMMIT_CONFIG = FileType(".pre-commit-config.yaml", (".pre-commit-config.yaml",))
SPHINX_CONFIG = FileType("Sphinx config", ("conf.py",), start="docs")
