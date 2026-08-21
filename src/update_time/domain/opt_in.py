"""What opts a reference into a behaviour that is off by default: its own marker, or a run-wide flag.

Adopting hash drift and keeping a floating pin are both opted into this way, so the rule is stated once here: the
reference's own `allow` directives win, and the flag opts in every reference whose marker decides nothing. Each
behaviour declares the flag that opts every reference in, beside the private channel carrying it from the CLI to
the updater subprocesses.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from update_time.domain.bound import Verb

if TYPE_CHECKING:
    from update_time.domain.marker import Marker
    from update_time.primitives.environment import EnvVar


@dataclass(frozen=True)
class RunWideOptIn:
    """The command-line option that opts every reference into a behaviour, and the channel that carries it.

    `env_var` is the private channel the CLI passes the option down through; `flag` is the option as the user
    spells it, which the message reporting what opted the reference in names.
    """

    env_var: EnvVar[bool]
    flag: str

    def cause(self, marker: Marker, *, allowed: bool) -> str | None:
        """Return what opts the reference in, the marker or the flag, or None when neither does.

        `allowed` is whether the reference's marker opts it in, which each behaviour reads off the field of its own.
        """
        if allowed:
            return f"update-time: {marker.raw_directives(Verb.ALLOW)}"
        return self.flag if self.env_var.get() else None
