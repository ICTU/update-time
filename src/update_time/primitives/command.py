"""An external command, and the outcome of running it."""

from dataclasses import dataclass
from json import loads
from typing import Self


class Command(tuple[str, ...]):
    """An external command, as the sequence of words a shell would run.

    A tuple, so the machinery that runs it can take it as it is, and rendering it gives the command line it runs.
    """

    __slots__ = ()

    def __new__(cls, *words: str) -> Self:
        """Create the command from the executable and the arguments to run it with."""
        return super().__new__(cls, words)

    def __str__(self) -> str:
        """Render the command as the command line it runs."""
        return " ".join(self)

    @property
    def executable(self) -> str:
        """Return the executable the command runs, the first of its words."""
        return self[0]


@dataclass(frozen=True)
class Result:
    """The outcome of running a command: its captured output and whether it produced something usable."""

    stdout: str
    stderr: str
    succeeded: bool  # Whether the process exited with status 0 (and its executable was found).

    def __post_init__(self) -> None:
        """Drop the trailing newline a command's captured stderr ends with.

        Trimming it here rather than where the stderr is reported means a command whose stderr is only whitespace
        counts as having written nothing, instead of being reported as having written an empty line.
        """
        object.__setattr__(self, "stderr", self.stderr.rstrip())

    @property
    def json(self) -> dict | list:
        """Return the stdout parsed as JSON, or an empty dict when the command produced no output."""
        return loads(self.stdout) if self.stdout.strip() else {}

    @property
    def ok(self) -> bool:
        """Return whether the command produced a usable result.

        True when it exited cleanly, or produced output despite a non-zero exit — some tools use a non-zero exit as
        a normal signal (e.g. `npm outdated` exits non-zero when packages are outdated). A command that both failed
        and produced nothing (a genuine failure, such as an unreachable registry) is not ok, and its stderr is worth
        surfacing; a caller can check this to skip follow-up work the failure would make pointless.
        """
        return self.succeeded or bool(self.stdout.strip())
