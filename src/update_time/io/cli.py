"""Command-line interface."""

import argparse
import os
from importlib.metadata import version
from pathlib import Path

from update_time.domain.cooldown import COOLDOWN_DAYS
from update_time.domain.staleness import STALE_AFTER_DAYS
from update_time.io.log import DEFAULT_LOG_LEVEL, LOG_LEVELS


def days(value: str) -> int:
    """Parse the value as a non-negative integer number of days."""
    if (number := int(value)) < 0:
        message = f"{value} is not a non-negative integer"
        raise argparse.ArgumentTypeError(message)
    return number


def directory(value: str) -> Path:
    """Parse the value as the path to an existing directory."""
    if not (path := Path(value)).is_dir():
        message = f"{value} is not an existing directory"
        raise argparse.ArgumentTypeError(message)
    return path


def exclude_paths(value: str) -> list[Path]:
    """Parse a comma-separated list of directories to exclude from the walk, each relative to the scan root.

    Entries are normalised (trailing separators and redundant `.`/`..` segments collapsed). An absolute path, or one
    that escapes the scan root (`../…`), is rejected: the option only narrows the tree, it can't redirect the walk.
    """
    paths = []
    for entry in value.split(","):
        if not (stripped := entry.strip()):
            continue
        if (path := Path(stripped)).is_absolute():
            message = f"{stripped} is not a relative path"
            raise argparse.ArgumentTypeError(message)
        normalized = Path(os.path.normpath(path))
        if normalized.parts and normalized.parts[0] == "..":
            message = f"{stripped} is outside the scan root"
            raise argparse.ArgumentTypeError(message)
        paths.append(normalized)
    return paths


def parse_args() -> argparse.Namespace:
    """Parse the command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="update-time",
        description="Scan the current repository for pinned dependencies and update them to their latest versions, "
        "rewriting the pinned versions in place. Looks at pyproject.toml, requirements.txt, package.json, Dockerfiles, "
        "GitHub Actions workflows, CircleCI configs, GitLab CI configs, Docker Compose and Helm manifests, "
        "devcontainer configs, and jsDelivr URLs. A cooldown period holds back releases that are too fresh to trust.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"v{version('update-time')}")
    parser.add_argument(
        "path",
        nargs="?",
        type=directory,
        default=Path(),
        metavar="PATH",
        help="the directory to scan for dependencies to update; paths in the log are reported relative to it "
        "(default: the current directory)",
    )
    parser.add_argument(
        "--cooldown",
        type=days,
        default=COOLDOWN_DAYS,
        metavar="DAYS",
        help="number of days to hold back newly published Docker image, GitHub Action, requirements.txt, npm, pnpm, "
        "pyproject.toml, and jsDelivr versions (default: %(default)s)",
    )
    parser.add_argument(
        "--stale-after",
        type=days,
        default=STALE_AFTER_DAYS,
        metavar="DAYS",
        help="warn when a dependency's newest release is older than this many days; 0 disables the check "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--exclude-path",
        type=exclude_paths,
        default=[],
        metavar="PATHS",
        help="comma-separated list of directories, relative to the scan root, to exclude from the walk (e.g. "
        "vendor,packages/legacy); every file under an excluded directory is skipped, on top of the always-ignored "
        "build, node_modules, __pycache__, and hidden folders",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=LOG_LEVELS,
        default=DEFAULT_LOG_LEVEL,
        help="the minimum severity of messages to log; available new versions are logged at INFO (default: "
        "%(default)s)",
    )
    return parser.parse_args()
