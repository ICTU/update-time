"""Unit tests for the YAML file format, with file I/O mocked."""

import itertools
import unittest
from typing import cast

from mutation_time.mutation import kills
from update_time.formats import yaml

from tests.helpers import mock_path


class ReadTest(unittest.TestCase):
    """Unit tests for parsing a YAML file."""

    def test_mapping(self):
        """Test that a YAML mapping is parsed into a document holding a dict."""
        self.assertEqual(
            yaml.read(mock_path("machine:\n  image: ubuntu-2204:1\n")),
            yaml.Document({"machine": {"image": "ubuntu-2204:1"}}),
        )

    def test_sequence(self):
        """Test that a YAML sequence is parsed into a document holding a list."""
        self.assertEqual(yaml.read(mock_path("- a\n- b\n")), yaml.Document(["a", "b"]))

    def test_empty(self):
        """Test that an empty YAML file is parsed as an empty document."""
        self.assertEqual(yaml.read(mock_path("")), yaml.Document())

    @kills.delete(
        yaml.parse,
        ", ValueError",
        "a YAML file holding a date that does not exist stops the run",
        raises="ValueError: month must be in 1..12, not 13",
    )
    def test_a_date_that_does_not_exist_makes_the_yaml_invalid(self):
        """Test that a YAML file holding a date that does not exist is not valid YAML."""
        self.assertIsNone(yaml.parse("released: 2023-13-45\n"))

    @kills.delete(
        yaml,
        '_Loader.add_multi_constructor("!", _value_under_unknown_tag)\n',
        "an unknown tag reaches no constructor, so a Compose override file reads as unparsable",
    )
    def test_unknown_tag(self):
        """Test that a value under a tag the parser does not know is read as if the tag were not there."""
        for case, contents, expected in (
            ("a scalar", "environment: !reset null\n", {"environment": None}),
            ("a sequence", "command: !override [serve]\n", {"command": ["serve"]}),
            ("a mapping", "environment: !override {DEBUG: '1'}\n", {"environment": {"DEBUG": "1"}}),
        ):
            with self.subTest(case=case):
                self.assertEqual(yaml.read(mock_path(contents)), yaml.Document(expected))

    @kills.replace(
        yaml,
        '_Loader.add_multi_constructor("!", _value_under_unknown_tag)\n',
        '_Loader.add_multi_constructor("", _value_under_unknown_tag)\n',
        "every tag reaches the constructor, so a tag naming a Python object is applied rather than refused",
    )
    def test_python_object_tag(self):
        """Test that a tag naming a Python object is refused, so reading a file constructs no arbitrary object."""
        self.assertIsNone(yaml.read(mock_path("!!python/object/apply:os.system ['echo']\n")))

    def test_character_the_parser_refuses(self):
        """Test that a file holding a character YAML does not allow, such as a bell, reads back as None."""
        self.assertIsNone(yaml.read(mock_path('{"name": "x\x07"}\n')))


class DocumentTest(unittest.TestCase):
    """Unit tests for walking a parsed YAML document."""

    # More nodes than any document below has, so a walk that does not end fails rather than hangs.
    MAX_NODES = 100

    def nodes_of(self, contents: str) -> list[yaml.Document]:
        """Return the nodes of the YAML document the contents parse into, at most `MAX_NODES` of them."""
        return list(itertools.islice(cast("yaml.Document", yaml.read(mock_path(contents))).nodes(), self.MAX_NODES))

    @kills.force_false(
        yaml.Document.nodes,
        "id(value) in returned_ids",
        "a mapping an alias names again is walked again, so a self-referencing anchor is walked without end",
    )
    def test_nodes_of_a_self_referencing_anchor(self):
        """Test that a node an alias refers back to is returned once, rather than walked without end."""
        nodes = self.nodes_of("a: &x\n  b: *x\n")
        self.assertEqual(len(nodes), 2)  # The document, and the mapping under `a` that holds itself under `b`.

    @kills.delete(
        yaml._children,
        "list(value) if isinstance(value, list) else ",
        "the items of a sequence are not walked, so a machine-executor image in a list is looked up on a registry",
    )
    @kills.delete(
        yaml.Document.nodes,
        "if isinstance(value, dict | list):\n                ",
        "the walk returns a repeated scalar, such as an interned string, only once",
    )
    def test_nodes_of_a_sequence(self):
        """Test that the items of a sequence are returned, a repeated scalar as often as the sequence holds it."""
        nodes = self.nodes_of("- a\n- a\n- [b]\n")
        self.assertEqual(sorted(string for node in nodes if (string := node.string()) is not None), ["a", "a", "b"])
        self.assertEqual(len(nodes), 5)  # The sequence, its three items, and the `b` in the one holding it.

    def test_a_merged_entry_is_keyed_where_the_merged_mapping_writes_it(self):
        """Test that a merged entry is a keyed value at its key in the merged mapping, and a text drops its quotes."""
        document = cast("yaml.Document", yaml.parse("base: &base\n  a: 1\nderived:\n  <<: *base\n  b: '2'\n"))
        keyed = [(each.key, each.text, each.line_number, each.column) for each in document["derived"].keyed_values()]
        self.assertEqual(keyed, [("a", "1", 2, 2), ("b", "2", 5, 2)])

    @kills.replace(
        yaml.Document.keyed_values,
        "if isinstance(self._value, _Mapping) else []",
        "if self._value is not None else []",
        "the keyed values of a sequence are read as those of a mapping, which stops the run",
        raises="AttributeError: 'list' object has no attribute 'keyed_values'",
    )
    def test_a_sequence_has_no_keyed_values(self):
        """Test that a sequence has no keyed values."""
        self.assertEqual(cast("yaml.Document", yaml.parse("- left-pad\n")).keyed_values(), [])
