"""Unit tests for generating the README."""

import logging
import re
import sys
import unittest
from typing import TYPE_CHECKING, Final
from unittest.mock import Mock, patch

from tools.generate_log_svg import _LogOutput
from tools.generate_readme import _README, _SCREENSHOT, _TEMPLATE, _help_output, _render, _table_of_contents, main
from tools.log_index import _TEMPLATE as _LOG_INDEX_TEMPLATE
from tools.log_index import LOG_INDEX
from tools.log_samples import _LogSamples, sample_log_lines

from update_time.io.log import Logger

from tests.environment import patch_environ

if TYPE_CHECKING:
    from update_time.io.log import _LogMessage  # simplint: ignore[private-import-path] # annotates with it

_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"@@\w+@@")

# The placeholders `render` fills itself, rather than from the log samples.
_FILLED_BY_RENDER: Final[frozenset[str]] = frozenset(
    {"@@DOCKER_IMAGE_FILES_TABLE@@", "@@HELP_OUTPUT@@", "@@LOG_OUTPUT@@", "@@TABLE_OF_CONTENTS@@"}
)

_TABLE_OF_CONTENTS_HEADER: Final[str] = "## ☰ Table of contents\n\n"

# An argument of a log message's format string, such as `%(location)s`.
_MESSAGE_ARGUMENT: Final[re.Pattern[str]] = re.compile(r"%\(\w+\)[sd]")


class PlaceholderTest(unittest.TestCase):
    """Unit tests that the template leaves its placeholders, and the log lines it quotes, to the samples."""

    def test_placeholders_and_samples_agree(self):
        """Test that every placeholder in the template has a sample, and every sample is named by the template."""
        in_template = set(_PLACEHOLDER.findall(_TEMPLATE.read_text()))
        self.assertEqual(in_template, (set(sample_log_lines().blocks) | _FILLED_BY_RENDER))

    def test_the_templates_quote_no_log_message_themselves(self):
        """Test that each log message the README and the index quote comes from a sample, not from a template's text."""
        for generated, template in ((_README, _TEMPLATE), (LOG_INDEX, _LOG_INDEX_TEMPLATE)):
            with self.subTest(template=template.name):
                # The generated file quotes the samples, so the search does find a message where one is quoted.
                self.assertNotEqual(_quoted_log_messages(generated.read_text()), [])
                self.assertEqual(_quoted_log_messages(template.read_text()), [])


def _quoted_log_messages(text: str) -> list[str]:
    """Return the name of each log message the text quotes, whatever values fill in its arguments."""
    return [message.name for message in Logger.messages() if _message_pattern(message).search(text)]


def _message_pattern(message: _LogMessage) -> re.Pattern[str]:
    """Return a pattern matching the message on a single line, with any text in place of each argument."""
    literal_parts = _MESSAGE_ARGUMENT.split(message.format)
    pattern = "[^\n]+?".join(re.escape(part) for part in literal_parts)
    # A match needs only one character in place of an argument that opens the message. A run there makes the search
    # retry that run at every position of the text.
    return re.compile(pattern.replace("[^\n]+?", "[^\n]", 1) if not literal_parts[0] else pattern)


@patch_environ({}, clear=False)
class HelpOutputTest(unittest.TestCase):
    """Unit tests for the command-line help the README quotes."""

    def test_help_is_wrapped_and_plain(self):
        """Test that the CLI's own help comes back fitting 80 columns, with none of the escape sequences colour adds."""
        help_output = _help_output()
        self.assertIn("usage: update-time", help_output)
        self.assertLessEqual(max(len(line) for line in help_output.splitlines()), 80)
        self.assertNotIn("\x1b[", help_output)

    def test_arguments_are_restored(self):
        """Test that the arguments the process was started with survive being swapped for `-h`."""
        started_with = list(sys.argv)
        _help_output()
        self.assertEqual(sys.argv, started_with)


