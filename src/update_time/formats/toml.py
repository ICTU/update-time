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
    except tomlkit.exceptions.TOMLKitError:
        return None


def read_document(path: Path) -> tomlkit.TOMLDocument:
    """Return the file's TOML parsed into a document that preserves the layout."""
    return tomlkit.parse(path.read_text())


def dumps(document: tomlkit.TOMLDocument) -> str:
    """Return the document as the TOML text it stands for."""
    return tomlkit.dumps(document)


def is_string(value: object) -> bool:
    """Return whether the value is a TOML string rather than another kind of item."""
    return isinstance(value, tomlkit.items.String)


def nested_value(document: tomlkit.TOMLDocument, *keys: str) -> tuple[str, str] | None:
    """Return the value the nested keys hold and the comment trailing it, or None when they hold nothing."""
    item: object = document
    for key in keys:
        item = item.get(key) if isinstance(item, dict) else None
    if not isinstance(item, tomlkit.items.Item):
        return None
    return str(item), item.trivia.comment


def set_nested_value(document: tomlkit.TOMLDocument, *keys: str, value: str, comment: str = "") -> None:
    """Set the value the nested keys hold, with an optional comment trailing it, adding the tables it needs."""
    *tables, key = keys
    table = document
    for name in tables[:-1]:
        table = table.setdefault(name, tomlkit.table(is_super_table=True))
    if tables and tables[-1] not in table:
        table[tables[-1]] = tomlkit.table()
    table = table[tables[-1]] if tables else table
    item = tomlkit.item(value)
    if comment:
        item.comment(comment)
    table[key] = item


def string(text: str, *, quoted_as: tomlkit.items.String) -> tomlkit.items.String:
    """Return the text as a TOML string, quoted the way the given string is quoted."""
    return tomlkit.string(text, literal=quoted_as.as_string().startswith("'"))
