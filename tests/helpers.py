"""Test helpers that name nothing of this project's domain, so tests of any part of it can share them."""

import contextlib
import logging
import os
import time
from collections.abc import MutableMapping
from logging import INFO
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast
from unittest.mock import Mock, patch

from tests.mutation import CHECKED_TEST, CHECKS_OFF

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
    from unittest.mock import _patch, _patch_dict

_Decorated = TypeVar("_Decorated", bound="Callable[..., object]")


def mock_response(json: Mapping | list | str | None = None, **kwargs: object) -> Mock:
    """Return a mock requests Response whose .json() returns the given value.

    Extra response attributes (text, status_code, headers, ...) can be set via keyword arguments.
    """
    response = Mock(json=Mock(return_value=json))
    response.links = {}  # requests parses the Link header into `.links`; default to none, override per test.
    response.headers = {}  # A real response always carries a mapping of headers; default to none, override per test.
    response.configure_mock(**kwargs)
    return response


def patch_get(json: Mapping | list | None = None, **kwargs: object) -> _patch:
    """Patch Session.get to return a mock requests Response whose .json() returns the given value.

    Extra response attributes (text, status_code, headers, ...) can be set via keyword arguments.
    """
    return patch("requests.Session.get", Mock(return_value=mock_response(json, **kwargs)))


def requested_urls(mock_get: Mock) -> list[str]:
    """Return the URLs the mock Session.get was asked for, in the order they were asked for."""
    return [call.args[0] for call in mock_get.call_args_list]


def patch_environ(environment_variables: dict[str, str] | None = None, *, clear: bool | None = None) -> _patch_dict:
    """Mock os.environ with the given environment variables.

    If none are given, clear the environment, unless overridden by an explicit clear=True or clear=False.
    """
    clear = not environment_variables if clear is None else clear
    in_dict = environment_variables or {}
    # Carry the mutation variables through a cleared environment, so the registered mutations run as they would
    # outside it.
    for sentinel in (CHECKS_OFF, CHECKED_TEST):
        if (value := os.environ.get(sentinel)) is not None:
            in_dict.setdefault(sentinel, value)
    return patch.dict("os.environ", in_dict, clear=clear)


@contextlib.contextmanager
def time_zone(name: str) -> Generator[None]:
    """Set the process's time zone, and restore the machine's own zone afterwards, whatever happens in between."""
    try:
        with patch_environ({"TZ": name}):
            time.tzset()
            yield
    finally:
        time.tzset()


def patch_pathlib_path(*methods: str, **methods_and_return_values: object) -> Callable[[_Decorated], _Decorated]:
    """Patch one or more pathlib.Path methods, each to return the given value, for the test's duration.

    Usable as a decorator on a test method or class (like `unittest.mock.patch`, by stacking one patch per method);
    it adds no mock argument. Each keyword names a pathlib.Path method and gives the value it should return, so
    several can be patched at once, for example `@patch_pathlib_path(exists=True, read_text="file contents")`.
    """

    def decorate(target: _Decorated) -> _Decorated:
        decorated: Callable[..., object] = target
        for method in methods:
            decorated = patch(f"pathlib.Path.{method}")(decorated)
        for method, return_value in methods_and_return_values.items():
            decorated = patch(f"pathlib.Path.{method}", Mock(return_value=return_value))(decorated)
        return cast("_Decorated", decorated)

    return decorate


def log_record(message: str, level: int = INFO) -> logging.LogRecord:
    """Return a log record carrying the message, as the logging machinery hands one to a filter or a handler."""
    return logging.LogRecord("update-time", level, "log.py", 1, message, args=None, exc_info=None)


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

    def _exists(self, path: Path) -> bool:
        return path in self._files or path in self._directories

    def _glob(self, path: Path, pattern: str, *_arguments: object, **_options: object) -> list[Path]:
        known = [*self._files, *self._directories]
        entries = {entry for name in known for entry in (name, *name.parents) if entry.is_relative_to(path)}
        return sorted(entry for entry in entries if entry != path and entry.relative_to(path).full_match(pattern))

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

    def _unlink(self, path: Path, **_options: object) -> None:
        self._files.pop(path, None)
