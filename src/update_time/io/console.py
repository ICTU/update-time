"""What the console shows for a log record: the styles, and the runs a message delimits for them."""

import logging
import re
from logging import WARNING
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.emoji import Emoji
from rich.highlighter import ReprHighlighter
from rich.logging import RichHandler
from rich.markdown import Markdown, MarkdownElement
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

from update_time.domain.changelog import is_markdown
from update_time.primitives.digest import SHA256_DIGEST

if TYPE_CHECKING:
    from typing import ClassVar, Self

    from markdown_it.token import Token
    from rich.console import ConsoleOptions, ConsoleRenderable, RenderResult


# A Private Use Area code point, which never occurs in real content and which Rich does not strip, so it survives
# message formatting. A dependency name is bracketed in it, because a name has no fixed shape a pattern could
# match; `LogHighlighter` styles the bracketed run and strips the delimiters.
_DEPENDENCY_DELIMITER = ""

# The same for a file location, in a code point of its own so the two runs never collide. A location cannot be
# matched by shape either: a regex over the finished message cannot tell `Dockerfile:1` from the versions and
# digests around it.
_LOCATION_DELIMITER = ""

_DELIMITERS = dict.fromkeys(ord(delimiter) for delimiter in (_DEPENDENCY_DELIMITER, _LOCATION_DELIMITER))


def delimit_dependency(dependency: str) -> str:
    """Bracket a dependency name so the highlighter styles it as one token."""
    return f"{_DEPENDENCY_DELIMITER}{dependency}{_DEPENDENCY_DELIMITER}"


def delimit_location(location: object) -> str:
    """Bracket a location's text so the highlighter styles the whole run as one token."""
    return f"{_LOCATION_DELIMITER}{location}{_LOCATION_DELIMITER}"


def undelimited(message: str) -> str:
    """Return the message without the delimiters, which nothing but the highlighter reads."""
    return message.translate(_DELIMITERS)


class LogHighlighter(ReprHighlighter):
    """Rich highlighter that colours a whole `sha256:` digest, dependency name, and file location as single tokens.

    Rich's built-in rules otherwise match only fragments of a digest: the `256` reads as a number and a run such as
    `a256:a4fd` reads as an IPv6 address. Dropping those sub-spans styles the digest uniformly, while every other
    message keeps Rich's default highlighting.
    """

    _DIGEST = re.compile(rf"\b{SHA256_DIGEST}\b")
    _DEPENDENCY = re.compile(f"{_DEPENDENCY_DELIMITER}[^{_DEPENDENCY_DELIMITER}]*{_DEPENDENCY_DELIMITER}")
    _LOCATION = re.compile(f"{_LOCATION_DELIMITER}[^{_LOCATION_DELIMITER}]*{_LOCATION_DELIMITER}")

    def highlight(self, text: Text) -> None:
        """Apply the default highlighting, restyle each digest, then style and unwrap dependency names and locations."""
        super().highlight(text)
        for match in self._DIGEST.finditer(text.plain):
            start, end = match.span()
            text.spans[:] = [span for span in text.spans if span.end <= start or span.start >= end]
            text.stylize("repr.digest", start, end)
        self._restyle_delimited(text, self._DEPENDENCY, "repr.dependency", keep_inner=True)
        self._restyle_delimited(text, self._LOCATION, "repr.filename", keep_inner=False)

    @staticmethod
    def _restyle_delimited(text: Text, pattern: re.Pattern[str], style: str, *, keep_inner: bool) -> None:
        """Style each delimiter-bracketed run as `style` and remove its two delimiters from the text.

        The run is rebuilt from slices of the text, so Rich remaps the surrounding spans across the removed
        delimiters. With `keep_inner` the run keeps Rich's inner colours; without it the whole run takes `style`,
        so a location's `path:line` reads as one token.
        """
        matches = list(pattern.finditer(text.plain))
        if not matches:
            return
        result = text[: matches[0].start()]
        for index, match in enumerate(matches):
            inner = text[match.start() + 1 : match.end() - 1]  # the run itself, without its two delimiters
            if not keep_inner:
                inner.spans.clear()  # Drop Rich's path/filename/number fragments so the run colours uniformly.
            inner.stylize(style)
            result += inner
            following = matches[index + 1].start() if index + 1 < len(matches) else len(text.plain)
            result += text[match.end() : following]
        text.plain = result.plain
        text.spans = result.spans


