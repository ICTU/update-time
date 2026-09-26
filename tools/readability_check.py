"""Report hard-to-read sentences in the prose of the Python and Markdown files under a directory."""

import argparse
import ast
import inspect
import io
import re
import sys
import textwrap
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

# nltk ships no `py.typed` marker, so mypy has no types to resolve for it.
import nltk  # type: ignore[import-untyped]
from nltk.tokenize import PunktTokenizer  # type: ignore[import-untyped]

from tools.markdown import lines_without_code_blocks

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

_INLINE_CODE = re.compile(r"`[^`]*`")

# What a Markdown line opens with rather than with its prose: a heading's hashes, or a list item's marker. A heading
# is a sentence of its own, and an item is one line of prose like any other. The space keeps a rule (`---`) whole.
_MARKUP_PREFIX = re.compile(r"^\s*(?:#+|[-*])\s+")

# A printf-style interpolation, such as a log message's `%(location)s`: a value rather than prose.
_INTERPOLATION = re.compile(r"%\(\w+\)[a-z]")

# A string literal's opening: the letters prefixing it, if any, and its quote. Anchored at the start of a node's
# source, so it tells a literal from anything else the parse tree holds.
_STRING_PREFIX = re.compile(r"(?P<prefix>[A-Za-z]*)['\"]")

# Abbreviations the splitter would otherwise take for the end of a sentence, spelled without their trailing period.
_ABBREVIATIONS = frozenset({"e.g", "i.e", "etc"})

# The directory holding this check's nltk data, and the datasets fetched into it. The tagger reads a word's part
# of speech, and the splitter reads where a sentence ends.
_NLTK_DATA = ".nltk"
_TAGGER = "averaged_perceptron_tagger_eng"
_SPLITTER = "punkt_tab"

# A subject reaching its own verb opens one noun phrase; a second one before that verb is a clause wedged between.
_SPLIT_SUBJECT_DETERMINERS = 2

# The nltk tags this check reads. A determiner opens a noun phrase. A verb carries a tag starting with the verb
# prefix, whatever its tense. A preposition introduces a noun phrase belonging to the subject, and a conjunction or
# a comma another subject standing beside it, so neither introduces a noun phrase that interrupts the subject. The
# tagger gives `to` a tag of its own rather than the preposition's, so the set names both.
_DETERMINER = "DT"
_VERB_PREFIX = "VB"
_BESIDE_THE_SUBJECT = frozenset({"IN", "TO", "CC", ","})

# The `there` of `there is no new version`, which sits two words before the negation it introduces: itself, and the
# `is` or `are` between them.
_EXISTENTIAL = "EX"
_EXISTENTIAL_REACH = 2

# A parenthesised aside without an aside of its own, so repeating the substitution reaches the nested ones too.
_ASIDE = re.compile(r"\([^()]*\)")

# Below this many words a ratio says more about a sentence's length than about its density.
_RATIO_WORDS = 15

# Above this many backslashes a string reads as a regular expression rather than as prose.
_REGEXP_BACKSLASHES = 5

# A traceback's last line: the exception's name, then its message.
_EXCEPTION_MESSAGE = re.compile(r"\w+(Error|Exception): ")


class Prose:
    """A run of prose from a file, and the line it starts on."""

    def __init__(self, file_path: Path, text: str, line_number: int) -> None:
        """Store the prose, ending the text with punctuation so the tokenizer reads it as a sentence."""
        self.file_path = file_path
        self.text = text if text[-1] in ".?!" else text + "."
        self.line_number = line_number

    @property
    def location(self) -> str:
        """Return the `path:line` the prose starts at."""
        return f"{self.file_path}:{self.line_number}"


def _drop_markup(text: str, code: str = "") -> str:
    """Drop Markdown images, reduce links to their text, and replace each run of inline code with `code`.

    Comments and docstrings are written in the same style as Markdown, so both prose sources need this.
    """
    text = re.sub(r" ?!\[[^\]]*\]\([^)]*\)", "", text)  # Images.
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # Links, reduced to their text.
    return _INLINE_CODE.sub(code, text)


