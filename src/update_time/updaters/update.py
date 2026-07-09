"""Update all dependencies by running the individual updater scripts."""

import os
import subprocess  # nosec B404
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from update_time.domain.cooldown import COOLDOWN_DAYS_ENV_VAR
from update_time.domain.staleness import STALE_AFTER_DAYS_ENV_VAR
from update_time.io.cli import parse_args
from update_time.io.filesystem import EXCLUDE_PATHS_ENV_VAR
from update_time.io.log import LOG_LEVEL_ENV_VAR, get_logger
from update_time.io.rewrite import ALLOW_IMAGE_DIGEST_DRIFT_ENV_VAR

SRC = Path(__file__).parent

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


def configure_excluded_paths(paths: list[Path]) -> None:
    """Pass the excluded directories down to the updater subprocesses and log them once for the whole run.

    The walk runs inside each subprocess, so the excluded set travels through the environment (like the cooldown and
    log level). Logging happens here, in the parent, so each excluded path is reported once rather than once per
    subprocess. Existence is checked relative to the scan root (the parent has already chdir'd there); a path that
    does not exist is not an error — layouts vary between checkouts — but it is surfaced at WARNING and left out of
    the environment, so what is passed down reflects what is actually held back.
    """
    logger = get_logger(__name__)
    existing = []
    for path in paths:
        if path.exists():
            logger.excluded_path(path)
            existing.append(path)
        else:
            logger.missing_excluded_path(path)
    os.environ[EXCLUDE_PATHS_ENV_VAR] = ",".join(str(path) for path in existing)


def main() -> int:
    """Parse the command-line arguments and update all dependencies."""
    args = parse_args()
    # Scope the whole run to the requested directory; everything downstream keys off the current working directory.
    os.chdir(args.path)
    # Pass the cooldown, staleness threshold, log level, and drift opt-in down to the subprocesses via the environment.
    os.environ[COOLDOWN_DAYS_ENV_VAR] = str(args.cooldown)
    os.environ[STALE_AFTER_DAYS_ENV_VAR] = str(args.stale_after)
    os.environ[LOG_LEVEL_ENV_VAR] = args.log_level
    os.environ[ALLOW_IMAGE_DIGEST_DRIFT_ENV_VAR] = "1" if args.allow_image_digest_drift else "0"
    configure_excluded_paths(args.exclude_path)
    return update_dependencies()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
