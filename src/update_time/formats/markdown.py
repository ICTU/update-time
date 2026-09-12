"""What a Markdown file's own markup says about its lines."""

# The runs of characters a fenced code block is opened and closed with.
_FENCES = ("```", "~~~")


def is_fence(line: str) -> bool:
    """Return whether the line opens or closes a fenced code block."""
    return line.startswith(_FENCES)


def heading(line: str) -> tuple[int, str] | None:
    """Return the level and title of the heading the line marks, or None when it marks none."""
    after_hashes = line.lstrip("#")
    level = len(line) - len(after_hashes)
    if not level or not after_hashes.startswith(" "):
        return None
    return level, after_hashes.strip()
