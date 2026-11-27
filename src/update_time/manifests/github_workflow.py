"""Read and spell the GitHub Action references of workflows and composite action definitions."""

import re
from typing import TYPE_CHECKING, Final

from packaging.version import VERSION_PATTERN

from update_time.primitives.digest import COMMIT_SHA

if TYPE_CHECKING:
    from update_time.domain.reference import Reference

# A tag or branch name, and the end of a comment word that stands alone or before another comment.
_REF_CHARACTER: Final[str] = r"[\w.\-/]"
_REF: Final[str] = rf"{_REF_CHARACTER}+"
_ALONE: Final[str] = r"\s*(?:#|$)"
# Match a `uses:` reference that names an owner and a repository, as an action reference does:
# - An unpinned reference names a version (`@vX`, `@vX.Y.Z`), a branch (`@main`), a tag such as `@stable-2024`, or a
#   commit SHA without a comment.
# - A reference pinned to a commit SHA names a version, branch, or tag in its comment (`<sha> # vX.Y.Z`).
# - The first word of the comment counts as a version when the character after it cannot continue a tag or branch name,
#   as in `# v4.1.1, see notes`.
# - The first word counts as a tag or branch only when it stands alone, or before another comment such as a marker. A
#   lone `TODO` or `FIXME` counts as neither.
# - Any other comment leaves the commit SHA bare, such as `# pinned by hand` or `# ratchet:actions/checkout@v4`.
# - A tag or branch is read whole, so `v3-node20` and `release/v1` keep their names.
# - `myaction@v1` does not name an owner, so it is passed over.
# - A local action does not carry an `@`, so it never matches.
ACTION_REFERENCE: Final[re.Pattern[str]] = re.compile(
    rf"uses: (?P<dependency>[\w\d\.-]+/[\w\d\./-]+)@(?:(?P<sha>{COMMIT_SHA}) # )?"
    rf"(?P<version>(?(sha)(?:(?ix:{VERSION_PATTERN})(?!{_REF_CHARACTER})"
    rf"|(?!(?:TODO|FIXME){_ALONE}){_REF}(?={_ALONE}))"
    rf"|{_REF}))"
)


def spell_action(reference: Reference, sha: str, comment: str) -> str:
    """Return the `uses:` reference pinned to the commit SHA, with the tag or branch, if any, as a comment."""
    return f"uses: {reference.dependency}@{sha}{f' # {comment}' if comment else ''}"
