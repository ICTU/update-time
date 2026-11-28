"""Unit tests for finding where a JSON document declares an entry."""

import unittest

from update_time.formats.json import entry_offset


class EntryOffsetTest(unittest.TestCase):
    """Unit tests for the offset at which a section's own entry for a name starts."""

    def test_the_entry_the_section_declares(self):
        """Test that the entry is found where the section declares it."""
        contents = '{\n  "engines": {\n    "node": "18"\n  }\n}\n'
        self.assertEqual(self.offset(contents), contents.index('"node"'))

    @staticmethod
    def offset(contents: str) -> int | None:
        """Return where the document's `engines` section declares `node`."""
        return entry_offset(contents, "engines", "node")

    def test_a_section_nested_in_another_one(self):
        """Test that a section of the same name nested in another one is not taken for the section."""
        contents = (
            "{\n"
            '  "overrides": {\n    "engines": {\n      "node": "16"\n    }\n  },\n'
            '  "engines": {\n    "node": "18"\n  }\n'
            "}\n"
        )
        self.assertEqual(self.offset(contents), contents.rindex('"node"'))

    def test_a_name_spelled_with_an_escape(self):
        """Test that the entry is found although it spells its name with a JSON escape."""
        contents = '{"engines": {"\\u006eode": "18"}}'
        self.assertEqual(self.offset(contents), contents.index('"\\u006eode"'))

    def test_a_document_declaring_no_such_entry(self):
        """Test that a document declaring no such entry has no offset to report."""
        for case, contents in (
            ("no such section", '{"dependencies": {"react": "^18.0.0"}}'),
            ("a section declaring no such name", '{"engines": {"npm": ">=10"}}'),
            ("a section that is not an object", '{"engines": "18"}'),
        ):
            with self.subTest(case=case):
                self.assertIsNone(self.offset(contents))
