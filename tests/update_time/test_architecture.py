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

    def test_domain_makes_no_network_calls(self):
        """Test that the pure domain layer does no network I/O of its own (it doesn't depend on requests)."""
        assert_passes(
            project_files("src/").in_folder("domain").should_not().depend_on_external_modules().matching(r"requests"),
        )