def _drop_irrelevant_parentheses(text: str) -> str:
    """Drop parentheses that are no aside: those without word content, and word-attached ones like `(re)writes`."""
    text = re.sub(r" ?\([^\w()]*\)", "", text)  # Left empty by dropped inline code, e.g. `(/)`.
    return re.sub(r"(?<=\w)\([^()]*\)|\([^()]*\)(?=\w)", lambda match: match[0].strip("()"), text)


def extract_prose_from_markdown(markdown_file: Path) -> Iterator[Prose]:
    """Yield the prose in the Markdown file, with the line each run starts on."""
    for line_number, line in lines_without_code_blocks(markdown_file.read_text()):
        if line.startswith("|"):  # A table row: not prose.
            continue
        if text := _MARKUP_PREFIX.sub("", line, count=1).strip():
            yield Prose(markdown_file, text, line_number)


def _is_standalone(comment: tokenize.TokenInfo) -> bool:
    """Return whether the comment has its line to itself."""
    return comment.line[: comment.start[1]].strip() == ""


def _string_fragments(source_code: str) -> Iterator[_Fragment]:
    """Yield a fragment per string literal that holds prose, taken from the parse tree.

    The tree folds implicitly concatenated literals into one node. An f-string yields its literal parts, without
    the interpolations between them, and those parts are not yielded again on their own.
    """
    tree = ast.parse(source_code)
    interpolated = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for part in node.values}
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr) and _is_prose_string(node, source_code):
            yield _Fragment(node.lineno, _literal_text(node))
        elif isinstance(node, ast.Constant) and (text := _own_prose(node, source_code, interpolated)) is not None:
            yield _Fragment(node.lineno, text)


def _literal_text(node: ast.JoinedStr) -> str:
    """Return an f-string's literal parts joined: the text it holds without the interpolations between them."""
    parts = (part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str))
    return "".join(parts)


def _own_prose(node: ast.Constant, source_code: str, interpolated: set[int]) -> str | None:
    """Return the prose the constant holds as a literal in its own right, or None when it holds none."""
    if not isinstance(node.value, str) or id(node) in interpolated or not _is_prose_string(node, source_code):
        return None
    return inspect.cleandoc(node.value)


def _is_prose_string(node: ast.expr, source_code: str) -> bool:
    """Return whether the node is a string literal holding prose rather than a regexp."""
    prefix = _STRING_PREFIX.match(ast.get_source_segment(source_code, node) or "")
    return prefix is not None and "r" not in prefix.group("prefix").lower()


def _is_regexp(text: str) -> bool:
    """Return whether the text reads as a regular expression rather than as prose."""
    return text.count("\\") > _REGEXP_BACKSLASHES


def _is_exception_message(text: str) -> bool:
    """Return whether the text reads as a traceback's last line rather than as a sentence."""
    return _EXCEPTION_MESSAGE.match(text) is not None


# A block's body is indented by this when a snippet is tried as one. Any consistent width would do.
_INDENT = "    "


def _is_code(text: str) -> bool:
    """Return whether the text reads as Python source rather than as prose.

    A snippet quoted from a file need not parse on its own. It may hold a compound statement's header without the
    body below it, or a body whose indentation a docstring's cleaning stripped, so it is tried as a block as well.
    """
    source = textwrap.dedent(text).strip()
    header, _, body = source.partition("\n")
    block = f"{header}\n{textwrap.indent(body, _INDENT)}\n{_INDENT}pass"
    for candidate in (source, block):
        try:
            tree = ast.parse(candidate)
        except SyntaxError:
            continue
        return any(isinstance(node, ast.Call) for node in ast.walk(tree))
    return False


@dataclass(order=True)
class _Fragment:
    """A comment or string from a Python file, and the line it starts on."""

    line_number: int
    text: str


