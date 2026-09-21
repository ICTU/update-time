"""Unit tests for what the console shows for a log record."""

import io
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import TestCase
from unittest.mock import patch

from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text

from update_time.domain.dependency import NO_CHANGES, Changes, DependencyVersion
from update_time.io import console as console_module
from update_time.io.console import (
    _MARKDOWN_PARSER,
    LOG_THEME,
    LogHighlighter,
    configure_logging,
)
from update_time.io.log import Logger, get_logger
from update_time.primitives.command import Command

from tests.helpers import patch_environ
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import DIGEST, DIGEST2
from tests.update_time.helpers import reference
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

    @staticmethod
    def rendered(report: Callable[[Logger], None], level: str = "INFO") -> str:
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
        return console.export_text()

    @staticmethod
    def report_new_version(log: Logger, changes: Changes = NO_CHANGES) -> None:
        """Report a new version of `pkg`, with the changes a changelog records for it."""
        version = DependencyVersion("1.2.0", changes)
        log.new_version(reference("pkg", create_location("requirements.txt", 3)), version)

    @classmethod
    def report_new_version_twice(cls, log: Logger) -> None:
        """Report the same new version of `pkg` twice, so the second report suppresses the changelog."""
        cls.report_new_version(log)
        cls.report_new_version(log)

    def rendered_changes(self, changes: Changes, level: str = "INFO") -> str:
        """Return what the console shows at the level when a new version of `pkg` reports the changes."""
        return self.rendered(lambda log: self.report_new_version(log, changes), level)

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
            console_module,
            "        return _ChangelogMarkdown(changes, hyperlinks=self.console.is_terminal)",
            "        return _ChangelogMarkdown(changes, hyperlinks=True)",
            "a link's URL is written into an escape nothing renders, so a file or a CI log holds the text alone",
        )
    )
    def test_a_links_url_is_printed_where_nothing_can_be_clicked(self):
        """Test that a Markdown link's URL reaches an output that can hold no clickable link, such as a CI log."""
        url = "https://github.com/python-humanize/humanize/issues/42"
        # Rich reads FORCE_COLOR as a terminal, and a CI runner sets it to keep the colour in its log.
        with patch_environ({"FORCE_COLOR": "1"}):
            rendered = self.rendered_changes(Changes(f"- A fix ([#42]({url}))", markdown=True))
        self.assertIn(url, rendered)

    @kills(
        Mutation(
            console_module,
            '        if note := getattr(record, NOTE, ""):\n'
            "            return Group(rendered, Text(note))\n"
            "        return rendered",
            '        return Group(rendered, Text(getattr(record, NOTE, "")))',
            "every record without changes gets a blank line below it",
        )
    )
    def test_a_record_without_changes_gets_no_empty_block(self):
        """Test that a record carrying no changes renders as its message alone."""
        rendered = self.rendered(lambda log: log.skipped(Path("a.txt"), "it is compiled"))
        self.assertIn("Skipping a.txt: it is compiled", rendered)
        self.assertEqual(rendered.splitlines()[1:], [])

    @kills(
        Mutation(
            console_module,
            "        html = self.html.strip()\n"
            '        if not (html.startswith("<!--") and html.endswith("-->")):\n'
            '            yield Text(self.html.rstrip("\\n"))',
            '        yield Text(self.html.rstrip("\\n"))',
            "a comment the changelog's author wrote to be invisible is printed in the log",
        )
    )
    def test_an_html_comment_is_not_shown(self):
        """Test that a comment a changelog hides in its markup stays hidden."""
        changes = Changes("## 1.2.0\n\n<!-- towncrier release notes start -->\n\n- A fix\n", markdown=True)
        rendered = self.rendered_changes(changes)
        self.assertNotIn("towncrier", rendered)
        self.assertIn("• A fix", rendered)

    @kills(
        Mutation(
            console_module,
            '    elements: ClassVar = {**Markdown.elements, "html_block": _RawHtml}',
            "    elements: ClassVar = {**Markdown.elements}",
            "a raw HTML block is dropped, taking the changes a `<details>` section wraps with it",
        ),
        Mutation(
            console_module,
            '            yield Text(self.html.rstrip("\\n"))',
            "            yield Text(self.html)",
            "a raw HTML block keeps the newline ending it, so a blank line follows every one",
        ),
    )
    def test_raw_html_in_a_markdown_changelog_is_shown_as_written(self):
        """Test that a Markdown changelog's raw HTML block reaches the reader as the project wrote it."""
        html = "<details>\n<summary>Dependency updates</summary>\n"
        rendered = self.rendered_changes(Changes(f"## 1.2.0\n\n{html}\n- bump foo\n</details>\n", markdown=True))
        block = ["1.2.0", "", "<details>", "<summary>Dependency updates</summary>", "", "• bump foo", "", "</details>"]
        self.assert_boxed(rendered, block)

    @kills(
        Mutation(
            console_module,
            "    logging.getLogger(_MARKDOWN_PARSER).setLevel(WARNING)\n    return handler",
            "    return handler",
            "the Markdown parser traces every block rule it tries, burying the run's own debug output",
        )
    )
    def test_the_markdown_parser_logs_nothing_at_debug_level(self):
        """Test that rendering a Markdown changelog at debug level adds no record of its own to the output."""
        rendered = self.rendered_changes(Changes("## 1.2.0\n\n- A fix\n", markdown=True), level="DEBUG")
        self.assertIn("New version available for pkg in requirements.txt:3: 1.2.0", rendered)
        self.assertNotIn("DEBUG", rendered)

    def test_changes_render_as_markdown(self):
        """Test that a Markdown link in the changes renders as its text, under the header naming the new version."""
        url = "https://pypi.org/project/coverage/7.16.0"
        rendered = self.rendered_changes(Changes(f"PyPI page: [coverage 7.16.0]({url}).", markdown=True))
        self.assertIn("New version available for pkg in requirements.txt:3: 1.2.0", rendered)
        self.assertIn("PyPI page: coverage 7.16.0", rendered)
        self.assertNotIn("[coverage 7.16.0]", rendered)

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
        rendered = self.rendered_changes(Changes(f"{html}\n- :zap: bump foo\n</details>\n", markdown=True))
        self.assertIn("<summary>⚡ Dependency updates</summary>", rendered)
        self.assertNotIn(":zap:", rendered)

    @kills(
        Mutation(
            console_module,
            "super().__init__(markup, hyperlinks=hyperlinks)",
            "super().__init__(Emoji.replace(markup), hyperlinks=hyperlinks)",
            "replacing before the parse eats the shortcodes a code span and a fenced block hold",
        )
    )
    def test_a_shortcode_renders_as_its_emoji_except_inside_code(self):
        """Test that a shortcode in the prose reaches the reader as its emoji, and one inside code as written."""
        changes = Changes(":zap: fixed `:zap:` in\n\n```yaml\nlabel: ':zap:'\n```\n", markdown=True)
        rendered = self.rendered_changes(changes)
        self.assertIn("⚡ fixed :zap: in", rendered)  # the prose has its emoji, the code span keeps its shortcode
        self.assertIn("label: ':zap:'", rendered)  # and so does the fenced block

    @kills(
        Mutation(
            console_module,
            "            return Group(rendered, Text(note))",
            "            return Group(rendered, self._rendered_changes(note))",
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

    def test_changes_that_are_not_markdown_render_as_written(self):
        """Test that a changelog that is not Markdown is boxed too, keeping its lines and its markup as written."""
        changes = Changes("Fixed bug A\nFixed bug B\n- and a dash that is no bullet", markdown=False)
        rendered = self.rendered_changes(changes)
        self.assert_boxed(rendered, changes.splitlines())

    @kills(
        Mutation(
            console_module,
            "        rendered = super().render_message(record, message)",
            "        rendered = Markdown(super().render_message(record, message).plain)",
            "the message is rendered as Markdown too, mangling a command's stderr",
        )
    )
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
        plain, styled = self.highlighted(Logger._MESSAGE_NEW_VERSION.format % fields)
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
