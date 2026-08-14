"""Unit tests for rendering the log-output screenshot."""

import logging
import unittest
from datetime import datetime
from unittest.mock import patch

from tools.generate_log_svg import LogOutput, _FixedTime, _portable, generate

from tests.helpers import log_record


class FixedTimeTest(unittest.TestCase):
    """Unit tests for the timestamp the screenshot is pinned to."""

    def test_timestamp_is_pinned(self):
        """Test that the record's time is overwritten with the one the screenshot shows."""
        pinned = log_record("A message")
        _FixedTime().filter(pinned)
        self.assertEqual(datetime.fromtimestamp(pinned.created).strftime("%H:%M:%S"), "09:14:03")  # noqa: DTZ006

    def test_record_is_kept(self):
        """Test that the filter keeps the record, so pinning the time drops no line from the screenshot."""
        self.assertTrue(_FixedTime().filter(log_record("A message")))


class PortableSvgTest(unittest.TestCase):
    """Unit tests for the tweaks that make Rich's SVG render where the README embeds it."""

    # Markup rather than prose, so it is written raw and the sentence complexity check leaves it alone.
    RICH_SVG = (
        r'<svg class="rich-terminal" viewBox="0 0 994 670">'
        r"<style>@font-face { font-family: 'Fira Code'; src: local('FiraCode-Regular'),"
        r" url('https://cdn.example/FiraCode.woff2') format('woff2'); }</style></svg>"
    )

    def test_sized_from_its_viewbox(self):
        """Test that the viewBox's size is copied onto the `<svg>` element, which Rich emits without one."""
        self.assertIn(r'<svg class="rich-terminal" width="994" height="670"', _portable(self.RICH_SVG))

    def test_cdn_font_source_dropped(self):
        """Test that a font's external source is dropped and its local one kept."""
        portable = _portable(self.RICH_SVG)
        self.assertIn(r"src: local('FiraCode-Regular');", portable)
        self.assertNotIn("cdn.example", portable)


class GenerateTest(unittest.TestCase):
    """Unit tests for the sample log output the README embeds."""

    def generated(self) -> LogOutput:
        """Return the generated output, which the generator can only produce while the root logger has no handlers."""
        with patch.object(logging.getLogger(), "handlers", []):
            return generate()

    def test_text_holds_every_sample(self):
        """Test that the fallback text holds each line the README quotes, at the pinned timestamp."""
        text = self.generated().text
        for quoted in ("09:14:03", "humanize", "3.14.6", "actions/checkout", "512 days"):
            with self.subTest(quoted=quoted):
                self.assertIn(quoted, text)

    def test_trailing_padding_is_stripped(self):
        """Test that no line keeps the padding the render adds, which would trail spaces through the README."""
        self.assertNotIn(" \n", self.generated().text)

    def test_svg_is_portable(self):
        """Test that the screenshot is the portable form, sized so it renders where the README embeds it."""
        self.assertIn('<svg class="rich-terminal" width="', self.generated().svg)
