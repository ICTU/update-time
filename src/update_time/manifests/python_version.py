"""Read the CPython version a `.python-version` file pins."""

import re
from typing import Final

# The dependency every `.python-version` entry names. The file holds only a bare version, so the name is not on the
# line to be captured.
PYTHON: Final[str] = "python"

# A `.python-version` entry that is a plain CPython version, `X.Y` or `X.Y.Z` (e.g. `3.12` or `3.12.6`), alone on
# its line except for an optional trailing `#` comment. That comment is what lets an inline `# update-time:` marker
# be recognised, and preserved when the version is rewritten. Everything else is left untouched: alternative
# implementations (`pypy3.10-…`), free-threaded/variant suffixes (`3.13t`), prefixed forms (`cpython@3.12`,
# `>=3.10`), sentinels (`system`), and any other trailing text.
PYTHON_VERSION_REFERENCE: Final[re.Pattern[str]] = re.compile(
    r"""
    ^\s*(?P<version>\d+\.\d+(?:\.\d+)?)  # the version
    \s*(?:\#.*)?$  # a comment, if any, to the end of the line
    """,
    re.VERBOSE,
)