# A file location needs no entry: it reuses Rich's built-in `repr.filename`.
LOG_THEME = Theme({"repr.digest": "dim", "repr.dependency": "bold white"})


# The name of the record attribute that carries a changelog's changes, which the log shows in a box.
CHANGES = "changes"

# The name of the record attribute that carries Update-time's own note about the changes, which it shows as a line.
NOTE = "note"

_CHANGES_BORDER = "dim"


class _RawHtml(MarkdownElement):
    """A block of raw HTML in a changelog, shown as the project wrote it.

    Rich drops such a block, a terminal having nowhere to render it, and a `<details>` section holds changes.
    """

    @classmethod
    def create(cls, markdown: Markdown, token: Token) -> Self:  # noqa: ARG003
        """Create the element from the raw HTML the token holds."""
        return cls(token.content)

    def __init__(self, html: str) -> None:
        """Keep the raw HTML the block holds."""
        self.html = html

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render the raw HTML as the text it is, and a comment as the nothing its author meant it to be."""
        html = self.html.strip()
        if not (html.startswith("<!--") and html.endswith("-->")):
            yield Text(self.html.rstrip("\n"))


# The kinds of token a changelog writes for its reader: its prose and its raw HTML. A code span and a fenced block
# hold their text in a token of their own, so the shortcodes the project wrote as code stay as written.
_READ_AS_TEXT = ("text", "html_block")


class _ChangelogMarkdown(Markdown):
    """Markdown that shows a raw HTML block instead of dropping it, and renders the emoji a shortcode names."""

    elements: ClassVar = {**Markdown.elements, "html_block": _RawHtml}

    def __init__(self, markup: str, *, hyperlinks: bool) -> None:
        """Parse the changes, replacing the shortcodes in every token the reader reads as text."""
        super().__init__(markup, hyperlinks=hyperlinks)
        for token in self.parsed:
            for part in (token, *(token.children or ())):
                if part.type in _READ_AS_TEXT:
                    part.content = Emoji.replace(part.content)


class _ChangelogHandler(RichHandler):
    """Rich log handler that boxes a changelog's changes below the message, and renders a note about one as a line."""

    def render_message(self, record: logging.LogRecord, message: str) -> ConsoleRenderable:
        """Return the message Rich renders, with the changes or the note the record carries below it."""
        rendered = super().render_message(record, message)
        if changes := getattr(record, CHANGES, ""):
            return Group(rendered, self._rendered_changes(changes))
        if note := getattr(record, NOTE, ""):
            return Group(rendered, Text(note))
        return rendered

    def _rendered_changes(self, changes: str) -> ConsoleRenderable:
        """Return the changes in a box, so the blank lines a changelog holds do not read as breaks in the log."""
        return Panel(self._changes_markup(changes), border_style=_CHANGES_BORDER)

    def _changes_markup(self, changes: str) -> ConsoleRenderable:
        """Return the changes as Markdown when the changelog writes them in it, and as text otherwise.

        A terminal makes a link clickable, so Rich hides its URL there and prints it everywhere else.
        """
        if not is_markdown(changes):
            return Text(changes)
        return _ChangelogMarkdown(changes, hyperlinks=self.console.is_terminal)


# The library Rich parses a changelog's Markdown with, which traces every block rule it tries at DEBUG.
_MARKDOWN_PARSER = "markdown_it"


_LOG_TIME_FORMAT = "[%X]"
_LOG_MESSAGE_FORMAT = "%(message)s"


def configure_logging(console: Console, level: str) -> RichHandler:
    """Send every record at the level or above to the console, and return the handler that renders it there."""
    handler = _ChangelogHandler(console=console, highlighter=LogHighlighter(), show_path=False)
    logging.basicConfig(level=level, datefmt=_LOG_TIME_FORMAT, format=_LOG_MESSAGE_FORMAT, handlers=[handler])
    logging.getLogger(_MARKDOWN_PARSER).setLevel(WARNING)
    return handler
