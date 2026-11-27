"""The files the scan of an updater finds."""

from pathlib import PurePath
from typing import TYPE_CHECKING, Final
from unittest.mock import patch

from update_time.io.filesystem import glob_for

from tests.update_time.fixtures import SCAN_ROOT

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.file_type import FileType


# The pyproject.toml at the root of the scan.
PYPROJECT_TOML: Final[Path] = SCAN_ROOT / "pyproject.toml"


def scanned_files(file_type: FileType, *tree: str) -> list[str]:
    """Return the files of the tree that the scan for the file type finds, once for each pattern that keeps the file."""

    def glob(self: Path, pattern: str, *, case_sensitive: bool | None = None, **_: object) -> list[Path]:
        return [self / path for path in tree if PurePath(path).full_match(pattern, case_sensitive=case_sensitive)]

    with patch("pathlib.Path.cwd", return_value=SCAN_ROOT), patch("pathlib.Path.glob", autospec=True, side_effect=glob):
        return [str(path.relative_to(SCAN_ROOT)) for path in glob_for(file_type)]
