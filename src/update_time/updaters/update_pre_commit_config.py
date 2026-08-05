"""Pre-commit config updater bumps the `rev:` of each GitHub-hosted hook repository to its latest version."""

import re
from functools import partial
from typing import TYPE_CHECKING

from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.references.file import rewrite_file
from update_time.references.github import PinUpdater
from update_time.references.rewrite import apply_marker
from update_time.sources.github import github_owner_and_repository

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.line import Line
    from update_time.domain.version import DependencyVersion, Reference

_LOG = get_logger("pre-commit config")

# The pre-commit configuration file, read from the repository root but supported per sub-project too (a monorepo can
# carry one per package), so it is looked up recursively from the scan root.
_PRE_COMMIT_CONFIG = ".pre-commit-config.yaml"

# Match a `repo:` key and capture its value: a repository URL (`https://github.com/owner/repo`), or the `local` /
# `meta` sentinels that carry no `rev:`. The value sets the repository the following `rev:` lines belong to.
_REPO_RE = re.compile(r"repo:\s*(?P<repo>\S+)")
# Match a `rev:` value that is either already pinned to a commit SHA with a pre-commit `# frozen: <version>` comment
# (`rev: <sha> # frozen: v4.5.0`) or unpinned to a version tag (`rev: v4.5.0`, optionally quoted). A bare commit SHA
# without a frozen comment doesn't resolve to a version, so it falls through to the tag branch and is rejected as a
# non-version by the `is_valid` check in `_update_rev`, like a branch name. The tag stops at whitespace, a quote, or
# a `#`, so a trailing `# update-time:` marker (or any comment) is left outside the match and preserved.
_REV_RE = re.compile(
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


def _spell_rev(reference: Reference, latest: DependencyVersion) -> str:
    """Return the `rev:` pinned to the latest version's commit SHA, with the version in a `# frozen:` comment."""
    frozen_version = f"v{latest.version}" if reference.current_version.startswith("v") else latest.version
    return f"rev: {latest.sha}  # frozen: {frozen_version}"


_REV = PinUpdater(_spell_rev, _LOG)


def _updated_lines(lines: list[Line]) -> list[str]:
    """Return the config's lines with every GitHub-hosted hook's `rev:` pinned or bumped, honouring markers.

    Unlike the single-line references most updaters rewrite, a hook's repository and its `rev:` sit on separate
    lines, so the pass tracks the repository from each `repo:` line and applies it to the `rev:` lines that follow.
    A `rev:` whose repository is not on GitHub carries no dependency, so it is left untouched.
    """
    result = []
    dependency = ""  # The GitHub owner/repository of the `repo:` in scope, or "" for a local/meta/non-GitHub repo.
    for line in lines:
        if repo_match := _REPO_RE.search(line.text):
            dependency = _dependency_from_repo(repo_match.group("repo"))
            result.append(line.text)
        elif (rev_match := _REV_RE.search(line.text)) and dependency:
            update_line = partial(_REV.update_line, dependency=dependency)
            result.append(apply_marker(line, rev_match, update_line, _LOG, dependency))
        else:
            result.append(line.text)
    return result


def update_pre_commit_configs(start: Path | None = None) -> None:
    """Update the hook revs in all `.pre-commit-config.yaml` files found recursively from the start directory."""
    for config in glob(_PRE_COMMIT_CONFIG, start=start):
        rewrite_file(config, _updated_lines, _LOG)


def main() -> None:  # pragma: no cover
    """Update the hook revs in the repository's pre-commit configuration files."""
    update_pre_commit_configs()


if __name__ == "__main__":  # pragma: no cover
    main()
