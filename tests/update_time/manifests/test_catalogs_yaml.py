"""Unit tests for reading a YAML file holding a workspace's catalogs, with file I/O mocked."""

import unittest

from mutation_time.mutation import Mutation, kills
from update_time.domain.reference import Reference
from update_time.manifests import catalogs_yaml
from update_time.manifests.catalogs_yaml import CatalogsYaml
from update_time.manifests.npm_declaration import CatalogEntry, Catalogs
from update_time.primitives.location import Location

from tests.helpers import mock_path


class DeclarationsTest(unittest.TestCase):
    """Unit tests for reading a declaration for each entry of the catalogs."""

    def test_a_declaration_per_catalog_entry(self):
        """Test that each entry of a catalog, named or not, is declared at its line, steered by its marker."""
        path = mock_path(
            "packages:\n"
            "  - packages/*\n"
            "catalog:\n"
            "  semver: ^7.0.0  # update-time: set[stale-after=0]\n"
            "  left-pad: 1.3.0\n"
            "  is-odd: 3\n"
            "catalogs:\n"
            "  testing:\n"
            "    # update-time: ignore[yanked]\n"
            "    is-number: 6.0.0\n"
        )
        declarations = list(CatalogsYaml.read(path).declarations())
        self.assertEqual(
            [(each.dependency, each.current_version, each.location, each.marker.raw) for each in declarations],
            [
                ("semver", "", Location(path, 4, 2), "set[stale-after=0]"),
                ("left-pad", "1.3.0", Location(path, 5, 2), ""),
                ("is-odd", "", Location(path, 6, 2), ""),
                ("is-number", "6.0.0", Location(path, 10, 4), "ignore[yanked]"),
            ],
        )
        self.assertTrue(all(declaration.names_a_release for declaration in declarations))


class CatalogsTest(unittest.TestCase):
    """Unit tests for reading the catalogs of a pnpm-workspace.yaml."""

    @kills(
        Mutation(
            catalogs_yaml.CatalogsYaml._catalogs,
            "(catalog_name(named.key), named.value)",
            "(named.key, named.value)",
            "a version pnpm moved for a `catalog:` dependency is reported at the member, not at the default catalog",
        )
    )
    def test_the_named_catalog_default_is_the_unnamed_one(self):
        """Test that the entries of a named catalog called default are located as the unnamed catalog's."""
        path = mock_path("catalogs:\n  default:\n    left-pad: 1.3.0\n")
        expected = Catalogs({CatalogEntry("", "left-pad"): Reference("left-pad", "1.3.0", Location(path, 3, 4))})
        self.assertEqual(CatalogsYaml.read(path).catalogs(), expected)

    @kills(
        Mutation(
            catalogs_yaml.CatalogsYaml.catalogs,
            "                if entry.text is not None\n",
            "",
            "a pnpm catalog entry whose value is a mapping is taken for an entry without a range",
        )
    )
    def test_an_entry_whose_value_is_not_a_scalar_is_left_out(self):
        """Test that a catalog entry whose value is a mapping is left out, and the entry beside it is read."""
        path = mock_path("catalog:\n  react:\n    version: 1\n  left-pad: 1.3.0\n")
        self.assertEqual(list(CatalogsYaml.read(path).catalogs().entries), [CatalogEntry("", "left-pad")])
