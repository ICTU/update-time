"""Unit tests for the typed environment variable."""

import os
import unittest

from update_time.primitives.environment import EnvVar

from tests.helpers import patch_environ

INT_VAR: EnvVar[int] = EnvVar("_UPDATE_TIME_TEST_INT", default=7, parse=int)
BOOL_VAR: EnvVar[bool] = EnvVar(
    "_UPDATE_TIME_TEST_BOOL",
    default=False,
    parse=lambda value: value == "1",
    serialize=lambda flag: "1" if flag else "0",
)


class EnvVarTest(unittest.TestCase):
    """Unit tests for getting and setting a typed environment variable."""

    def test_get_returns_the_default_when_unset(self):
        """Test that the default is returned when the variable is not set in the environment."""
        with patch_environ():
            self.assertEqual(INT_VAR.get(), 7)

    def test_get_parses_the_stored_value(self):
        """Test that a stored string is parsed into the typed value."""
        with patch_environ({INT_VAR.name: "14"}):
            self.assertEqual(INT_VAR.get(), 14)

    def test_set_serialises_the_value(self):
        """Test that set stores the value serialised to a string, which get parses back."""
        with patch_environ():
            INT_VAR.set(21)
            self.assertEqual(INT_VAR.get(), 21)

    def test_serialize_defaults_to_str(self):
        """Test that a variable without an explicit serialize stores the value's `str`."""
        with patch_environ():
            INT_VAR.set(21)
            self.assertEqual(os.environ[INT_VAR.name], "21")

    def test_custom_parse_and_serialize_round_trip(self):
        """Test that a variable's parse and serialize are inverses, so a set value survives a get."""
        with patch_environ():
            BOOL_VAR.set(value=True)
            self.assertTrue(BOOL_VAR.get())
            BOOL_VAR.set(value=False)
            self.assertFalse(BOOL_VAR.get())
