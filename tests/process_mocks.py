"""Mocks of the commands Update-time runs through `subprocess.run`, and of what the runs answer."""

from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from unittest.mock import Mock

    from update_time.primitives.command import Command

    class RunStub(Protocol):
        """A stand-in for `subprocess.run`, answering a command as the tool it runs does."""

        def __call__(self, command: Command, **options: object) -> Mock:
            """Return the completed process the command gives."""
            ...


# What Update-time passes to subprocess.run besides the working directory, where it does not add environment variables.
SUBPROCESS_RUN_OPTIONS: Final[dict[str, bool | None]] = {
    "capture_output": True,
    "text": True,
    "check": True,
    "env": None,
}

# A stubbed run returns a mock of the completed process, or raises an error.
type RunAnswer = Mock | BaseException
