"""Test the architecture of the tool."""

import inspect
import unittest

from archunitpython import assert_passes, project_files

from update_time.io.log import Logger


class DependenciesTest(unittest.TestCase):
    """Unit test for module dependencies within the tool."""

    def test_no_cyclic_dependencies(self):
        """Test that there are no cyclic dependencies."""
        assert_passes(project_files("src/").should().have_no_cycles())

    def test_no_script_imports(self):
        """Test that scripts are not imported."""
        assert_passes(project_files("src/").should_not().depend_on_files().with_name("update_*.py"))

    def test_version_primitives_are_a_leaf(self):
        """Test that the version primitives depend on nothing else in the project, so everything can build on them.

        `version.py` holds the primitives the rest of the domain builds on (`bound.py` imports `is_valid` and the
        type aliases from it), so it must import nothing back. Since `domain` is the innermost layer (`LayeringTest`
        keeps it from reaching any outer one), depending on nothing else in `domain` makes `version.py` a
        project-wide leaf. `have_no_cycles` only forbids a two-way dependency, not a one-way inversion, so pinning
        the direction here keeps `version.py` reasoned about (and tested) without the bound machinery.
        """
        rule = project_files("src/").with_name("version.py").should_not().depend_on_files().in_folder("domain")
        assert_passes(rule)


class LayeringTest(unittest.TestCase):
    """Test the layered architecture: domain < io < {file_formats, sources} < package_managers < updaters.

    Each layer may use the ones before it but not the ones after it: `domain` is the pure, I/O-free core; `io` wraps
    file, process, and log I/O; `file_formats` read/write/parse specific manifest formats and `sources` are the
    registry/API clients (parallel siblings, neither using the other); `package_managers` drive the external managers
    (uv/npm/pnpm) using file_formats and sources; and `updaters` wire everything together. Keeping the arrows pointing
    one way is what lets `domain` be tested in isolation and `file_formats`/`sources` be reused.
    """

    def assert_layer_does_not_depend_on(self, layer: str, *outer_layers: str) -> None:
        """Assert that the files in the given layer don't depend on any of the listed outer layers."""
        for outer_layer in outer_layers:
            with self.subTest(layer=layer, outer_layer=outer_layer):
                rule = project_files("src/").in_folder(layer).should_not().depend_on_files().in_folder(outer_layer)
                assert_passes(rule)

    def test_domain_depends_on_no_other_layer(self):
        """Test that the domain layer is self-contained, depending on none of the layers above it."""
        self.assert_layer_does_not_depend_on("domain", "io", "file_formats", "sources", "package_managers", "updaters")

    def test_io_does_not_depend_on_outer_layers(self):
        """Test that the io layer doesn't depend on any of the layers above it."""
        self.assert_layer_does_not_depend_on("io", "file_formats", "sources", "package_managers", "updaters")

    def test_file_formats_do_not_depend_on_outer_layers(self):
        """Test that file_formats don't depend on their sibling sources or on the layers above them."""
        self.assert_layer_does_not_depend_on("file_formats", "sources", "package_managers", "updaters")

    def test_sources_do_not_depend_on_outer_layers(self):
        """Test that sources don't depend on their sibling file_formats or on the layers above them."""
        self.assert_layer_does_not_depend_on("sources", "file_formats", "package_managers", "updaters")

    def test_package_managers_do_not_depend_on_updaters(self):
        """Test that package_managers don't depend on the updaters layer above them."""
        self.assert_layer_does_not_depend_on("package_managers", "updaters")

    def test_network_access_goes_through_io(self):
        """Test that only the io layer touches the network directly.

        Every other layer reaches the network through `io.fetch`, never by importing `requests` itself, so all HTTP
        goes through one place with a uniform timeout, error handling, and logging. This also keeps the pure domain
        layer free of any I/O.
        """
        for layer in ("domain", "sources", "package_managers", "updaters"):
            with self.subTest(layer=layer):
                rule = project_files("src/").in_folder(layer).should_not().depend_on_external_modules()
                assert_passes(rule.matching(r"requests"))

    def test_registry_access_goes_through_sources(self):
        """Test that updaters reach registries through the sources layer, never fetching from them directly.

        `sources` own every registry/API client, so an updater fetches nothing itself: it wires a source's
        `get_latest_*` to the file-rewriting machinery. Keeping the HTTP in `sources` is what lets a single client
        be reused across updaters and keeps updaters as thin as each other.
        """
        rule = project_files("src/").in_folder("updaters").should_not().depend_on_files().with_name("fetch.py")
        assert_passes(rule)

    def test_manifest_parsing_goes_through_file_formats(self):
        """Test that reading/writing manifest files is confined to the file_formats layer.

        `file_formats` owns the manifest formats, so the parsers only it needs (`tomllib`/`tomlkit` for TOML, `yaml`
        for YAML) are manifest-only and no other layer imports them. `json` is not confined the same way — `io.process`
        parses JSON *command output* (`npm`/`pnpm --json`), which is not a manifest — but no updater parses a manifest
        itself: it goes through file_formats. So updaters import none of the four.
        """
        for layer in ("domain", "io", "sources", "package_managers", "updaters"):
            for module in ("tomllib", "tomlkit", "yaml"):
                with self.subTest(layer=layer, module=module):
                    rule = project_files("src/").in_folder(layer).should_not().depend_on_external_modules()
                    assert_passes(rule.matching(module))
        for module in ("json", "tomllib", "tomlkit", "yaml"):
            with self.subTest(layer="updaters", module=module):
                rule = project_files("src/").in_folder("updaters").should_not().depend_on_external_modules()
                assert_passes(rule.matching(module))


class LoggerMessageTest(unittest.TestCase):
    """Test that Logger's message templates and its log methods pair one-to-one.

    Each `MESSAGE_` template on `Logger` belongs to the log method that emits it, but the class layout can only
    express that by convention (each template sits directly above its method), so the pairing is checked here by
    inspecting which templates each method references.
    """

    @staticmethod
    def methods_by_template() -> dict[str, set[str]]:
        """Return, for each message template on Logger, the names of the log methods that reference it."""
        templates = {name for name in vars(Logger) if name.removeprefix("_").startswith("MESSAGE_")}
        references: dict[str, set[str]] = {template: set() for template in templates}
        for name in vars(Logger):
            if name.startswith("__") or not inspect.isfunction(function := getattr(Logger, name)):
                continue
            for template in templates & set(function.__code__.co_names):
                references[template].add(name)
        return references

    def test_each_template_belongs_to_exactly_one_method(self):
        """Test that each message template is referenced by exactly one log method: no orphans, no sharing."""
        unpaired = {template: methods for template, methods in self.methods_by_template().items() if len(methods) != 1}
        self.assertEqual(unpaired, {})

    def test_each_method_references_at_most_one_template(self):
        """Test that no log method references more than one message template."""
        template_counts: dict[str, int] = {}
        for methods in self.methods_by_template().values():
            for method in methods:
                template_counts[method] = template_counts.get(method, 0) + 1
        self.assertEqual({method: count for method, count in template_counts.items() if count > 1}, {})
