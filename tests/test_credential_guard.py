"""Unit tests for the guard that scrubs the credentials."""

import importlib
import os
import unittest

import tests


class CredentialGuardTest(unittest.TestCase):
    """Unit tests for the scrubbing of the credentials from the environment."""

    def test_no_variable_names_a_token(self):
        """Test that the environment holds variables, but none whose name contains TOKEN."""
        names = list(os.environ)
        self.assertNotEqual(names, [])
        self.assertEqual([name for name in names if "TOKEN" in name], [])

    def test_importing_the_package_again_scrubs_no_further(self):
        """Test that re-executing the package leaves the environment it scrubbed as it is."""
        environment = dict(os.environ)
        self.assertNotEqual(environment, {})
        importlib.reload(tests)
        self.assertEqual(dict(os.environ), environment)
