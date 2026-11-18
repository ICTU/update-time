"""Unit tests for what the console shows for a log record."""

import io
import logging
from typing import TYPE_CHECKING
from unittest import TestCase
from unittest.mock import patch

from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text

from mutation_time.mutation import Mutation, kills
from update_time.domain.dependency import NO_CHANGES, Changes, DependencyVersion
from update_time.formats import restructured_text
from update_time.io import console as console_module
from update_time.io.console import (
    _MARKDOWN_PARSER,
    LOG_THEME,
    MESSAGE_NAME,
    LogHighlighter,
    configure_logging,
)
from update_time.io.log import Logger, get_logger
from update_time.primitives.command import Command

from tests.update_time.fixtures import DIGEST, DIGEST2
from tests.update_time.helpers import message_of, reference
from tests.update_time.io.helpers import at, create_location, dependency

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.style import Style


# The corners and sides Rich draws a rounded box with.
_BOX_TOP_LEFT = "╭"
_BOX_BOTTOM_LEFT = "╰"
_BOX_SIDE = "│"


class RecordRenderingTests(TestCase):
    """Unit tests for what the console shows for a record."""

    RELEASE_PAGE = "https://github.com/python-humanize/humanize/releases/tag/4.15.0"

    @staticmethod
    def rendered(report: Callable[[Logger], None], level: str = "INFO", *, styles: bool = False) -> str:
        """Return what the console shows at the level for the records `report` logs through the logger it is handed."""
        console = Console(width=100, file=io.StringIO(), record=True, theme=LOG_THEME, force_terminal=False)
        root, parser = logging.getLogger(), logging.getLogger(_MARKDOWN_PARSER)
        # Stand the root logger's handlers aside so configure_logging installs its own, and the levels aside so
        # neither the level it configures nor the one an earlier test left behind outlives this render.
        with (
            patch.object(root, "handlers", []),
            patch.object(root, "level", root.level),
            patch.object(parser, "level", logging.NOTSET),
        ):
            configure_logging(console, level)
            report(Logger("rendering"))
        return console.export_text(styles=styles)

    @staticmethod
    def report_new_version(log: Logger, changes: Changes = NO_CHANGES) -> None:
        """Report a new version of `pkg`, with the changes a changelog records for it."""
        version = DependencyVersion("1.2.0", changes)
        log.new_version(reference("pkg", create_location("requirements.txt", 3)), version)

    @staticmethod
    def report_pin(log: Logger) -> None:
        """Report that `pkg` was pinned to a version."""
        log.pinned_version(reference("pkg", create_location("requirements.txt", 3)), DependencyVersion("1.2.0"))

    @classmethod
    def report_new_version_twice(cls, log: Logger) -> None:
        """Report the same new version of `pkg` twice, so the second report suppresses the changelog."""
        cls.report_new_version(log)
        cls.report_new_version(log)

    def rendered_changes(self, changes: Changes, level: str = "INFO", *, styles: bool = False) -> str:
        """Return what the console shows at the level when a new version of `pkg` reports the changes."""
        return self.rendered(lambda log: self.report_new_version(log, changes), level, styles=styles)

    def assert_boxed(self, rendered: str, contents: list[str]) -> None:
        """Assert that the lines below the message sit in a box, and that the box holds these lines and no others."""
        lines = [line.strip() for line in rendered.splitlines()][1:]
        corners = [line[:1] for line in lines[:1] + lines[-1:]]
        self.assertEqual(corners, [_BOX_TOP_LEFT, _BOX_BOTTOM_LEFT], rendered)
        sides = {(line[0], line[-1]) for line in lines[1:-1]}
        self.assertEqual(sides, {(_BOX_SIDE, _BOX_SIDE)}, rendered)
        self.assertEqual([line.strip(_BOX_SIDE).strip() for line in lines[1:-1]], contents, rendered)

    @kills(
        Mutation(
            console_module._LogHandler._changes_markup,
            "return _ChangelogMarkdown(changes, hyperlinks=self.console.is_terminal)",
            "return _ChangelogMarkdown(changes, hyperlinks=True)",
            "a link's URL is written into an escape nothing renders, so a file or a CI log holds the text alone",
        )
    )
    def test_a_links_url_is_printed_where_nothing_can_be_clicked(self):
        """Test that a Markdown link's URL reaches an output that can hold no clickable link, such as a CI log."""
        url = "https://github.com/python-humanize/humanize/issues/42"
        rendered = self.rendered_changes(Changes(f"- A fix ([#42]({url}))", markdown=True, url=""))
        self.assertIn(url, rendered)

    def test_a_record_without_changes_gets_no_empty_block(self):
        """Test that a record carrying no changes renders as its message alone."""
        rendered = self.rendered(self.report_pin)
        self.assertIn("Pinned pkg in requirements.txt:3 to 1.2.0", rendered)
        self.assertEqual(rendered.splitlines()[1:], [])

    @kills(
        Mutation(
            console_module.named_message,
            'len(message.plain.partition("\\n")[0])',
            "len(message.plain)",
            "the name of a command's failure follows the output the command wrote, so it reads as part of that output",
        )
    )
    def test_the_name_of_a_message_of_several_lines_ends_its_first_line(self):
        """Test that a message of several lines, such as a command's output, carries its name on its first line."""
        rendered = self.rendered(
            lambda _: logging.getLogger("update-time").error(
                "npm audit failed:\nfound 3 vulnerabilities", extra={MESSAGE_NAME: "command-failed"}
            )
        )
        lines = [line.strip() for line in rendered.splitlines()]
        self.assertTrue(lines[0].endswith("npm audit failed: (command-failed)"), rendered)
        self.assertEqual(lines[1:], ["found 3 vulnerabilities"])

    def test_the_name_of_a_message_with_lines_below_it_ends_the_first_line(self):
        """Test that a message with changes boxed below it, or a note, carries its name on the message's own line."""
        cases = {"changes": Changes("Fixed bug A", markdown=False, url=""), "a note": NO_CHANGES}
        for case, changes in cases.items():
            with self.subTest(below=case):
                first_line = self.rendered_changes(changes).splitlines()[0]
                self.assertTrue(first_line.rstrip().endswith("requirements.txt:3: 1.2.0 (new-version)"), first_line)

    @kills(
        Mutation(
            console_module.named_message,
            "append(name, link)",
            "append(name)",
            "the name of the log message is plain text, so a terminal cannot open its entry in the index",
        )
    )
    def test_the_name_of_a_message_is_a_hyperlink(self):
        """Test that the name of a log message is a link a terminal can open, to the name's heading in the index."""
        url = "https://github.com/ICTU/update-time/blob/main/docs/log-messages.md#pinned-to-version"
        self.assertIn(f";{url}\x1b\\", self.rendered(self.report_pin, styles=True))

    @kills(
        Mutation(
            console_module.named_message,
            "named.stylize(_MESSAGE_NAME_STYLE, first_line_end)",
            'named.stylize("", first_line_end)',
            "the name of a log message is as bright as the message, so the message no longer stands out",
        ),
        Mutation(
            console_module.named_message,
            "named.stylize(_MESSAGE_NAME_STYLE, first_line_end)",
            "named.stylize(_MESSAGE_NAME_STYLE, 0)",
            "the message is as dim as its name, so the message no longer stands out",
        ),
    )
    def test_the_name_of_a_message_is_dim(self):
        """Test that the name of a log message renders dim, with the parentheses around it, and the message does not."""
        rendered = self.rendered(self.report_pin, styles=True)
        self.assertIn("\x1b[2m (\x1b[0m", rendered)
        self.assertIn("\x1b\\\x1b[2mpinned-to-version\x1b[0m", rendered)
        self.assertIn("\x1b[2m)\x1b[0m", rendered)
        self.assertIn("Pinned", rendered)
        self.assertNotIn("\x1b[2mPinned", rendered)

    def test_an_html_comment_is_not_shown(self):
        """Test that a comment a changelog hides in its markup stays hidden, the box closing up around it."""
        changes = Changes("## 1.2.0\n\n<!-- towncrier release notes start -->\n\n- A fix\n", markdown=True, url="")
        self.assert_boxed(self.rendered_changes(changes), ["1.2.0", "", "• A fix"])

    @kills(
        Mutation(
            console_module._ChangelogMarkdown._without_blank_blocks,
            "{index - 1, index, index + 1} & tags_alone",
            "{index} & tags_alone",
            "an HTML anchor above a heading still shows as a blank line, its paragraph kept without its text",
        ),
        Mutation(
            console_module._ChangelogMarkdown._without_blank_blocks,
            "and all(child.type",
            "and any(child.type",
            "a line of changes that opens with an HTML anchor is dropped, its text with it",
        ),
    )
    def test_an_html_anchor_takes_no_line(self):
        """Test that an HTML anchor stays hidden, alone on its line or before text, the box closing up around it."""
        anchor = '<a name="release-1-2-0-fixed"></a>'
        cases = {
            "opening the changes": (f"{anchor}\n\n### Fixed\n\n- A fix\n", ["Fixed", "", "• A fix"]),
            "between headings": (
                f"## 1.2.0\n\n{anchor}\n\n### Fixed\n\n- A fix\n",
                ["1.2.0", "", "Fixed", "", "• A fix"],
            ),
            "beside text": (f"{anchor}A fix\n", ["A fix"]),
        }
        for case, (markup, contents) in cases.items():
            with self.subTest(case=case):
                self.assert_boxed(self.rendered_changes(Changes(markup, markdown=True, url="")), contents)

    @kills(
        Mutation(
            console_module._ChangelogMarkdown,
            'elements: ClassVar = {**Markdown.elements, "html_block": _RawHtml}',
            "elements: ClassVar = {**Markdown.elements}",
            "a raw HTML block is dropped, taking the changes a `<details>` section wraps with it",
        ),
        Mutation(
            console_module._RawHtml.__rich_console__,
            "html.text(self.html)",
            "self.html.strip()",
            "a raw HTML block shows its tags, which the reader has to read past",
        ),
    )
    def test_raw_html_in_a_markdown_changelog_shows_its_text_without_its_tags(self):
        """Test that a Markdown changelog's raw HTML block reaches the reader as the text it holds, without tags."""
        details = "<details>\n<summary>Dependency updates</summary>\n\n- bump foo\n</details>\n"
        rendered = self.rendered_changes(Changes(f"## 1.2.0\n\n{details}", markdown=True, url=""))
        self.assert_boxed(rendered, ["1.2.0", "", "Dependency updates", "", "• bump foo"])

    @kills(
        Mutation(
            console_module._ChangelogMarkdown.__rich_console__,
            "if index or segment != Segment.line():",
            "if True:",
            "the list, quote, or table that opens a changelog gets a line break above it",
        )
    )
    def test_changes_opening_with_a_list_a_quote_or_a_table_get_no_leading_line_break(self):
        """Test that the box starts with the list, quote, or table that opens the changes."""
        cases = {
            "bullet list": ("- A fix\n", ["• A fix"]),
            "ordered list": ("1. A fix\n", ["1 A fix"]),
            "blockquote": ("> A fix\n", ["▌ A fix"]),
            "table": ("| Fix |\n|---|\n| A |\n", ["", "Fix", "───", "A", ""]),  # the blank lines are the table's edges
        }
        for case, (markup, contents) in cases.items():
            with self.subTest(case=case):
                self.assert_boxed(self.rendered_changes(Changes(markup, markdown=True, url="")), contents)

    @kills(
        Mutation(
            console_module._ChangelogMarkdown._without_blank_blocks,
            'if tokens[index - 1].type == "paragraph_open"',
            'if token.type == "inline"',
            "a table cell holding nothing is dropped, moving the cells after it a column to the left",
        )
    )
    def test_a_table_cell_without_text_keeps_the_cells_after_it_in_their_columns(self):
        """Test that a table cell holding nothing, or an HTML tag alone, leaves the next cell under its heading."""
        for case, cell in {"empty": " ", "tag alone": "<br>"}.items():
            with self.subTest(case=case):
                rendered = self.rendered_changes(
                    Changes(f"| a | b | c |\n|---|---|---|\n| 1 |{cell}| 3 |\n", markdown=True, url="")
                )
                header, row = (next(line for line in rendered.splitlines() if text in line) for text in (" a ", " 1 "))
                self.assertEqual(row.index("3"), header.index("c"), rendered)

    @kills(
        Mutation(
            console_module.configure_logging,
            "logging.getLogger(_MARKDOWN_PARSER).setLevel(WARNING)\n    return handler",
            "return handler",
            "the Markdown parser traces every block rule it tries, burying the run's own debug output",
        )
    )
    def test_the_markdown_parser_logs_nothing_at_debug_level(self):
        """Test that rendering a Markdown changelog at debug level adds no record of its own to the output."""
        rendered = self.rendered_changes(Changes("## 1.2.0\n\n- A fix\n", markdown=True, url=""), level="DEBUG")
        self.assertIn("New version available for pkg in requirements.txt:3: 1.2.0", rendered)
        self.assertNotIn("DEBUG", rendered)

    def test_changes_render_as_markdown(self):
        """Test that a Markdown link in the changes renders as its text, under the header naming the new version."""
        url = "https://pypi.org/project/coverage/7.16.0"
        rendered = self.rendered_changes(Changes(f"PyPI page: [coverage 7.16.0]({url}).", markdown=True, url=""))
        self.assertIn("New version available for pkg in requirements.txt:3: 1.2.0", rendered)
        self.assertIn("PyPI page: coverage 7.16.0", rendered)
        self.assertNotIn("[coverage 7.16.0]", rendered)

    @kills(
        Mutation(
            console_module._ChangelogMarkdown._unindent_headings,
            "title.children[0].content.lstrip()",
            "title.children[0].content",
            "a heading opening with non-breaking spaces is indented by them",
        ),
        Mutation(
            console_module._ChangelogMarkdown._unindent_headings,
            ' and title.children[0].type == "text":',
            ":",
            "a code span opening a heading loses the spaces the project wrote in it",
        ),
        Mutation(
            console_module._ChangelogMarkdown._unindent_headings,
            "title.children[0].content.lstrip()",
            'title.children[0].content.replace("\\xa0", "")',
            "a heading loses the non-breaking spaces inside its title, running its words together",
        ),
    )
    def test_a_heading_drops_only_the_non_breaking_spaces_opening_its_text(self):
        """Test that a heading keeps its spaces, in code or out, except the non-breaking ones opening its title."""
        cases = {
            "text": ("### &nbsp;&nbsp;&nbsp;🚀 Features", "🚀 Features"),
            "link": ("##### &nbsp;&nbsp;&nbsp;&nbsp;[View changes on GitHub](https://github.com)", "View changes"),
            "code": ("### `  indented()`", "  indented()"),
            "inside": ("### &nbsp;A&nbsp;B", "A\xa0B"),
        }
        for case, (markup, title) in cases.items():
            with self.subTest(case=case):
                rendered = self.rendered_changes(Changes(markup, markdown=True, url=""))
                self.assertIn(f"{_BOX_SIDE} {title}", rendered)

    @kills(
        Mutation(
            console_module,
            '_READ_AS_TEXT = ("text", "html_block")',
            '_READ_AS_TEXT = ("text",)',
            "a shortcode in a <details> section's summary is shown as typed, beside prose that gets its emoji",
        )
    )
    def test_a_shortcode_in_a_raw_html_block_renders_as_its_emoji(self):
        """Test that a shortcode a raw HTML block holds reaches the reader as its emoji, as one in the prose does."""
        html = "<details>\n<summary>:zap: Dependency updates</summary>\n"
        rendered = self.rendered_changes(Changes(f"{html}\n- :zap: bump foo\n</details>\n", markdown=True, url=""))
        self.assertIn("⚡ Dependency updates", rendered)
        self.assertNotIn(":zap:", rendered)

    @kills(
        Mutation(
            console_module._ChangelogMarkdown.__init__,
            "super().__init__(markup, hyperlinks=hyperlinks)",
            "super().__init__(Emoji.replace(markup), hyperlinks=hyperlinks)",
            "replacing before the parse eats the shortcodes a code span and a fenced block hold",
        )
    )
    def test_a_shortcode_renders_as_its_emoji_except_inside_code(self):
        """Test that a shortcode in the prose reaches the reader as its emoji, and one inside code as written."""
        changes = Changes(":zap: fixed `:zap:` in\n\n```yaml\nlabel: ':zap:'\n```\n", markdown=True, url="")
        rendered = self.rendered_changes(changes)
        self.assertIn("⚡ fixed :zap: in", rendered)  # the prose has its emoji, the code span keeps its shortcode
        self.assertIn("label: ':zap:'", rendered)  # and so does the fenced block

    @kills(
        Mutation(
            console_module._LogHandler.render_message,
            "return Group(rendered, Text(note))",
            "return Group(rendered, Panel(Text(note)))",
            "Update-time's own note about a changelog is boxed as if it were a changelog's changes",
        )
    )
    def test_a_note_about_the_changelog_is_not_boxed(self):
        """Test that Update-time's own note about a changelog renders as a bare line, the box holding changes alone."""
        notes = [
            (Logger._NO_CHANGELOG, self.report_new_version),
            (Logger._SUPPRESSING_CHANGELOG, self.report_new_version_twice),
        ]
        for note, report in notes:
            with self.subTest(note=note):
                rendered = self.rendered(report)
                self.assertEqual([line.strip() for line in rendered.splitlines()][-1], note)
                self.assertNotIn(_BOX_TOP_LEFT, rendered)

    def test_changes_that_are_not_markdown_keep_their_lines_and_a_dash_as_written(self):
        """Test that a changelog that is not Markdown is boxed too, keeping its lines and a dash as written."""
        changes = Changes("Fixed bug A\nFixed bug B\n- and a dash that is no bullet", markdown=False, url="")
        rendered = self.rendered_changes(changes)
        self.assert_boxed(rendered, changes.splitlines())

    @kills(
        Mutation(
            restructured_text.text,
            "for title, line in pairwise(lines) if not heading_underline(title, line))",
            "for title, line in pairwise(lines))",
            "a reStructuredText changelog shows the underline of each heading",
        ),
        Mutation(
            restructured_text.heading_underline,
            " or len(underline) >= _SHORTEST_UNDERLINE)",
            ")",
            "a reStructuredText changelog shows the underline of a heading that is shorter than its title",
        ),
    )
    def test_a_heading_in_changes_that_are_not_markdown_shows_without_its_underline(self):
        """Test that a heading underlined as reStructuredText marks one shows as its title alone."""
        changes = Changes("3.13.0 (2026-10-23)\n----\n\n* Added Python 3.15.", markdown=False, url="")
        self.assert_boxed(self.rendered_changes(changes), ["3.13.0 (2026-10-23)", "", "* Added Python 3.15."])

    @kills(
        Mutation(
            restructured_text.heading_underline,
            "and (len(underline) >= len(title) or len(underline) >= _SHORTEST_UNDERLINE)",
            "and True",
            "the box drops the `...` that says the changes were cut short, as if it underlined the line above",
        )
    )
    def test_a_short_run_of_punctuation_in_changes_that_are_not_markdown_is_shown_as_written(self):
        """Test that a run shorter than the line above it and than four characters, such as a `...`, stays."""
        changes = Changes("1.0\n\n- Fixed bug A\n...", markdown=False, url="")
        self.assert_boxed(self.rendered_changes(changes), changes.splitlines())

    @kills(
        Mutation(
            restructured_text.text,
            'return _INLINE_LITERAL.sub(r"\\1", shown)',
            "return shown",
            "a reStructuredText changelog shows the double backticks around each inline literal",
        ),
        Mutation(
            restructured_text,
            '_INLINE_LITERAL = re.compile(r"``(.+?)``")',
            '_INLINE_LITERAL = re.compile(r"``(.+)``")',
            "a line holding two inline literals keeps the backticks closing the first and opening the second",
        ),
    )
    def test_an_inline_literal_in_changes_that_are_not_markdown_shows_without_its_backticks(self):
        """Test that each inline literal reStructuredText marks with double backticks shows as its text alone."""
        changes = Changes("* Nest ``ThreadSensitiveContext`` with an ``executor``.", markdown=False, url="")
        self.assert_boxed(self.rendered_changes(changes), ["* Nest ThreadSensitiveContext with an executor."])

    def test_the_changes_url_is_shown_in_full_below_the_box(self):
        """Test that the URL of the page the changes come from is shown in full below the box, folded if it's long."""
        urls = {
            "short": self.RELEASE_PAGE,
            "wider than the console": "https://github.com/open-telemetry/opentelemetry-python-contrib/releases/tag/"
            "opentelemetry-instrumentation-requests==0.48b0",
        }
        for case, url in urls.items():
            with self.subTest(case=case):
                lines = self.rendered_changes(Changes("Fixed bug A", markdown=False, url=url)).splitlines()
                box_end = next(index for index, line in enumerate(lines) if line.strip().startswith(_BOX_BOTTOM_LEFT))
                label, *fragments = lines[box_end + 1].split()
                fragments += [line.strip() for line in lines[box_end + 2 :]]
                self.assertEqual((label, "".join(fragments)), ("Changelog:", url))
                self.assert_boxed("\n".join(lines[: box_end + 1]), ["Fixed bug A"])

    def test_the_changes_url_is_a_hyperlink(self):
        """Test that the URL of the page the changes come from is a link a terminal can open."""
        url = self.RELEASE_PAGE
        rendered = self.rendered_changes(Changes("Fixed bug A", markdown=False, url=url), styles=True)
        self.assertIn(f";{url}\x1b\\{url}", rendered)

    def test_a_message_is_not_rendered_as_markdown(self):
        """Test that a command's stderr keeps the Markdown markup it wrote."""
        stderr = "# npm audit report\n- run `npm audit fix` to fix them"
        rendered = self.rendered(lambda log: log.command_stderr(Command("npm", "audit"), stderr))
        self.assertIn("npm audit wrote to stderr:", rendered)
        self.assertIn("# npm audit report", rendered)
        self.assertIn("- run `npm audit fix` to fix them", rendered)


