"""Read an XML file as a tree of elements, each knowing where in the file it sits.

Note: `from xml.parsers import expat` below resolves to the standard library's parser, not to this module — imports
are absolute.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from xml.parsers import expat  # nosec B405: the files parsed here are the user's own

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# What this format is called in a message about a file that does not parse.
FORMAT = "XML"


@dataclass(frozen=True)
class XmlElement:
    """An element of an XML document, and where in the file it starts.

    The tag leaves out the namespace prefix, so an element reads the same however the document spells its namespace.
    """

    tag: str
    text: str
    line: int  # 1-based, as expat counts lines
    column: int  # 0-based, as `Location` counts columns
    children: tuple[XmlElement, ...]

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


def read(path: Path) -> XmlElement | None:
    """Return the root element of the XML file at the path, or None when it cannot be read or does not parse."""
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

    parser.StartElementHandler = start
    parser.CharacterDataHandler = data
    parser.EndElementHandler = end
    try:
        parser.Parse(path.read_bytes(), True)  # noqa: FBT003 # `isfinal` is positional: pyexpat takes no keyword
    except OSError, expat.ExpatError:
        return None
    return roots[0]
