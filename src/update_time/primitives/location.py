"""A place in a file: which file, and optionally which line of it."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Location:
    """The location of a reference and/or marker: a file path and, optionally, a 1-based line number within it."""

    path: Path
    line_number: int | None = None

    def __str__(self) -> str:
        """Render the location as the relative `path`, or `path:line` when it carries a line number."""
        relative = self.relative()
        return f"{relative}:{self.line_number}" if self.line_number is not None else str(relative)

    def relative(self) -> Path:
        """Return the path relative to the working directory, or its absolute self when it sits outside it.

        Most logged paths are files under the scan root (which is the working directory), but some — such as a uv
        workspace root above the current member — are not, so fall back to the absolute path rather than raising.
        """
        try:
            return self.path.relative_to(Path.cwd())
        except ValueError:
            return self.path
