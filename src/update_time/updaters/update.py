"""Update all dependencies by running the individual updater scripts."""

import os
import platform
import signal
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from signal import Signals
from typing import TYPE_CHECKING

from update_time.domain.archival import IGNORE_ARCHIVED
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency_type import DEPENDENCY_TYPES, DependencyType
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.vulnerability import IGNORE_VULNERABILITIES, VULNERABILITY_LEVEL
from update_time.io.cli import VERSION, parse_args
from update_time.io.filesystem import EXCLUDE_PATHS, inside_git_repository
from update_time.io.log import get_logger, set_log_level
from update_time.io.network import report_network_settings
from update_time.io.process import Processes
from update_time.markers.drift import ALLOW_HASH_DRIFT
from update_time.markers.floating import ALLOW_FLOATING_PIN
from update_time.primitives.command import Command
from update_time.sources import docker_hub, github

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from update_time.io.log import Logger

_SRC = Path(__file__).parent


@dataclass(frozen=True)
class _Script:
    """An updater script, and whether it may run beside the others."""

    name: str
    parallel: bool = True

    @property
    def command(self) -> Command:
        """Return the command that runs the script."""
        return Command(sys.executable, str(_SRC / f"update_{self.name}.py"))


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

    def dependency_type(self, script: _Script) -> DependencyType:
        """Return the dependency type the script serves."""
        return next(dependency_type for dependency_type, scripts in self.scripts.items() if script in scripts)

    def run(self, processes: Processes, logger: Logger) -> int:
        """Run every updater script and return the highest exit code, counting a script a signal stopped as 1.

        The scripts that may run beside another go first and run concurrently; the rest follow one at a time.
        """

        def result(script: _Script, exit_code: int) -> int:
            if exit_code >= 0 or processes.stop_signal is not None:
                return exit_code
            logger.update_stopped(self.dependency_type(script).name, Signals(-exit_code))
            return 1

        parallel = self.parallel_scripts
        results = list(map(result, parallel, processes.run(*(script.command for script in parallel)), strict=True))
        results.extend(result(script, *processes.run(script.command)) for script in self.sequential_scripts)
        return max(results, default=0)


# The updater scripts serving each dependency type. package_json rewrites the package.json files that node_engine
# rewrites too, so package_json may not run beside another script.
_SCRIPTS = _Scripts(
    {
        DEPENDENCY_TYPES.python_dependencies: (
            _Script("pyproject_toml"),
            _Script("requirements_txt"),
            _Script("python_inline_script_metadata"),
        ),
        DEPENDENCY_TYPES.npm_dependencies: (_Script("package_json", parallel=False),),
        DEPENDENCY_TYPES.maven_dependencies: (_Script("pom_xml"),),
        DEPENDENCY_TYPES.node_engine_version: (_Script("node_engine"),),
        DEPENDENCY_TYPES.python_version: (_Script("python_version_file"),),
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


_STOP_SIGNALS = (Signals.SIGINT, Signals.SIGQUIT, Signals.SIGTERM, Signals.SIGHUP)

# The environment variables holding the credentials that raise the rate limits of GitHub and Docker Hub.
_CREDENTIALS = (github.TOKEN_VARIABLE, docker_hub.USERNAME_VARIABLE, docker_hub.TOKEN_VARIABLE)


def _report_context(logger: Logger) -> None:
    """Log the version, the platform, the arguments, the credentials, and the network settings the run starts with."""
    logger.running(VERSION, platform.platform(), platform.python_version())
    logger.arguments(sys.argv[1:])
    logger.credentials({name: bool(os.environ.get(name)) for name in _CREDENTIALS})
    report_network_settings(logger)


def _stop(processes: Processes, logger: Logger, signal_number: int, _frame: object) -> None:
    """Log the signal, and stop the updaters running and the ones still to start."""
    logger.stopped(Signals(signal_number))
    processes.stop(Signals(signal_number))


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
    # Pass the cooldown, thresholds, ignored advisories, the archival check, log level, and the drift and
    # floating-pin opt-ins down to the subprocesses via the environment.
    COOLDOWN.set(args.cooldown)
    STALE_AFTER.set(args.stale_after)
    VULNERABILITY_LEVEL.set(args.vulnerability_level)
    IGNORE_VULNERABILITIES.set(args.ignore_vulnerability)
    IGNORE_ARCHIVED.set(args.ignore_archived)
    set_log_level(args.log_level)
    ALLOW_HASH_DRIFT.set(args.allow_hash_drift)
    ALLOW_FLOATING_PIN.set(args.allow_floating_pin)
    logger = get_logger(__name__)
    _report_context(logger)
    # parse_args has already refused to run outside a git repository unless --force was given; when it was, warn that
    # the in-place rewrites cannot be reverted.
    scan_root = Path.cwd()
    if not inside_git_repository(scan_root):
        logger.forced_outside_git_repository(scan_root)
    _configure_excluded_paths(args.exclude_path)
    processes = Processes()
    for signal_number in _STOP_SIGNALS:
        signal.signal(signal_number, partial(_stop, processes, logger))
    exit_code = _SCRIPTS.run(processes, logger)
    # A stopped run exits as a shell reports a signal
    return exit_code if processes.stop_signal is None else 128 + processes.stop_signal


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
