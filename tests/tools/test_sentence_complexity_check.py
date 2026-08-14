"""Unit tests for the sentence complexity check."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from tools.sentence_complexity_check import (
    _ABBREVIATIONS,
    Prose,
    _faults,
    _is_code,
    _sentence_tokenizer,
    extract_prose,
    extract_prose_from_markdown,
    extract_prose_from_python,
    main,
    sentence_complexity,
)

from tests.helpers import mock_path


class IsCodeTest(unittest.TestCase):
    """Unit tests for telling Python source from prose, so a lint rule's test cases are not measured as sentences."""

    def test_code_and_prose(self):
        """Test that source with a call is code, while prose and a bare word are not."""
        classifications = {
            'self.assertEqual([Path("/file.txt")], list(glob("*.txt")))': True,  # A lint rule's test case.
            "def test_changes(self):\n    self.assertTrue(matches(name))\n": True,  # ...and one across lines.
            "Return whether the text reads as prose.": False,  # Prose does not parse as Python.
            "code": False,  # A bare word parses as a name, with no call in it.
        }
        for text, is_code in classifications.items():
            with self.subTest(text=text):
                self.assertEqual(_is_code(text), is_code)


class ExtractProseFromPythonTest(unittest.TestCase):
    """Unit tests for the prose extracted from a Python file."""

    def prose(self, source_code: str) -> list[str]:
        """Return the text of each run of prose the extractor finds in the source code."""
        return [prose.text for prose in extract_prose_from_python(mock_path(source_code))]

    def test_standalone_comments_join(self):
        """Test that consecutive standalone comments join, so a sentence across them is measured whole."""
        source_code = "# A sentence that starts here\n# and ends on the next line.\nversion = 1\n"
        self.assertEqual(self.prose(source_code), ["A sentence that starts here and ends on the next line."])

    def test_an_f_string_is_measured_once(self):
        """Test that an f-string's literal parts are measured with it, rather than again as literals of their own."""
        source_code = 'msg = f"{name}\'s widgets"\n'
        self.assertEqual(self.prose(source_code), ["'s widgets."])

    def test_raw_string_is_not_prose(self):
        """Test that a raw string is left out whether it interpolates or not, and whatever the case of its prefix."""
        source_code = (
            'pattern = r"the raw string"\n'
            'comment = "the quoted string"\n'
            'interpolated = rf"the raw f-string {name}"\n'
            'message = f"the quoted f-string {name}"\n'
            'shouted = R"the upper-case raw string"\n'
            'announced = F"the upper-case f-string {name}"\n'
        )
        self.assertEqual(
            self.prose(source_code),
            ["the quoted string.", "the quoted f-string.", "the upper-case f-string."],
        )

    def test_f_string_keeps_its_literal_parts(self):
        """Test that an f-string's literal parts are kept and its interpolations dropped.

        The parts are joined where the interpolation stood, leaving the space on either side of it.
        """
        source_code = 'message = f"Pinned {dependency} to the latest version."\n'
        self.assertEqual(self.prose(source_code), ["Pinned  to the latest version."])

    def test_quoted_regexp_is_not_prose(self):
        """Test that a quoted string of backslashes is left out, since it reads as a regexp rather than a sentence."""
        backslashes = "\\\\" * 6  # Six escaped backslashes, one more than a sentence is allowed.
        source_code = f'pattern = "{backslashes}"\ncomment = "A sentence."\n'
        self.assertEqual(self.prose(source_code), ["A sentence."])


class ExtractProseFromMarkdownTest(unittest.TestCase):
    """Unit tests for the prose extracted from a Markdown file."""

    def prose(self, markdown: str) -> list[str]:
        """Return the text of each run of prose the extractor finds in the Markdown."""
        return [prose.text for prose in extract_prose_from_markdown(mock_path(markdown))]

    def test_code_block_is_skipped(self):
        """Test that the lines inside a fenced code block are not prose, and neither are the fences."""
        markdown = "Before the block.\n```console\nnot prose at all\n```\nAfter the block.\n"
        self.assertEqual(self.prose(markdown), ["Before the block.", "After the block."])

    def test_table_row_is_skipped(self):
        """Test that a table row is not prose, since its cells are not sentences."""
        markdown = "| Marker | Effect |\n| ------ | ------ |\nA sentence.\n"
        self.assertEqual(self.prose(markdown), ["A sentence."])

    def test_heading_is_its_own_sentence(self):
        """Test that a heading is a sentence of its own, read without its leading hashes."""
        self.assertEqual(self.prose("## Holding a reference back\n"), ["Holding a reference back."])

    def test_blank_line_is_not_prose(self):
        """Test that a line with nothing on it but whitespace yields nothing to measure."""
        markdown = "A sentence.\n\n   \nAnother sentence.\n"
        self.assertEqual(self.prose(markdown), ["A sentence.", "Another sentence."])


class ExtractProseTest(unittest.TestCase):
    """Unit tests for finding the files to extract prose from, and handing each to the extractor for its kind."""

    def prose(self, *paths: Mock) -> list[str]:
        """Return the text of each run of prose found under the paths."""
        return [prose.text for prose in extract_prose(*paths)]

    def test_a_path_that_is_itself_a_matching_file(self):
        """Test that a file the glob matches is read as it is, rather than searched in."""
        python_file = mock_path("# A comment sentence.\n")
        python_file.is_file.return_value = True
        python_file.match.side_effect = lambda glob: glob == "*.py"
        self.assertEqual(self.prose(python_file), ["A comment sentence."])

    def test_a_directory_is_searched_for_each_glob(self):
        """Test that a directory is searched with every glob, and each file handed to the extractor for its kind."""
        markdown_file = mock_path("A sentence in Markdown.\n")
        directory = Mock(is_file=Mock(return_value=False))
        directory.rglob.side_effect = lambda glob: [markdown_file] if glob == "*.md" else []
        self.assertEqual(self.prose(directory), ["A sentence in Markdown."])


