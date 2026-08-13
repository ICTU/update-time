"""Test the architecture of the tool."""

import ast
import inspect
import pathlib
import re
import unittest
from typing import TYPE_CHECKING

from archunitpython import assert_passes, project_files
from archunitpython.files.assertion import CustomFileViolation

from tests.update_time import fixtures
from tests.update_time.helpers import _module_level_assignments, _project

if TYPE_CHECKING:
    from archunitpython.files.assertion import CustomFileCondition, FileInfo

_MANIFEST_PARSERS = ("tomllib", "tomlkit", "yaml")

# What a file violating the settings rule did, named once so the rule and the test of the rule report it alike.
_READS_A_SETTING = "reads a setting the command line configures a run with"

# The layers, innermost rank first; the layers sharing a rank are siblings.
_RANKS = (
    ("primitives",),
    ("domain",),
    ("io",),
    ("file_formats", "sources"),
    ("package_managers", "references"),
    ("updaters",),
)
_LAYERS = tuple(layer for rank in _RANKS for layer in rank)


def _outer_layers(layer: str) -> tuple[str, ...]:
    """Return the layers the given layer may not use: its siblings, and everything further out."""
    rank = next(index for index, names in enumerate(_RANKS) if layer in names)
    siblings = tuple(name for name in _RANKS[rank] if name != layer)
    return siblings + tuple(name for names in _RANKS[rank + 1 :] for name in names)


def _module_pattern(module: str) -> re.Pattern[str]:
    """Return a pattern matching the module and its submodules.

    `matching()` reads a plain string as a glob, anchored to the whole module name recorded for an import, so `yaml`
    alone matches `import yaml` but not `from yaml.parser import Parser`. The glob `yaml*` would match the submodule,
    but also an unrelated distribution such as `yamllint`.
    """
    return re.compile(rf"{re.escape(module)}($|\.)")


def _package_init(package: str) -> str:
    """Return a path pattern matching the package's `__init__.py`.

    `from update_time.io import fetch` records a dependency on `io/__init__.py` rather than on `io/fetch.py`, so a
    rule naming the module alone never sees the import written that way. Forbidding the package as well closes that
    route, and leaves `from update_time.io.fetch import get` untouched, which records `io/fetch.py` as expected.
    The package is spelled as a path, `tests/update_time` rather than `tests.update_time`.
    """
    return f"*/{package}/__init__.py"


def _env_var_globals(files: list[pathlib.Path]) -> set[str]:
    """Return the names assigned an `EnvVar`: the settings the command line configures a run with.

    Discovered rather than listed, so a setting added later is covered without this test being edited.
    """
    names: set[str] = set()
    for path in files:
        for targets, value in _module_level_assignments(ast.parse(path.read_text())):
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "EnvVar":
                names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _reads_one_of(names: set[str]) -> CustomFileCondition:
    """Return a rule condition holding for a file that reads one of the names, by import or as an attribute.

    A condition rather than a rule of its own because archunitpython records a dependency on the module a name
    comes from, not on the name, and a source may use `cooldown.within_cooldown` while reading no setting.
    """

    def reads_one_of(file_info: FileInfo) -> bool:
        return any(
            (isinstance(node, ast.ImportFrom) and any(alias.name in names for alias in node.names))
            or (isinstance(node, ast.Attribute) and node.attr in names)
            for node in ast.walk(ast.parse(file_info.content))
        )

    return reads_one_of


class DependenciesTest(unittest.TestCase):
    """Unit test for module dependencies within the tool."""

    def test_no_cyclic_dependencies(self):
        """Test that there are no cyclic dependencies."""
        assert_passes(project_files("src/").should().have_no_cycles())

    def test_no_script_imports(self):
        """Test that scripts are not imported, by name or through the package holding them."""
        assert_passes(project_files("src/").should_not().depend_on_files().with_name("update_*.py"))
        assert_passes(project_files("src/").should_not().depend_on_files().in_path(_package_init("updaters")))

    def test_dependency_module_is_a_leaf(self):
        """Test that `dependency.py` depends on nothing else in `domain`, so the rest of `domain` can build on it.

        `dependency.py` holds the foundational types and helpers the rest of `domain` builds on: `bound.py` imports
        `is_valid` and the type aliases from it. So `dependency.py` must import nothing back from `domain`, making it
        a leaf within the layer. It may still build on the inner `primitives` layer, but not on its `domain` siblings.
        `have_no_cycles` forbids only a two-way dependency, not a one-way inversion, so pinning the direction here
        keeps `dependency.py` reasoned about and tested without the bound machinery.
        """
        rule = project_files("src/").with_name("dependency.py").should_not().depend_on_files().in_folder("domain")
        assert_passes(rule)


