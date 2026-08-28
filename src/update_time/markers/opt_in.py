"""What opts a reference into a behaviour that is off by default: its own marker, or a run-wide flag.

Adopting hash drift and keeping a floating pin are both opted into this way, so the rule is stated once here: the
reference's own `allow` directives win, and the flag opts in every reference whose marker decides nothing. Each
behaviour declares the flag that opts every reference in, beside the private channel carrying it from the CLI to
the updater subprocesses.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from update_time.markers.marker import Marker, Scope
    from update_time.primitives.environment import EnvVar


@dataclass(frozen=True)
class RunWideOptIn:
    """The command-line option that opts every reference into a behaviour, and the channel that carries it.

    `env_var` is the private channel the CLI passes the option down through; `flag` is the option as the user
    spells it, which the message reporting what opted the reference in names. `scope` is the behaviour itself, as
    the marker language names it, so the marker and the directive naming it cannot come apart.
    """

    env_var: EnvVar[bool]
    flag: str
    scope: Scope

    def cause(self, marker: Marker) -> str | None:
        """Return what opts the reference in, the marker's own directive or the flag, or None when neither does.

        An empty directive means the marker says nothing about this behaviour, whatever else it opts into, so the
        flag decides — unless the reference asked for the default itself, which no command-line option overrides.
        """
        if directive := marker.allow_directive(self.scope):
            return f"update-time: {directive}"
        if self.scope in marker.written_scopes:
            return None
        return self.flag if self.env_var.get() else None
