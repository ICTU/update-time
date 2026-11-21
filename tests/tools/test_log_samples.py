"""Unit tests for the sample log lines the README and the index of the log messages show."""

import ast
import logging
import pathlib
import unittest

from tools.log_samples import _Capture

from mutation_time.mutation import kills
from update_time.io.console import MESSAGE_NAME, delimit_dependency, delimit_location

from tests.helpers import log_record

# The generators that build the sample log lines: the README's console blocks and screenshot, and the index's samples.
_SAMPLE_GENERATORS = ("tools/log_samples.py", "tools/generate_log_svg.py")


def _names_a_line(location: ast.Call) -> bool:
    """Return whether the `Location(...)` call names a line number, whether positionally or by keyword."""
    return bool(location.args[1:]) or any(keyword.arg == "line_number" for keyword in location.keywords)


def _sample_locations() -> list[ast.Call]:
    """Return every `Location(...)` the sample generators build."""
    return [
        node
        for generator in _SAMPLE_GENERATORS
        for node in ast.walk(ast.parse(pathlib.Path(generator).read_text()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Location"
    ]


class SampleLocationTest(unittest.TestCase):
    """Unit tests for the locations the sample generators build."""

    def test_every_sample_location_names_a_line(self):
        """Test that each location the samples build carries a line number."""
        locations = _sample_locations()
        self.assertNotEqual(locations, [])  # An empty scan would pass the check below without checking anything.
        self.assertEqual([ast.unparse(location) for location in locations if not _names_a_line(location)], [])


def _named_record(message: str, name: str) -> logging.LogRecord:
    """Return an info record carrying the message and the name of its log message, as the logger emits one."""
    record = log_record(message, logging.INFO)
    setattr(record, MESSAGE_NAME, name)
    return record


class CaptureTest(unittest.TestCase):
    """Unit tests for collecting logged records as the README's blocks and the index's samples."""

    def capture(self, *messages: str) -> _Capture:
        """Return a handler that has collected one record for each of the messages."""
        handler = _Capture()
        for message in messages:
            handler.emit(log_record(message, logging.WARNING))
        return handler

    def test_delimiters_are_dropped(self):
        """Test that a record renders as `LEVEL message`, without the delimiters only the highlighter reads."""
        dependency = delimit_dependency("humanize")
        location = delimit_location("docs/requirements.txt")
        collected = self.capture(f"Stale dependency {dependency} in {location}")
        self.assertEqual(collected.take(), "WARNING Stale dependency humanize in docs/requirements.txt")

    @kills.replace(
        _Capture.emit,
        "self.samples.setdefault(name, line)",
        "self.samples[name] = line",
        "the index shows the last sample logged for a name rather than the first",
    )
    def test_the_first_line_collected_for_a_name_is_its_sample(self):
        """Test that the handler keeps the first line it collected for each name, and none for a record without one."""
        handler = _Capture()
        handler.emit(_named_record("Pinned python to 3.14.7", "pinned-to-version"))
        handler.emit(log_record("Checking if there are updates for Dockerfile", logging.DEBUG))
        handler.emit(_named_record("Pinned node to 26.7.0", "pinned-to-version"))
        handler.take()
        handler.emit(_named_record("New version available for humanize", "new-version"))
        expected = {
            "pinned-to-version": "INFO Pinned python to 3.14.7 (pinned-to-version)",
            "new-version": "INFO New version available for humanize (new-version)",
        }
        self.assertEqual(handler.samples, expected)

    def test_taking_a_block_starts_the_next(self):
        """Test that taking a block empties the handler, so the next block holds only what followed it."""
        collected = self.capture("first", "second")
        self.assertEqual(collected.take(), "WARNING first\nWARNING second")
        self.assertEqual(collected.take(), "")
