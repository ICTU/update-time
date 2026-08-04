"""Report hard-to-read sentences in the prose of the Python and Markdown files under a directory."""

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

if TYPE_CHECKING:
    from collections.abc import Iterator

_INLINE_CODE = re.compile(r"`[^`]*`")

# A printf-style interpolation, such as a log message's `%(location)s`: a value rather than prose.
_INTERPOLATION = re.compile(r"%\(\w+\)[a-z]")

# Abbreviations the splitter would otherwise take for the end of a sentence, spelled without their trailing period.
_ABBREVIATIONS = frozenset({"e.g", "i.e", "etc"})

# Below this many words a ratio says more about a sentence's length than about its density.
_RATIO_WORDS = 15

# Above this many backslashes a string reads as a regular expression rather than as prose.
_REGEXP_BACKSLASHES = 5


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
    in_code_block = False
    for line_number, line in enumerate(markdown_file.read_text().splitlines(), start=1):
        if line.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or line.startswith("|"):  # Code, or a table row: not prose.
            continue
        heading = re.match(r"#+ (.+)", line)  # A heading is a sentence of its own.
        if text := (heading[1] if heading else line).strip():
            yield Prose(markdown_file, text, line_number)


def _is_standalone(comment: tokenize.TokenInfo) -> bool:
    """Return whether the comment has its line to itself."""
    return comment.line[: comment.start[1]].strip() == ""


def _is_regexp(text: str) -> bool:
    """Return whether the text reads as a regular expression rather than as prose."""
    return text.count("\\") > _REGEXP_BACKSLASHES


def _is_code(text: str) -> bool:
    """Return whether the text reads as Python source rather than as prose.

    A lint rule's test cases hold the code they lint as string literals, which parse as Python where prose does not.
    A call is required as well, since a bare word parses as a name and is more likely prose than code.
    """
    try:
        tree = ast.parse(textwrap.dedent(text).strip())
    except SyntaxError:
        return False
    return any(isinstance(node, ast.Call) for node in ast.walk(tree))


@dataclass(order=True)
class _Fragment:
    """A comment or string from a Python file, and the line it starts on.

    Ordered, since the comments and the strings are collected in separate passes and sorted back into file order.
    """

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
    # Strings come from the parse tree, which folds implicitly concatenated literals into one node.
    for node in ast.walk(ast.parse(source_code)):
        if isinstance(node, ast.JoinedStr):  # An f-string: keep the literal parts, drop the interpolations.
            literals = [
                part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ]
            fragments.append(_Fragment(node.lineno, "".join(literals)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            source = ast.get_source_segment(source_code, node) or ""
            if source.startswith(('"', "'")):  # A quoted string is prose; a raw string (a regexp, say) is not.
                fragments.append(_Fragment(node.lineno, inspect.cleandoc(node.value)))
    for fragment in sorted(fragments):
        if (text := _INTERPOLATION.sub("", fragment.text).strip()) and not (
            _is_regexp(fragment.text) or _is_code(fragment.text)
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
    """Yield the prose in the files under the paths, each of which may be a file or a directory.

    `README.md.in` is read as Markdown, so a sentence is reported at the template's line, where editing it survives
    regeneration.
    """
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


def sentence_words(sentence: str) -> int:
    """Return how many words the sentence has, a run of inline code counting as one and a link as its own text."""
    return len(_drop_markup(sentence, "code").split())


def sentence_density(complexity: int, words: int) -> float:
    """Return the asides and clause joins per word, or zero for a sentence too short to read a ratio off.

    Complexity starts at one, so it is the count above that which the ratio measures.
    """
    return (complexity - 1) / words if words >= _RATIO_WORDS else 0.0


def _faults(sentence: str, max_complexity: int, max_words: int, max_density: float) -> str:
    """Return what makes the sentence hard to read, or empty when nothing does."""
    complexity, words = sentence_complexity(sentence), sentence_words(sentence)
    faults = []
    if complexity > max_complexity:
        faults.append(f"complexity {complexity}")
    if words > max_words:
        faults.append(f"{words} words")
    if (density := sentence_density(complexity, words)) > max_density:
        faults.append(f"{density:.2f} complexity-density")
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
    """Return the sentence splitter, taught the abbreviations that would otherwise end a sentence for it."""
    nltk.data.path.append(".nltk")
    try:
        tokenizer = PunktTokenizer()
    except LookupError:
        nltk.download("punkt_tab", quiet=True, download_dir=".nltk")
        tokenizer = PunktTokenizer()
    tokenizer._params.abbrev_types.update(_ABBREVIATIONS)  # noqa: SLF001
    return tokenizer


def main(max_complexity: int = 3, max_words: int = 50, max_density: float = 0.13) -> int:
    """Report the sentences over any limit, in the files under the paths given or the current directory."""
    exit_code = 0
    tokenizer = _sentence_tokenizer()
    paths = [Path(start) for start in sys.argv[1:] or ["."]]
    for prose in extract_prose(*paths):
        for sentence in _sentences(tokenizer, prose.text):
            if faults := _faults(sentence, max_complexity, max_words, max_density):
                sys.stdout.write(f"{prose.location}: {faults}:\n{textwrap.fill(sentence, width=100)}\n\n")
                exit_code = 1
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