def extract_prose_from_python(python_file: Path) -> Iterator[Prose]:
    """Yield the prose in the Python file, its comments and strings, with the line each starts on."""
    source_code = python_file.read_text()
    # Comments come from the token stream, as the parse tree discards them. A block of standalone comment lines
    # joins into one fragment, so a sentence running across two of them is measured whole, brackets and all.
    fragments: list[_Fragment] = []
    previous_line = None  # The line of the comment before this one, when that one stood alone.
    for token in tokenize.generate_tokens(io.StringIO(source_code).readline):
        if token.type != tokenize.COMMENT:
            continue
        line_number, comment = token.start[0], token.string.lstrip("#")
        standalone = _is_standalone(token)
        if standalone and previous_line == line_number - 1 and fragments:
            fragments[-1].text += comment
        else:
            fragments.append(_Fragment(line_number, comment))
        previous_line = line_number if standalone else None
    fragments.extend(_string_fragments(source_code))
    for fragment in sorted(fragments):
        if (text := _INTERPOLATION.sub("", fragment.text).strip()) and not (
            _is_regexp(fragment.text) or _is_code(fragment.text) or _is_exception_message(fragment.text)
        ):
            yield Prose(python_file, text, fragment.line_number)


def _matching_files(path: Path, glob: str) -> Iterator[Path]:
    """Yield the path itself when it is a file the glob matches, or its matches when it is a directory."""
    if path.is_file():
        if path.match(glob):
            yield path
    else:
        yield from sorted(path.rglob(glob))


def extract_prose(*paths: Path) -> Iterator[Prose]:
    """Yield the prose in the files under the paths, each of which may be a file or a directory."""
    extractors = {
        "*.py": extract_prose_from_python,
        "*.md": extract_prose_from_markdown,
        "*.md.in": extract_prose_from_markdown,
    }
    for path in paths:
        for glob, extractor in extractors.items():
            for file_path in _matching_files(path, glob):
                yield from extractor(file_path)


def sentence_complexity(sentence: str) -> int:
    """Return the sentence complexity: one, plus a cost per aside or clause join that grows with nesting depth."""
    sentence = _drop_irrelevant_parentheses(_drop_markup(sentence))
    if not sentence:
        return 0
    em_dash_count = sentence.count("—")
    lone_em_dash = em_dash_count if em_dash_count % 2 else 0  # An odd count leaves the last em-dash unpaired.
    complexity = 1
    depth = 0
    em_dashes_seen = 0
    for character in sentence:
        mark = character
        if character == "—":
            em_dashes_seen += 1
            # The unpaired em-dash joins a clause like a semicolon; each paired one brackets an aside.
            if em_dashes_seen == lone_em_dash:
                mark = ";"
            elif em_dashes_seen % 2:
                mark = "("
            else:
                mark = ")"
        if mark in ("(", ";"):  # An opening aside or a clause join costs more the deeper it nests.
            complexity += (depth + 1) ** 2
        if mark == "(":
            depth += 1
        elif mark == ")":
            depth -= 1
    return complexity


def _sentence_words(sentence: str) -> int:
    """Return how many words the sentence has, a run of inline code counting as one and a link as its own text."""
    return len(_drop_markup(sentence, "code").split())


def _sentence_density(complexity: int, words: int) -> float:
    """Return the asides and clause joins per word, or zero for a sentence too short to read a ratio off."""
    return (complexity - 1) / words if words >= _RATIO_WORDS else 0.0


def _with_nltk_data[T](dataset: str, build: Callable[[], T]) -> T:
    """Return what `build` makes, fetching the nltk dataset into this check's own directory where it is missing."""
    if _NLTK_DATA not in nltk.data.path:
        nltk.data.path.append(_NLTK_DATA)
    try:
        return build()
    except LookupError:
        nltk.download(dataset, quiet=True, download_dir=_NLTK_DATA)
        return build()


