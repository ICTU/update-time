"""The shape of a `sha256:` digest, stated once so that everything matching one matches the same thing."""

SHA256_HEX_CHARS = 64  # The digest's hexadecimal characters, not counting the `sha256:` prefix
SHA256_DIGEST = rf"sha256:[0-9a-f]{{{SHA256_HEX_CHARS}}}"
