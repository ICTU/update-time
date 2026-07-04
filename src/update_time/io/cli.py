"""Command-line interface."""

import argparse
from importlib.metadata import version
from pathlib import Path

from update_time.domain.cooldown import COOLDOWN_DAYS
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
        "--log-level",
        type=str.upper,
        choices=LOG_LEVELS,
        default=DEFAULT_LOG_LEVEL,
        help="the minimum severity of messages to log; available new versions are logged at INFO (default: "
        "%(default)s)",
    )
    return parser.parse_args()
