"""Write the files a tool generates, or name the ones whose content on disk is out of date."""

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _out_of_date(generated: dict[Path, str]) -> list[Path]:
    """Return the generated files whose content on disk is not what regenerating them produces."""
    return [path for path, content in generated.items() if not path.is_file() or path.read_text() != content]


def write_or_check(generated: dict[Path, str], root: Path, regenerate_with: str) -> None:
    """Write the generated files, or, given `--check`, exit naming the ones out of date without writing any."""
    if "--check" not in sys.argv[1:]:
        for path, content in generated.items():
            path.write_text(content)
        return
    if stale := _out_of_date(generated):
        names = ", ".join(str(path.relative_to(root)) for path in sorted(stale))
        sys.exit(f"{names} out of date, run `{regenerate_with}` to regenerate")
