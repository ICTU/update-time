"""Read and spell the jsDelivr URLs of Sphinx configs, and the integrity hashes of their attribute dictionaries."""

import re
from typing import Final

# A jsDelivr npm URL. The file path after the version is captured, since the integrity hash is the hash of that file
# rather than of the package's default file.
URL_REFERENCE: Final[re.Pattern[str]] = re.compile(
    r"""
    https://cdn\.jsdelivr\.net/npm/
    (?P<dependency>[\w-]+)  # the package
    @(?P<version>[\d.]+)  # its version
    (?P<filename>/[^"]*)  # the path of the file
    """,
    re.VERBOSE,
)

# The attribute dictionary a URL is accompanied by, and the Subresource Integrity hash it declares. The hash is
# optional, so a dictionary that declares none matches too.
ATTRIBUTES: Final[re.Pattern[str]] = re.compile(
    r"""
    (?P<open>\{)  # the dictionary's opening brace
    (?:"integrity":\ "(?P<sha>sha\d+-[A-Za-z0-9+/=]+)",\ )?  # the integrity hash, if any
    """,
    re.VERBOSE,
)


def spell_integrity(sha: str) -> str:
    """Return the opening of an attribute dictionary that declares the integrity hash in front of its other entries."""
    return f'{{"integrity": "{sha}", '
