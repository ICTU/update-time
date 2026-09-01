"""Read and rewrite the TOML a PEP 723 `# /// script` block comments out.

A script declares its dependencies in a `# /// script … # ///` block, as TOML commented out with `# ` prefixes, so
the block has to be uncommented before it parses and commented back before it is written. See
https://peps.python.org/pep-0723/.
"""

import re

# The pattern PEP 723 itself gives for a metadata block of type `script`: the opening line, the commented lines
# that follow it, and the closing line.
_BLOCK = re.compile(r"(?m)^# /// script$\s(?P<content>(^#(| .*)$\s)+)^# ///$")
# The line that opens a block. A file carrying one is a script, whether or not the block is ever closed.
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


def replace_toml_block(contents: str, toml_text: str) -> str:
    """Return the script with the TOML of its metadata block replaced, commented out again as PEP 723 wants it.

    Each line regains the `# ` prefix `toml_block` stripped, and a line holding nothing regains the bare `#`.
    """
    lines = toml_text.splitlines(keepends=True)
    commented = "".join(f"# {line}" if line.strip() else f"#{line}" for line in lines)
    return _BLOCK.sub(lambda match: match[0].replace(match["content"], commented, 1), contents, count=1)
