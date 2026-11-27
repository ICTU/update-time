"""The shapes of the hashes a reference is pinned to, each stated once so that everything matching one agrees."""

from typing import Final

SHA256_HEX_CHARS: Final[int] = 64  # The digest's hexadecimal characters, not counting the `sha256:` prefix
SHA256_DIGEST: Final[str] = rf"sha256:[0-9a-f]{{{SHA256_HEX_CHARS}}}"

_COMMIT_SHA_HEX_CHARS: Final[int] = (
    40  # The hexadecimal characters of the git commit SHA an action or a hook is pinned to
)
COMMIT_SHA: Final[str] = rf"[0-9a-f]{{{_COMMIT_SHA_HEX_CHARS}}}"
_SHORT_COMMIT_SHA_HEX_CHARS: Final[int] = 7  # The hexadecimal characters git abbreviates a commit SHA to by default
SHORT_COMMIT_SHA: Final[str] = rf"[0-9a-f]{{{_SHORT_COMMIT_SHA_HEX_CHARS},{_COMMIT_SHA_HEX_CHARS - 1}}}"
