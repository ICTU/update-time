"""Read the files Update-time reads whole, and rewrite the ones it changes line by line."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Self

from update_time.primitives.line import located_lines

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from update_time.io.log import Logger
    from update_time.primitives.line import Line


class File(ABC):
    """A file read whole, once, which `readable` passes over where the read fails."""

    @classmethod
    @abstractmethod
    def read(cls, path: Path) -> Self:
        """Return the file at the path, raising an `OSError` or a `ValueError` where it cannot be read."""

    @classmethod
    def readable(cls, path: Path, report: Callable[[Path, OSError | ValueError], None] | None = None) -> Self | None:
        """Return the file, or None where it cannot be read, after reporting why where a report is given."""
        try:
            return cls.read(path)
        except (OSError, ValueError) as error:
            if report is not None:
                report(path, error)
            return None


def rewrite_file(path: Path, transform: Callable[[list[Line]], list[str]], logger: Logger) -> list[Line]:
    """Write back the lines that `transform` makes of the file's lines where they changed, and return the new lines.

    Each line keeps its own ending, so a file's CRLF endings and a missing final newline survive the rewrite.
    """
    logger.path(path)
    old_lines = path.read_text().splitlines(keepends=True)
    new_lines = transform(located_lines(path, old_lines))
    if old_lines != new_lines:
        path.write_text("".join(new_lines))
    return located_lines(path, new_lines)
