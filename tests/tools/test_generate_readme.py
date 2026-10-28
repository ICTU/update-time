"""Unit tests for generating the README."""

import re
import sys
import unittest
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from tools.generate_log_svg import LogOutput
from tools.generate_readme import _README, _SCREENSHOT, _TEMPLATE, _help_output, _table_of_contents, main, render
from tools.log_samples import sample_log_lines

from tests.helpers import patch_environ
from tests.update_time.helpers import logged_messages

if TYPE_CHECKING:
    from update_time.io.log import LogMessage

_PLACEHOLDER = re.compile(r"@@\w+@@")

# The placeholders `render` fills itself, rather than from the log samples.
_FILLED_BY_RENDER = frozenset(
    {"@@DOCKER_IMAGE_FILES_TABLE@@", "@@HELP_OUTPUT@@", "@@LOG_OUTPUT@@", "@@TABLE_OF_CONTENTS@@"}
)

_TABLE_OF_CONTENTS_HEADER = "## ☰ Table of contents\n\n"

# An argument of a log message's format string, such as `%(location)s`.
_MESSAGE_ARGUMENT = re.compile(r"%\(\w+\)[sd]")


def _quoted_log_messages(text: str) -> list[str]:
    """Return the name of each log message the text quotes, whatever values fill in its arguments."""
    return [name for name, message in logged_messages().items() if _message_pattern(message).search(text)]


def _message_pattern(message: LogMessage) -> re.Pattern[str]:
    """Return a pattern matching the message on a single line, with any text in place of each argument."""
    literal_parts = _MESSAGE_ARGUMENT.split(message.format)
    pattern = "[^\n]+?".join(re.escape(part) for part in literal_parts)
    # A match needs only one character in place of an argument that opens the message. A run there makes the search
    # retry that run at every position of the text.
    return re.compile(pattern.replace("[^\n]+?", "[^\n]", 1) if not literal_parts[0] else pattern)


class PlaceholderTest(unittest.TestCase):
    """Unit tests that the template leaves its placeholders, and the log lines it quotes, to the samples."""

    def test_placeholders_and_samples_agree(self):
        """Test that every placeholder in the template has a sample, and every sample is named by the template."""
        in_template = set(_PLACEHOLDER.findall(_TEMPLATE.read_text()))
        filled = set(sample_log_lines()) | _FILLED_BY_RENDER
        self.assertEqual(in_template, filled)

    def test_the_template_quotes_no_log_message_itself(self):
        """Test that each log message the README quotes comes from a sample, rather than from the template's text."""
        # The generated README quotes the samples, so the search does find a message where one is quoted.
        self.assertNotEqual(_quoted_log_messages(_README.read_text()), [])
        self.assertEqual(_quoted_log_messages(_TEMPLATE.read_text()), [])


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


@patch("tools.generate_readme.sample_log_lines")
@patch("tools.generate_readme._help_output")
@patch("tools.generate_readme.generate_log_output")
@patch("tools.generate_readme._TEMPLATE")
class RenderTest(unittest.TestCase):
    """Unit tests for the content each generated file is given."""

    def test_every_placeholder_is_filled(self, template: Mock, log_output: Mock, help_output: Mock, samples: Mock):
        """Test that the template's placeholders are replaced, and the screenshot returned beside the README."""
        template.read_text.return_value = "Usage\n@@HELP_OUTPUT@@\nOutput\n@@LOG_OUTPUT@@\nWarning\n@@A_WARNING@@\n"
        log_output.return_value = LogOutput(svg="<svg/>", text="the sample output")
        help_output.return_value = "the help"
        samples.return_value = {"@@A_WARNING@@": "the warning"}
        generated = render()
        self.assertEqual(generated[_README], "Usage\nthe help\nOutput\nthe sample output\nWarning\nthe warning\n")
        self.assertEqual(generated[_SCREENSHOT], "<svg/>")

    def test_table_of_contents_lists_the_chapters(self, template: Mock, log: Mock, help_output: Mock, samples: Mock):
        """Test that the table of contents links to each chapter, in the order the template has them."""
        template.read_text.return_value = "@@TABLE_OF_CONTENTS@@\n\n## ⚡ Usage\n\n## 📌 Pinning\n"
        log.return_value = LogOutput(svg="<svg/>", text="")
        help_output.return_value = ""
        samples.return_value = {}
        contents = f"{_TABLE_OF_CONTENTS_HEADER}- [⚡ Usage](#-usage)\n- [📌 Pinning](#-pinning)"
        self.assertEqual(render()[_README], f"{contents}\n\n## ⚡ Usage\n\n## 📌 Pinning\n")


class TableOfContentsTest(unittest.TestCase):
    """Unit tests for the chapters the table of contents lists."""

    def test_only_chapters_are_listed_at_depth_two(self):
        """Test that a section or subsection heading gets no entry, so the table of contents lists chapters only."""
        contents = _table_of_contents("## ⚡ Usage\n\n### Workflow\n\n#### Detail\n", depth=2)
        self.assertEqual(contents, f"{_TABLE_OF_CONTENTS_HEADER}- [⚡ Usage](#-usage)")

    def test_deeper_headings_are_listed_indented(self):
        """Test that a deeper table of contents adds the sections under their chapter, and stops at the depth."""
        contents = _table_of_contents("## ⚡ Usage\n\n### Workflow\n\n#### Detail\n", depth=3)
        expected = f"{_TABLE_OF_CONTENTS_HEADER}- [⚡ Usage](#-usage)\n  - [Workflow](#workflow)"
        self.assertEqual(contents, expected)

    def test_heading_in_a_code_block_is_not_listed(self):
        """Test that a `##` line in a fenced code block gets no entry, since it is sample content, not a chapter."""
        contents = _table_of_contents("## ⚡ Usage\n\n```console\n## not a chapter\n```\n")
        self.assertEqual(contents, f"{_TABLE_OF_CONTENTS_HEADER}- [⚡ Usage](#-usage)")


@patch("tools.generate_readme.render")
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
