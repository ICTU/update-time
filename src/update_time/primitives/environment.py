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
    """A process-environment variable holding a typed value, with a paired `get` and `set`.

    `parse` and `serialize` are inverses, kept together so the read and write sides of one variable stay in step.
    """

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
