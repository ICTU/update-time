"""Unit tests for the Maven plugin releases Update-time runs."""

import unittest
from unittest.mock import Mock, patch

from update_time.package_managers.maven import plugins


class PluginVersionTest(unittest.TestCase):
    """Unit tests for the plugin releases the shipped pom declares."""

    def test_the_versions_the_shipped_pom_declares(self):
        """Test that each plugin's release is read from the pom Update-time ships beside the module."""
        for version_property in ("help.plugin.version", "versions.plugin.version"):
            with self.subTest(version_property=version_property):
                self.assertRegex(plugins._declared_plugin_version(version_property), r"^\d+\.\d+\.\d+$")

    def test_a_shipped_pom_declaring_no_release(self):
        """Test that a pom declaring no release raises an error naming the file, rather than one about a lookup."""
        patched = patch.object(plugins.pom_xml_manifest, "properties", Mock(return_value={}))
        with patched, self.assertRaises(RuntimeError) as error:
            plugins._declared_plugin_version("help.plugin.version")
        self.assertIn("pom.xml", str(error.exception))
