"""Generate docs/architecture.md, which draws the layers of the package and the dependencies between them."""

from pathlib import Path
from typing import TYPE_CHECKING, Final

from archunitpython.graph import project_graph

from tools.generated_files import write_or_check

if TYPE_CHECKING:
    from collections.abc import Iterable

    from archunitpython.graph.graph_reporter import GraphReportEdge

_ROOT: Final[Path] = Path(__file__).parents[1]
_ARCHITECTURE: Final[Path] = _ROOT / "docs" / "architecture.md"

# The package whose folders are the layers, and the folder depth that collapses each module into its layer.
_PACKAGE: Final[str] = "update_time"
_LAYER_DEPTH: Final[int] = 2

_INTRODUCTION: Final[str] = (
    "# Architecture\n\n"
    "The package has a folder per layer. An arrow points from a layer to a layer it imports. The architecture tests in "
    "`tests/update_time/test_architecture.py` let a layer import only the layers ranked before it. Run "
    "`just architecture` to regenerate this document.\n"
)


def _layer(label: str) -> str:
    """Return the layer a folder of the package is, named without the package."""
    return label.removeprefix(f"{_PACKAGE}/")


def _diagram(edges: Iterable[GraphReportEdge]) -> str:
    """Return the Mermaid flowchart of the layers, top down, with an arrow from each layer to each layer it imports."""
    arrows = [f"  {_layer(edge.source)} --> {_layer(edge.target)}" for edge in edges]
    return f"{'\n'.join(['flowchart TD', *arrows])}\n"


def main() -> None:
    """Write the document, or, given `--check`, report it when it is out of date without writing it."""
    snapshot = project_graph(str(_ROOT / "src")).collapse_to_folder_depth(_LAYER_DEPTH).snapshot()
    document = f"{_INTRODUCTION}\n```mermaid\n{_diagram(snapshot.edges)}```\n"
    write_or_check({_ARCHITECTURE: document}, _ROOT, "just architecture")


if __name__ == "__main__":
    main()