def _tagged(sentence: str) -> list[tuple[str, str]]:
    """Return the sentence's words, each with the part of speech nltk reads for it.

    Reading the words needs the splitter, and tagging them the tagger, so each fetches the dataset it needs.
    """
    text = _drop_markup(sentence, "code")
    words = _with_nltk_data(_SPLITTER, lambda: nltk.word_tokenize(text))
    return _with_nltk_data(_TAGGER, lambda: nltk.pos_tag(words))


def _without_asides(text: str) -> str:
    """Return the text with its parenthesised asides dropped, however deeply they nest."""
    while _ASIDE.search(text):
        text = _ASIDE.sub("", text)
    return text


def _subject_is_split(tagged: list[tuple[str, str]]) -> bool:
    """Return whether a second noun phrase starts before the sentence's subject reaches its verb."""
    determiners = 0
    previous = ""
    for _word, tag in tagged:
        if tag.startswith(_VERB_PREFIX):
            return determiners >= _SPLIT_SUBJECT_DETERMINERS
        if tag == _DETERMINER and previous not in _BESIDE_THE_SUBJECT:
            determiners += 1
        previous = tag
    return False


def _negates_a_noun_phrase(tagged: list[tuple[str, str]]) -> bool:
    """Return whether the sentence hangs a negation on a noun phrase rather than on the verb it belongs to.

    Reported: `reports no publication date`, `asked about no artefact whose coordinates name a property`.
    Left alone: `does not report a publication date`, `no artefact is asked about`, `there is no new version`.
    """
    seen_verb = False
    for index, (word, tag) in enumerate(tagged):
        if tag.startswith(_VERB_PREFIX):
            seen_verb = True
        elif seen_verb and tag == _DETERMINER and word.lower() == "no" and not _denies_existence(tagged, index):
            return True
    return False


def _denies_existence(tagged: list[tuple[str, str]], index: int) -> bool:
    """Return whether the negation at the index denies that anything exists, as `there is no new version` does."""
    return any(tag == _EXISTENTIAL for _word, tag in tagged[max(0, index - _EXISTENTIAL_REACH) : index])


@dataclass(frozen=True)
class _Limits:
    """What a sentence may reach before this check reports it."""

    complexity: int = 3
    words: int = 50
    density: float = 0.13


def _faults(sentence: str, limits: _Limits) -> str:
    """Return what makes the sentence hard to read, or empty when nothing does."""
    complexity, words = sentence_complexity(sentence), _sentence_words(sentence)
    faults = []
    if complexity > limits.complexity:
        faults.append(f"complexity {complexity}")
    if words > limits.words:
        faults.append(f"{words} words")
    if (density := _sentence_density(complexity, words)) > limits.density:
        faults.append(f"{density:.2f} complexity-density")
    # Both rules read the same tagging, so the tagger runs once a sentence rather than once a rule.
    tagged = _tagged(_without_asides(sentence))
    if _subject_is_split(tagged):
        faults.append("subject split from its verb")
    if _negates_a_noun_phrase(tagged):
        faults.append("negation in a noun phrase (put the negation on the verb, as in 'does not have a release')")
    return " and ".join(faults)


def _sentences(tokenizer: PunktTokenizer, text: str) -> list[str]:
    """Split the text into sentences, keeping each run of inline code whole.

    A period inside a run such as `==3.12.*` otherwise ends a sentence for the splitter, leaving fragments with a
    backtick they never close. Masking each run with filler of its own length keeps the offsets, so the sentences
    can be sliced from the original text.
    """
    masked = _INLINE_CODE.sub(lambda match: "x" * len(match[0]), text)
    return [text[start:end] for start, end in tokenizer.span_tokenize(masked)]


def _sentence_tokenizer() -> PunktTokenizer:
    """Return the sentence splitter."""
    tokenizer = _with_nltk_data(_SPLITTER, PunktTokenizer)
    tokenizer._params.abbrev_types.update(_ABBREVIATIONS)  # noqa: SLF001
    return tokenizer


def _normalized(sentence: str) -> str:
    """Return the sentence with its whitespace collapsed, so rewrapping the prose leaves it the same sentence."""
    return " ".join(sentence.split())


