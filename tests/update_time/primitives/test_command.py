"""Unit tests for the command value object."""

import unittest

from update_time.primitives.command import Command

from tests.mutation import Mutation, kills


class CommandTest(unittest.TestCase):
    """Unit tests for the command value object."""

    def test_str_is_the_command_line(self):
        """Test that a command renders as the command line it runs, its arguments separated by spaces."""
        self.assertEqual(str(Command("npm", "update", "--save")), "npm update --save")

    def test_executable_is_the_first_word(self):
        """Test that a command's executable is the first of the words it runs."""
        self.assertEqual(Command("npm", "update", "--save").executable, "npm")

    @kills(
        Mutation(
            Command.name,
            "takewhile(_is_not_an_option, self)",
            "self[:2]",
            "a command's name drops the subcommands after the first, so `uv pip compile` reads as `uv pip`",
        )
    )
    def test_name_is_the_words_before_the_first_option(self):
        """Test that a command's name is its executable and the subcommands before its first option."""
        self.assertEqual(Command("uv", "pip", "compile", "--quiet").name, "uv pip compile")
