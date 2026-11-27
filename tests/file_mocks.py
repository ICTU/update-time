"""Mocks of the files a run reads and writes: a mock path, a file system in memory, and patches of `pathlib.Path`."""

import contextlib
from collections.abc import MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast
from unittest.mock import Mock, patch

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Iterator, Mapping


_Decorated = TypeVar("_Decorated", bound="Callable[..., object]")


def patch_pathlib_path(*methods: str, **methods_and_return_values: object) -> Callable[[_Decorated], _Decorated]:
    """Patch pathlib.Path methods for the test's duration, as a decorator on a test method or class.

    A method named alone is patched with a mock that the test receives as an argument, as `unittest.mock.patch`
    passes one. A keyword names a method and the value it returns, and does not add an argument. So
    `@patch_pathlib_path("rglob", exists=True)` passes the test one mock, for `rglob`.
    """

    def decorate(target: _Decorated) -> _Decorated:
        decorated = target
        for method in methods:
            decorated = cast("_Decorated", patch(f"pathlib.Path.{method}")(decorated))
        for method, return_value in methods_and_return_values.items():
            decorated = cast("_Decorated", patch(f"pathlib.Path.{method}", Mock(return_value=return_value))(decorated))
        return decorated

    return decorate


def mock_path(content: str, parent: Path | None = None, name: str = "f.py") -> Mock:
    """Return a mock Path with the given content and name, an optional parent, and a no-op relative_to()."""
    path = Mock(
        parts=(*(parent.parts if parent is not None else ()), name),
        relative_to=Mock(return_value=Mock(parts=[])),
        read_text=Mock(return_value=content),
        read_bytes=Mock(return_value=content.encode()),
        __str__=lambda _: name,
    )
    if parent is not None:
        path.parent = parent
    return path


class FakeFiles(MutableMapping[Path, str]):
    """A file system in memory, which maps the path of each file to its text, and holds the directories."""

    def __init__(
        self,
        files: Mapping[Path, str] | None = None,
        *,
        directories: Iterable[Path] = (),
        failing_write: OSError | None = None,
    ) -> None:
        """Hold the files, the directories, and the error each write raises after leaving half its text behind."""
        self._files = dict(files or {})
        self.read_errors: dict[Path, Exception] = {}  # The error reading a file raises, by the file's path.
        self.write_errors: dict[Path, Exception] = {}  # The error writing a file raises, by the file's path.
        self.remove_errors: dict[Path, Exception] = {}  # The error removing a file raises, by the file's path.
        self.written_paths: list[Path] = []  # The paths of the files written to, in the order of the writes.
        self._directories = set(directories)
        self._failing_write = failing_write

    def __getitem__(self, path: Path) -> str:
        """Return the text of the file."""
        return self._files[path]

    def __setitem__(self, path: Path, text: str) -> None:
        """Hold the text as the file's."""
        self._files[path] = text

    def __delitem__(self, path: Path) -> None:
        """Remove the file."""
        del self._files[path]

    def __iter__(self) -> Iterator[Path]:
        """Return an iterator over the paths of the files."""
        return iter(self._files)

    def __len__(self) -> int:
        """Return the number of files."""
        return len(self._files)

    def replace_all(self, files: Mapping[Path, str]) -> None:
        """Hold the files in place of the ones held before."""
        self._files = dict(files)

    @contextlib.contextmanager
    def patched(self) -> Generator[None]:
        """Stand in for the `Path` methods that find, read, write, replace, and remove files, and make folders."""
        with (
            patch.object(Path, "read_text", autospec=True, side_effect=self._read_text),
            patch.object(Path, "write_text", autospec=True, side_effect=self._write_text),
            patch.object(Path, "replace", autospec=True, side_effect=self._replace),
            patch.object(Path, "unlink", autospec=True, side_effect=self._unlink),
            patch.object(Path, "exists", autospec=True, side_effect=self._exists),
            patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path in self._files),
            patch.object(Path, "mkdir", autospec=True),
            patch.object(Path, "glob", autospec=True, side_effect=self._glob),
        ):
            yield

    def _read_text(self, path: Path, *_arguments: object, **_options: object) -> str:
        if (error := self.read_errors.get(path)) is not None:
            raise error
        if path not in self._files:
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return self._files[path]

    def _write_text(self, path: Path, text: str, *_arguments: object, **_options: object) -> int:
        if (error := self.write_errors.get(path)) is not None:
            raise error
        self.written_paths.append(path)
        if self._failing_write is not None:
            self._files[path] = text[: len(text) // 2]
            raise self._failing_write
        self._files[path] = text
        return len(text)

    def _replace(self, path: Path, target: Path) -> Path:
        self._files[target] = self._files.pop(path)
        return target

    def _unlink(self, path: Path, *, missing_ok: bool = False) -> None:
        if (error := self.remove_errors.get(path)) is not None:
            raise error
        if path not in self._files and not missing_ok:
            raise FileNotFoundError(2, "No such file or directory", str(path))
        self._files.pop(path, None)

    def _exists(self, path: Path) -> bool:
        return path in self._files or path in self._directories

    def _glob(self, path: Path, pattern: str, *_arguments: object, **_options: object) -> list[Path]:
        known = [*self._files, *self._directories]
        entries = {entry for name in known for entry in (name, *name.parents) if entry.is_relative_to(path)}
        return sorted(entry for entry in entries if entry != path and entry.relative_to(path).full_match(pattern))