def _whitelisted_sentences(whitelist: Path | None) -> set[str]:
    """Return the sentences the whitelist file holds, which this check passes over."""
    if whitelist is None or not whitelist.exists():
        return set()
    return set(whitelist.read_text().splitlines())


type _Flagged = Iterable[tuple[Prose, str, str]]


def _flagged(paths: list[Path], limits: _Limits) -> _Flagged:
    """Yield each sentence over a limit, with the prose holding it and what makes it hard to read."""
    tokenizer = _sentence_tokenizer()
    for prose in extract_prose(*paths):
        for sentence in _sentences(tokenizer, prose.text):
            if faults := _faults(sentence, limits):
                yield prose, sentence, faults


def main() -> int:
    """Report the sentences that are hard to read, in the files under the paths given or the current directory.

    Passing `--make-whitelist FILE` rewrites FILE to hold the sentences it holds that the prose still has.
    Passing `--whitelist FILE` passes over the sentences FILE holds. Passing `--check-whitelist FILE` does so too, and
    also reports the entries FILE no longer needs, which only a run over every file can tell.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path, default=[Path()])
    parser.add_argument("--install-data", action="store_true")
    whitelist = parser.add_mutually_exclusive_group()
    whitelist.add_argument("--whitelist", type=Path)
    whitelist.add_argument("--check-whitelist", type=Path)
    whitelist.add_argument("--make-whitelist", type=Path)
    arguments = parser.parse_args()
    if arguments.install_data:
        return _install_data()
    flagged = _flagged(arguments.paths, _Limits())
    if arguments.make_whitelist:
        return _write_whitelist(flagged, arguments.make_whitelist)
    if arguments.check_whitelist:
        return _check_whitelist(flagged, arguments.check_whitelist)
    return _report(flagged, arguments.whitelist)


def _install_data() -> int:
    """Fetch the nltk datasets this check reads, by splitting and tagging a sentence of its own.

    The unit tests read prose through the same datasets, and refuse the network, so theirs have to arrive first.
    """
    _sentence_tokenizer()
    _tagged("A sentence.")
    return 0


def _write_whitelist(flagged: _Flagged, whitelist: Path) -> int:
    """Write the flagged sentences the whitelist holds back to the given whitelist file, sorted and one per line.

    A sentence several files hold is written once, so regenerating the file rewrites the lines that changed
    rather than reshuffling all of them. It leaves out a sentence the whitelist does not hold yet, so that sentence
    has to be rewritten to pass the check.
    """
    sentences = {_normalized(sentence) for _prose, sentence, _faults in flagged} & _whitelisted_sentences(whitelist)
    whitelist.write_text("".join(f"{sentence}\n" for sentence in sorted(sentences)))
    return 0


def _check_whitelist(flagged: _Flagged, whitelist: Path) -> int:
    """Report each flagged sentence the whitelist does not hold, then each entry the prose does not hold anymore."""
    sentences = list(flagged)
    stale = _whitelisted_sentences(whitelist) - {_normalized(sentence) for _prose, sentence, _faults in sentences}
    return max(_report(sentences, whitelist), _report_stale(stale, whitelist))


def _report(flagged: _Flagged, whitelist: Path | None) -> int:
    """Report each flagged sentence the whitelist does not hold, and return 1 where any was reported."""
    whitelisted = _whitelisted_sentences(whitelist)
    exit_code = 0
    for prose, sentence, faults in flagged:
        if _normalized(sentence) not in whitelisted:
            sys.stdout.write(f"{prose.location}: {faults}:\n{textwrap.fill(sentence, width=100)}\n\n")
            exit_code = 1
    return exit_code


def _report_stale(stale: set[str], whitelist: Path) -> int:
    """Report the whitelist entries the run did not match, and return 1 where any was reported."""
    for sentence in sorted(stale):
        message = f"{whitelist} holds a sentence the prose no longer has, run `just update-whitelists`:"
        sys.stdout.write(f"{message}\n{sentence}\n\n")
    return 1 if stale else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
