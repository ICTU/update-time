"""Unit tests for the XML file format, with file I/O mocked."""

import unittest
from unittest.mock import Mock

from update_time.formats import xml

from tests.helpers import mock_path


def _latin_1_file(contents: str) -> Mock:
    """Return a mock path holding the contents as latin-1 bytes."""
    encoded = contents.encode("latin-1")
    undecodable = UnicodeDecodeError("utf-8", encoded, 0, 1, "invalid continuation byte")
    return Mock(read_bytes=Mock(return_value=encoded), read_text=Mock(side_effect=undecodable))


class ReadTest(unittest.TestCase):
    """Unit tests for parsing an XML file."""

    def test_a_document_in_the_encoding_it_declares(self):
        """Test that a document is decoded as its own declaration says, rather than as UTF-8."""
        declared = '<?xml version="1.0" encoding="ISO-8859-1"?>\n<project>\n  <name>café</name>\n</project>\n'
        project = xml.read(_latin_1_file(declared))
        self.assertEqual([name.text for name in project.descendants("name")] if project else [], ["café"])

    def test_an_element_whose_text_sits_on_a_line_of_its_own(self):
        """Test that an element's text leaves out the whitespace the document wraps it in."""
        padded = "<project>\n  <name>\n    probe\n  </name>\n</project>\n"
        project = xml.read(mock_path(padded))
        self.assertEqual([name.text for name in project.descendants("name")] if project else [], ["probe"])

    def test_a_document_written_with_a_namespace_prefix(self):
        """Test that an element written with a namespace prefix is found under its plain name."""
        prefixed = (
            '<pom:project xmlns:pom="http://maven.apache.org/POM/4.0.0">\n'
            "  <pom:name>probe</pom:name>\n</pom:project>\n"
        )
        project = xml.read(mock_path(prefixed))
        self.assertEqual([name.text for name in project.descendants("name")] if project else [], ["probe"])

    def test_a_file_that_cannot_be_read(self):
        """Test that a file the filesystem refuses is read as unparsable, rather than ending the run."""
        self.assertIsNone(xml.read(Mock(read_bytes=Mock(side_effect=OSError))))
