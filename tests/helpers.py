"""Test helpers that name nothing of this project's domain, so tests of any part of it can share them."""

import logging
from logging import INFO
from typing import TYPE_CHECKING, TypeVar, cast
from unittest.mock import Mock, patch

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from unittest.mock import _patch, _patch_dict

_Decorated = TypeVar("_Decorated", bound="Callable[..., object]")


def mock_response(json: Mapping | list | None = None, **kwargs: object) -> Mock:
    """Return a mock requests Response whose .json() returns the given value.

    Extra response attributes (text, status_code, headers, ...) can be set via keyword arguments.
    """
    response = Mock(json=Mock(return_value=json))
    response.links = {}  # requests parses the Link header into `.links`; default to none, override per test.
    response.configure_mock(**kwargs)
    return response


def patch_get(json: Mapping | list | None = None, **kwargs: object) -> _patch:
    """Patch requests.get to return a mock requests Response whose .json() returns the given value.

    Extra response attributes (text, status_code, headers, ...) can be set via keyword arguments.
    """
    return patch("requests.get", Mock(return_value=mock_response(json, **kwargs)))


def patch_environ(environment_variables: dict[str, str] | None = None, *, clear: bool | None = None) -> _patch_dict:
    """Mock os.environ with the given environment variables.

    If none are given, clear the environment, unless overridden by an explicit clear=True or clear=False.
    """
    clear = not environment_variables if clear is None else clear
    return patch.dict("os.environ", environment_variables or {}, clear=clear)


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


def mock_path(content: str, parent: Path | None = None) -> Mock:
    """Return a mock Path with the given text content, an optional parent, and a no-op relative_to()."""
    path = Mock(relative_to=Mock(return_value=Mock(parts=[])), read_text=Mock(return_value=content))
    if parent is not None:
        path.parent = parent
    return path
