"""requirements.txt updater script bumps exact-pinned dependencies to their latest PyPI versions.

Extras, environment markers and inline comments on a line are preserved because only the version substring is
replaced.
"""

from typing import TYPE_CHECKING

from update_time.file_formats import requirements_txt as requirements_txt_format
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.pypi import get_latest_version

if TYPE_CHECKING:
    from pathlib import Path

_LOG = get_logger("requirements.txt")
# Match an exact pin at the start of a line: an optionally-extra'd package name followed by `==` and a version.
# `===` (arbitrary equality) and other operators don't match, so only `==` pins are updated.
_REQUIREMENT_RE = (
    r"^(?P<dependency>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*(?P<version>[A-Za-z0-9][A-Za-z0-9._!+-]*)"
)
# Requirements files follow the flat conventions `requirements.txt`, `requirements-<purpose>.txt` (e.g.
# `requirements-dev.txt`) and `<purpose>-requirements.txt` (e.g. `dev-requirements.txt`), plus a nested
# `requirements/` directory. The purpose is hyphen-separated on both sides for symmetry, and matching is
# restricted to the `.txt` extension, so unrelated files such as `constraints.txt`, `requirements.in`, or an
# arbitrary `requirementsfoo.txt` are not picked up.
_REQUIREMENTS_GLOB_PATTERNS = ("requirements.txt", "requirements-*.txt", "*-requirements.txt", "requirements/*.txt")


def update_requirements_txt(requirements_txt: Path) -> None:
    """Update the exact pins in a single requirements file, unless it is compiled or locked."""
    if requirements_txt_format.is_compiled(requirements_txt):
        _LOG.skipped(requirements_txt, "compiled or hash-pinned requirements file")
        return
    update_file(requirements_txt, _REQUIREMENT_RE, get_new_version=get_latest_version, logger=_LOG)


def update_requirements_txts() -> None:
    """Find all requirements files and update the exact pins in them."""
    requirements_files = set(glob(*_REQUIREMENTS_GLOB_PATTERNS, case_sensitive=True))
    for requirements_txt in requirements_files:
        update_requirements_txt(requirements_txt)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's requirements files."""
    update_requirements_txts()


if __name__ == "__main__":  # pragma: no cover
    main()
