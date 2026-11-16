"""Unit tests for generating the index of the log messages."""

import unittest

from tools import log_index as log_index_module

from mutation_time.mutation import Mutation, kills

_SAMPLE = "INFO New version available for humanize in docs/requirements.txt:12: 4.15.0 (new-version)"


class LogIndexTest(unittest.TestCase):
    """Unit tests for filling in the template of the index."""

    def assert_stops(self, template: str, names: list[str], reason: str) -> None:
        """Assert that generating the index from the template, with a sample of `new-version` alone, stops."""
        with self.assertRaises(SystemExit) as raised:
            log_index_module._log_index(template, {"new-version": _SAMPLE}, names)
        self.assertEqual(str(raised.exception), reason)

    @kills(
        Mutation(
            log_index_module._entry,
            '\\n\\n```console\\n{sample}\\n```"',
            '\\n\\n{sample}"',
            "a sample is shown as prose rather than as console output, so Markdown reflows it",
        )
    )
    def test_a_placeholder_becomes_the_heading_and_the_sample_of_its_message(self):
        """Test that a message's placeholder becomes a heading naming the message, and a console block of its sample."""
        template = "@@new-version@@\n\nUpdate-time found a newer version.\n"
        expected = f"### new-version\n\n```console\n{_SAMPLE}\n```\n\nUpdate-time found a newer version.\n"
        self.assertEqual(log_index_module._log_index(template, {"new-version": _SAMPLE}, ["new-version"]), expected)

    @kills(
        Mutation(
            log_index_module._stop_unless_complete,
            "names - placed.keys(),",
            "set() - placed.keys(),",
            "the index leaves out a log message without saying so",
        )
    )
    def test_a_message_without_a_placeholder_stops_the_generator(self):
        """Test that the generator stops, naming each log message the template has no placeholder for."""
        self.assert_stops(
            "@@new-version@@\n",
            ["new-version", "pinned-to-hash", "pinned-to-version"],
            "docs/log-messages.md.in has no placeholder for these log messages: pinned-to-hash, pinned-to-version",
        )

    @kills(
        Mutation(
            log_index_module._stop_unless_complete,
            "count > 1",
            "count > 99",
            "a message gets two entries in the index, and the console links to the first alone",
        )
    )
    def test_a_placeholder_appearing_twice_stops_the_generator(self):
        """Test that the generator stops, naming each message the template has more than one placeholder for."""
        self.assert_stops(
            "@@new-version@@\n@@new-version@@\n",
            ["new-version"],
            "docs/log-messages.md.in has more than one placeholder for these log messages: new-version",
        )

    def test_a_placeholder_naming_no_message_stops_the_generator(self):
        """Test that the generator stops, naming each placeholder of the template that does not name a log message."""
        self.assert_stops(
            "@@new-version@@\n@@pinned-to-tag@@\n@@newer-version@@\n",
            ["new-version"],
            "docs/log-messages.md.in has placeholders that do not name a log message: newer-version, pinned-to-tag",
        )

    @kills(
        Mutation(
            log_index_module._stop_on_dead_links,
            "set(_README_LINK.findall(template))",
            "set()",
            "the index links to a README section that a reworded heading took away, and nothing says so",
        )
    )
    def test_a_link_to_a_section_the_readme_lacks_stops_the_generator(self):
        """Test that the generator stops, naming each anchor the template links to that the README does not provide."""
        template = "See [a](../README.md#pinning), [b](../README.md#cooldown), and [c](../README.md#markers-1).\n"
        with self.assertRaises(SystemExit) as raised:
            log_index_module._stop_on_dead_links(template, "## Pinning\n\n## Markers\n")
        expected = "docs/log-messages.md.in links to README sections that do not exist: #cooldown, #markers-1"
        self.assertEqual(str(raised.exception), expected)

    def test_a_message_without_a_sample_stops_the_generator(self):
        """Test that the generator stops, naming each log message that has no sample."""
        self.assert_stops(
            "@@new-version@@\n@@pinned-to-version@@\n@@pinned-to-hash@@\n",
            ["new-version", "pinned-to-version", "pinned-to-hash"],
            "tools/log_samples.py does not log a sample of these log messages: pinned-to-hash, pinned-to-version",
        )
