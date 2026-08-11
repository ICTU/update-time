"""Locating and rewriting the text a regular expression matched."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import re


def line_number(text: str, offset: int) -> int:
    """Return the 1-based number of the line the offset falls on."""
    return text.count("\n", 0, offset) + 1


def rewrite_match(match: re.Match[str], replacements: dict[str, str]) -> str:
    """Return the matched text with the named groups replaced, leaving the rest of the match untouched.

    Only the spans the regex captured are rewritten, so the same value occurring elsewhere within the match is left
    alone. Groups are replaced right-to-left, so an earlier replacement doesn't shift the spans still to come.
    """
    text = match.group(0)
    offset = match.start()
    for group in sorted(replacements, key=match.start, reverse=True):
        start, end = match.span(group)
        text = text[: start - offset] + replacements[group] + text[end - offset :]
    return text


def replace_match(match: re.Match[str], replacement: str) -> str:
    """Return the whole string the match came from, with the matched text replaced by `replacement`."""
    string = match.string
    return string[: match.start()] + replacement + string[match.end() :]


def rewrite_string(match: re.Match[str], replacements: dict[str, str]) -> str:
    """Return the whole string the match came from, with the match's named groups replaced."""
    return replace_match(match, rewrite_match(match, replacements))
