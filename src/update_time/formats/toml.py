"""Read, parse, and edit TOML files."""

import tomllib
from typing import TYPE_CHECKING, cast

import tomlkit
import tomlkit.container
import tomlkit.exceptions
import tomlkit.items

if TYPE_CHECKING:
    from pathlib import Path


# What this format is called in a message about a file that does not parse.
FORMAT = "TOML"


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


def set_nested_value(document: tomlkit.TOMLDocument, *keys: str, value: str, comment: str = "") -> bool:
    """Set `[<super table>.<table>] <key>` to the value, with an optional comment trailing it, adding the tables.

    An inline table cannot hold a comment, so either table written inline becomes a table. Return False, changing
    nothing, where another kind of value stands in for one of the tables.
    """
    super_table, table, key = keys
    _expand_inline_tables(document, super_table, table)
    parent = document.setdefault(super_table, tomlkit.table(is_super_table=True))
    if not _holds_commented_keys(parent) or (table in parent and not _holds_commented_keys(parent[table])):
        return False
    item = tomlkit.item(value)
    if comment:
        item.comment(comment)
    if table in parent:
        parent[table][key] = item
        return True
    new_table = tomlkit.table()
    new_table[key] = item
    _add_table(document, super_table, table, new_table)
    return True


def _expand_inline_tables(document: tomlkit.TOMLDocument, super_table: str, table: str) -> None:
    """Turn the super table, and the table in it, into tables wherever the document writes them inline."""
    parent = document.get(super_table)
    if isinstance(parent, tomlkit.items.InlineTable):
        _remove_root_key(document, super_table)
        document.append(super_table, _table_from(parent, expanding=table))
    elif isinstance(parent, tomlkit.items.Table) and isinstance(child := parent.get(table), tomlkit.items.InlineTable):
        del parent[table]
        _add_table(document, super_table, table, _table_from(child))


def _table_from(inline: tomlkit.items.InlineTable, expanding: str = "") -> tomlkit.items.Table:
    """Return a table holding the inline table's items, with the item named `expanding` turned into a table too."""
    items = [
        (key.key, _table_from(item) if key.key == expanding and isinstance(item, tomlkit.items.InlineTable) else item)
        for key, item in inline.value.body
        if key is not None
    ]
    table = tomlkit.table(is_super_table=all(isinstance(item, tomlkit.items.Table) for _, item in items))
    for key, item in items:
        if not isinstance(item, tomlkit.items.Table):
            item.trivia.trail = "\n"
        table.append(key, item)
    return table


def _remove_root_key(document: tomlkit.TOMLDocument, key: str) -> None:
    """Remove the key from the document, and the blank lines after it when it is the document's first key."""
    body = document.body
    index = next(index for index, (name, _) in enumerate(body) if name is not None and name.key == key)
    del document[key]
    if any(name is not None for name, _ in body[:index]):
        return
    while (
        index + 1 < len(body)
        and body[index + 1][0] is None
        and isinstance(body[index + 1][1], tomlkit.items.Whitespace)
    ):
        del body[index + 1]


def _holds_commented_keys(item: object) -> bool:
    """Return whether the item is a table that can hold a commented key, rather than an inline table or a value."""
    return isinstance(item, tomlkit.items.Table | tomlkit.container.OutOfOrderTableProxy)


def _add_table(document: tomlkit.TOMLDocument, super_table: str, name: str, table: tomlkit.items.Table) -> None:
    """Add the table after the last table of the first part of the super table, and a blank line after it if needed.

    The blank lines and comments ending the table before it lead into what follows, so they move after the new table.
    """
    index = next(index for index, (key, _) in enumerate(document.body) if key is not None and key.key == super_table)
    part = cast("tomlkit.items.Table", document.body[index][1])
    decorations = _pop_trailing_decorations(_last_table(part))
    following: list[tomlkit.items.Item] = [*decorations, *(item for _, item in document.body[index + 1 : index + 2])]
    if following and not _starts_with_blank_line(following):
        table.add(tomlkit.nl())
    for decoration in decorations:
        table.add(decoration)
    part.append(name, table)


def _last_table(table: tomlkit.items.Table) -> tomlkit.items.Table:
    """Return the table rendered last within the table: the table itself, or its last sub-table, nested or not."""
    while keyed := [item for key, item in table.value.body if key is not None]:
        last = keyed[-1]
        if isinstance(last, tomlkit.items.AoT):  # AoT = Array of Tables
            last = last.body[-1]
        if not isinstance(last, tomlkit.items.Table):
            break
        table = last
    return table


def _starts_with_blank_line(items: list[tomlkit.items.Item]) -> bool:
    """Return whether the first of the items is a blank line."""
    return isinstance(items[0], tomlkit.items.Whitespace) and "\n" in items[0].s


def _pop_trailing_decorations(table: tomlkit.items.Table) -> list[tomlkit.items.Whitespace | tomlkit.items.Comment]:
    """Remove the blank lines and comments ending the table's body, and return them in order."""
    body = table.value.body
    decorations: list[tomlkit.items.Whitespace | tomlkit.items.Comment] = []
    while (
        body
        and body[-1][0] is None
        and isinstance(item := body[-1][1], tomlkit.items.Whitespace | tomlkit.items.Comment)
    ):
        body.pop()
        decorations.insert(0, item)
    return decorations


def string(text: str, *, quoted_as: tomlkit.items.String) -> tomlkit.items.String:
    """Return the text as a TOML string, quoted the way the given string is quoted."""
    return tomlkit.string(text, literal=quoted_as.as_string().startswith("'"))
