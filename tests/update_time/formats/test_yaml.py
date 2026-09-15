"""Unit tests for the YAML file format, with file I/O mocked."""

import unittest

from update_time.formats import yaml

from tests.helpers import mock_path
from tests.mutation import Mutation, kills


class ReadTest(unittest.TestCase):
    """Unit tests for parsing a YAML file."""

    def test_mapping(self):
        """Test that a YAML mapping is parsed into a dict."""
        self.assertEqual(
            yaml.read(mock_path("machine:\n  image: ubuntu-2204:1\n")), {"machine": {"image": "ubuntu-2204:1"}}
        )

    def test_sequence(self):
        """Test that a YAML sequence is parsed into a list."""
        self.assertEqual(yaml.read(mock_path("- a\n- b\n")), ["a", "b"])

    def test_empty(self):
        """Test that an empty YAML file is parsed as None."""
        self.assertIsNone(yaml.read(mock_path("")))

    @kills(
        Mutation(
            yaml,
            '_Loader.add_multi_constructor("!", _value_under_unknown_tag)\n',
            "",
            "an unknown tag reaches no constructor, so a Compose override file reads as unparsable",
        )
    )
    def test_unknown_tag(self):
        """Test that a value under a tag the parser does not know is read as if the tag were not there."""
        for case, contents, expected in (
            ("a scalar", "environment: !reset null\n", {"environment": None}),
            ("a sequence", "command: !override [serve]\n", {"command": ["serve"]}),
            ("a mapping", "environment: !override {DEBUG: '1'}\n", {"environment": {"DEBUG": "1"}}),
        ):
            with self.subTest(case=case):
                self.assertEqual(yaml.read(mock_path(contents)), expected)

    @kills(
        Mutation(
            yaml,
            '_Loader.add_multi_constructor("!", _value_under_unknown_tag)\n',
            '_Loader.add_multi_constructor("", _value_under_unknown_tag)\n',
            "every tag reaches the constructor, so a tag naming a Python object is applied rather than refused",
        )
    )
    def test_python_object_tag(self):
        """Test that a tag naming a Python object is refused, so reading a file constructs no arbitrary object."""
        self.assertIs(yaml.read(mock_path("!!python/object/apply:os.system ['echo']\n")), yaml.UNPARSABLE)
