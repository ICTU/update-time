"""Command-line interface."""

import argparse
import os
from importlib.metadata import version
from pathlib import Path

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.vulnerability import (
    IGNORE_VULNERABILITIES,
    NO_RISK_LEVEL,
    RISK_LEVELS,
    VULNERABILITY_LEVEL,
)
from update_time.io.filesystem import ALWAYS_IGNORED_DIRECTORIES, inside_git_repository
from update_time.io.log import LOG_LEVEL, LOG_LEVELS


def days(value: str) -> int:
    """Parse the value as a non-negative integer number of days."""
    if (number := int(value)) < 0:
        message = f"{value} is not a non-negative integer"
        raise argparse.ArgumentTypeError(message)
    return number


def _directory(value: str) -> Path:
    """Parse the value as the path to an existing directory."""
    if not (path := Path(value)).is_dir():
        message = f"{value} is not an existing directory"
        raise argparse.ArgumentTypeError(message)
    return path


def _exclude_paths(value: str) -> list[Path]:
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


def _advisories(value: str) -> frozenset[str]:
    """Parse a comma-separated list of advisory identifiers, each naming an advisory not to warn about."""
    return frozenset(stripped for entry in value.split(",") if (stripped := entry.strip()))


def parse_args() -> argparse.Namespace:
    """Parse the command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="update-time",
        description="Scan the PATH for pinned dependencies and update them to their latest versions, rewriting the "
        "pinned versions in place. Looks at pyproject.toml, requirements.txt, Python PEP 723 inline script metadata, "
        ".python-version files, package.json, Dockerfiles, GitHub Actions workflows, pre-commit configs, CircleCI "
        "configs, GitLab CI configs, Docker Compose and Helm manifests, devcontainer configs, and jsDelivr URLs. A "
        "cooldown period holds back releases that are too fresh to trust.",
        epilog="Update-time exits with status 0 when it ran successfully, 1 when an error prevented it from finishing, "
        "and 2 when any command-line argument was invalid, including a PATH that is not inside a git repository "
        "(unless --force is passed). Exit status does not indicate whether anything was updated. Inspect the diff or "
        "the INFO-level log for that.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"v{version('update-time')}")
    parser.add_argument(
        "path",
        nargs="?",
        type=_directory,
        default=Path(),
        metavar="PATH",
        help="the directory to scan recursively for dependencies to update; paths in the log are reported relative to "
        "it (default: the current directory)",
    )
    parser.add_argument(
        "--cooldown",
        type=days,
        default=COOLDOWN.default,
        metavar="DAYS",
        help="number of days to hold back newly published Docker image, GitHub Action, pre-commit hook, "
        "requirements.txt, npm, pnpm, pyproject.toml, Python inline script metadata, .python-version, and jsDelivr "
        "versions, except for references that set a cooldown of their own with an # update-time: "
        "ignore[cooldown<DAYS] marker (default: %(default)s)",
    )
    parser.add_argument(
        "--stale-after",
        type=days,
        default=STALE_AFTER.default,
        metavar="DAYS",
        help="warn when a dependency's newest release is older than this many days; 0 disables the check, except "
        "for references that set a threshold of their own with an # update-time: ignore[stale<DAYS] marker "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--vulnerability-level",
        type=str.lower,
        choices=[*RISK_LEVELS, NO_RISK_LEVEL],
        default=VULNERABILITY_LEVEL.default,
        help="warn about a known vulnerability in the version a dependency is pinned to when the advisory's risk "
        "level is at least this severe; a vulnerability whose risk level cannot be read is always warned about. "
        f"Pass {NO_RISK_LEVEL} to switch the check off, which queries the advisory database not at all, except for "
        "references that set a level of their own with an # update-time: ignore[vulnerable<LEVEL] marker "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--ignore-vulnerability",
        type=_advisories,
        default=IGNORE_VULNERABILITIES.default,
        metavar="IDS",
        help="comma-separated list of advisories to never warn about, wherever in the scan they turn up, for "
        "example GHSA-2gwj-7jmv-h26r,CVE-2021-31542. An advisory can be named by any of the identifiers it is known "
        "by. To silence one for a single reference instead, mark that reference with an # update-time: "
        "ignore[vulnerable=ID] marker",
    )
    parser.add_argument(
        "--exclude-path",
        type=_exclude_paths,
        default=[],
        metavar="PATHS",
        help="comma-separated list of directories, relative to the scan root, to exclude from the scan, for example "
        "vendor,packages/legacy. Every file under an excluded directory is skipped, on top of the always-ignored "
        f"{', '.join(ALWAYS_IGNORED_DIRECTORIES)}, and hidden folders. "
        "Directories are matched by relative path, not by name: "
        "--exclude-path vendor excludes vendor/ at the root but not sub/vendor/. Directories that don't exist are "
        "ignored and logged at log-level WARNING. Absolute paths, or paths that escape the scan root (../…), "
        "are rejected. Run with --log-level DEBUG to see excluded directories",
    )
    parser.add_argument(
        "--allow-hash-drift",
        action="store_true",
        help="when an already-pinned image tag has been re-pushed, or a pinned version tag has been moved to another "
        "commit, adopt the new digest or commit instead of only warning; equivalent to marking every reference with "
        "# update-time: allow[hash-drift] (an # update-time: ignore marker still wins)",
    )
    parser.add_argument(
        "--allow-floating-pin",
        action="store_true",
        help="keep every floating image tag in the scan as it is, instead of pinning it to the version and digest "
        "it currently serves; equivalent to marking every reference with # update-time: allow[floating-pin] (an "
        "# update-time: ignore[floating-pin] marker still pins that reference)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="run even when not inside a git repository (changes are made in place and cannot be reverted)",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=LOG_LEVELS,
        default=LOG_LEVEL.default,
        help="the minimum severity of messages to log; available new versions are logged at INFO (default: "
        "%(default)s)",
    )
    args = parser.parse_args()
    # Treat a PATH outside a git repository as an invalid argument and exit with status 2, unless overridden by --force.
    # Resolve the (possibly relative) PATH first so the walk up to the root has parents to visit.
    if not args.force and not inside_git_repository(args.path.resolve()):
        parser.error(
            f"{args.path} is not inside a git repository; rerun inside a repository so changes can be reverted, or "
            "pass --force to run anyway"
        )
    return args
