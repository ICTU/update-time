"""Update all dependencies by running the individual updater scripts."""

import os
import subprocess  # nosec B404
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from update_time.domain.cooldown import COOLDOWN_DAYS_ENV_VAR
from update_time.io.cli import parse_args
from update_time.io.log import LOG_LEVEL_ENV_VAR, get_logger

SRC = Path(__file__).parent
GIT_REPOSITORY_REFUSAL_MESSAGE = (
    "Refusing to run: not inside a git repository. Update-time rewrites files in place; "
    "run it inside a repository so changes can be reverted, or pass --force to run anyway."
)

# These scripts update different files, so they can run concurrently.
PARALLEL_SCRIPTS = (
    "dockerfile_base_image",
    "pyproject_toml",
    "requirements_txt",
    "github_action",
    "circle_ci_config",
    "gitlab_ci_config",
    "manifest_images",
    "devcontainer",
    "jsdelivr",
)
# node_engine and package_json both rewrite the package.json files, so they run sequentially (after the
# parallel scripts and after each other) to avoid concurrent writes to the same files. Also, node_engine
# reads the Node version from the Dockerfile so it can't run in parallel with dockerfile_base_image.
SEQUENTIAL_SCRIPTS = ("node_engine", "package_json")


def run_script(name: str) -> int:
    """Run the updater script with the given name and return its exit code."""
    return subprocess.run([sys.executable, str(SRC / f"update_{name}.py")], check=False).returncode  # noqa: S603 # nosec: B603


def update_dependencies() -> int:
    """Run all updater scripts and return the highest exit code."""
    with ThreadPoolExecutor() as executor:
        results = list(executor.map(run_script, PARALLEL_SCRIPTS))
    results.extend(run_script(name) for name in SEQUENTIAL_SCRIPTS)
    return max(results, default=0)


def is_inside_git_repository(start: Path | None = None) -> bool:
    """Return whether start is inside a git repository."""
    current = (start or Path.cwd()).resolve()
    return any((directory / ".git").exists() for directory in (current, *current.parents))


def main() -> int:
    """Parse the command-line arguments and update all dependencies."""
    args = parse_args()
    # Pass the cooldown and log level down to the updater subprocesses via the environment.
    os.environ[COOLDOWN_DAYS_ENV_VAR] = str(args.cooldown)
    os.environ[LOG_LEVEL_ENV_VAR] = args.log_level
    if not args.force and not is_inside_git_repository():
        get_logger("update").log.error(GIT_REPOSITORY_REFUSAL_MESSAGE)
        return 1
    return update_dependencies()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
