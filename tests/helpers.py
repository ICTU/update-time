"""Test helpers that name nothing of this project's domain, so tests of any part of it can share them."""

import contextlib
import logging
import os
from logging import INFO
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast
from unittest.mock import Mock, patch

from tests.mutation import CHECKED_TEST, CHECKS_OFF

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping
    from unittest.mock import _patch, _patch_dict

_Decorated = TypeVar("_Decorated", bound="Callable[..., object]")


def mock_response(json: Mapping | list | str | None = None, **kwargs: object) -> Mock:
    """Return a mock requests Response whose .json() returns the given value.

    Extra response attributes (text, status_code, headers, ...) can be set via keyword arguments.
    """
    response = Mock(json=Mock(return_value=json))
    response.links = {}  # requests parses the Link header into `.links`; default to none, override per test.
    response.configure_mock(**kwargs)
    return response


def patch_get(json: Mapping | list | None = None, **kwargs: object) -> _patch:
    """Patch Session.get to return a mock requests Response whose .json() returns the given value.

    Extra response attributes (text, status_code, headers, ...) can be set via keyword arguments.
    """
    return patch("requests.Session.get", Mock(return_value=mock_response(json, **kwargs)))


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
        relative_to=Mock(return_value=Mock(parts=[])),
        read_text=Mock(return_value=content),
        read_bytes=Mock(return_value=content.encode()),
        __str__=lambda _: name,
    )
    if parent is not None:
        path.parent = parent
    return path


class FakeFiles:
    """A file system in memory, which holds the text of each file under the file's path."""

    def __init__(self, files: Mapping[str | Path, str] | None = None, *, failing_write: OSError | None = None) -> None:
        """Hold the files, and the error each write raises after leaving half its text behind, if any."""
        self.files = {str(path): text for path, text in (files or {}).items()}
        self._failing_write = failing_write

    def __getitem__(self, path: str | Path) -> str:
        """Return the text of the file."""
        return self.files[str(path)]

    @contextlib.contextmanager
    def patched(self) -> Generator[None]:
        """Stand in for the `Path` methods that look up, read, write, replace, and make files and folders."""
        with (
            patch.object(Path, "read_text", autospec=True, side_effect=self._read_text),
            patch.object(Path, "write_text", autospec=True, side_effect=self._write_text),
            patch.object(Path, "replace", autospec=True, side_effect=self._replace),
            patch.object(Path, "exists", autospec=True, side_effect=lambda path: str(path) in self.files),
            patch.object(Path, "mkdir", autospec=True),
        ):
            yield

    def _read_text(self, path: Path, *_arguments: object, **_options: object) -> str:
        if str(path) not in self.files:
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return self.files[str(path)]

    def _write_text(self, path: Path, text: str, *_arguments: object, **_options: object) -> int:
        if self._failing_write is not None:
            self.files[str(path)] = text[: len(text) // 2]
            raise self._failing_write
        self.files[str(path)] = text
        return len(text)

    def _replace(self, path: Path, target: Path) -> Path:
        self.files[str(target)] = self.files.pop(str(path))
        return target
