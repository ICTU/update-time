"""Command-line interface."""

import argparse
from importlib.metadata import version

from update_time.domain.cooldown import COOLDOWN_DAYS


def non_negative_int(value: str) -> int:
    """Parse the value as a non-negative integer number of days."""
    if (days := int(value)) < 0:
        message = f"{value} is not a non-negative integer"
        raise argparse.ArgumentTypeError(message)
    return days


def parse_args() -> argparse.Namespace:
    """Parse the command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="update-time",
        description="Scan the current repository for pinned dependencies and update them to their latest versions, "
        "rewriting the pinned versions in place. Looks at pyproject.toml, package.json, Dockerfiles, GitHub Actions "
        "workflows, CircleCI configs, Docker Compose and Helm manifests, and jsDelivr URLs. A cooldown period holds "
        "back releases that are too fresh to trust.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"v{version('update-time')}")
    parser.add_argument(
        "--cooldown",
        type=non_negative_int,
        default=COOLDOWN_DAYS,
        metavar="DAYS",
        help="number of days to hold back newly published Docker image and GitHub Action versions; Python and npm "
        "cooldowns are configured via uv and npm instead (default: %(default)s)",
    )
    return parser.parse_args()
