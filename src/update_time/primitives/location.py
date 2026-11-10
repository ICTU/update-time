"""A place in a file: which file, which line of it, and where on that line."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Location:
    """The location of a reference and/or marker: a file path, a 1-based line number, and a 0-based column.

    A location whose line is not known names the file alone. A location read off a line leaves the column at the
    line's start, since the whole of that line carries the reference. A location a file format resolves for one
    entry names the column too, so an entry sharing its line with another is told apart from it.
    """

    path: Path
    line_number: int | None = None
    column: int = 0

    def __str__(self) -> str:
        """Render the location as the relative `path`, or `path:line` when it carries a line number."""
        relative = self.relative()
        return f"{relative}:{self.line_number}" if self.line_number is not None else str(relative)

    def is_on_the_same_line_as(self, other: Location) -> bool:
        """Return whether this location sits on the same line of the same file as the other one.

        The comparison leaves the columns out.
        """
        return (self.path, self.line_number) == (other.path, other.line_number)

    def relative(self) -> Path:
        """Return the path relative to the working directory, walking up with `..` to a path outside it.

        A path that is relative already stays as it is, and so does one that shares only the root with the working
        directory.
        """
        cwd = Path.cwd()
        if self.path.is_absolute() and (cwd == Path(cwd.anchor) or self.path.parts[:2] == cwd.parts[:2]):
            return self.path.relative_to(cwd, walk_up=True)
        return self.path
