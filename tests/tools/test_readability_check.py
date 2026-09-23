"""Unit tests for the readability check."""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from tools import readability_check
from tools.readability_check import (
    _ABBREVIATIONS,
    Prose,
    _faults,
    _is_code,
    _Limits,
    _sentence_tokenizer,
    _tagged,
    _whitelisted_sentences,
    extract_prose,
    extract_prose_from_markdown,
    extract_prose_from_python,
    main,
    sentence_complexity,
)

from tests.helpers import mock_path
from tests.mutation import Mutation, kills

# Every fixture below stays under these limits, so a case reports the rule under test alone.
_RELAXED = _Limits(complexity=10, words=50, density=1.0)


class IsCodeTest(unittest.TestCase):
    """Unit tests for telling Python source from prose, so a lint rule's test cases are not measured as sentences."""

    @kills(
        Mutation(
            readability_check._is_code,
            "for candidate in (source, block):",
            "for candidate in (source,):",
            "the check measures a snippet as prose when it does not parse on its own, so code reads as a sentence",
        )
    )
    def test_code_and_prose(self):
        """Test that source with a call is code, while prose and a bare word are not."""
        classifications = {
            'self.assertEqual([Path("/file.txt")], list(glob("*.txt")))': True,  # A lint rule's test case.
            "def test_changes(self):\n    self.assertTrue(matches(name))\n": True,  # ...and one across lines.
            'for name in filter(None, parts.split(".")):': True,  # ...and a header a mutation snippet quotes alone.
            "if found:\nanchor = getattr(anchor, name)": True,  # ...and a block a docstring's cleaning flattened.
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

    def test_an_exception_message_is_not_prose(self):
        """Test that a string opening with an exception's name is left out, since it quotes a traceback's last line."""
        source_code = (
            'raises = "ValueError: zip() argument 2 is shorter than argument 1"\n'
            "reason = \"KeyError: 'access_token'\"\n"
            'comment = "A sentence."\n'
        )
        self.assertEqual(self.prose(source_code), ["A sentence."])

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

    def test_list_item_is_read_without_its_marker(self):
        """Test that a list item is read without the marker opening it, at whatever depth it sits."""
        markdown = "- A sentence.\n* Another sentence.\n  - A nested sentence.\n"
        self.assertEqual(self.prose(markdown), ["A sentence.", "Another sentence.", "A nested sentence."])

    def test_a_rule_is_not_a_list_item(self):
        """Test that a horizontal rule keeps its dashes, since the marker of a list item is followed by a space."""
        self.assertEqual(self.prose("---\n"), ["---."])

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
        cases = (  # The sentence, the limits it is measured against, and what is reported.
            (self.ASIDE, _Limits(complexity=1, words=50, density=1.0), "complexity 2"),
            (self.ASIDE, _Limits(complexity=10, words=3, density=1.0), "5 words"),
            (self.LONG, _Limits(complexity=10, words=50, density=0.05), "0.07 complexity-density"),
            (self.ASIDE, _Limits(complexity=10, words=50, density=1.0), ""),
        )
        for sentence, limits, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(_faults(sentence, limits), expected)

    # Written as raw strings, which the extractor does not read as prose, so this check does not flag its own
    # fixtures for the fault they are here to measure.
    SPLIT = r"The window the repository is asked about is the one the option sets."
    JOINED = r"The repository is asked about the window the option sets."
    ASIDE_CLAUSE = r"The repository (the one the option names) is asked about the window."
    PREPOSITION = r"A noun phrase inside an aside starts a clause of its own."
    TO_PHRASE = r"A change to the cooldown reaches every reference in the run."
    JOINED_SUBJECT = r"The bound and the cooldown are the two constraints on the candidates."
    LISTED_SUBJECT = r"A wildcard, an arbitrary equality, and a combined specifier are read without a version."

    @kills(
        Mutation(
            readability_check,
            "            return determiners >= _SPLIT_SUBJECT_DETERMINERS\n",
            "            return False\n",
            "no sentence is reported for splitting its subject from its verb, so the fault goes unreported",
        ),
        Mutation(
            readability_check,
            "    tagged = _tagged(_without_asides(sentence))\n",
            "    tagged = _tagged(sentence)\n",
            "an aside's noun phrase reads as a clause wedged into the subject, so plain prose is reported",
        ),
        Mutation(
            readability_check,
            "        if tag == _DETERMINER and previous not in _BESIDE_THE_SUBJECT:\n",
            "        if tag == _DETERMINER:\n",
            "a noun phrase standing beside the subject reads as a clause wedged into it, so plain prose is reported",
        ),
    )
    def test_a_subject_split_from_its_verb_is_reported(self):
        """Test that a sentence starting a second noun phrase before its verb is reported, and the rest are not.

        An aside holds one kind of second noun phrase, a preposition or `to` opens another, and `and` or a comma
        joins a third to the first. None of them wedges a clause between the subject and its verb.
        """
        cases = {
            "a clause between the subject and its verb": (self.SPLIT, "subject split from its verb"),
            "a subject that reaches its verb": (self.JOINED, ""),
            "an aside holds the noun phrase": (self.ASIDE_CLAUSE, ""),
            "a preposition opens the noun phrase": (self.PREPOSITION, ""),
            "`to` opens the noun phrase": (self.TO_PHRASE, ""),
            "`and` joins two subjects": (self.JOINED_SUBJECT, ""),
            "a comma lists three subjects": (self.LISTED_SUBJECT, ""),
        }
        for case, (sentence, expected) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(_faults(sentence, _RELAXED), expected)

    NEGATED_NOUN = r"The repository is asked about no artefact whose coordinates name a property."
    NEGATED_VERB = r"The repository is not asked about an artefact whose coordinates name a property."
    NEGATED_SUBJECT = r"No artefact whose coordinates name a property is asked about."
    NEGATED_EXISTENCE = r"The check passes when there is no new version."

    @kills(
        Mutation(
            readability_check,
            "    if _negates_a_noun_phrase(tagged):\n",
            "    if False:\n",
            "no sentence is reported for hanging its negation on a noun phrase, so the fault goes unreported",
        ),
        Mutation(
            readability_check,
            "        elif seen_verb and tag == _DETERMINER",
            "        elif tag == _DETERMINER",
            "a subject opening with the negation is reported, though it carries the negation to the verb",
        ),
        Mutation(
            readability_check,
            "and not _denies_existence(tagged, index)",
            "",
            "`there is no new version` is reported, though that is how plain prose says it",
        ),
    )
    def test_a_negation_in_a_noun_phrase_is_reported(self):
        """Test that a negation the sentence hangs on a noun phrase is reported, and one on its verb is not."""
        cases = {
            "a negation hung on an object": (
                self.NEGATED_NOUN,
                "negation in a noun phrase (put the negation on the verb, as in 'does not have a release')",
            ),
            "a negation on the verb": (self.NEGATED_VERB, ""),
            "a negation opening the subject": (self.NEGATED_SUBJECT, ""),
            "a negation denying that anything exists": (self.NEGATED_EXISTENCE, ""),
        }
        for case, (sentence, expected) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(_faults(sentence, _RELAXED), expected)

    def test_faults_are_joined(self):
        """Test that a sentence over every threshold reports its faults in one line."""
        reported = _faults(self.LONG, _Limits(complexity=1, words=3, density=0.05))
        self.assertEqual(reported, "complexity 2 and 15 words and 0.07 complexity-density")


@patch("tools.readability_check.nltk")
class TaggedTest(unittest.TestCase):
    """Unit tests for reading a word's part of speech."""

    def test_the_tagger_is_used_as_it_is(self, nltk: Mock):
        """Test that a tagger already in place tags the words, and nothing is fetched."""
        self.assertIs(_tagged("A sentence."), nltk.pos_tag.return_value)
        nltk.download.assert_not_called()

    def test_the_data_directory_is_searched_once(self, nltk: Mock):
        """Test that the directories nltk searches gain this check's own once, however many sentences are tagged."""
        nltk.data.path = []
        _tagged("A sentence.")
        _tagged("Another sentence.")
        self.assertEqual(nltk.data.path, [".nltk"])

    def test_a_missing_splitter_is_downloaded(self, nltk: Mock):
        """Test that a missing splitter is fetched and the words read after, since reading them needs it."""
        words = Mock()
        nltk.word_tokenize.side_effect = [LookupError, words]
        self.assertIs(_tagged("A sentence."), nltk.pos_tag.return_value)
        nltk.download.assert_called_once_with("punkt_tab", quiet=True, download_dir=".nltk")
        nltk.pos_tag.assert_called_once_with(words)

    def test_missing_data_is_downloaded(self, nltk: Mock):
        """Test that a missing tagger is fetched into the directory nltk searches, and the words tagged after."""
        tagged = Mock()
        nltk.pos_tag.side_effect = [LookupError, tagged]
        self.assertIs(_tagged("A sentence."), tagged)
        nltk.download.assert_called_once_with("averaged_perceptron_tagger_eng", quiet=True, download_dir=".nltk")


@patch("tools.readability_check.nltk")
@patch("tools.readability_check.PunktTokenizer")
class InstallDataTest(unittest.TestCase):
    """Unit tests for fetching the nltk datasets ahead of a run that cannot fetch them itself."""

    def test_both_datasets_are_fetched(self, tokenizer: Mock, nltk: Mock):
        """Test that a run given `--install-data` fetches the splitter and the tagger, and reports success."""
        tokenizer.side_effect = [LookupError, Mock()]
        nltk.pos_tag.side_effect = [LookupError, Mock()]
        with patch.object(sys, "argv", ["check", "--install-data"]):
            self.assertEqual(main(), 0)
        fetched = [call.args[0] for call in nltk.download.call_args_list]
        self.assertEqual(fetched, ["punkt_tab", "averaged_perceptron_tagger_eng"])


@patch("tools.readability_check.nltk")
@patch("tools.readability_check.PunktTokenizer")
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


class WhitelistedSentencesTest(unittest.TestCase):
    """Unit tests for reading the sentences the check passes over."""

    def test_a_sentence_per_line(self):
        """Test that the file holds one sentence per line, and that every line is read as one."""
        whitelist = mock_path("One sentence.\nAnother sentence.\n")
        self.assertEqual(_whitelisted_sentences(whitelist), {"One sentence.", "Another sentence."})

    def test_no_whitelist_file(self):
        """Test that a whitelist file that does not exist passes over nothing."""
        self.assertEqual(_whitelisted_sentences(Mock(exists=Mock(return_value=False))), set())


@patch("tools.readability_check.extract_prose")
@patch("tools.readability_check._sentence_tokenizer")
class MainTest(unittest.TestCase):
    """Unit tests for the exit code the check returns, and what it writes."""

    # The extractor does not read a raw string as prose, so this check does not flag its own fixture. The wrapped
    # spelling is the same sentence, with the prose around it broken across two lines.
    NESTED = r"A sentence (with asides (nested inside it))."
    WRAPPED = NESTED.replace("asides (", "asides\n(")
    ASIDE = r"Another sentence (with an aside (nested in it))."

    def check(
        self,
        tokenizer: Mock,
        extract: Mock,
        *texts: str,
        arguments: tuple[str, ...] = ("src",),
        whitelist: tuple[str, ...] | None = (),
        option: str = "--whitelist",
    ) -> tuple[int, str]:
        """Run the check over the runs of prose, and return its exit code and output."""
        extract.return_value = [Prose(Path("conf.py"), text, 1) for text in texts]
        tokenizer.return_value.span_tokenize.side_effect = lambda masked: [(0, len(masked))]
        written = io.StringIO()
        files = {} if whitelist is None else {Path("prose.txt"): "".join(f"{sentence}\n" for sentence in whitelist)}
        read = patch.object(Path, "read_text", autospec=True, side_effect=lambda path: files[path])
        found = patch.object(Path, "exists", autospec=True, side_effect=lambda path: path in files)
        argv = ["check", *(() if whitelist is None else (option, "prose.txt")), *arguments]
        with redirect_stdout(written), patch.object(sys, "argv", argv), read, found:
            return main(), written.getvalue()

    def test_a_stale_whitelist_entry_is_reported(self, tokenizer: Mock, extract: Mock):
        """Test that an entry of the file `--check-whitelist` names that the run does not match is reported."""
        stale = r"A sentence the prose no longer holds."
        whitelist = (self.NESTED, stale)  # The flagged sentence is held, so the stale entry is the only report.
        exit_code, written = self.check(
            tokenizer, extract, self.NESTED, whitelist=whitelist, option="--check-whitelist"
        )
        self.assertEqual(exit_code, 1)
        self.assertIn(
            f"prose.txt holds a sentence the prose no longer has, run `just update-whitelists`:\n{stale}", written
        )

    def test_a_whitelist_entry_that_is_still_needed(self, tokenizer: Mock, extract: Mock):
        """Test that an entry matching a sentence the run flags is left unreported, and leaves the run passing."""
        whitelist = (self.NESTED,)
        exit_code, written = self.check(
            tokenizer, extract, self.NESTED, whitelist=whitelist, option="--check-whitelist"
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(written, "")

    def test_hard_to_read_sentence(self, tokenizer: Mock, extract: Mock):
        """Test that a sentence over a threshold is written out with its location, and fails the run."""
        exit_code, written = self.check(tokenizer, extract, self.NESTED)
        self.assertEqual(exit_code, 1)
        self.assertIn("conf.py:1: complexity 6:", written)

    def test_a_run_naming_no_whitelist(self, tokenizer: Mock, extract: Mock):
        """Test that a run without `--whitelist` reports every flagged sentence, without reading a file."""
        exit_code, written = self.check(tokenizer, extract, self.NESTED, whitelist=None)
        self.assertEqual(exit_code, 1)
        self.assertIn("conf.py:1: complexity 6:", written)

    def test_naming_the_whitelist_twice_is_an_error(self, _tokenizer: Mock, _extract: Mock):
        """Test that a run naming the whitelist in two options at once ends with a usage error."""
        cases = {
            "plain and check": ("--whitelist", "--check-whitelist"),
            "check and make": ("--check-whitelist", "--make-whitelist"),
        }
        for case, (first, second) in cases.items():
            with self.subTest(case=case):
                argv = ["check", first, "one.txt", second, "two.txt", "src"]
                with (
                    patch.object(sys, "argv", argv),
                    patch.object(Path, "exists", autospec=True, return_value=False),
                    patch.object(Path, "write_text", autospec=True) as write_text,
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    main()
                self.assertEqual(raised.exception.code, 2)
                write_text.assert_not_called()

    def test_readable_sentence(self, tokenizer: Mock, extract: Mock):
        """Test that a sentence under every threshold is not written out, and leaves the run passing."""
        exit_code, written = self.check(tokenizer, extract, "A plain sentence.")
        self.assertEqual(exit_code, 0)
        self.assertEqual(written, "")

    def test_a_whitelisted_sentence(self, tokenizer: Mock, extract: Mock):
        """Test that the check passes over a sentence the whitelist holds, and leaves the run passing."""
        exit_code, written = self.check(tokenizer, extract, self.NESTED, whitelist=(self.NESTED,))
        self.assertEqual(exit_code, 0)
        self.assertEqual(written, "")

    def test_a_whitelisted_sentence_the_prose_wraps(self, tokenizer: Mock, extract: Mock):
        """Test that the whitelist holds a sentence however the prose wraps it, since it records the text alone."""
        exit_code, written = self.check(tokenizer, extract, self.WRAPPED, whitelist=(self.NESTED,))
        self.assertEqual(exit_code, 0)
        self.assertEqual(written, "")

    def make_whitelist(
        self, tokenizer: Mock, extract: Mock, *texts: str, whitelist: tuple[str, ...]
    ) -> tuple[Path, str]:
        """Make the whitelist from the given runs of prose, and return the file the run wrote and its text."""
        with patch.object(Path, "write_text", autospec=True) as write_text:
            exit_code, written = self.check(tokenizer, extract, *texts, whitelist=whitelist, option="--make-whitelist")
        self.assertEqual((exit_code, written), (0, ""))
        path, text = write_text.call_args.args
        return path, text

    def test_making_the_whitelist(self, tokenizer: Mock, extract: Mock):
        """Test that the file `--make-whitelist` names keeps a flagged sentence on one line only where it holds it."""
        path, text = self.make_whitelist(tokenizer, extract, self.WRAPPED, self.ASIDE, whitelist=(self.NESTED,))
        self.assertEqual(path, Path("prose.txt"))
        self.assertEqual(text, f"{self.NESTED}\n")

    def test_the_whitelist_holds_each_sentence_once(self, tokenizer: Mock, extract: Mock):
        """Test that a sentence two files hold is written once, and that the sentences come out sorted.

        Regenerating the whitelist then rewrites the lines that changed rather than reshuffling the whole file.
        """
        texts = (self.ASIDE, self.NESTED, self.ASIDE)
        _path, text = self.make_whitelist(tokenizer, extract, *texts, whitelist=(self.ASIDE, self.NESTED))
        self.assertEqual(text, f"{self.NESTED}\n{self.ASIDE}\n")
