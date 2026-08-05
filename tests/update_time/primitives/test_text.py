"""Unit tests for the text module."""

import re
import unittest
from typing import cast

from update_time.primitives.text import replace_match, rewrite_match, rewrite_string


def _search(pattern: str, text: str) -> re.Match[str]:
    """Search for the pattern in the text and return the match (which the test's inputs always produce)."""
    return cast("re.Match[str]", re.search(pattern, text))


_PATTERN = r"(?P<name>\w+)@(?P<version>[\d.]+)"


class RewriteMatchTest(unittest.TestCase):
    """Unit tests for replacing only the captured groups within a match."""

    def test_replaces_only_the_captured_span(self):
        """Test that a group is replaced only where it was captured, not where its value recurs within the match."""
        match = _search(r"pkg@(?P<version>[\d.]+)/dist/pkg-[\d.]+\.js", "pkg@2.0.11/dist/pkg-2.0.11.js")
        self.assertEqual(rewrite_match(match, {"version": "2.0.12"}), "pkg@2.0.12/dist/pkg-2.0.11.js")

    def test_replaces_multiple_groups(self):
        """Test that several groups are replaced, each at its own captured span."""
        match = _search(_PATTERN, "before pkg@2.0.11 after")
        self.assertEqual(rewrite_match(match, {"name": "lib", "version": "3.0"}), "lib@3.0")

    def test_leaves_the_rest_of_the_string_out(self):
        """Test that only the matched text is returned, not the string it was found in."""
        match = _search(_PATTERN, "before pkg@2.0.11 after")
        self.assertEqual(rewrite_match(match, {"version": "3.0"}), "pkg@3.0")


class ReplaceMatchTest(unittest.TestCase):
    """Unit tests for replacing a whole match within the string it was found in."""

    def test_replaces_the_match(self):
        """Test that the matched text is swapped out and the text around it kept."""
        match = _search(_PATTERN, "before pkg@2.0.11 after")
        self.assertEqual(replace_match(match, "lib@3.0"), "before lib@3.0 after")


class RewriteStringTest(unittest.TestCase):
    """Unit tests for replacing captured groups within the string the match was found in."""

    def test_replaces_the_groups_in_place(self):
        """Test that the groups are replaced where they were captured and the text around the match kept."""
        match = _search(_PATTERN, "before pkg@2.0.11 after")
        self.assertEqual(rewrite_string(match, {"version": "3.0"}), "before pkg@3.0 after")

    def test_leaves_the_same_value_outside_the_match_alone(self):
        """Test that a value recurring outside the match is left alone, only the captured span rewritten."""
        match = _search(_PATTERN, "pkg@2.0 and pkg@2.0")
        self.assertEqual(rewrite_string(match, {"version": "3.0"}), "pkg@3.0 and pkg@2.0")
