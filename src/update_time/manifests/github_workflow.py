"""Read and spell the GitHub Action references of workflows and composite action definitions."""

import re
from typing import TYPE_CHECKING

from packaging.version import VERSION_PATTERN

from update_time.primitives.digest import COMMIT_SHA

if TYPE_CHECKING:
    from update_time.domain.reference import Reference

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
ACTION_REFERENCE = re.compile(
    r"uses: (?P<dependency>[\w\d\.-]+/[\w\d\./-]+)@"
    rf"(?:(?P<sha>{COMMIT_SHA}) # (?P<version>(?ix:{VERSION_PATTERN})(?!{_REF_CHARACTER})"
    rf"|(?!(?:TODO|FIXME){_ALONE}){_REF}(?={_ALONE}))"
    rf"|(?P<tag>{_REF}))"
)


def spell_action(reference: Reference, sha: str, comment: str) -> str:
    """Return the `uses:` reference pinned to the commit SHA, with the tag or branch, if any, as a comment."""
    return f"uses: {reference.dependency}@{sha}" + (f" # {comment}" if comment else "")
