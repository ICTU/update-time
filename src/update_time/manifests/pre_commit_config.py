"""Read and spell the hook repositories and revs of pre-commit configs."""

import re
from typing import TYPE_CHECKING

from update_time.primitives.digest import COMMIT_SHA

if TYPE_CHECKING:
    from update_time.domain.reference import Reference

# Match a `repo:` key and capture its value: a repository URL (`https://github.com/owner/repo`), or the `local` /
# `meta` sentinels that carry no `rev:`. The value sets the repository the following `rev:` lines belong to. It may be
# quoted, and the quotes are left outside the value.
REPOSITORY_KEY = re.compile(r"repo:\s*(?P<quote>['\"]?)(?P<repo>[^\s'\"]+)(?P=quote)")
# Match a `rev:` value that is either already pinned to a commit SHA with a pre-commit `# frozen:` comment naming a
# version or a branch (`rev: <sha> # frozen: v4.5.0`, `rev: <sha> # frozen: main`), or unpinned (`rev: v4.5.0`,
# optionally quoted). The tag group also takes an unpinned branch, a tag such as `stable-2024`, and a bare commit
# SHA. The tag stops at whitespace, a quote, or a `#`, so a trailing `# update-time:` marker (or any comment) is left
# outside the match and preserved.
REV_REFERENCE = re.compile(
    r"rev:\s*"
    rf"(?:(?P<sha>{COMMIT_SHA})\s*#\s*frozen:\s*(?P<version>\S+)|(?P<quote>['\"]?)(?P<tag>[^\s'\"#]+)(?P=quote))"
)


def spell_rev(_reference: Reference, sha: str, comment: str) -> str:
    """Return the `rev:` pinned to the commit SHA, with the tag or branch, if any, in a `# frozen:` comment."""
    return f"rev: {sha}" + (f"  # frozen: {comment}" if comment else "")
