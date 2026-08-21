"""Update all dependencies by running the individual updater scripts."""

import os
import subprocess  # nosec B404
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.drift import ALLOW_HASH_DRIFT
from update_time.domain.floating import ALLOW_FLOATING_PIN
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.vulnerability import IGNORE_VULNERABILITIES, VULNERABILITY_LEVEL
from update_time.io.cli import parse_args
from update_time.io.filesystem import EXCLUDE_PATHS, inside_git_repository
from update_time.io.log import LOG_LEVEL, get_logger

_SRC = Path(__file__).parent

# These scripts update different files, so they can run concurrently.
_PARALLEL_SCRIPTS = (
    "dockerfile_base_image",
    "pyproject_toml",
    "requirements_txt",
    "github_action",
    "circle_ci_config",
    "gitlab_ci_config",
    "manifest_images",
    "devcontainer",
    "jsdelivr",
    "python_inline_script_metadata",
    "pre_commit_config",
)
# node_engine and package_json both rewrite the package.json files, so they run sequentially (after the
# parallel scripts and after each other) to avoid concurrent writes to the same files. Also, node_engine
# and python_version read a version from the Dockerfile, so they can't run in parallel with dockerfile_base_image.
_SEQUENTIAL_SCRIPTS = ("node_engine", "package_json", "python_version_file")

# A new updater script must be registered in _PARALLEL_SCRIPTS or _SEQUENTIAL_SCRIPTS above, or it would silently
# never run; fail fast on import when the registered names and the `update_<name>.py` scripts on disk disagree.
_REGISTERED = set(_PARALLEL_SCRIPTS + _SEQUENTIAL_SCRIPTS)
_ON_DISK = {path.stem.removeprefix("update_") for path in _SRC.glob("update_*.py")}
if _REGISTERED != _ON_DISK:  # pragma: no cover
    _message = f"The registered updater scripts and the scripts on disk differ: {sorted(_REGISTERED ^ _ON_DISK)}"
    raise RuntimeError(_message)


def run_script(name: str) -> int:
    """Run the updater script with the given name and return its exit code."""
    return subprocess.run([sys.executable, str(_SRC / f"update_{name}.py")], check=False).returncode  # noqa: S603 # nosec: B603


def update_dependencies() -> int:
    """Run all updater scripts and return the highest exit code."""
    with ThreadPoolExecutor() as executor:
        results = list(executor.map(run_script, _PARALLEL_SCRIPTS))
    results.extend(run_script(name) for name in _SEQUENTIAL_SCRIPTS)
    return max(results, default=0)


def _configure_excluded_paths(paths: list[Path]) -> None:
    """Pass the excluded directories down to the updater subprocesses and log them once for the whole run.

    The walk runs inside each subprocess, so the excluded set travels through the environment (like the cooldown and
    log level). Logging happens here, in the parent, so each excluded path is reported once rather than once per
    subprocess. Existence is checked relative to the scan root, where the parent has already chdir'd. A path that
    does not exist is not an error, since layouts vary between checkouts, but it is surfaced at WARNING and left out
    of the environment, so what is passed down reflects what is actually held back.
    """
    logger = get_logger(__name__)
    existing = []
    for path in paths:
        if path.exists():
            logger.excluded_path(path)
            existing.append(path)
        else:
            logger.missing_excluded_path(path)
    EXCLUDE_PATHS.set(existing)


def main() -> int:
    """Parse the command-line arguments and update all dependencies."""
    args = parse_args()
    # Scope the whole run to the requested directory; everything downstream keys off the current working directory.
    os.chdir(args.path)
    # Pass the cooldown, thresholds, ignored advisories, log level, and the drift and floating-pin opt-ins down to
    # the subprocesses via the environment.
    COOLDOWN.set(args.cooldown)
    STALE_AFTER.set(args.stale_after)
    VULNERABILITY_LEVEL.set(args.vulnerability_level)
    IGNORE_VULNERABILITIES.set(args.ignore_vulnerability)
    LOG_LEVEL.set(args.log_level)
    ALLOW_HASH_DRIFT.set(args.allow_hash_drift)
    ALLOW_FLOATING_PIN.set(args.allow_floating_pin)
    # parse_args has already refused to run outside a git repository unless --force was given; when it was, warn that
    # the in-place rewrites cannot be reverted. The log level is exported above, so the warning honours --log-level.
    scan_root = Path.cwd()
    if not inside_git_repository(scan_root):
        get_logger(__name__).forced_outside_git_repository(scan_root)
    _configure_excluded_paths(args.exclude_path)
    return update_dependencies()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
