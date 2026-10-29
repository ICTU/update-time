"""Unit tests for the YAML file format, with file I/O mocked."""

import itertools
import unittest
from typing import cast

from update_time.formats import yaml

from tests.helpers import mock_path
from tests.mutation import Mutation, kills


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
                self.assertEqual(yaml.read(mock_path(contents)), yaml.Document(expected))

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
        self.assertIsNone(yaml.read(mock_path("!!python/object/apply:os.system ['echo']\n")))


class DocumentTest(unittest.TestCase):
    """Unit tests for walking a parsed YAML document."""

    # More nodes than any document below has, so a walk that does not end fails rather than hangs.
    MAX_NODES = 100

    def nodes_of(self, contents: str) -> list[yaml.Document]:
        """Return the nodes of the YAML document the contents parse into, at most `MAX_NODES` of them."""
        return list(itertools.islice(cast("yaml.Document", yaml.read(mock_path(contents))).nodes(), self.MAX_NODES))

    @kills(
        Mutation(
            yaml.Document.nodes,
            "if id(value) in returned_ids:\n",
            "if False:\n",
            "a mapping an alias names again is walked again, so a self-referencing anchor is walked without end",
        )
    )
    def test_nodes_of_a_self_referencing_anchor(self):
        """Test that a node an alias refers back to is returned once, rather than walked without end."""
        nodes = self.nodes_of("a: &x\n  b: *x\n")
        self.assertEqual(len(nodes), 2)  # The document, and the mapping under `a` that holds itself under `b`.

    @kills(
        Mutation(
            yaml._children,
            "return list(value) if isinstance(value, list) else []\n",
            "return []\n",
            "the items of a sequence are not walked, so a machine-executor image in a list is looked up on a registry",
        ),
        Mutation(
            yaml.Document.nodes,
            "if isinstance(value, dict | list):\n                returned_ids.add(id(value))\n",
            "returned_ids.add(id(value))\n",
            "the walk returns a repeated scalar, such as an interned string, only once",
        ),
    )
    def test_nodes_of_a_sequence(self):
        """Test that the items of a sequence are returned, a repeated scalar as often as the sequence holds it."""
        nodes = self.nodes_of("- a\n- a\n- [b]\n")
        self.assertEqual(sorted(string for node in nodes if (string := node.string()) is not None), ["a", "a", "b"])
        self.assertEqual(len(nodes), 5)  # The sequence, its three items, and the `b` in the one holding it.
