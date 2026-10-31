"""Helpers for the package manager tests."""

from subprocess import CalledProcessError  # nosec
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from tests.update_time.helpers import LoggingTestCase

if TYPE_CHECKING:
    from update_time.primitives.command import Command

# The comment Update-time writes beside a cutoff it manages, spelled out so the tests pin the wording.
MANAGED_BY_COMMENT = "managed by Update-time — remove this comment to prevent Update-time from changing it"

# How `uv --show-settings` prints an `exclude-newer` that is set.
CONFIGURED_EXCLUDE_NEWER = "Some(Absolute(2024-06-01T22:00:00Z))"

# Answers to `uv --show-settings` that do not say whether uv's settings hold an `exclude-newer`.
UNREADABLE_SETTINGS = {
    "a format without the exclude-newer": Mock(stdout="ResolverSettings {}\n", stderr=""),
    "a uv that fails": CalledProcessError(2, "uv", "", "error: unexpected argument '--show-settings'"),
}


class UvSettingsTestCase(LoggingTestCase):
    """A test case whose stubbed processes answer `uv --show-settings`, without an `exclude-newer` unless set."""

    def setUp(self) -> None:
        """Stub out subprocess.run, so a test does not start a process."""
        super().setUp()
        self.configured_exclude_newer = "None"
        self.settings_answer: object = None
        self.mock_run = self.enterContext(patch("subprocess.run", side_effect=self.run_tool))

    def run_tool(self, _command: Command, /, **_options: object) -> object:
        """Answer the command as `uv --show-settings` does."""
        return self.uv_settings(self.configured_exclude_newer)

    def uv_settings(self, exclude_newer: str) -> object:
        """Return what `uv --show-settings` answers: the answer the test sets, or else the `exclude-newer`."""
        if isinstance(self.settings_answer, BaseException):
            raise self.settings_answer
        if self.settings_answer is not None:
            return self.settings_answer
        return Mock(stdout=f"        exclude_newer: ExcludeNewer {{\n            global: {exclude_newer},\n", stderr="")
