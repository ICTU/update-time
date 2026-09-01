"""Read and parse TOML files."""

import tomllib
from typing import TYPE_CHECKING

import tomlkit
import tomlkit.exceptions
import tomlkit.items

if TYPE_CHECKING:
    from pathlib import Path


def parse(text: str) -> dict | None:
    """Return the TOML parsed into a dict, or None when the text isn't valid TOML."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def read(path: Path) -> dict | None:
    """Return the parsed TOML, or None when the file can't be read or isn't valid TOML."""
    try:
        text = path.read_text()
    except OSError:
        return None
    return parse(text)


def parse_document(text: str) -> tomlkit.TOMLDocument | None:
    """Return the TOML parsed into a document that preserves the layout, or None when the text isn't valid TOML."""
    try:
        return tomlkit.parse(text)
    except tomlkit.exceptions.ParseError:
        return None


def string(text: str, *, quoted_as: tomlkit.items.String) -> tomlkit.items.String:
    """Return the text as a TOML string, quoted the way the given string is quoted."""
    return tomlkit.string(text, literal=quoted_as.as_string().startswith("'"))