class SentenceComplexityTest(unittest.TestCase):
    """Unit tests for what a sentence's asides and clause joins cost."""

    def test_complexity(self):
        """Test that a nested aside costs more than a flat one, and that a lone em-dash joins clauses."""
        complexities = {
            "A plain sentence.": 1,
            "A sentence (with an aside).": 2,
            # Written as a raw string, which the extractor does not read as prose, so this check does not flag its
            # own fixture for the nesting it is here to measure.
            r"A sentence (with an aside (nested inside it)).": 6,
            "A sentence — with a join (and an aside).": 3,  # A lone em-dash joins, so the aside stays at depth zero.
            "A sentence — an aside — and the rest.": 2,  # A pair of em-dashes brackets one aside.
            "`code`": 0,  # Nothing is left to measure once the inline code is dropped.
        }
        for sentence, complexity in complexities.items():
            with self.subTest(sentence=sentence):
                self.assertEqual(sentence_complexity(sentence), complexity)


class FaultsTest(unittest.TestCase):
    """Unit tests for what makes a sentence hard to read."""

    ASIDE = "A sentence (with an aside)."  # complexity 2, five words, too short for a density
    LONG = "One two three four five six seven eight nine ten eleven twelve thirteen fourteen (fifteen)."

    def test_each_threshold_is_reported_on_its_own(self):
        """Test that complexity, word count and density are each reported alone when only that one is exceeded."""
        cases = (  # The sentence, the maximum complexity, words and density, and what is reported.
            (self.ASIDE, 1, 50, 1.0, "complexity 2"),
            (self.ASIDE, 10, 3, 1.0, "5 words"),
            (self.LONG, 10, 50, 0.05, "0.07 complexity-density"),
            (self.ASIDE, 10, 50, 1.0, ""),
        )
        for sentence, max_complexity, max_words, max_density, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(_faults(sentence, max_complexity, max_words, max_density), expected)

    def test_faults_are_joined(self):
        """Test that a sentence over every threshold reports all three faults in one line."""
        reported = _faults(self.LONG, max_complexity=1, max_words=3, max_density=0.05)
        self.assertEqual(reported, "complexity 2 and 15 words and 0.07 complexity-density")


@patch("tools.sentence_complexity_check.nltk")
@patch("tools.sentence_complexity_check.PunktTokenizer")
class SentenceTokenizerTest(unittest.TestCase):
    """Unit tests for the sentence splitter the check measures with."""

    def test_abbreviations_are_taught(self, punkt_tokenizer: Mock, nltk: Mock):
        """Test that the splitter is taught the abbreviations that would otherwise end a sentence for it."""
        tokenizer = punkt_tokenizer.return_value
        self.assertIs(_sentence_tokenizer(), tokenizer)
        tokenizer._params.abbrev_types.update.assert_called_once_with(_ABBREVIATIONS)
        nltk.download.assert_not_called()  # The data was there, so nothing was fetched.

    def test_missing_data_is_downloaded(self, punkt_tokenizer: Mock, nltk: Mock):
        """Test that missing punkt data is fetched into the directory the splitter searches."""
        built = Mock()
        punkt_tokenizer.side_effect = [LookupError, built]
        self.assertIs(_sentence_tokenizer(), built)
        nltk.data.path.append.assert_called_once_with(".nltk")
        nltk.download.assert_called_once_with("punkt_tab", quiet=True, download_dir=".nltk")
        built._params.abbrev_types.update.assert_called_once_with(_ABBREVIATIONS)


@patch("tools.sentence_complexity_check.extract_prose")
@patch("tools.sentence_complexity_check._sentence_tokenizer")
class MainTest(unittest.TestCase):
    """Unit tests for the exit code the check returns, and what it writes."""

    def check(self, tokenizer: Mock, extract: Mock, text: str) -> tuple[int, str]:
        """Run the check over one run of prose, and return its exit code and what it wrote."""
        prose = Prose(Path("conf.py"), text, 1)
        extract.return_value = [prose]
        tokenizer.return_value.span_tokenize.return_value = [(0, len(prose.text))]
        written = io.StringIO()
        with redirect_stdout(written), patch.object(sys, "argv", ["check", "src"]):
            return main(), written.getvalue()

    def test_hard_to_read_sentence(self, tokenizer: Mock, extract: Mock):
        """Test that a sentence over a threshold is written out with its location, and fails the run."""
        exit_code, written = self.check(tokenizer, extract, r"A sentence (with an aside (nested inside it)).")
        self.assertEqual(exit_code, 1)
        self.assertIn("conf.py:1: complexity 6:", written)

    def test_readable_sentence(self, tokenizer: Mock, extract: Mock):
        """Test that a sentence under every threshold is not written out, and leaves the run passing."""
        exit_code, written = self.check(tokenizer, extract, "A plain sentence.")
        self.assertEqual(exit_code, 0)
        self.assertEqual(written, "")
