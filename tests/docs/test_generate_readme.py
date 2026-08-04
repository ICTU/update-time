"""Unit tests for generating the README."""

import re
import sys
import unittest
from unittest.mock import Mock, patch

from docs.generate_log_svg import LogOutput
from docs.generate_readme import _README, _SCREENSHOT, _TEMPLATE, _help_output, main, render
from docs.log_samples import sample_log_lines

from tests.helpers import patch_environ

_PLACEHOLDER = re.compile(r"@@\w+@@")

# The placeholders `render` fills itself, rather than from the log samples.
_FILLED_BY_RENDER = frozenset({"@@HELP_OUTPUT@@", "@@LOG_OUTPUT@@"})


class PlaceholderTest(unittest.TestCase):
    """Unit tests that the template and the samples filling it name the same placeholders."""

    def test_placeholders_and_samples_agree(self):
        """Test that every placeholder in the template has a sample, and every sample is named by the template.

        A placeholder with no sample survives into the README as its own literal text, and a sample the template
        never names is generated for nobody. Neither is reported anywhere else.
        """
        in_template = set(_PLACEHOLDER.findall(_TEMPLATE.read_text()))
        filled = set(sample_log_lines()) | _FILLED_BY_RENDER
        self.assertEqual(in_template, filled)


@patch_environ({}, clear=False)
class HelpOutputTest(unittest.TestCase):
    """Unit tests for the command-line help the README quotes."""

    def test_help_is_wrapped_and_plain(self):
        """Test that the CLI's own help comes back fitting 80 columns, with none of the escape sequences colour adds.

        The README embeds the help verbatim, so a wider terminal would rewrite it and a colouring one would leave
        escape sequences in the file.
        """
        help_output = _help_output()
        self.assertIn("usage: update-time", help_output)
        self.assertLessEqual(max(len(line) for line in help_output.splitlines()), 80)
        self.assertNotIn("\x1b[", help_output)

    def test_arguments_are_restored(self):
        """Test that the arguments the process was started with survive being swapped for `-h`."""
        started_with = list(sys.argv)
        _help_output()
        self.assertEqual(sys.argv, started_with)


@patch("docs.generate_readme.sample_log_lines")
@patch("docs.generate_readme._help_output")
@patch("docs.generate_readme.generate_log_output")
@patch("docs.generate_readme._TEMPLATE")
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


@patch("docs.generate_readme.render")
class MainTest(unittest.TestCase):
    """Unit tests for writing the generated files, and for naming the ones that are out of date."""

    CONTENT = "the generated content"

    def generated_file(self, on_disk: str | None) -> Mock:
        """Return a mock generated file holding the given content, or none at all when it does not exist yet.

        Reading a file that does not exist raises, as it does of a real path, so a caller that reads one without
        asking whether it exists fails rather than quietly seeing no content.
        """
        exists = on_disk is not None
        read_text = Mock(return_value=on_disk) if exists else Mock(side_effect=FileNotFoundError)
        path = Mock(is_file=Mock(return_value=exists), read_text=read_text)
        path.relative_to.return_value = "README.md"
        return path

    def check(self, render: Mock, path: Mock) -> None:
        """Run the generator over the one file, in checking mode."""
        render.return_value = {path: self.CONTENT}
        with patch.object(sys, "argv", ["generate_readme", "--check"]):
            main()

    def test_files_are_written(self, render: Mock):
        """Test that each generated file is written when the generator is not checking."""
        path = self.generated_file("what it held before")
        render.return_value = {path: self.CONTENT}
        with patch.object(sys, "argv", ["generate_readme"]):
            main()
        path.write_text.assert_called_once_with(self.CONTENT)

    def test_current_file_passes_the_check(self, render: Mock):
        """Test that a file already holding the generated content is not reported, and is not written either."""
        path = self.generated_file(self.CONTENT)
        self.check(render, path)
        path.write_text.assert_not_called()

    def test_stale_file_is_named(self, render: Mock):
        """Test that a file whose content has drifted exits the run, naming it and how to regenerate it."""
        path = self.generated_file("what it held before")
        with self.assertRaises(SystemExit) as raised:
            self.check(render, path)
        self.assertIn("README.md out of date, run `just readme` to regenerate", str(raised.exception))
        path.write_text.assert_not_called()

    def test_missing_file_is_named(self, render: Mock):
        """Test that a generated file that does not exist yet counts as out of date."""
        with self.assertRaises(SystemExit):
            self.check(render, self.generated_file(None))
