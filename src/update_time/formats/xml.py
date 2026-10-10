"""Read an XML file as a tree of elements, each knowing where in the file it sits, and as the lines it holds.

Note: `from xml.parsers import expat` below resolves to the standard library's parser, not to this module — imports
are absolute.
"""

import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from xml.parsers import expat  # nosec B405: the files parsed here are the user's own

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# What this format is called in a message about a file that does not parse.
FORMAT = "XML"

# Expat numbers an element's line by the line breaks XML counts: `\r\n`, `\r`, and `\n`. `str.splitlines` also
# breaks a line at characters XML allows inside text, such as U+2028, so it would number the lines differently.
_LINE_BREAK = re.compile(r"\r\n?|\n")


@dataclass(frozen=True)
class XmlElement:
    """An element of an XML document, and where in the file it starts.

    The tag leaves out the namespace prefix, so an element reads the same however the document spells its namespace.
    The comment is the one following the element, before the element's next sibling starts.
    """

    tag: str
    text: str
    line: int  # 1-based, as expat counts lines
    column: int  # 0-based, as `Location` counts columns
    children: tuple[XmlElement, ...]
    comment: str = ""

    def child(self, tag: str) -> XmlElement | None:
        """Return the first child carrying the tag, or None where this element holds no such child."""
        return next((child for child in self.children if child.tag == tag), None)

    def descendants(self, tag: str) -> Iterator[XmlElement]:
        """Yield each element carrying the tag, at whatever depth below this one it sits."""
        for child in self.children:
            if child.tag == tag:
                yield child
            yield from child.descendants(tag)


@dataclass
class _OpenElement:
    """An element the parser has started but not yet closed, and what it has reported for it so far."""

    tag: str
    line: int
    column: int
    text: list[str] = field(default_factory=list)
    children: list[XmlElement] = field(default_factory=list)

    def close(self) -> XmlElement:
        """Return the element, now that the parser has reported all of its text and children."""
        return XmlElement(self.tag, "".join(self.text).strip(), self.line, self.column, tuple(self.children))


@dataclass(frozen=True)
class _XmlDocument:
    """An XML document's root element, and its lines, numbered as its elements' lines are."""

    root: XmlElement
    lines: list[str]


def read(path: Path) -> XmlElement | None:
    """Return the root element of the XML file at the path, or None when it cannot be read or does not parse."""
    document = read_document(path)
    return None if document is None else document.root


def read_document(path: Path) -> _XmlDocument | None:
    """Return the XML file at the path, or None when it cannot be read or does not parse."""
    try:
        content = path.read_bytes()
    except OSError:
        return None
    root = parse(content)
    if root is None:
        return None
    # The lines are decoded as UTF-8, whatever encoding the XML declares. A character that does not decode is
    # replaced rather than ending the run.
    return _XmlDocument(root, _LINE_BREAK.split(content.decode(errors="replace")))


def parse(document: bytes) -> XmlElement | None:
    """Return the root element of the XML document, or None when it does not parse."""
    open_elements: list[_OpenElement] = []
    roots: list[XmlElement] = []
    parser = expat.ParserCreate()

    def start(tag: str, _attributes: dict[str, str]) -> None:
        name = tag.rpartition(":")[2]
        open_elements.append(_OpenElement(name, parser.CurrentLineNumber, parser.CurrentColumnNumber))

    def data(text: str) -> None:
        open_elements[-1].text.append(text)

    def end(_tag: str) -> None:
        element = open_elements.pop().close()
        (open_elements[-1].children if open_elements else roots).append(element)

    def comment(text: str) -> None:
        siblings = open_elements[-1].children if open_elements else roots
        if siblings:
            siblings[-1] = replace(siblings[-1], comment=text.strip())

    parser.StartElementHandler = start
    parser.CharacterDataHandler = data
    parser.EndElementHandler = end
    parser.CommentHandler = comment
    try:
        parser.Parse(document, True)  # noqa: FBT003 # `isfinal` is positional: pyexpat takes no keyword
    except expat.ExpatError, LookupError:  # A `LookupError` names an encoding the document declares but Python lacks.
        return None
    return roots[0]
