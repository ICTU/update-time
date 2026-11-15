"""Read how npm, pnpm, yarn, and bun declare a dependency, and the catalogs a workspace takes its versions from."""

import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, NamedTuple, TypeGuard

from update_time.domain.reference import Reference

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyName
    from update_time.domain.marker import Marker
    from update_time.primitives.location import Location


class CatalogEntry(NamedTuple):
    """An entry of a workspace's catalogs: the name of its catalog, empty for the unnamed one, and of its dependency."""

    catalog: str
    dependency: DependencyName


# A spec that pins a dependency to one version, such as 1.3.0, =v1.3.0, or 1.3.0+build. npm ignores the build.
_EXACT_VERSION = re.compile(r"=?v?(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)(?:\+[0-9A-Za-z.-]+)?")

# The protocol of a spec that takes its version from a catalog, followed by the catalog's name, if it has one.
_CATALOG_PROTOCOL = "catalog:"

# A spec that aliases a registry package, naming the package and the range it takes, as in `npm:react-is@^18.0.0`.
_ALIAS = re.compile(r"npm:(?P<package>@?[^@]+)(?:@(?P<range>.*))?")

# The name that pnpm and bun give the unnamed catalog, as in `catalog:default`.
_DEFAULT_CATALOG = "default"


@dataclass(frozen=True, kw_only=True)
class Declaration(Reference):
    """A dependency declared in a section or a catalog, and whether its spec names a release on the npm registry.

    Where the spec takes its version from a catalog, as `catalog:testing` does, `catalog` holds the catalog's name,
    empty for the unnamed catalog. Where the spec aliases a registry package, as `npm:react-is@^18.0.0` does, the
    declaration names that package, and `alias` holds the name the section declares it under.
    """

    names_a_release: bool
    catalog: str | None = None
    alias: DependencyName | None = None

    @property
    def declared_name(self) -> DependencyName:
        """Return the name the section declares the dependency under, which a lock lists it under as well."""
        return self.alias or self.dependency


def _catalog(spec: object) -> str | None:
    """Return the name of the catalog the spec takes its version from, empty for the unnamed one, or else None."""
    if isinstance(spec, str) and spec.startswith(_CATALOG_PROTOCOL):
        return catalog_name(spec.removeprefix(_CATALOG_PROTOCOL))
    return None


def catalog_name(name: str) -> str:
    """Return the name of the catalog that a spec or a file gives it, empty for the unnamed catalog."""
    return "" if name == _DEFAULT_CATALOG else name


def declaration(name: DependencyName, spec: object, location: Location, marker: Marker) -> Declaration:
    """Return the declaration of the dependency with the spec, at the exact version the spec pins if any."""
    if isinstance(spec, str) and (aliased := _ALIAS.fullmatch(spec)):
        return replace(declaration(aliased["package"], aliased["range"] or "", location, marker), alias=name)
    if not is_registry_spec(spec):
        return Declaration(name, "", location, marker=marker, names_a_release=False, catalog=_catalog(spec))
    match = _EXACT_VERSION.fullmatch(spec)
    return Declaration(name, match["version"] if match else "", location, marker=marker, names_a_release=True)


@dataclass(frozen=True)
class Catalogs:
    """The catalogs that the projects of a workspace can take their versions from: the spec of each entry."""

    entries: dict[CatalogEntry, Reference] = field(default_factory=dict)

    def ranges(self) -> dict[CatalogEntry, Reference]:
        """Return a reference to the range each entry declares for a registry dependency."""
        return {
            entry: reference for entry, reference in self.entries.items() if is_registry_spec(reference.current_version)
        }

    def location_of(self, declaration: Declaration) -> Location | None:
        """Return the location of the entry the declaration takes its version from, or None where it takes none."""
        if declaration.catalog is None:
            return None
        entry = self.entries.get(CatalogEntry(declaration.catalog, declaration.dependency))
        return None if entry is None else entry.location


def is_registry_spec(spec: object) -> TypeGuard[str]:
    """Return whether the spec is a plain semver range that resolves to an npm registry release.

    A catalog, git, file, link, workspace, alias, or github-shorthand reference does not, and is recognisable by the
    `:` or `/` it carries.
    """
    return isinstance(spec, str) and ":" not in spec and "/" not in spec
