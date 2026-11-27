"""Unit tests for the package.json file format, with file I/O mocked."""

import json
import unittest
from typing import TYPE_CHECKING

from mutation_time.mutation import kills
from update_time.domain.reference import Reference
from update_time.manifests.npm_declaration import CatalogEntry, Catalogs, Declaration, _catalog
from update_time.manifests.package_json import PackageJson
from update_time.primitives.location import Location

from tests.file_mocks import mock_path

if TYPE_CHECKING:
    from unittest.mock import Mock


class ReadTest(unittest.TestCase):
    """Unit tests for reading a package.json."""

    def test_read(self):
        """Test that a package.json is read as its text and the contents parsed from that text."""
        contents = '{"name": "x", "version": "1.0"}'
        path = mock_path(contents)
        self.assertEqual(PackageJson.read(path), PackageJson(path, contents, {"name": "x", "version": "1.0"}))


class NodeEngineTest(unittest.TestCase):
    """Unit tests for where a package.json declares its Node engine."""

    def test_the_entry_the_section_declares(self):
        """Test that the entry is located at the line and the column the section declares it on."""
        path = mock_path('{\n  "engines": {\n    "node": "18"\n  }\n}\n')
        self.assertEqual(self.location(path), Location(path, 3, column=4))

    @staticmethod
    def location(path: Mock) -> Location:
        """Return where the package.json declares its Node engine."""
        return PackageJson.read(path).node_engine().location

    def test_a_file_declaring_no_such_entry(self):
        """Test that an entry the file does not declare is located at the file, rather than at a guessed line."""
        path = mock_path('{"engines": {"npm": ">=10"}}')
        self.assertEqual(self.location(path), Location(path))


class DependencyLocationsTest(unittest.TestCase):
    """Unit tests for reading the direct registry dependencies and where each is declared."""

    def test_every_entry_across_the_sections(self):
        """Test that every entry in the dependency sections is returned, in order, each at its own line."""
        path = mock_path(
            "{\n"
            '  "dependencies": {\n    "react": "^18.0.0",\n    "left-pad": "1.3.0"\n  },\n'
            '  "devDependencies": {\n    "typescript": "~5.4.0",\n    "react": "^18.0.0"\n  },\n'
            '  "optionalDependencies": {\n    "fsevents": "2.3.3"\n  },\n'
            '  "peerDependencies": {\n    "webpack": "^5.0.0"\n  }\n'  # npm installs none of these: excluded
            "}\n"
        )
        self.assertEqual(
            self.locations(path),
            {
                "react": [Location(path, 3, 4), Location(path, 8, 4)],  # declared twice, so it keeps both lines
                "left-pad": [Location(path, 4, 4)],
                "typescript": [Location(path, 7, 4)],
                "fsevents": [Location(path, 11, 4)],
            },
        )

    @staticmethod
    def locations(path: Mock) -> dict[str, list[Location]]:
        """Return each direct registry dependency of the package.json and where its entries sit."""
        references = PackageJson.read(path).dependency_references(Catalogs())
        return {name: [reference.location for reference in each] for name, each in references.items()}

    def test_the_line_each_dependency_is_declared_on(self):
        """Test that each dependency is located at the line its entry sits on."""
        path = mock_path('{\n  "dependencies": {\n    "react": "^18.0.0",\n    "left-pad": "1.3.0"\n  }\n}\n')
        self.assertEqual(self.locations(path), {"react": [Location(path, 3, 4)], "left-pad": [Location(path, 4, 4)]})

    def test_a_name_declared_in_a_section_that_is_not_installed_from(self):
        """Test that a dependency is located in the section declaring it, not at a key of the same name elsewhere."""
        path = mock_path(
            "{\n"
            '  "peerDependencies": {\n    "react": "^18.0.0"\n  },\n'
            '  "overrides": {\n    "left-pad": "1.3.0"\n  },\n'
            '  "dependencies": {\n    "react": "^18.0.0",\n    "left-pad": "^1.0.0"\n  }\n'
            "}\n"
        )
        self.assertEqual(self.locations(path), {"react": [Location(path, 9, 4)], "left-pad": [Location(path, 10, 4)]})

    def test_non_registry_specs_skipped(self):
        """Test that git, file, workspace and github-shorthand specs are skipped (only registry ranges kept)."""
        contents = json.dumps(
            {
                "dependencies": {
                    "clipboard": "^2.0.11",  # registry range: kept
                    "bats": "git+https://github.com/calj/bats.git",  # git: has ':'
                    "local": "file:../local",  # file: has ':'
                    "shared": "workspace:*",  # workspace: has ':'
                    "forked": "github:org/repo",  # github shorthand: has ':' and '/'
                }
            }
        )
        self.assertEqual(list(self.locations(mock_path(contents))), ["clipboard"])

    def test_no_dependencies(self):
        """Test that a package.json without dependency sections yields nothing."""
        self.assertEqual(self.locations(mock_path('{"name": "x"}')), {})


