"""The shapes of the hashes a reference is pinned to, each stated once so that everything matching one agrees."""

SHA256_HEX_CHARS = 64  # The digest's hexadecimal characters, not counting the `sha256:` prefix
SHA256_DIGEST = rf"sha256:[0-9a-f]{{{SHA256_HEX_CHARS}}}"

_COMMIT_SHA_HEX_CHARS = 40  # The hexadecimal characters of the git commit SHA an action or a hook is pinned to
COMMIT_SHA = rf"[0-9a-f]{{{_COMMIT_SHA_HEX_CHARS}}}"
