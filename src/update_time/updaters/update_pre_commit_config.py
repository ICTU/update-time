"""Pre-commit config updater bumps the `rev:` of each GitHub-hosted hook repository to its latest version.

A rev given as a version tag only (e.g. `rev: v4.5.0`) is pinned to the commit SHA of the latest version, with the
version travelling in pre-commit's own `# frozen: <version>` comment convention — the same format `pre-commit
autoupdate --freeze` produces and understands. A rev already pinned to a commit SHA with such a comment is bumped to
the SHA of the latest version, exactly like a GitHub Action reference. `repo: local` and `repo: meta` entries, revs
that are a branch name rather than a version, and repositories hosted outside GitHub are left untouched.

If an environment variable GITHUB_TOKEN is set, the script will use it to increase the GitHub rate limit.
"""

import re
from functools import partial
from itertools import pairwise
from typing import TYPE_CHECKING

from update_time.domain.marker import parse_marker
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.references.file import rewrite_file
from update_time.references.github import GitHubReference, latest_pin
from update_time.references.rewrite import apply_marker
from update_time.sources.github import github_owner_and_repository

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.marker import Marker

LOG = get_logger("pre-commit config")

# The pre-commit configuration file, read from the repository root but supported per sub-project too (a monorepo can
# carry one per package), so it is looked up recursively from the scan root.
PRE_COMMIT_CONFIG = ".pre-commit-config.yaml"

# Match a `repo:` key and capture its value: a repository URL (`https://github.com/owner/repo`), or the `local` /
# `meta` sentinels that carry no `rev:`. The value sets the repository the following `rev:` lines belong to.
REPO_RE = re.compile(r"repo:\s*(?P<repo>\S+)")
# Match a `rev:` value that is either already pinned to a commit SHA with a pre-commit `# frozen: <version>` comment
# (`rev: <sha> # frozen: v4.5.0`) or unpinned to a version tag (`rev: v4.5.0`, optionally quoted). A bare commit SHA
# without a frozen comment doesn't resolve to a version, so it falls through to the tag branch and is rejected as a
# non-version by the `is_valid` check in `_update_rev`, like a branch name. The tag stops at whitespace, a quote, or
# a `#`, so a trailing `# update-time:` marker (or any comment) is left outside the match and preserved.
REV_RE = re.compile(
    r"rev:\s*"
    r"(?:(?P<sha>[0-9a-f]{40})\s*#\s*frozen:\s*(?P<version>\S+)|(?P<quote>['\"]?)(?P<tag>[^\s'\"#]+)(?P=quote))"
)


def _dependency_from_repo(repo: str) -> str:
    """Return the GitHub `owner/repository` for a `repo:` value, or an empty string when it isn't on GitHub.

    Covers the `local` and `meta` sentinels and non-GitHub hosts alike: `github_owner_and_repository` returns two
    empty strings for anything it can't parse as a GitHub URL, so those all collapse to an empty dependency, which
    tells the caller to leave the following `rev:` lines untouched.
    """
    owner, repository = github_owner_and_repository(repo.strip("'\""))
    return f"{owner}/{repository}" if owner and repository else ""


def _update_rev(match: re.Match[str], dependency: str, path: Path, marker: Marker) -> str:
    """Pin or bump a single `rev:` to the latest version's commit SHA, or leave it unchanged.

    A hook `rev:` pinned to a commit SHA with a `# frozen: v4.5.0` comment, or referenced by version tag only, is the
    same GitHub-SHA-pinned reference as a GitHub Action `uses:`, so the decision is shared with `_update_action` via
    `latest_pin`; only the `rev:` output is spelled here. The version is echoed in the `# frozen:` comment keeping the
    tag's own `v` prefix convention, so the config stays interoperable with pre-commit's tooling.
    """
    current_sha = match.group("sha")
    current_version = match.group("version") if current_sha else match.group("tag")
    latest = latest_pin(GitHubReference(dependency, current_version, current_sha), marker, path, LOG)
    if latest is None:
        return match.string  # Invalid, held back, unpinnable, or already up to date: leave the reference as it is
    frozen_version = f"v{latest.version}" if current_version.startswith("v") else latest.version
    line = match.string
    return f"{line[: match.start()]}rev: {latest.sha}  # frozen: {frozen_version}{line[match.end() :]}"


def _updated_lines(lines: list[str], path: Path) -> list[str]:
    """Return the config's lines with every GitHub-hosted hook's `rev:` pinned or bumped, honouring markers.

    Unlike the single-line references most updaters rewrite, a hook's repository and its `rev:` sit on separate
    lines, so the pass tracks the repository from each `repo:` line and applies it to the `rev:` lines that follow.
    An `# update-time:` marker on the `rev:` line, or on a standalone comment directly above it, holds the reference
    back, bounds it, or (an unparsable item) leaves it unchanged; each is logged at debug level so users can confirm
    a marker was recognised. Each line is paired with the line before it, so a standalone marker comment can apply
    to the `rev:` below it.
    """
    result = []
    dependency = ""  # The GitHub owner/repository of the `repo:` in scope, or "" for a local/meta/non-GitHub repo.
    for previous_line, line in pairwise(["", *lines]):
        if repo_match := REPO_RE.search(line):
            dependency = _dependency_from_repo(repo_match.group("repo"))
            result.append(line)
        elif (rev_match := REV_RE.search(line)) and dependency:
            marker = parse_marker(line, previous_line)
            update = partial(_update_rev, rev_match, dependency, path, marker)
            result.append(apply_marker(line, dependency, marker, path, LOG, update))
        else:
            result.append(line)
    return result


def update_pre_commit_configs(start: Path | None = None) -> None:
    """Update the hook revs in all `.pre-commit-config.yaml` files found recursively from the start directory."""
    for config in glob(PRE_COMMIT_CONFIG, start=start):
        rewrite_file(config, partial(_updated_lines, path=config), LOG)


def main() -> None:  # pragma: no cover
    """Update the hook revs in the repository's pre-commit configuration files."""
    update_pre_commit_configs()


if __name__ == "__main__":  # pragma: no cover
    main()
