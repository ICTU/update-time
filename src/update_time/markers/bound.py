"""Parse a marker bracket item into the version bound it expresses."""

from packaging.specifiers import SpecifierSet

from update_time.domain.bound import UpdateLevel, Verb, VersionBound


def parse_bound(verb: Verb, item: str) -> VersionBound | None:
    """Parse a marker item into a version bound, or None when the item is not a bound.

    An `update` bound whose specifier is unparsable raises `InvalidSpecifier` rather than returning None, so a
    caller can tell a malformed bound (which it should report) from an item that is simply not a bound. A bound
    built here holds exactly one of its two forms, the specifier or the level.
    """
    if (level := next((level for level in UpdateLevel if item == f"{level}-update"), None)) is not None:
        return VersionBound(verb, level=level, item=item)
    if item.startswith("update"):
        return VersionBound(verb, SpecifierSet(item.removeprefix("update")), item=item)
    return None


def directive(verb: Verb, item: str) -> str:
    """Return a directive as the language spells it: a verb and the bracketed item that sets a scope."""
    return f"{verb}[{item}]"


def spell(version_bound: VersionBound) -> str:
    """Return the bound as the directive that expresses it, e.g. `allow[update<3.13]`."""
    return directive(version_bound.verb, version_bound.item)
