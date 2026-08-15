"""Read the PEP 723 inline script metadata of a standalone Python script.

A `# /// script … # ///` block declares a script's dependencies as TOML commented out with `# ` prefixes, so the
block has to be uncommented before it parses as TOML. See https://peps.python.org/pep-0723/.
"""

import re

# The pattern PEP 723 itself gives for a metadata block of type `script`: the opening line, the commented lines
# that follow it, and the closing line.
_BLOCK = re.compile(r"(?m)^# /// script$\s(?P<content>(^#(| .*)$\s)+)^# ///$")
# The line that opens a block. A file carrying one is a script whose dependencies uv resolves, whether or not the
# block is ever closed: uv reports an unterminated block as the error it is, which passing the file over would not.
_OPENING = re.compile(r"^# /// script\s*$", re.MULTILINE)


def has_block(contents: str) -> bool:
    """Return whether the file carries a PEP 723 `# /// script` inline-metadata block."""
    return _OPENING.search(contents) is not None


def toml_block(contents: str) -> str | None:
    """Return the script's metadata block as the TOML it comments out, or None when the file carries no block.

    Each line loses the `# ` prefix that comments it out, and a line that is a bare `#` loses that.
    """
    match = _BLOCK.search(contents)
    if match is None:
        return None
    lines = match["content"].splitlines(keepends=True)
    return "".join(line.removeprefix("# ") if line.startswith("# ") else line.removeprefix("#") for line in lines)
