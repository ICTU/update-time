"""GitHub Action updater script finds YAML files in the GitHub directory and updates 'uses' keys to latest versions."""

import re
from functools import partial
from typing import TYPE_CHECKING

from update_time.domain.file_type import GITHUB_WORKFLOWS
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.primitives.digest import COMMIT_SHA
from update_time.references.file import rewrite_file
from update_time.references.github import PinUpdater
from update_time.references.rewrite import updated_lines

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyVersion
    from update_time.domain.reference import Reference

_LOG = get_logger("github action")
# Match a `uses:` reference: one already pinned to a commit SHA with a version comment (`<sha> # vX.Y.Z`), one
# unpinned to a version tag (`@vX` / `@vX.Y.Z`), and one naming a branch (`@main`), whose repository is checked for
# staleness although no update is resolved for it. The dependency names an owner and a repository, which is what an
# action reference names, so `myaction@v1` is passed over. A local action carries no `@`, so it doesn't match at all.
_ACTION_RE = re.compile(
    r"uses: (?P<dependency>[\w\d\.-]+/[\w\d\./-]+)@"
    rf"(?:(?P<sha>{COMMIT_SHA}) # v?(?P<version>[\d\w\.\-]+)|v?(?P<tag>[\d\w\.\-]+))"
)


def _spell_action(reference: Reference, latest: DependencyVersion) -> str:
    """Return the `uses:` reference pinned to the latest version's commit SHA, with the version as a comment.

    The SHA is the latest version's, or — for a reference adopting a moved tag — that tag's new commit.
    """
    return f"uses: {reference.dependency}@{latest.sha} # v{latest.version}"


_ACTION = PinUpdater(_spell_action, _LOG)


def update_github_actions() -> None:
    """Update the GitHub Actions in all YAML files under the GitHub directory, including composite actions."""
    pin_the_references = partial(updated_lines, regexp=_ACTION_RE, update_line=_ACTION.update_line, logger=_LOG)
    for yaml_file in glob_for(GITHUB_WORKFLOWS):
        rewrite_file(yaml_file, pin_the_references, _LOG)


def main() -> None:  # pragma: no cover
    """Update the GitHub Actions in the repository's workflows."""
    update_github_actions()


if __name__ == "__main__":  # pragma: no cover
    main()
