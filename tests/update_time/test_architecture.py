"""Test the architecture of the tool."""

import unittest

from archunitpython import assert_passes, project_files


class DependenciesTest(unittest.TestCase):
    """Unit test for module dependencies within the tool."""

    def test_no_cyclic_dependencies(self):
        """Test that there are no cyclic dependencies."""
        assert_passes(project_files("src/").should().have_no_cycles())

    def test_no_script_imports(self):
        """Test that scripts are not imported."""
        assert_passes(project_files("src/").should_not().depend_on_files().with_name("update_*.py"))


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
