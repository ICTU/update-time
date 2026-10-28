"""Pin the `rev:` of each GitHub-hosted hook in pre-commit configs to a commit, and bump it to the latest version."""

from functools import partial
from typing import TYPE_CHECKING

from update_time.domain.file_type import PRE_COMMIT_CONFIG
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.pre_commit_config import REPOSITORY_KEY, REV_REFERENCE, spell_rev
from update_time.references.file import rewrite_file
from update_time.references.github import PinUpdater
from update_time.references.rewrite import apply_marker
from update_time.sources.github import github_repository

if TYPE_CHECKING:
    from update_time.domain.line import Line

_LOG = get_logger("pre-commit config")
_REV = PinUpdater(spell_rev, _LOG)


def _updated_lines(lines: list[Line]) -> list[str]:
    """Return the config's lines with every GitHub-hosted hook's `rev:` pinned or bumped, honouring markers.

    Unlike the single-line references most updaters rewrite, a hook's repository and its `rev:` sit on separate
    lines, so the pass tracks the repository from each `repo:` line and applies it to the `rev:` lines that follow.
    A `rev:` whose repository is not on GitHub carries no dependency, so it is left untouched.
    """
    result = []
    repository = None  # The repository of the `repo:` in scope, None for `local`, `meta`, and a host other than GitHub
    for line in lines:
        if repo_match := REPOSITORY_KEY.search(line.text):
            repository = github_repository(repo_match.group("repo"))
            result.append(line.text)
        elif (rev_match := REV_REFERENCE.search(line.text)) and repository is not None:
            dependency = str(repository)
            update_line = partial(_REV.update_line, dependency=dependency)
            result.append(apply_marker(line, rev_match, update_line, _LOG, dependency))
        else:
            result.append(line.text)
    return result


def update_pre_commit_configs() -> None:
    """Update the hook revs in all `.pre-commit-config.yaml` files found recursively from the start directory."""
    for config in glob_for(PRE_COMMIT_CONFIG):
        rewrite_file(config, _updated_lines, _LOG)


def main() -> None:  # pragma: no cover
    """Update the hook revs in the repository's pre-commit configuration files."""
    update_pre_commit_configs()


if __name__ == "__main__":  # pragma: no cover
    main()
