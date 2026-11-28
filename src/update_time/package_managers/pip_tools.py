"""pip-tools as the tool that recompiles a requirements file pip-compile compiled."""

import re
from typing import ClassVar, Final, override

from update_time.io.process import result_or_failure
from update_time.primitives.command import Command

from .requirements_compiler import RequirementsCompiler

# What `pip config list` prints about `uploaded-prior-to`, a line per place that sets it: `global.uploaded-prior-to=…`
# for a config file, and `:env:.uploaded-prior-to=…` for `PIP_UPLOADED_PRIOR_TO`.
_CONFIG_UPLOADED_PRIOR_TO: Final[re.Pattern[str]] = re.compile(r"^[^.\n]*\.uploaded-prior-to=", re.MULTILINE)


class PipCompile(RequirementsCompiler):
    """pip-compile as the tool that recompiles a requirements file, with pip's `uploaded-prior-to` as the cutoff."""

    tool: ClassVar[str] = "pip-compile"
    setting: ClassVar[str] = "pip-compile uploaded-prior-to"
    cutoff_option: ClassVar[str] = "--uploaded-prior-to"
    package_cooldown_option: ClassVar[str | None] = None

    @override
    def settings_hold_cutoff(self, command: Command) -> bool:
        """Return whether pip's settings hold an `uploaded-prior-to`.

        pip-compile runs on a pip of its own, so a `pip` that cannot list its settings stops nothing.
        """
        config = result_or_failure(Command("pip", "config", "list"), self.compiled.compile_directory)
        if isinstance(config, str):
            self.log.settings_unreadable(self.compiled.path, "pip", "uploaded-prior-to", config)
            return False
        return bool(_CONFIG_UPLOADED_PRIOR_TO.search(config.stdout))

    @override
    def duration(self, days: int) -> str:
        """Return the number of days as pip reads a relative `uploaded-prior-to`."""
        return f"P{days}D"
