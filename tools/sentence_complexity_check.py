# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "nltk>=3.10.0",
# ]
# ///
"""Report overly complex sentences in the prose of the Python and Markdown files under a directory."""

import ast
import inspect
import io
import re
import sys
import textwrap
import tokenize
from pathlib import Path
from typing import TYPE_CHECKING

import nltk
from nltk.tokenize import PunktTokenizer

if TYPE_CHECKING:
    from collections.abc import Iterator


class Prose:
    """Prose holds prose extracted from files and keeps track of the line where the prose starts."""

    def __init__(self, file_path: Path, text: str, line_number: int) -> None:
        """Keep track of file path, text and line number, making sure text ends with punctuation."""
        self.file_path = file_path
        self.text = text if text[-1] in ".?!" else text + "."
        self.line_number = line_number

    @property
    def location(self) -> str:
        """Return the location of the text."""
        return f"{self.file_path}:{self.line_number}"


def _drop_inline_markup(text: str) -> str:
    """Drop inline Markdown markup (images, links, and inline code) so it is not scored as prose.

    Comments and docstrings are written in the same style as Markdown, so both prose sources need this cleanup.
    """
    text = re.sub(r" ?!\[[^\]]*\]\([^)]*\)", "", text)  # Images.
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # Links, reduced to their link text.
    return re.sub(r" ?`[^`]*`", "", text)  # Inline code.


def _drop_irrelevant_parentheses(text: str) -> str:
    """Drop parentheses that are not real asides: those without word content, and word-attached groups like `(re)writes`."""
    text = re.sub(r" ?\([^\w()]*\)", "", text)  # Parentheses with no word content, e.g. `(/)` left by removed inline code.
    return re.sub(r"(?<=\w)\([^()]*\)|\([^()]*\)(?=\w)", lambda match: match[0].strip("()"), text)


def extract_prose_from_markdown(markdown_file: Path) -> Iterator[Prose]:
    """Extract the prose from the Markdown file with its line number, with the markup reduced away."""
    in_code_block = False
    for line_number, line in enumerate(markdown_file.read_text().splitlines(), start=1):
        if line.startswith("```"):  # A fenced code block delimiter, toggling whether we are inside one.
            in_code_block = not in_code_block
            continue
        if in_code_block or line.startswith("|"):  # Code block content or a table row: not prose.
            continue
        heading = re.match(r"#+ (.+)", line)  # A heading becomes a sentence of its own.
        prose = heading[1] if heading else line
        if text := prose.strip():
            yield Prose(markdown_file, text, line_number)


def _standalone_comments(*comment_tokens: tokenize.TokenInfo | None) -> bool:
    """Return whether the comment tokens are standalone comments."""
    for token in comment_tokens:
        if token is None:
            continue
        if token.line[: token.start[1]].strip() != "":
            return False
    return True


def extract_prose_from_python(python_file: Path) -> Iterator[Prose]:
    """Yield the prose in the Python code with its line number: the text of its comments and strings."""
    source_code = python_file.read_text()
    # Comments come from the token stream, as the parse tree discards them.
    fragments = []
    previous_token = None
    for token in tokenize.generate_tokens(io.StringIO(source_code).readline):
        if token.type == tokenize.COMMENT:
            line_number = token.start[0]
            comment = token.string.lstrip("#")
            if _standalone_comments(token, previous_token) and fragments and fragments[-1][0] + 1 == line_number:
                fragments[-1][1] += comment  # Comment continuation
            else:
                fragments.append([line_number, comment])
        previous_token = token
    # Strings come from the parse tree, which folds implicitly concatenated literals into a single node.
    for node in ast.walk(ast.parse(source_code)):
        if isinstance(node, ast.JoinedStr):  # An f-string: keep its literal parts, drop its interpolations.
            literals = [
                part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ]
            fragments.append([node.lineno, "".join(literals)])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            source = ast.get_source_segment(source_code, node) or ""
            if source.startswith(('"', "'")):  # A quoted string is prose; a raw string (a regexp, say) is not.
                fragments.append([node.lineno, inspect.cleandoc(node.value)])
    for line_number, fragment in sorted(fragments):
        if text := fragment.strip():
            yield Prose(python_file, text, line_number)


def extract_prose(*paths: Path) -> Iterator[Prose]:
    """Extract the prose from the files in the paths."""
    extractors = {"py": extract_prose_from_python, "md": extract_prose_from_markdown}
    for path in paths:
        for file_type, extractor in extractors.items():
            for file_path in sorted(path.rglob(f"*.{file_type}")):
                yield from extractor(file_path)


def sentence_complexity(sentence: str) -> int:
    """Return the sentence complexity: one, plus a cost per aside or clause join that grows with nesting depth."""
    sentence = _drop_irrelevant_parentheses(_drop_inline_markup(sentence))
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


def main(max_complexity: int = 4) -> int:
    """Report too complex sentences in the Python and Markdown files under the starting directory."""
    exit_code = 0
    nltk.data.path.append(".nltk")
    try:
        tokenizer = PunktTokenizer()
    except LookupError:
        nltk.download("punkt_tab", quiet=True, download_dir=".nltk")
        tokenizer = PunktTokenizer()
    paths = [Path(start) for start in sys.argv[1:] or ["."]]
    for prose in extract_prose(*paths):
        for sentence in tokenizer.tokenize(prose.text):
            if (complexity := sentence_complexity(sentence)) > max_complexity:
                wrapped_sentence = textwrap.fill(sentence, width=100)
                sys.stdout.write(f"{prose.location}: complexity {complexity}:\n{wrapped_sentence}\n\n")
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
