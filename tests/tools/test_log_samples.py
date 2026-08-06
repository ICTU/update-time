"""Unit tests for the sample log lines the README quotes."""

import logging
import unittest

from tools.log_samples import _Capture

from update_time.io.log import DEPENDENCY_DELIMITER, LOCATION_DELIMITER

from tests.helpers import log_record


class CaptureTest(unittest.TestCase):
    """Unit tests for collecting logged records as the blocks the README quotes."""

    def capture(self, *messages: str) -> _Capture:
        """Return a handler that has collected one record for each of the messages."""
        handler = _Capture()
        for message in messages:
            handler.emit(log_record(message, logging.WARNING))
        return handler

    def test_delimiters_are_dropped(self):
        """Test that a record renders as `LEVEL message`, without the delimiters only the highlighter reads."""
        dependency = f"{DEPENDENCY_DELIMITER}humanize{DEPENDENCY_DELIMITER}"
        location = f"{LOCATION_DELIMITER}docs/requirements.txt{LOCATION_DELIMITER}"
        collected = self.capture(f"Stale dependency {dependency} in {location}")
        self.assertEqual(collected.take(), "WARNING Stale dependency humanize in docs/requirements.txt")

    def test_taking_a_block_starts_the_next(self):
        """Test that taking a block empties the handler, so the next block holds only what followed it."""
        collected = self.capture("first", "second")
        self.assertEqual(collected.take(), "WARNING first\nWARNING second")
        self.assertEqual(collected.take(), "")