class CatalogsTest(unittest.TestCase):
    """Unit tests for reading the catalogs of a package.json."""

    def test_the_catalogs_of_the_workspaces_hide_the_top_level_ones(self):
        """Test that where the workspaces hold a catalog, named or not, the top-level catalogs are not read."""
        cases = {
            "a catalog in the workspaces": ({"catalog": {"left-pad": "1.3.0"}}, ""),
            "a named catalog in the workspaces": ({"catalogs": {"testing": {"left-pad": "1.3.0"}}}, "testing"),
        }
        for case, (workspaces, name) in cases.items():
            with self.subTest(case):
                contents = {"workspaces": workspaces, "catalog": {"is-number": "6.0.0"}}
                read = PackageJson.read(mock_path(json.dumps(contents)))
                self.assertEqual(list(read.catalogs().entries), [CatalogEntry(name, "left-pad")])
                self.assertEqual([declaration.dependency for declaration in read.declarations()], ["left-pad"])

    @kills.replace(
        PackageJson._catalogs,
        "(catalog_name(name)",
        "(str(name)",
        "a version bun moved for a `catalog:` dependency is reported at the member, not at the default catalog",
    )
    def test_the_named_catalog_default_is_the_unnamed_one(self):
        """Test that the entries of a named catalog called default are located as the unnamed catalog's."""
        path = mock_path(json.dumps({"catalogs": {"default": {"left-pad": "1.3.0"}}}, indent=2))
        self.assertEqual(
            PackageJson.read(path).catalogs(),
            Catalogs({CatalogEntry("", "left-pad"): Reference("left-pad", "1.3.0", Location(path, 4, 6))}),
        )


class DeclaredRangesTest(unittest.TestCase):
    """Unit tests for reading the range each registry dependency declares."""

    def test_a_range_per_section_declaring_the_name(self):
        """Test that a name two sections declare has a range per section, each at its own line."""
        path = mock_path(
            "{\n"
            '  "dependencies": {\n    "react": "^18.0.0"\n  },\n'
            '  "devDependencies": {\n    "react": "~18.2.0"\n  }\n'
            "}\n"
        )
        self.assertEqual(
            self.ranges(path),
            {
                (("dependencies",), "react"): Reference("react", "^18.0.0", Location(path, 3, 4)),
                (("devDependencies",), "react"): Reference("react", "~18.2.0", Location(path, 6, 4)),
            },
        )

    @staticmethod
    def ranges(path: Mock) -> dict[tuple[tuple[str, ...], str], Reference]:
        """Return a reference to the range each registry dependency of the package.json declares."""
        return PackageJson.read(path).declared_ranges()

    def test_non_registry_specs_skipped(self):
        """Test that a git URL and the github shorthand are skipped, since neither names a registry release."""
        specs = {"clipboard": "^2.0.11", "bats": "git+https://github.com/calj/bats.git", "forked": "github:org/repo"}
        self.assertEqual(
            list(self.ranges(mock_path(json.dumps({"dependencies": specs})))), [(("dependencies",), "clipboard")]
        )


