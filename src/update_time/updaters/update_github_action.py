"""GitHub Action updater script finds YAML files in the GitHub directory and updates 'uses' keys to latest versions.

Actions referenced by a version tag only (e.g. `@v4`) are automatically pinned to the commit SHA of the latest
version, with the version added as a trailing comment. Already-pinned actions are bumped to the latest version.

If an environment variable GITHUB_TOKEN is set, the script will use it to increase the GitHub rate limit.
"""

import re
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.io.filesystem import YAML_GLOB_PATTERNS, glob
from update_time.io.log import get_logger
from update_time.references.file import rewrite_file
from update_time.references.github import PinUpdater
from update_time.references.rewrite import updated_lines

if TYPE_CHECKING:
    from update_time.domain.line import Line
    from update_time.domain.version import DependencyVersion, Reference

LOG = get_logger("github action")
# Match a `uses:` reference that is either already pinned to a commit SHA with a version comment
# (`<sha> # vX.Y.Z`) or unpinned to a version tag (`@vX` / `@vX.Y.Z`). Branch references (e.g. `@main`) and
# local actions (no `@`) don't carry a resolvable version, so they don't match and are left untouched.
ACTION_RE = re.compile(
    r"uses: (?P<dependency>[\w\d\./-]+)@"
    r"(?:(?P<sha>[a-f0-9]{40}) # v?(?P<version>[\d\w\.\-]+)|v(?P<tag>[\d\w\.\-]+))"
)


def _spell_action(reference: Reference, latest: DependencyVersion) -> str:
    """Return the `uses:` reference pinned to the latest version's commit SHA, with the version as a comment.

    The SHA is the latest version's, or — for a reference adopting a moved tag — that tag's new commit.
    """
    return f"uses: {reference.dependency}@{latest.sha} # v{latest.version}"


_ACTION = PinUpdater(_spell_action, LOG)


def _updated_lines(lines: list[Line]) -> list[str]:
    """Return the file's lines with every `uses:` reference pinned or bumped, honouring markers."""
    return updated_lines(lines, ACTION_RE, _ACTION.update_line, LOG)


def update_github_actions(github_dir: Path) -> None:
    """Update the GitHub Actions in all YAML files under the GitHub directory, including composite actions."""
    for yaml_file in glob(*YAML_GLOB_PATTERNS, start=github_dir):
        rewrite_file(yaml_file, _updated_lines, LOG)


def main() -> None:  # pragma: no cover
    """Update the GitHub Actions in the repository's workflows."""
    update_github_actions(Path.cwd() / ".github")


if __name__ == "__main__":  # pragma: no cover
    main()
