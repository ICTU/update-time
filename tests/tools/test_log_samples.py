"""Unit tests for the sample log lines the README quotes."""

import ast
import logging
import pathlib
import unittest

from tools.log_samples import _Capture

from update_time.io.log import DEPENDENCY_DELIMITER, LOCATION_DELIMITER

from tests.helpers import log_record

# The generators that build the samples the README shows: its console blocks, and its log-output screenshot.
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
