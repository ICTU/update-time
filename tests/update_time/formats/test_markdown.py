"""Unit tests for recognising Markdown and reading its markup."""

import unittest

from update_time.formats import markdown

from tests.mutation import Mutation, kills


class MarkupTest(unittest.TestCase):
    """Unit tests for what a file's name and a content type say about whether a text is written in Markdown."""

    @kills(
        Mutation(
            markdown.is_file,
            "return urlparse(name).path.lower().endswith(EXTENSION)",
            "return urlparse(name).path.endswith(EXTENSION)",
            "a file that shouts its extension is read as text, so its Markdown is shown raw",
        )
    )
    def test_an_extension_is_read_whatever_its_case(self):
        """Test that a file shouting its extension is read as Markdown all the same."""
        self.assertTrue(markdown.is_file("CHANGELOG.MD"))

    @kills(
        Mutation(
            markdown.is_content_type,
            'return content_type.partition(";")[0].strip().lower() == _CONTENT_TYPE',
            'return content_type.partition(";")[0].lower() == _CONTENT_TYPE',
            "a content type padded with spaces matches nothing, so its Markdown is shown raw",
        ),
        Mutation(
            markdown.is_content_type,
            'return content_type.partition(";")[0].strip().lower() == _CONTENT_TYPE',
            'return content_type.partition(";")[0].strip() == _CONTENT_TYPE',
            "a content type spelled in another case matches nothing, so its Markdown is shown raw",
        ),
    )
    def test_a_content_type_is_read_past_its_case_and_spacing(self):
        """Test that a content type is read past the case it is spelled in and the spaces around its parameters."""
        for content_type in ("Text/Markdown", " text/markdown ; charset=UTF-8"):
            with self.subTest(content_type=content_type):
                self.assertTrue(markdown.is_content_type(content_type))
