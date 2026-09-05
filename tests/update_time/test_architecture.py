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
from tests.update_time.helpers import module_level_assignments, project

if TYPE_CHECKING:
    from archunitpython.files.assertion import CustomFileCondition, FileInfo

_MANIFEST_PARSERS = ("tomllib", "tomlkit", "yaml")

# What a file violating the settings rule did, named once so the rule and the test of the rule report it alike.
_READS_A_SETTING = "reads a setting the command line configures a run with"

# The layers, innermost rank first; the layers sharing a rank are siblings.
_RANKS = (
    ("primitives",),
    ("domain",),
    ("markers",),
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


# The calls that build a setting, `flag` being the one that wraps an `EnvVar` for an on-or-off command-line option.
_SETTING_CALLS = ("EnvVar", "flag")


def _setting_globals(files: list[pathlib.Path]) -> set[str]:
    """Return the names assigned a setting: what the command line configures a run with."""
    names: set[str] = set()
    for path in files:
        for targets, value in module_level_assignments(ast.parse(path.read_text())):
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in _SETTING_CALLS:
                names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


# The setting whose readers are not followed. Every module logs, so `get_logger` reads the log level for each of
# them, and how a run logs is no decision a module takes about what it resolves.
_LOGGING_SETTING = "LOG_LEVEL"


def _reads_setting(node: ast.AST, settings: set[str]) -> bool:
    """Return whether the node reads a setting's value, which is the `get` of one called on it."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "get"
        and isinstance(node.value, ast.Name)
        and node.value.id in settings
    )


def _setting_readers(files: list[pathlib.Path], settings: set[str]) -> set[str]:
    """Return the functions that read one of the settings, since calling one reads it as surely as naming it does."""
    readers: set[str] = set()
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef) and any(_reads_setting(inner, settings) for inner in ast.walk(node)):
                readers.add(node.name)
    return readers


def _imported_modules(tree: ast.Module) -> set[str]:
    """Return the names the file's imports bind to a module, so an attribute read off one can be told apart."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.update(alias.asname or alias.name for alias in node.names)
    return modules


def _reads_one_of(names: set[str]) -> CustomFileCondition:
    """Return a rule condition holding for a file that reads one of the names, by import or off its module.

    A condition rather than a rule of its own because archunitpython records a dependency on the module a name
    comes from, not on the name, and a source may use `cooldown.within_cooldown` while reading no setting. An
    attribute counts only when read off an imported module, so a class of the file's own carrying a member of that
    name — `Scope.COOLDOWN` beside the `COOLDOWN` setting — is no violation.
    """

    def reads_one_of(file_info: FileInfo) -> bool:
        tree = ast.parse(file_info.content)
        modules = _imported_modules(tree)
        return any(
            (isinstance(node, ast.ImportFrom) and any(alias.name in names for alias in node.names))
            or (
                isinstance(node, ast.Attribute)
                and node.attr in names
                and isinstance(node.value, ast.Name)
                and node.value.id in modules
            )
            for node in ast.walk(tree)
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
        """Test that `dependency.py` depends on nothing else in `domain`, so the rest of `domain` can build on it."""
        rule = project_files("src/").with_name("dependency.py").should_not().depend_on_files().in_folder("domain")
        assert_passes(rule)


class TestSupportTest(unittest.TestCase):
    """Test the split between the two shared test modules.

    `fixtures.py` holds values the tests reuse and `helpers.py` holds behaviour — base test cases, builders,
    decorators — so a test looking for something reusable knows which of the two to read.
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
    - `markers` parse the `# update-time:` language and decide what each directive steers.
    - `io` wraps file, process, log, network, and command-line I/O.
    - `file_formats` read, write, and parse specific manifest formats.
    - `sources` are the backends the outer layers ask about a dependency.
    - `package_managers` drive the external managers, uv, npm, and pnpm, using file_formats and sources.
    - `references` decide which version a pinned reference should update to, and rewrite the reference accordingly.
    - `updaters` wire everything together.
    """

    def test_no_layer_uses_an_outer_layer(self):
        """Test that no layer depends on a sibling of it or on a layer further out."""
        for layer in _LAYERS:
            for outer_layer in _outer_layers(layer):
                with self.subTest(layer=layer, outer_layer=outer_layer):
                    rule = project_files("src/").in_folder(layer).should_not().depend_on_files().in_folder(outer_layer)
                    assert_passes(rule)

    def test_every_folder_a_rule_names_is_a_layer(self):
        """Test that each folder name spelled out in a rule is a layer.

        A rule scoped to a mistyped folder is caught whether or not whoever wrote it thought about the check.
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
        """Test that no layer but io imports `requests`, so all HTTP goes through `io.fetch`."""
        for layer in (name for name in _LAYERS if name != "io"):
            with self.subTest(layer=layer):
                rule = project_files("src/").in_folder(layer).should_not().depend_on_external_modules()
                assert_passes(rule.matching(_module_pattern("requests")))

    def test_registry_access_goes_through_sources(self):
        """Test that updaters reach registries through the sources layer, never fetching from them directly."""
        updaters = project_files("src/").in_folder("updaters")
        assert_passes(updaters.should_not().depend_on_files().with_name("fetch.py"))
        assert_passes(updaters.should_not().depend_on_files().in_path(_package_init("io")))

    def test_manifest_parsing_goes_through_file_formats(self):
        """Test that the manifest parsers are confined to file_formats, and that updaters parse nothing themselves."""
        for layer in (name for name in _LAYERS if name != "file_formats"):
            for module in _MANIFEST_PARSERS:
                with self.subTest(layer=layer, module=module):
                    rule = project_files("src/").in_folder(layer).should_not().depend_on_external_modules()
                    assert_passes(rule.matching(_module_pattern(module)))
        # The loop above already covers `updaters` for the manifest parsers; an updater parses no `json` either.
        with self.subTest(layer="updaters", module="json"):
            rule = project_files("src/").in_folder("updaters").should_not().depend_on_external_modules()
            assert_passes(rule.matching(_module_pattern("json")))


class ConfigurationReadingTest(unittest.TestCase):
    """Test that a module which decides nothing about a run's configuration is told it rather than reading it."""

    def settings(self) -> set[str]:
        """Return the settings a run is configured with, asserting the scan found them.

        Without the assertion a scan that found nothing would pass every rule below without checking anything. The
        settings are discovered from `src` alone, since the tests declare `EnvVar`s of their own to exercise the
        class, and those are not settings a run is configured with.
        """
        settings = _setting_globals(sorted(pathlib.Path("src").rglob("*.py")))
        self.assertIn("COOLDOWN", settings)
        return settings

    def test_sources_read_no_configuration_global(self):
        """Test that no source reads a setting, whether by naming it or by calling a function that reads it."""
        settings = self.settings()
        readers = _setting_readers(sorted(pathlib.Path("src").rglob("*.py")), settings - {_LOGGING_SETTING})
        sources = project_files("src/").in_folder("sources")
        assert_passes(sources.should_not().adhere_to(_reads_one_of(settings | readers), _READS_A_SETTING))

    def test_the_marker_parser_reads_no_configuration_global(self):
        """Test that the marker parser reads no setting, so a marker means the same whatever a run is configured with.

        `marker.py` imports `RISK_LEVELS` from the module that also defines `VULNERABILITY_LEVEL`, which puts a
        setting one import away.
        """
        settings = self.settings()
        marker_parser = project_files("src/").with_name("marker.py")
        assert_passes(marker_parser.should_not().adhere_to(_reads_one_of(settings), _READS_A_SETTING))

    def test_a_module_reading_a_setting_is_reported(self):
        """Test that a module is reported whether it imports the setting, reads it off its module, or calls a reader.

        Without this the rule above would pass just as well when the condition found nothing whatever a source does.
        A class member of the setting's name is not a read, so the file carrying one is left unreported. A setting
        `flag` builds counts as one too, and so does a function that reads a setting on its caller's behalf. The
        logging setting is the exception: every module logs, so its reader says nothing about the module calling it.
        """
        files = {
            "settings.py": (
                "SETTING = EnvVar('X')\nFLAG = flag('Y')\nLOG_LEVEL = EnvVar('Z')\n\n"
                "def read_setting():\n    return SETTING.get()\n\n"
                "def get_logger():\n    return LOG_LEVEL.get()\n"
            ),
            "importer.py": "from settings import SETTING\n",
            "flag_importer.py": "from settings import FLAG\n",
            "attribute.py": "import settings\n\nsettings.SETTING.get()\n",
            "reader.py": "from settings import read_setting\n",
            "logging_importer.py": "from settings import get_logger\n",
            "namesake.py": "class Scope:\n    SETTING = 'setting'\n\nScope.SETTING\n",
        }
        with project(files) as directory:
            paths = sorted(pathlib.Path(directory).rglob("*.py"))
            settings = _setting_globals(paths)
            readers = _setting_readers(paths, settings - {_LOGGING_SETTING})
            condition = _reads_one_of(settings | readers)
            rule = project_files(directory).should_not().adhere_to(condition, _READS_A_SETTING)
            violations = [violation for violation in rule.check() if isinstance(violation, CustomFileViolation)]
            reported = sorted(pathlib.Path(violation.file_info.path).name for violation in violations)
        self.assertEqual(reported, ["attribute.py", "flag_importer.py", "importer.py", "reader.py"])


class ToolInvocationTest(unittest.TestCase):
    """Test that the tools are run as modules rather than as scripts."""

    def test_tools_are_run_as_modules(self):
        """Test that no recipe runs a tool as a script, reporting the recipe lines that do."""
        lines = pathlib.Path("justfile").read_text().splitlines()
        self.assertEqual([line.strip() for line in lines if "python tools/" in line], [])


class SubmoduleImportTest(unittest.TestCase):
    """Test that the rules naming an external module see the submodule form this project imports them in."""

    def assert_reports_submodule_import(self, module: str) -> None:
        """Assert that the module's pattern reports a file that imports a submodule of the module."""
        with project({"importer.py": f"from {module}.sub import thing\n"}) as directory:
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
    fetch`.
    """

    def test_pattern_reports_a_package_import(self):
        """Test that the pattern reports the import written through the package, and not the one naming the module."""
        files = {
            "pkg/__init__.py": "",
            "pkg/target.py": "thing = 1\n",
            "package_form.py": "from pkg import target\n\ntarget\n",
            "module_form.py": "from pkg.target import thing\n\nthing\n",
        }
        with project(files) as directory:
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
                with project(files) as directory:
                    by_module = project_files(directory).should_not().depend_on_files().with_name("target.py")
                    by_package = project_files(directory).should_not().depend_on_files().in_path(_package_init("pkg"))
                    self.assertEqual(by_module.check(), [])
                    self.assertEqual(len(by_package.check()), 1)
