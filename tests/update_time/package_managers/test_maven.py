"""Unit tests for running Maven over a pom.xml."""

import unittest
from unittest.mock import Mock, patch

from update_time.package_managers import maven


class PluginVersionTest(unittest.TestCase):
    """Unit tests for the versions plugin release the shipped pom declares."""

    def test_the_version_the_shipped_pom_declares(self):
        """Test that the versions plugin release is read from the pom Update-time ships beside the module."""
        self.assertRegex(maven._declared_plugin_version(), r"^\d+\.\d+\.\d+$")

    def test_a_shipped_pom_declaring_no_release(self):
        """Test that a pom declaring no release raises an error naming the file, rather than one about a lookup."""
        patched = patch.object(maven.pom_xml_format, "properties", Mock(return_value={}))
        with patched, self.assertRaises(RuntimeError) as error:
            maven._declared_plugin_version()
        self.assertIn("pom.xml", str(error.exception))
