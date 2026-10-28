"""Unit tests for generating the architecture document from the dependencies between the layers."""

import re
import sys
import unittest
from unittest.mock import Mock, patch

from archunitpython.graph.graph_reporter import GraphReportEdge
from tools import generate_architecture
from tools.generate_architecture import diagram, main

from tests.mutation import Mutation, kills


def _edge(source: str, target: str, count: int = 1) -> GraphReportEdge:
    """Return an edge between two folders of the package, as the graph reports it."""
    return GraphReportEdge(f"update_time/{source}", f"update_time/{target}", count, external=False, import_kinds=())


class DiagramTest(unittest.TestCase):
    """Unit tests for drawing the layers and the dependencies between them."""

    def test_each_layer_is_named_once_without_the_package(self):
        """Test that each layer an edge ends at is named once, without the package it sits in."""
        drawn = diagram([_edge("io", "domain"), _edge("domain", "primitives")])
        self.assertEqual(sorted(set(re.findall(r"\b[a-z_]+\b", drawn)) - {"flowchart"}), ["domain", "io", "primitives"])

    @kills(
        Mutation(
            generate_architecture.diagram,
            'arrows = [f"  {_layer(edge.source)} --> {_layer(edge.target)}" for edge in edges]',
            'arrows = [f"  {_layer(edge.source)} -->|{edge.count}| {_layer(edge.target)}" for edge in edges]',
            "each arrow carries its count of imports, so a new import between two layers leaves the document stale",
        )
    )
    def test_an_edge_is_one_arrow_without_its_count_of_imports(self):
        """Test that the layers one layer imports from another get one arrow, which does not count the imports."""
        self.assertEqual(diagram([_edge("io", "domain", count=13)]), "flowchart TD\n  io --> domain\n")


@patch("tools.generate_architecture.project_graph")
class MainTest(unittest.TestCase):
    """Unit tests for regenerating the architecture document from the project's dependencies."""

    def test_stale_document_is_named_with_the_recipe_that_regenerates_it(self, project_graph: Mock):
        """Test that the document is drawn from the project's dependencies, and named with `just architecture`."""
        project_graph.return_value.collapse_to_folder_depth.return_value.snapshot.return_value.edges = (
            _edge("io", "domain"),
        )
        with (
            patch.object(sys, "argv", ["generate_architecture", "--check"]),
            patch("pathlib.Path.is_file", Mock(return_value=False)),
            self.assertRaises(SystemExit) as raised,
        ):
            main()
        self.assertIn("docs/architecture.md out of date, run `just architecture` to regenerate", str(raised.exception))
