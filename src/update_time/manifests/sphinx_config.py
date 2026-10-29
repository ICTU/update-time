"""Read and spell the jsDelivr URLs of Sphinx configs, and the integrity hashes of their attribute dictionaries."""

import re

# A jsDelivr npm URL. The file path after the version is captured, since the integrity hash is the hash of that file
# rather than of the package's default file.
URL_REFERENCE = re.compile(
    r'https://cdn\.jsdelivr\.net/npm/(?P<dependency>[\w-]+)@(?P<version>[\d.]+)(?P<filename>/[^"]*)'
)

# The attribute dictionary a URL is accompanied by, and the Subresource Integrity hash it declares. The hash is
# optional, so a dictionary that declares none matches too.
ATTRIBUTES = re.compile(r'(?P<open>\{)(?:"integrity": "(?P<sha>sha\d+-[A-Za-z0-9+/=]+)", )?')


def spell_integrity(sha: str) -> str:
    """Return the opening of an attribute dictionary that declares the integrity hash in front of its other entries."""
    return f'{{"integrity": "{sha}", '
