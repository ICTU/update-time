"""What the console shows for a log record: the styles, and the runs a message delimits for them."""

import logging
import re
from itertools import pairwise
from logging import WARNING
from typing import TYPE_CHECKING, cast

from rich.console import Console, Group
from rich.emoji import Emoji
from rich.highlighter import ReprHighlighter
from rich.logging import RichHandler
from rich.markdown import Markdown, MarkdownElement
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from rich.theme import Theme

from update_time.domain.dependency import NO_CHANGES
from update_time.formats import html, restructured_text
from update_time.primitives.digest import SHA256_DIGEST

if TYPE_CHECKING:
    from typing import ClassVar, Self

    from markdown_it.token import Token
    from rich.console import ConsoleOptions, ConsoleRenderable, RenderResult

    from update_time.domain.dependency import Changes


# A Private Use Area code point, which never occurs in real content and which Rich does not strip, so it survives
# message formatting. A dependency name is bracketed in it, because a name has no fixed shape a pattern could
# match; `_LogHighlighter` styles the bracketed run and strips the delimiters.
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


class _LogHighlighter(ReprHighlighter):
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

# The name of the record attribute that carries the name of the record's log message, which ends its first line.
MESSAGE_NAME = "message_name"

# The index that gives the name of each log message a heading of its own, which a name links to.
_LOG_MESSAGES_URL = "https://github.com/ICTU/update-time/blob/main/docs/log-messages.md"

# The style of the name of a log message and its parentheses, so the message itself stands out.
_MESSAGE_NAME_STYLE = "dim"


def named_message(record: logging.LogRecord, message: Text) -> Text:
    """Return the message with the name of its log message ending its first line, dim and linked to its index entry."""
    if not (name := getattr(record, MESSAGE_NAME, "")):
        return message
    first_line_end = len(message.plain.partition("\n")[0])
    link = Style(link=f"{_LOG_MESSAGES_URL}#{name}")
    named = message[:first_line_end]
    named.append(" (").append(name, link).append(")")
    named.stylize(_MESSAGE_NAME_STYLE, first_line_end)
    return named + message[first_line_end:]


_CHANGES_BORDER = "dim"

# The label of the line naming the page the changes come from.
_CHANGELOG_LABEL = "Changelog"


class _RawHtml(MarkdownElement):
    """A block of raw HTML in a changelog, shown as the text it holds.

    Rich drops such a block, a terminal having nowhere to render it, and a `<details>` section holds changes.
    """

    @classmethod
    def create(cls, markdown: Markdown, token: Token) -> Self:  # noqa: ARG003
        """Create the element from the raw HTML the token holds."""
        return cls(token.content)

    def __init__(self, markup: str) -> None:
        """Keep the raw HTML the block holds."""
        self.html = markup

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render the text the raw HTML holds, without its tags."""
        yield Text(html.text(self.html))


# The kinds of token a changelog writes for its reader: its prose and its raw HTML. A code span and a fenced block
# hold their text in a token of their own, so the shortcodes the project wrote as code stay as written.
_READ_AS_TEXT = ("text", "html_block")


class _ChangelogMarkdown(Markdown):
    """Markdown that shows raw HTML, hides blank blocks, renders shortcodes as emoji, and unindents headings."""

    elements: ClassVar = {**Markdown.elements, "html_block": _RawHtml}

    def __init__(self, markup: str, *, hyperlinks: bool) -> None:
        """Parse the changes, then drop their blank blocks, replace their shortcodes, and unindent their headings."""
        super().__init__(markup, hyperlinks=hyperlinks)
        self.parsed = self._without_blank_blocks(self.parsed)
        self._replace_shortcodes(self.parsed)
        self._unindent_headings(self.parsed)

    @staticmethod
    def _without_blank_blocks(tokens: list[Token]) -> list[Token]:
        """Return the tokens without the blocks that show as a blank line, such as an anchor or a comment.

        A paragraph of HTML tags alone drops its opening and closing tokens with it. Rich reads a closing token left on
        its own as the end of the quote or list item holding the paragraph.
        """
        tags_alone = {
            index
            for index, token in enumerate(tokens)
            if tokens[index - 1].type == "paragraph_open"
            and all(child.type == "html_inline" for child in token.children or ())
        }
        return [
            token
            for index, token in enumerate(tokens)
            if not {index - 1, index, index + 1} & tags_alone
            and not (token.type == "html_block" and not html.text(token.content))
        ]

    @staticmethod
    def _replace_shortcodes(tokens: list[Token]) -> None:
        """Replace the shortcodes written in prose or raw HTML with their emoji."""
        for token in tokens:
            for part in (token, *(token.children or ())):
                if part.type in _READ_AS_TEXT:
                    part.content = Emoji.replace(part.content)

    @staticmethod
    def _unindent_headings(tokens: list[Token]) -> None:
        """Strip the non-breaking spaces opening a heading's title, since Markdown strips only the ordinary ones."""
        for opening, title in pairwise(tokens):
            if opening.type == "heading_open" and title.children and title.children[0].type == "text":
                title.children[0].content = title.children[0].content.lstrip()

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render the changes, without the line break Rich writes above the list, quote, or table that opens them."""
        for index, segment in enumerate(super().__rich_console__(console, options)):
            if index or segment != Segment.line():
                yield segment


class _LogHandler(RichHandler):
    """Rich log handler that names a message, and boxes a changelog's changes, or adds a note, below it."""

    def render_message(self, record: logging.LogRecord, message: str) -> ConsoleRenderable:
        """Return the message Rich renders, with its name, and with the record's changes or note below it."""
        rendered = super().render_message(record, message)
        rendered = named_message(record, cast("Text", rendered))  # Rich renders a message as Text
        if changes := getattr(record, CHANGES, NO_CHANGES):
            return Group(rendered, self._rendered_changes(changes))
        if note := getattr(record, NOTE, ""):
            return Group(rendered, Text(note))
        return rendered

    def _rendered_changes(self, changes: Changes) -> ConsoleRenderable:
        """Box the changes, so their blank lines do not read as breaks in the log, and name their page below the box."""
        box = Panel(self._changes_markup(changes), border_style=_CHANGES_BORDER)
        if not changes.url:
            return box
        return Group(box, Text.assemble(f"{_CHANGELOG_LABEL}: ", (changes.url, Style(link=changes.url))))

    def _changes_markup(self, changes: Changes) -> ConsoleRenderable:
        """Return the changes rendered as Markdown when written in it, and as reStructuredText's text otherwise.

        A terminal makes a link clickable, so Rich hides its URL there and prints it everywhere else.
        """
        if not changes.markdown:
            return Text(restructured_text.text(changes))
        return _ChangelogMarkdown(changes, hyperlinks=self.console.is_terminal)


# The library Rich parses a changelog's Markdown with, which traces every block rule it tries at DEBUG.
_MARKDOWN_PARSER = "markdown_it"


_LOG_TIME_FORMAT = "[%X]"
_LOG_MESSAGE_FORMAT = "%(message)s"


def configure_logging(console: Console, level: str) -> RichHandler:
    """Send every record at the level or above to the console, replacing any earlier one, and return its handler."""
    handler = _LogHandler(console=console, highlighter=_LogHighlighter(), show_path=False)
    logging.basicConfig(
        level=level, datefmt=_LOG_TIME_FORMAT, format=_LOG_MESSAGE_FORMAT, handlers=[handler], force=True
    )
    logging.getLogger(_MARKDOWN_PARSER).setLevel(WARNING)
    return handler
