"""Read the YAML files holding a workspace's catalogs: the entries of the catalogs, and the markers beside them."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, override

from update_time.domain.reference import Reference
from update_time.formats.yaml import parse
from update_time.io.file import File
from update_time.markers.parser import parse_marker
from update_time.primitives.line import located_lines
from update_time.primitives.location import Location

from .npm_declaration import CatalogEntry, Catalogs, catalog_name, declaration

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from update_time.formats.yaml import Document, KeyedValue
    from update_time.primitives.line import Line

    from .npm_declaration import Declaration


@dataclass(frozen=True)
class CatalogsYaml(File):
    """A YAML file holding a workspace's catalogs, such as a pnpm-workspace.yaml: its lines, and their document."""

    path: Path
    lines: list[Line]
    document: Document

    @classmethod
    @override
    def read(cls, path: Path) -> Self:
        """Return the file at the path, raising a `ValueError` where it is not valid YAML."""
        text = path.read_text()
        # Why a file that does not parse cannot be read.
        if (document := parse(text)) is None:
            msg = "it is not valid YAML"
            raise ValueError(msg)
        return cls(path, located_lines(path, text.splitlines()), document)

    def declarations(self) -> Iterator[Declaration]:
        """Yield a declaration for each entry of the catalogs, steered by the marker on its line or the line above."""
        for _catalog, catalog in self._catalogs():
            for entry in catalog.keyed_values():
                marker = parse_marker(self.lines[entry.line_number - 1])
                yield declaration(entry.key, entry.text, self._location(entry), marker)

    def _catalogs(self) -> Iterator[tuple[str, Document]]:
        """Yield the name of each catalog, empty for the unnamed one, and the catalog."""
        yield "", self.document["catalog"]
        yield from ((catalog_name(named.key), named.value) for named in self.document["catalogs"].keyed_values())

    def _location(self, entry: KeyedValue) -> Location:
        """Return where the entry's key sits."""
        return Location(self.path, entry.line_number, entry.column)

    def catalogs(self) -> Catalogs:
        """Return the catalogs the file holds."""
        return Catalogs(
            {
                CatalogEntry(name, entry.key): Reference(entry.key, entry.text, self._location(entry))
                for name, catalog in self._catalogs()
                for entry in catalog.keyed_values()
                if entry.text is not None
            }
        )
