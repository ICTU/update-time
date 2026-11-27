"""A typed process-environment variable, bundling a setting's read and write sides so they cannot drift apart.

The variables are a private `_UPDATE_TIME_*` channel to the updater subprocesses, not user-facing settings.
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
