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
    """Test that the package respects its layered architecture: domain < io < sources < updaters.

    Each layer may use the ones before it but not the ones after it: `domain` is the pure, I/O-free core; `io`
    wraps file, process, and log I/O; `sources` are the registry/API clients; and `updaters` wire them together.
    Keeping the arrows pointing one way is what lets `domain` be tested in isolation and `sources` be reused.
    """

    def assert_layer_does_not_depend_on(self, layer: str, *outer_layers: str) -> None:
        """Assert that the files in the given layer don't depend on any of the listed outer layers."""
        for outer_layer in outer_layers:
            with self.subTest(layer=layer, outer_layer=outer_layer):
                rule = project_files("src/").in_folder(layer).should_not().depend_on_files().in_folder(outer_layer)
                assert_passes(rule)

    def test_domain_depends_on_no_other_layer(self):
        """Test that the domain layer is self-contained, depending on neither io, sources, nor updaters."""
        self.assert_layer_does_not_depend_on("domain", "io", "sources", "updaters")

    def test_io_does_not_depend_on_sources_or_updaters(self):
        """Test that the io layer doesn't depend on the sources or updaters layers above it."""
        self.assert_layer_does_not_depend_on("io", "sources", "updaters")

    def test_sources_do_not_depend_on_updaters(self):
        """Test that the sources layer doesn't depend on the updaters layer above it."""
        self.assert_layer_does_not_depend_on("sources", "updaters")

    def test_network_access_goes_through_io(self):
        """Test that only the io layer touches the network directly.

        Every other layer reaches the network through `io.fetch`, never by importing `requests` itself, so all HTTP
        goes through one place with a uniform timeout, error handling, and logging. This also keeps the pure domain
        layer free of any I/O.
        """
        for layer in ("domain", "sources", "updaters"):
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
