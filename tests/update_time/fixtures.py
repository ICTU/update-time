"""Test fixtures."""

from update_time.domain.marker import Marker

HASH1 = "a" * 64
HASH2 = "b" * 64
_HASH3 = "c" * 64
DIGEST = DIGEST1 = f"sha256:{HASH1}"
DIGEST2 = f"sha256:{HASH2}"
DIGEST3 = f"sha256:{_HASH3}"

# 40-character hex git commit SHAs, as a GitHub Action `uses:` or a pre-commit hook `rev:` pins to.
COMMIT_SHA = COMMIT_SHA1 = "a" * 40
COMMIT_SHA2 = "b" * 40

# The marker a bare `# update-time: ignore` expresses: every check the marker can hold back is held back.
BARE_IGNORE = Marker(ignore_update=True, ignore_stale=True, ignore_yanked=True, ignore_vulnerable=True)
