"""Unit tests for the command value object."""

import unittest

from update_time.primitives.command import Command


class CommandTest(unittest.TestCase):
    """Unit tests for the command value object."""

    def test_str_is_the_command_line(self):
        """Test that a command renders as the command line it runs, its arguments separated by spaces."""
        self.assertEqual(str(Command("npm", "update", "--save")), "npm update --save")

    def test_executable_is_the_first_word(self):
        """Test that a command's executable is the first of the words it runs."""
        self.assertEqual(Command("npm", "update", "--save").executable, "npm")
