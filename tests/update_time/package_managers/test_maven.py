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


class RuleSetFileTest(unittest.TestCase):
    """Unit tests for the file Update-time writes the rule set to.

    This is the one part of the run that reaches the file system, so it is exercised against a real file.
    """

    def test_the_rule_set_is_readable_until_the_file_is_removed(self):
        """Test that the file holds the rule set while the context is open, and is removed once it closes."""
        rule_set = "<ruleset/>"
        with maven._rule_set_file(rule_set) as path:
            self.assertEqual(path.read_text(), rule_set)
        self.assertFalse(path.exists())
