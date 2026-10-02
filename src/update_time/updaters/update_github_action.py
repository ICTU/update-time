"""Pin each GitHub Action `uses:` in the GitHub directory to a commit, and bump it to the latest version."""

import re
from functools import partial
from typing import TYPE_CHECKING

from packaging.version import VERSION_PATTERN

from update_time.domain.file_type import GITHUB_WORKFLOWS
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.primitives.digest import COMMIT_SHA
from update_time.references.file import rewrite_file
from update_time.references.github import PinUpdater
from update_time.references.rewrite import updated_lines

if TYPE_CHECKING:
    from update_time.domain.reference import Reference

_LOG = get_logger("github action")
# A tag or branch name, and the end of a comment word that stands alone or before another comment.
_REF_CHARACTER = r"[\w.\-/]"
_REF = rf"{_REF_CHARACTER}+"
_ALONE = r"\s*(?:#|$)"
# Match a `uses:` reference: one already pinned to a commit SHA with a comment naming a version, a branch, or a tag
# (`<sha> # vX.Y.Z`, `<sha> # main`), or one unpinned (`@vX`, `@vX.Y.Z`). The tag group also takes an unpinned branch
# (`@main`), a tag such as `@stable-2024`, and a commit SHA. The dependency names an owner and a repository, which is
# what an action reference names, so `myaction@v1` is passed over. A local action carries no `@`, so it doesn't match at
# all. A tag or branch is read whole, so `v3-node20` and `release/v1` keep their names. The first word of a comment
# counts as a version when the character after it cannot continue a tag or branch name, as in `# v4.1.1, see notes`. It
# counts as a tag or branch only when it stands alone, or before another comment such as a marker. A lone `TODO` or
# `FIXME` counts as neither. Any other comment leaves the commit SHA bare, such as `# pinned by hand` or
# `# ratchet:actions/checkout@v4`.
_ACTION_RE = re.compile(
    r"uses: (?P<dependency>[\w\d\.-]+/[\w\d\./-]+)@"
    rf"(?:(?P<sha>{COMMIT_SHA}) # (?P<version>(?ix:{VERSION_PATTERN})(?!{_REF_CHARACTER})"
    rf"|(?!(?:TODO|FIXME){_ALONE}){_REF}(?={_ALONE}))"
    rf"|(?P<tag>{_REF}))"
)


def _spell_action(reference: Reference, sha: str, comment: str) -> str:
    """Return the `uses:` reference pinned to the commit SHA, with the tag or branch, if any, as a comment."""
    return f"uses: {reference.dependency}@{sha}" + (f" # {comment}" if comment else "")


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