class TestSupportTest(unittest.TestCase):
    """Test the split between the two shared test modules.

    `fixtures.py` holds values the tests reuse and `helpers.py` holds behaviour — base test cases, builders,
    decorators — so a test looking for something reusable knows which of the two to read. Keeping the values free of
    behaviour is what lets `helpers.py` build on them without the dependency ever running the other way.
    """

    def test_fixtures_do_not_depend_on_helpers(self):
        """Test that fixtures.py doesn't import helpers.py, so the dependency only runs from helpers to fixtures."""
        fixtures_file = project_files("tests/").with_name("fixtures.py")
        assert_passes(fixtures_file.should_not().depend_on_files().with_name("helpers.py"))
        assert_passes(fixtures_file.should_not().depend_on_files().in_path(_package_init("tests/update_time")))

    def test_fixtures_define_no_behaviour(self):
        """Test that fixtures.py defines no function or class, so behaviour is only ever added to helpers.py."""
        defined_here = [
            name
            for name, value in vars(fixtures).items()
            if (inspect.isfunction(value) or inspect.isclass(value)) and value.__module__ == fixtures.__name__
        ]
        self.assertEqual(defined_here, [])


class LayeringTest(unittest.TestCase):
    """Test the layered architecture, which `_RANKS` declares innermost to outermost.

    A layer may use the ones before it. It may use neither a sibling sharing its rank nor a layer after it:
    - `primitives` are project-agnostic building blocks, like a typed environment variable, that even the pure core may
      reach for.
    - `domain` is the pure, I/O-free core.
    - `io` wraps file, process, and log I/O.
    - `file_formats` read, write, and parse specific manifest formats.
    - `sources` are the registry and API clients.
    - `package_managers` drive the external managers, uv, npm, and pnpm, using file_formats and sources.
    - `references` decide which version a pinned reference should update to, and rewrite the reference accordingly.
    - `updaters` wire everything together.

    Keeping the arrows pointing one way is what lets `domain` be tested in isolation and `file_formats` and `sources`
    be reused.
    """

    def test_no_layer_uses_an_outer_layer(self):
        """Test that no layer depends on a sibling of it or on a layer further out.

        Every layer is taken from the ranks rather than named here, so one added to them is covered by this rule
        without anyone having to remember to cover it.
        """
        for layer in _LAYERS:
            for outer_layer in _outer_layers(layer):
                with self.subTest(layer=layer, outer_layer=outer_layer):
                    rule = project_files("src/").in_folder(layer).should_not().depend_on_files().in_folder(outer_layer)
                    assert_passes(rule)

    def test_every_folder_a_rule_names_is_a_layer(self):
        """Test that each folder name spelled out in a rule is a layer.

        The names are read out of this module rather than declared here, so a rule scoped to a mistyped folder is
        caught whether or not whoever wrote it thought about the check.
        """
        tree = ast.parse(pathlib.Path(__file__).read_text())
        named = [
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "in_folder"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        self.assertNotEqual(named, [])  # An empty list would pass the check below without checking anything.
        self.assertEqual([name for name in named if name not in _LAYERS], [])

    def test_the_layers_are_the_folders(self):
        """Test that every layer named here is a folder, and every folder under the package is a layer.

        A layer whose folder was renamed would leave its rules selecting nothing, and a folder added without a layer
        to match would go unmentioned by any rule at all.
        """
        folders = [path.name for path in pathlib.Path("src/update_time").iterdir() if path.is_dir()]
        self.assertEqual(sorted(name for name in folders if not name.startswith("__")), sorted(_LAYERS))

    def test_network_access_goes_through_io(self):
        """Test that only the io layer touches the network directly.

        Every other layer reaches the network through `io.fetch`, never by importing `requests` or a submodule of it,
        so all HTTP goes through one place with a uniform timeout, error handling, and logging. This also keeps the
        pure domain layer free of any I/O.
        """
        for layer in (name for name in _LAYERS if name not in ("io", "file_formats")):
            with self.subTest(layer=layer):
                rule = project_files("src/").in_folder(layer).should_not().depend_on_external_modules()
                assert_passes(rule.matching(_module_pattern("requests")))

    def test_registry_access_goes_through_sources(self):
        """Test that updaters reach registries through the sources layer, never fetching from them directly.

        `sources` own every registry/API client, so an updater fetches nothing itself: it wires a source's
        `get_latest_*` to the file-rewriting machinery. Keeping the HTTP in `sources` is what lets a single client
        be reused across updaters and keeps updaters as thin as each other.
        """
        updaters = project_files("src/").in_folder("updaters")
        assert_passes(updaters.should_not().depend_on_files().with_name("fetch.py"))
        assert_passes(updaters.should_not().depend_on_files().in_path(_package_init("io")))

    def test_manifest_parsing_goes_through_file_formats(self):
        """Test that reading/writing manifest files is confined to the file_formats layer.

        `file_formats` owns the manifest formats, so the parsers only it needs (`tomllib`/`tomlkit` for TOML, `yaml`
        for YAML) are manifest-only and no other layer imports them. `json` is not confined the same way — `io.process`
        parses JSON *command output* (`npm`/`pnpm --json`), which is not a manifest — but no updater parses a manifest
        itself: it goes through file_formats. So updaters import none of the four.
        """
        for layer in (name for name in _LAYERS if name != "file_formats"):
            for module in _MANIFEST_PARSERS:
                with self.subTest(layer=layer, module=module):
                    rule = project_files("src/").in_folder(layer).should_not().depend_on_external_modules()
                    assert_passes(rule.matching(_module_pattern(module)))
        for module in ("json", *_MANIFEST_PARSERS):
            with self.subTest(layer="updaters", module=module):
                rule = project_files("src/").in_folder("updaters").should_not().depend_on_external_modules()
                assert_passes(rule.matching(_module_pattern(module)))


class ConfigurationReadingTest(unittest.TestCase):
    """Test that a module which decides nothing about a run's configuration is told it rather than reading it.

    Every setting a source honours — the cooldown, and any added later — reaches it as an argument of the
    `NewVersionGetter` contract, decided once by `references.resolve.latest_version`. A source reading the global
    itself would answer with the run's setting where the caller asked for another, which is what a per-reference
    override needs the source not to do. The marker parser is held to the same rule, for the reason its own test
    gives.
    """

    def test_sources_read_no_configuration_global(self):
        """Test that no source reads a setting, and that there are settings to be read.

        The settings are discovered from `src` alone, since the tests declare `EnvVar`s of their own to exercise the
        class, and those are not settings a run is configured with.
        """
        settings = _env_var_globals(sorted(pathlib.Path("src").rglob("*.py")))
        self.assertIn("COOLDOWN", settings)  # Assert the settings were found, so an empty scan can't pass silently.
        sources = project_files("src/").in_folder("sources")
        assert_passes(sources.should_not().adhere_to(_reads_one_of(settings), _READS_A_SETTING))

    def test_the_marker_parser_reads_no_configuration_global(self):
        """Test that the marker parser reads no setting, so a marker means the same whatever a run is configured with.

        `marker.py` imports `RISK_LEVELS` from the module that also defines `WARN_VULNERABILITY_LEVEL`, which puts a
        setting one import away. Reading it while parsing would decide there whether a marker beats the command line,
        where `Threshold.value_or` decides it once for whichever check asks.
        """
        settings = _env_var_globals(sorted(pathlib.Path("src").rglob("*.py")))
        self.assertIn("COOLDOWN", settings)  # Assert the settings were found, so an empty scan can't pass silently.
        marker_parser = project_files("src/").with_name("marker.py")
        assert_passes(marker_parser.should_not().adhere_to(_reads_one_of(settings), _READS_A_SETTING))

    def test_a_module_reading_a_setting_is_reported(self):
        """Test that a module is reported whether it imports the setting or reads it off its module.

        Without this the rule above would pass just as well when the condition found nothing whatever a source does.
        """
        files = {
            "settings.py": "SETTING = EnvVar('X')\n",
            "importer.py": "from settings import SETTING\n",
            "attribute.py": "import settings\n\nsettings.SETTING.get()\n",
            "reader.py": "from settings import read_setting\n",
        }
        with _project(files) as directory:
            settings = _env_var_globals(sorted(pathlib.Path(directory).rglob("*.py")))
            rule = project_files(directory).should_not().adhere_to(_reads_one_of(settings), _READS_A_SETTING)
            violations = [violation for violation in rule.check() if isinstance(violation, CustomFileViolation)]
            reported = sorted(pathlib.Path(violation.file_info.path).name for violation in violations)
        self.assertEqual(reported, ["attribute.py", "importer.py"])


class ToolInvocationTest(unittest.TestCase):
    """Test that the tools are run as modules rather than as scripts.

    Running `python tools/thing.py` puts `tools` itself on the import path, where the package by that name can't be
    found, so the script dies on its first `from tools...` import. The tests import the tools as a package and so
    never meet it, which leaves the recipe running them as the only place it shows.
    """

    def test_tools_are_run_as_modules(self):
        """Test that no recipe runs a tool as a script, reporting the recipe lines that do."""
        lines = pathlib.Path("justfile").read_text().splitlines()
        self.assertEqual([line.strip() for line in lines if "python tools/" in line], [])


class SubmoduleImportTest(unittest.TestCase):
    """Test that the rules naming an external module see the submodule form this project imports them in.

    This project imports external modules by submodule throughout, `from packaging.version import Version` for
    example, so a submodule is the likelier form in which a violation would arrive.
    """

    def assert_reports_submodule_import(self, module: str) -> None:
        """Assert that the module's pattern reports a file that imports a submodule of the module."""
        with _project({"importer.py": f"from {module}.sub import thing\n"}) as directory:
            rule = project_files(directory).should_not().depend_on_external_modules()
            self.assertEqual(len(rule.matching(_module_pattern(module)).check()), 1)

    def test_network_access_rule_reports_a_submodule_import(self):
        """Test that the rule confining network access to io reports a `requests` submodule import."""
        self.assert_reports_submodule_import("requests")

    def test_manifest_parsing_rule_reports_a_submodule_import(self):
        """Test that the rule confining manifest parsing to file_formats reports a parser's submodule import."""
        for module in ("json", *_MANIFEST_PARSERS):
            with self.subTest(module=module):
                self.assert_reports_submodule_import(module)


class PackageImportTest(unittest.TestCase):
    """Test that the pattern forbidding a package matches the `__init__.py` a package-form import records.

    The rules that name a module lean on this pattern to cover the import written as `from update_time.io import
    fetch`. A pattern matching nothing would leave those rules passing whatever the code does, and every one of
    them would keep passing, so nothing else in the suite would reveal it.
    """

    def test_pattern_reports_a_package_import(self):
        """Test that the pattern reports the import written through the package, and not the one naming the module."""
        files = {
            "pkg/__init__.py": "",
            "pkg/target.py": "thing = 1\n",
            "package_form.py": "from pkg import target\n\ntarget\n",
            "module_form.py": "from pkg.target import thing\n\nthing\n",
        }
        with _project(files) as directory:
            rule = project_files(directory).should_not().depend_on_files().in_path(_package_init("pkg"))
            self.assertEqual(len(rule.check()), 1)


class PackageImportBlindSpotTest(unittest.TestCase):
    """Test that a rule naming a module still misses the import written through the package.

    This pins what archunitpython records rather than anything this project decides, so that an upgrade changing it
    is noticed. Were a later version to record `pkg/target.py` for `from pkg import target`, this test would fail,
    and the companion assertions naming the package in the rules above could be dropped.
    """

    def test_a_package_import_is_not_recorded_against_the_module(self):
        """Test that naming the module reports nothing, while naming the package reports the very same import.

        The alias is a case of its own, since a module renamed on import is how several of this project's own
        package imports are written.
        """
        for importer in ("from pkg import target\n\ntarget\n", "from pkg import target as alias\n\nalias\n"):
            with self.subTest(importer=importer.splitlines()[0]):
                files = {"pkg/__init__.py": "", "pkg/target.py": "thing = 1\n", "importer.py": importer}
                with _project(files) as directory:
                    by_module = project_files(directory).should_not().depend_on_files().with_name("target.py")
                    by_package = project_files(directory).should_not().depend_on_files().in_path(_package_init("pkg"))
                    self.assertEqual(by_module.check(), [])
                    self.assertEqual(len(by_package.check()), 1)
