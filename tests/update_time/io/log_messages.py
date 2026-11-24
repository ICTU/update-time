"""The log messages a test expects, looked up on the logger's methods, and the extra a log call carries."""

from typing import TYPE_CHECKING

from update_time.io.console import MESSAGE_NAME
from update_time.io.log import (
    Logger,
    _Fields,  # simplint: ignore[private-import-path] # annotates with it
    _LogMessage,  # simplint: ignore[private-import-path] # annotates with it
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from update_time.domain.reference import DriftedPin


def drift_warning(drifted: DriftedPin) -> tuple[_LogMessage, _Fields]:
    """Return the warning about the drift, and the fields it carries."""
    fields = Logger._drift_warning_fields(drifted)
    return message_of(Logger.drift, *fields), fields


def message_of(log_method: Callable[..., None], *holes: str) -> _LogMessage:
    """Return the log method's message that has the holes, the one with these holes alone where several have them."""
    messages: list[_LogMessage] = getattr(log_method, "messages", [])
    having = [message for message in messages if message.holes >= set(holes)]
    (message,) = having if len(having) == 1 else [message for message in having if message.holes == set(holes)]
    return message


def adopted_drift_report(drifted: DriftedPin, directive: str) -> tuple[_LogMessage, _Fields]:
    """Return the report that the drift was adopted, and the fields it carries."""
    return message_of(Logger.adopted_drift), Logger._adopted_drift_fields(drifted, directive)


def logged_extra(log_kwargs: Mapping[str, _Fields]) -> _Fields:
    """Return the extra a log call passed beside the fields, without the name of its message, which the message has."""
    return {name: value for name, value in log_kwargs.get("extra", {}).items() if name != MESSAGE_NAME}