class LogHighlighterTests(TestCase):
    """Unit tests for how the log output is highlighted."""

    @staticmethod
    def highlighted(message: str) -> tuple[str, list[tuple[str, str | Style]]]:
        """Return the highlighted message: the text it reads as, and the styled run each of its spans covers."""
        text = Text(message)
        LogHighlighter().highlight(text)
        return text.plain, [(text.plain[span.start : span.end], span.style) for span in text.spans]

    def test_digest_highlighted_as_one_token(self):
        """Test that the full digest gets a single `repr.digest` span and no leftover fragment sub-spans inside it."""
        digest = f"sha256:{'a4fde3b2' + 'c' * 56}"  # a realistic 64-hex-character digest
        text = Text(f"pinned to {digest} but the registry now serves {DIGEST2}")
        LogHighlighter().highlight(text)
        start = text.plain.index(digest)
        spans_in_digest = [span for span in text.spans if span.start >= start and span.end <= start + len(digest)]
        self.assertEqual(spans_in_digest, [(start, start + len(digest), "repr.digest")])

    def test_version_numbers_still_highlighted(self):
        """Test that ordinary highlighting (e.g. of a version number) is preserved for messages without a digest."""
        _plain, styled = self.highlighted("New version available: 3.14")
        self.assertIn("repr.number", [style for _run, style in styled])

    def test_dependency_name_highlighted_and_markers_removed(self):
        """Test that a marker-wrapped dependency name is styled as `repr.dependency` and the markers leave no trace."""
        fields = {"dependency": dependency("actions/checkout"), "location": "a.txt", "version": "1.1"}
        plain, styled = self.highlighted(message_of(Logger.new_version).format % fields)
        self.assertEqual(plain, "New version available for actions/checkout in a.txt: 1.1")
        self.assertIn(("actions/checkout", "repr.dependency"), styled)

    def test_dependency_names_and_digest_together(self):
        """Test that several dependency names and a digest in one message are each styled without interfering."""
        plain, styled = self.highlighted(f"Pinned {dependency('ghcr.io/astral-sh/uv')} in Dockerfile to {DIGEST}")
        self.assertEqual(plain, f"Pinned ghcr.io/astral-sh/uv in Dockerfile to {DIGEST}")
        self.assertIn(("ghcr.io/astral-sh/uv", "repr.dependency"), styled)
        self.assertIn((DIGEST, "repr.digest"), styled)

    def test_location_highlighted_as_one_token(self):
        """Test that a delimited path:line is one `repr.filename` span, delimiters stripped and no stray number span."""
        plain, styled = self.highlighted(f"New version available in {at('docs/requirements.txt:42')}: 4.15.0")
        self.assertEqual(plain, "New version available in docs/requirements.txt:42: 4.15.0")
        self.assertIn(("docs/requirements.txt:42", "repr.filename"), styled)
        # The line number is part of the single location token, not highlighted as a separate number.
        self.assertNotIn(("42", "repr.number"), styled)

    def test_no_colour_output_is_plain_text(self):
        """Test that with colour disabled the styled name renders as the same plain text, markers and all removed."""
        highlighted = LogHighlighter()(Text(f"Pinned {dependency('python')} in Dockerfile"))
        console = Console(no_color=True, force_terminal=False)
        with console.capture() as capture:
            console.print(highlighted, end="")
        self.assertEqual(capture.get(), "Pinned python in Dockerfile")

    def test_dependency_style_is_bold_white(self):
        """Test that get_logger wires `repr.dependency` to bold white in the handler's console theme."""
        get_logger("theme")  # Ensure the root logger, and its themed RichHandler console, have been configured.
        handler = next(h for h in logging.getLogger().handlers if isinstance(h, RichHandler))
        self.assertEqual(str(handler.console.get_style("repr.dependency")), "bold white")