@patch_environ({}, clear=False)
@patch("tools.generate_readme.generate_log_index", Mock(return_value="the index"))
@patch("tools.generate_readme.sample_log_lines")
@patch("tools.generate_readme._help_output")
@patch("tools.generate_readme.generate")
@patch("tools.generate_readme._TEMPLATE")
class RenderTest(unittest.TestCase):
    """Unit tests for the content each generated file is given."""

    def test_every_placeholder_is_filled(self, template: Mock, log_output: Mock, help_output: Mock, samples: Mock):
        """Test that the template's placeholders are replaced, and the screenshot and the index returned beside it."""
        template.read_text.return_value = "Usage\n@@HELP_OUTPUT@@\nOutput\n@@LOG_OUTPUT@@\nWarning\n@@A_WARNING@@\n"
        log_output.return_value = _LogOutput(svg="<svg/>", text="the sample output")
        help_output.return_value = "the help"
        samples.return_value = _LogSamples(blocks={"@@A_WARNING@@": "the warning"}, by_name={})
        generated = _render()
        self.assertEqual(generated[_README], "Usage\nthe help\nOutput\nthe sample output\nWarning\nthe warning\n")
        self.assertEqual(generated[_SCREENSHOT], "<svg/>")
        self.assertEqual(generated[LOG_INDEX], "the index")

    def test_table_of_contents_lists_the_chapters(self, template: Mock, log: Mock, help_output: Mock, samples: Mock):
        """Test that the table of contents links to each chapter, in the order the template has them."""
        template.read_text.return_value = "@@TABLE_OF_CONTENTS@@\n\n## ⚡ Usage\n\n## 📌 Pinning\n"
        log.return_value = _LogOutput(svg="<svg/>", text="")
        help_output.return_value = ""
        samples.return_value = _LogSamples(blocks={}, by_name={})
        contents = f"{_TABLE_OF_CONTENTS_HEADER}- [⚡ Usage](#-usage)\n- [📌 Pinning](#-pinning)"
        self.assertEqual(_render()[_README], f"{contents}\n\n## ⚡ Usage\n\n## 📌 Pinning\n")


@patch_environ({}, clear=False)
class LogIndexTest(unittest.TestCase):
    """Unit tests for the index of the log messages that `render` generates beside the README."""

    def test_the_index_has_an_entry_for_every_log_message(self):
        """Test that the index gives each log message a heading naming the message."""
        with patch.object(logging.getLogger(), "handlers", []):  # The screenshot configures the root logger
            index = _render()[LOG_INDEX]
        headings = {line.removeprefix("### ") for line in index.splitlines() if line.startswith("### ")}
        names = [message.name for message in Logger.messages()]
        self.assertIn("command-run", names)
        self.assertEqual([name for name in names if name not in headings], [])


class TableOfContentsTest(unittest.TestCase):
    """Unit tests for the chapters the table of contents lists."""

    def test_headings_are_listed_indented(self):
        """Test that a table of contents adds the sections under their chapter, and stops at depth 3."""
        contents = _table_of_contents("## ⚡ Usage\n\n### Workflow\n\n#### Detail\n")
        self.assertEqual(contents, f"{_TABLE_OF_CONTENTS_HEADER}- [⚡ Usage](#-usage)\n  - [Workflow](#workflow)")

    def test_heading_in_a_code_block_is_not_listed(self):
        """Test that a `##` line in a fenced code block gets no entry, since it is sample content, not a chapter."""
        contents = _table_of_contents("## ⚡ Usage\n\n```console\n## not a chapter\n```\n")
        self.assertEqual(contents, f"{_TABLE_OF_CONTENTS_HEADER}- [⚡ Usage](#-usage)")


@patch("tools.generate_readme._render")
class MainTest(unittest.TestCase):
    """Unit tests for regenerating the README and its screenshot."""

    def test_stale_file_is_named_with_the_recipe_that_regenerates_it(self, render: Mock):
        """Test that a stale generated file is named with `just readme`, the recipe that regenerates it."""
        path = Mock(is_file=Mock(return_value=False))
        path.relative_to.return_value = "README.md"
        render.return_value = {path: "the generated content"}
        with patch.object(sys, "argv", ["generate_readme", "--check"]), self.assertRaises(SystemExit) as raised:
            main()
        self.assertIn("README.md out of date, run `just readme` to regenerate", str(raised.exception))
