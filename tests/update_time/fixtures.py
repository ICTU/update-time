"""Test fixtures."""

HASH1 = "a" * 64
HASH2 = "b" * 64
HASH3 = "c" * 64
DIGEST = DIGEST1 = f"sha256:{HASH1}"
DIGEST2 = f"sha256:{HASH2}"
DIGEST3 = f"sha256:{HASH3}"

# 40-character hex git commit SHAs, as a GitHub Action `uses:` or a pre-commit hook `rev:` pins to.
COMMIT_SHA1 = "a" * 40
COMMIT_SHA2 = "b" * 40
