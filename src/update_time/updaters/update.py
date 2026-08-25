"""Update all dependencies by running the individual updater scripts."""

import os
import subprocess  # nosec B404
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency_type import DEPENDENCY_TYPES, DependencyType
from update_time.domain.drift import ALLOW_HASH_DRIFT
from update_time.domain.floating import ALLOW_FLOATING_PIN
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.vulnerability import IGNORE_VULNERABILITIES, VULNERABILITY_LEVEL
from update_time.io.cli import parse_args
from update_time.io.filesystem import EXCLUDE_PATHS, inside_git_repository
from update_time.io.log import LOG_LEVEL, get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_SRC = Path(__file__).parent


@dataclass(frozen=True)
class _Script:
    """An updater script, and whether it may run beside the others."""

    name: str
    parallel: bool = True

    def run(self) -> int:
        """Run the script in a subprocess and return its exit code."""
        script = str(_SRC / f"update_{self.name}.py")
        return subprocess.run([sys.executable, script], check=False).returncode  # noqa: S603 # nosec: B603


@dataclass(frozen=True)
class _Scripts:
    """The updater scripts serving each dependency type."""

    scripts: Mapping[DependencyType, tuple[_Script, ...]]

    def __iter__(self) -> Iterator[_Script]:
        """Yield every registered updater script."""
        return (script for scripts in self.scripts.values() for script in scripts)

    @property
    def parallel_scripts(self) -> tuple[_Script, ...]:
        """Return each script that may run beside another."""
        return tuple(script for script in self if script.parallel)

    @property
    def sequential_scripts(self) -> tuple[_Script, ...]:
        """Return each script that may not run beside another."""
        return tuple(script for script in self if not script.parallel)

    def run(self) -> int:
        """Run every updater script and return the highest exit code.

        The scripts that may run beside another go first and run concurrently; the rest follow one at a time.
        """
        with ThreadPoolExecutor() as executor:
            results = list(executor.map(_Script.run, self.parallel_scripts))
        results.extend(script.run() for script in self.sequential_scripts)
        return max(results, default=0)


# The updater scripts serving each dependency type. node_engine and package_json both rewrite the package.json
# files, and node_engine and python_version_file read the base image that dockerfile_base_image updates, so
# those three may not run beside another script. They have no order among themselves.
_SCRIPTS = _Scripts(
    {
        DEPENDENCY_TYPES.python_dependencies: (
            _Script("pyproject_toml"),
            _Script("requirements_txt"),
            _Script("python_inline_script_metadata"),
        ),
        DEPENDENCY_TYPES.npm_dependencies: (_Script("package_json", parallel=False),),
        DEPENDENCY_TYPES.node_engine_version: (_Script("node_engine", parallel=False),),
        DEPENDENCY_TYPES.python_version: (_Script("python_version_file", parallel=False),),
        DEPENDENCY_TYPES.docker_images: (
            _Script("dockerfile_base_image"),
            _Script("circle_ci_config"),
            _Script("gitlab_ci_config"),
            _Script("manifest_images"),
            _Script("devcontainer"),
        ),
        DEPENDENCY_TYPES.github_actions: (_Script("github_action"),),
        DEPENDENCY_TYPES.pre_commit_hooks: (_Script("pre_commit_config"),),
        DEPENDENCY_TYPES.jsdelivr_npm_urls: (_Script("jsdelivr"),),
    }
)


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
    return _SCRIPTS.run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
