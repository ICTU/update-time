"""Build the log records the logging machinery hands to a handler or a filter."""

import logging
from logging import INFO


def log_record(message: str, level: int = INFO) -> logging.LogRecord:
    """Return a log record carrying the message, as the logging machinery hands one to a filter or a handler."""
    return logging.LogRecord("update-time", level, "log.py", 1, message, args=None, exc_info=None)
