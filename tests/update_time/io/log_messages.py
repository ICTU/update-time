"""The log messages a test expects, looked up on the logger's methods, and the extra a log call carries."""

from dataclasses import asdict
from typing import TYPE_CHECKING, cast

from update_time.io.console import MESSAGE_NAME
from update_time.io.log import (
    Logger,
    _Fields,  # simplint: ignore[private-import-path] # annotates with it
    _LogMessage,  # simplint: ignore[private-import-path] # annotates with it
    _Unanswered,  # simplint: ignore[private-import-path] # annotates with it
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from update_time.domain.reference import DriftedPin
    from update_time.io.log import _HoldsMessage  # simplint: ignore[private-import-path] # annotates with it


def drift_warning(drifted: DriftedPin) -> tuple[_LogMessage, _Fields]:
    """Return the warning about the drift, and the fields it carries."""
    return message_of(Logger.drift), Logger._drift_warning_fields(drifted)


def message_of(log_method: Callable[..., None]) -> _LogMessage:
    """Return the message the log method logs."""
    return cast("_HoldsMessage", log_method).message


def unknown_version_date(unanswered: _Unanswered, **fields: str) -> tuple[_LogMessage, _Fields]:
    """Return the error that a source did not answer for a candidate, and the fields it carries."""
    return message_of(Logger.unknown_version_date), {**fields, **asdict(unanswered)}


def adopted_drift_report(drifted: DriftedPin, directive: str) -> tuple[_LogMessage, _Fields]:
    """Return the report that the drift was adopted, and the fields it carries."""
    return message_of(Logger.adopted_drift), Logger._adopted_drift_fields(drifted, directive)


def logged_extra(log_kwargs: Mapping[str, _Fields]) -> _Fields:
    """Return the extra a log call passed beside the fields, without the name of its message, which the message has."""
    return {name: value for name, value in log_kwargs.get("extra", {}).items() if name != MESSAGE_NAME}
