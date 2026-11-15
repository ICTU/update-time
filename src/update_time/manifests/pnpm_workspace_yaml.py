"""Read pnpm-workspace.yaml files: the entries of their catalogs, and the markers beside them."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from update_time.domain.reference import Reference
from update_time.formats import yaml
from update_time.io.file import File
from update_time.manifests.npm_declaration import CatalogEntry, Catalogs, catalog_name, declaration
from update_time.markers.parser import parse_marker
from update_time.primitives.line import located_lines
from update_time.primitives.location import Location

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from update_time.manifests.npm_declaration import Declaration
    from update_time.primitives.line import Line


# Why a pnpm-workspace.yaml that does not parse cannot be read.
_NOT_VALID_YAML = "it is not valid YAML"


@dataclass(frozen=True)
class PnpmWorkspaceYaml(File):
    """A pnpm-workspace.yaml: its lines, and the YAML document they hold."""

    path: Path
    lines: list[Line]
    document: yaml.Document

    @classmethod
    def read(cls, path: Path) -> Self:
        """Return the pnpm-workspace.yaml at the path, raising a `ValueError` where it is not valid YAML."""
        text = path.read_text()
        if (document := yaml.parse(text)) is None:
            raise ValueError(_NOT_VALID_YAML)
        return cls(path, located_lines(path, text.splitlines()), document)

    def declarations(self) -> Iterator[Declaration]:
        """Yield a declaration for each entry of the catalogs, steered by the marker on its line or the line above."""
        for _catalog, catalog in self._catalogs():
            for entry in catalog.keyed_values():
                marker = parse_marker(self.lines[entry.line_number - 1])
                yield declaration(entry.key, entry.text, self._location(entry), marker)

    def catalogs(self) -> Catalogs:
        """Return the catalogs the pnpm-workspace.yaml holds."""
        return Catalogs(
            {
                CatalogEntry(name, entry.key): Reference(entry.key, entry.text, self._location(entry))
                for name, catalog in self._catalogs()
                for entry in catalog.keyed_values()
                if entry.text is not None
            }
        )

    def _catalogs(self) -> Iterator[tuple[str, yaml.Document]]:
        """Yield the name of each catalog, empty for the unnamed one, and the catalog."""
        yield "", self.document["catalog"]
        yield from ((catalog_name(named.key), named.value) for named in self.document["catalogs"].keyed_values())

    def _location(self, entry: yaml.KeyedValue) -> Location:
        """Return where the entry's key sits."""
        return Location(self.path, entry.line_number, entry.column)
