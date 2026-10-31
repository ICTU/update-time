"""pip-tools as the tool that recompiles a requirements file pip-compile compiled."""

import re
from typing import ClassVar, override

from update_time.io.process import run_unlogged
from update_time.package_managers.requirements_compiler import MANAGED_BY_COMMENT, RequirementsCompiler
from update_time.primitives.command import Command

# What `pip config list` prints about `uploaded-prior-to`, a line per place that sets it: `global.uploaded-prior-to=…`
# for a config file, and `:env:.uploaded-prior-to=…` for `PIP_UPLOADED_PRIOR_TO`.
_CONFIG_UPLOADED_PRIOR_TO = re.compile(r"^[^.\n]*\.uploaded-prior-to=", re.MULTILINE)


class PipCompile(RequirementsCompiler):
    """pip-compile as the tool that recompiles a requirements file, with pip's `uploaded-prior-to` as the cutoff."""

    tool: ClassVar[str] = "pip-compile"
    setting: ClassVar[str] = "pip-compile uploaded-prior-to"
    cutoff_option: ClassVar[str] = "--uploaded-prior-to"
    package_cooldown_option: ClassVar[str | None] = None
    managed_by_comment_line: ClassVar[str] = f"# uploaded-prior-to: {MANAGED_BY_COMMENT}"

    @override
    def settings_hold_cutoff(self, command: Command) -> bool:
        """Return whether pip's settings hold an `uploaded-prior-to`.

        pip-compile runs on a pip of its own, so a `pip` that cannot list its settings stops nothing. Update-time warns
        and treats the settings as holding none.
        """
        config = run_unlogged(Command("pip", "config", "list"), cwd=self.compiled.compile_directory)
        if isinstance(config, OSError):
            reason = str(config)
        elif not config.succeeded:
            reason = config.stderr.strip() or "pip failed without saying why"
        else:
            return bool(_CONFIG_UPLOADED_PRIOR_TO.search(config.stdout))
        self.log.pip_settings_unreadable(self.compiled.path, reason)
        return False

    @override
    def duration(self, days: int) -> str:
        """Return the number of days as pip reads a relative `uploaded-prior-to`."""
        return f"P{days}D"