class DeclarationsTest(unittest.TestCase):
    """Unit tests for reading a declaration for each entry of the dependency sections and the catalogs."""

    def test_a_declaration_per_entry(self):
        """Test that each entry yields a declaration at its own line, a name declared twice included."""
        path = mock_path(
            "{\n"
            '  "dependencies": {\n    "react": "18.2.0",\n    "left-pad": "^1.3.0"\n  },\n'
            '  "devDependencies": {\n    "react": "^18.0.0"\n  },\n'
            '  "optionalDependencies": {\n    "fsevents": "2.3.3"\n  }\n'
            "}\n"
        )
        self.assertEqual(
            list(PackageJson.read(path).declarations()),
            [
                Declaration("react", "18.2.0", Location(path, 3, 4), names_a_release=True),
                Declaration("left-pad", "", Location(path, 4, 4), names_a_release=True),
                Declaration("react", "", Location(path, 7, 4), names_a_release=True),
                Declaration("fsevents", "2.3.3", Location(path, 10, 4), names_a_release=True),
            ],
        )

    def test_a_section_that_is_not_an_object_declares_nothing(self):
        """Test that a section holding an array or a string declares nothing, and the sections beside it still do."""
        for case, section in {"an array": ["left-pad"], "a string": "left-pad"}.items():
            with self.subTest(case):
                path = mock_path(json.dumps({"dependencies": section, "devDependencies": {"react": "18.2.0"}}))
                read = PackageJson.read(path)
                self.assertEqual([declaration.dependency for declaration in read.declarations()], ["react"])
                self.assertEqual(list(read.declared_ranges()), [(("devDependencies",), "react")])

    def test_a_declaration_per_catalog_entry(self):
        """Test that each entry of a catalog, named or not, yields a declaration at its own line and column."""
        catalog = {"left-pad": "1.3.0"}
        cases = {
            "the catalog of the workspaces": ({"workspaces": {"catalog": catalog}}, [(4, 6)]),
            "a top-level catalog": ({"catalog": catalog}, [(3, 4)]),
            "a named catalog of the workspaces": ({"workspaces": {"catalogs": {"testing": catalog}}}, [(5, 8)]),
            "a top-level named catalog": ({"catalogs": {"testing": catalog}}, [(4, 6)]),
        }
        for case, (contents, positions) in cases.items():
            with self.subTest(case):
                path = mock_path(json.dumps(contents, indent=2))
                self.assertEqual(
                    list(PackageJson.read(path).declarations()),
                    [
                        Declaration("left-pad", "1.3.0", Location(path, line, column), names_a_release=True)
                        for line, column in positions
                    ],
                )

    @kills.replace(
        _catalog,
        "return catalog_name(spec.removeprefix(_CATALOG_PROTOCOL))",
        "return spec.removeprefix(_CATALOG_PROTOCOL)",
        "a `catalog:default` dependency is reported at the member rather than at the unnamed catalog",
    )
    def test_the_default_catalog_is_the_unnamed_one(self):
        """Test that a dependency declared as `catalog:default` takes its version from the unnamed catalog."""
        path = mock_path(json.dumps({"dependencies": {"semver": "catalog:default"}}))
        (declaration,) = PackageJson.read(path).declarations()
        self.assertEqual(declaration.catalog, "")

    def test_the_version_a_spec_pins(self):
        """Test that each spelling npm reads as one version pins that version, and that a range pins none."""
        for spec, version in [
            ("1.3.0", "1.3.0"),
            ("1.3.0-beta.1", "1.3.0-beta.1"),
            ("=1.3.0", "1.3.0"),
            ("v1.3.0", "1.3.0"),
            ("=v1.3.0", "1.3.0"),
            ("1.3.0+build", "1.3.0"),
            ("1.3.0 - 1.3.0", ""),
            ("1.3", ""),
        ]:
            with self.subTest(spec=spec):
                path = mock_path(json.dumps({"dependencies": {"left-pad": spec}}))
                (reference,) = PackageJson.read(path).declarations()
                self.assertEqual(reference.current_version, version)
