"""Unit tests for generating the architecture document from the dependencies between the layers."""

import re
import sys
import unittest
from typing import Final
from unittest.mock import Mock, patch

from archunitpython.graph.graph_reporter import GraphReportEdge
from tools import generate_architecture
from tools.generate_architecture import _diagram, main

from mutation_time.mutation import kills

_TEST_EACH_LAYER_IS_NAMED_ONCE_WITHOUT_THE_PACKAGE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b[a-z_]+\b")


class DiagramTest(unittest.TestCase):
    """Unit tests for drawing the layers and the dependencies between them."""

    def test_each_layer_is_named_once_without_the_package(self):
        """Test that each layer an edge ends at is named once, without the package it sits in."""
        drawn = _diagram([_edge("io", "domain"), _edge("domain", "primitives")])
        self.assertEqual(
            sorted(set(_TEST_EACH_LAYER_IS_NAMED_ONCE_WITHOUT_THE_PACKAGE_PATTERN.findall(drawn)) - {"flowchart"}),
            ["domain", "io", "primitives"],
        )

    @kills.append(
        generate_architecture._diagram,
        'arrows = [f"  {_layer(edge.source)} -->',
        "|{edge.count}|",
        "each arrow carries its count of imports, so a new import between two layers leaves the document stale",
    )
    def test_an_edge_is_one_arrow_without_its_count_of_imports(self):
        """Test that the layers one layer imports from another get one arrow, which does not count the imports."""
        self.assertEqual(_diagram([_edge("io", "domain", count=13)]), "flowchart TD\n  io --> domain\n")


def _edge(source: str, target: str, count: int = 1) -> GraphReportEdge:
    """Return an edge between two folders of the package, as the graph reports it."""
    return GraphReportEdge(f"update_time/{source}", f"update_time/{target}", count, external=False, import_kinds=())


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
