"""What HTML markup says about a line, and the text it shows."""

import re
from html.parser import HTMLParser
from typing import Final

# An anchor naming the section below it, such as `<a name="release-1-0"></a>`.
_ANCHOR: Final[re.Pattern[str]] = re.compile(r'<a name="[^"]*"></a>')


def is_anchor(line: str) -> bool:
    """Return whether the line holds an anchor and nothing else."""
    return _ANCHOR.fullmatch(line) is not None


def text(markup: str) -> str:
    """Return the text the markup shows, without the blank lines its tags leave around it."""
    parser = _TextParser()
    parser.feed(markup)
    parser.close()
    return "".join(parser.parts).strip()


class _TextParser(HTMLParser):
    """A parser that keeps the text of the markup it is fed, and drops its tags and comments."""

    def __init__(self) -> None:
        """Start with no text."""
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        """Keep the text between the tags."""
        self.parts.append(data)
