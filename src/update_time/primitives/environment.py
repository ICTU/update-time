"""A typed process-environment variable, bundling a setting's read and write sides so they cannot drift apart.

Update-time runs its updaters as subprocesses, so the CLI passes each parsed option down through the environment —
a private `_UPDATE_TIME_*` channel, not a user-facing setting. Each option is one `EnvVar`, owning the variable
name, the value to fall back on when it is unset, and how the typed value is parsed from and serialised to the
string the environment stores.
"""

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class EnvVar[T]:
    """A process-environment variable holding a typed value, with a paired `get` and `set`."""

    name: str
    default: T
    parse: Callable[[str], T]
    serialize: Callable[[T], str] = str

    def get(self) -> T:
        """Return the variable's parsed value, or the default when it is unset."""
        value = os.environ.get(self.name)
        return self.default if value is None else self.parse(value)

    def set(self, value: T) -> None:
        """Store the value in the environment, serialised to its string form."""
        os.environ[self.name] = self.serialize(value)


def flag(name: str) -> EnvVar[bool]:
    """Return a variable holding a flag, which the environment stores as `1` when on and `0` when off.

    An option the CLI either passes or does not is carried this way, so each declares its name alone.
    """
    return EnvVar(name, default=False, parse=lambda value: value == "1", serialize=lambda on: "1" if on else "0")
