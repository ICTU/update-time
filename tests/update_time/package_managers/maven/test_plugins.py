"""Unit tests for the Maven plugin releases Update-time runs."""

import unittest
from unittest.mock import Mock, patch

from update_time.manifests import pom_xml
from update_time.package_managers.maven.plugins import _declared_plugin_version


class PluginVersionTest(unittest.TestCase):
    """Unit tests for the plugin releases the shipped pom declares."""

    def test_the_versions_the_shipped_pom_declares(self):
        """Test that each plugin's release is read from the pom Update-time ships beside the module."""
        for version_property in ("help.plugin.version", "versions.plugin.version"):
            with self.subTest(version_property=version_property):
                self.assertRegex(_declared_plugin_version(version_property), r"^\d+\.\d+\.\d+$")

    def test_a_shipped_pom_declaring_no_release(self):
        """Test that a pom declaring no release raises an error naming the file, rather than one about a lookup."""
        with patch.object(pom_xml, "properties", Mock(return_value={})), self.assertRaises(RuntimeError) as error:
            _declared_plugin_version("help.plugin.version")
        self.assertIn("pom.xml", str(error.exception))
