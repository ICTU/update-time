"""Generate docs/architecture.md, which draws the layers of the package and the dependencies between them."""

from pathlib import Path
from typing import TYPE_CHECKING

from archunitpython.graph import project_graph

from tools.generated_files import write_or_check

if TYPE_CHECKING:
    from collections.abc import Iterable

    from archunitpython.graph.graph_reporter import GraphReportEdge

_ROOT = Path(__file__).parents[1]
_ARCHITECTURE = _ROOT / "docs" / "architecture.md"

# The package whose folders are the layers, and the folder depth that collapses each module into its layer.
_PACKAGE = "update_time"
_LAYER_DEPTH = 2

_INTRODUCTION = (
    "# Architecture\n\n"
    "The package has a folder per layer. An arrow points from a layer to a layer it imports. The architecture tests in "
    "`tests/update_time/test_architecture.py` let a layer import only the layers ranked before it. Run "
    "`just architecture` to regenerate this document.\n"
)


def _layer(label: str) -> str:
    """Return the layer a folder of the package is, named without the package."""
    return label.removeprefix(f"{_PACKAGE}/")


def diagram(edges: Iterable[GraphReportEdge]) -> str:
    """Return the Mermaid flowchart of the layers, top down, with an arrow from each layer to each layer it imports."""
    arrows = [f"  {_layer(edge.source)} --> {_layer(edge.target)}" for edge in edges]
    return "\n".join(["flowchart TD", *arrows]) + "\n"


def _render() -> dict[Path, str]:
    """Return the document, drawn from the imports between the layers of the package's code."""
    snapshot = project_graph(str(_ROOT / "src")).collapse_to_folder_depth(_LAYER_DEPTH).snapshot()
    return {_ARCHITECTURE: f"{_INTRODUCTION}\n```mermaid\n{diagram(snapshot.edges)}```\n"}


def main() -> None:
    """Write the document, or, given `--check`, report it when it is out of date without writing it."""
    write_or_check(_render(), _ROOT, "just architecture")


if __name__ == "__main__":  # pragma: no cover
    main()
