"""The Node package managers Update-time supports, and the one that a project's files name."""

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from update_time.io.log import FileContent, get_logger
from update_time.manifests.package_json import PackageJson
from update_time.package_managers.node.bun import BUN
from update_time.package_managers.node.npm import NPM
from update_time.package_managers.node.pnpm import PNPM
from update_time.package_managers.node.yarn import YARN

if TYPE_CHECKING:
    from pathlib import Path


_LOG = get_logger("package.json")

# The managers Update-time can update, keyed by name. The updater resolves a detected manager name against this
# catalog and skips a project whose `packageManager` field names a manager that isn't here, rather than mishandling it
# by running npm (which would write a stray package-lock.json and ignore the real lock).
SUPPORTED_MANAGERS = {manager.name: manager for manager in (NPM, PNPM, YARN, BUN)}
# The files that name a project's package manager. A corepack `packageManager` field wins over them, and npm is the
# default.
_MANAGER_FILES = {name: manager.name for manager in SUPPORTED_MANAGERS.values() for name in manager.files()}


@dataclass(frozen=True)
class _ManagerChoice:
    """The name of a project's package manager, and the files in the directory that named it."""

    name: str
    files: tuple[Path, ...] = ()

    def names_more_than_one_manager(self) -> bool:
        """Return whether the files name more than one package manager."""
        return len({_MANAGER_FILES[file.name] for file in self.files}) > 1


def manager_choice(package_json: PackageJson) -> _ManagerChoice:
    """Return the project's package manager (npm, pnpm, yarn, bun), defaulting to npm, and the files that named it.

    The nearest directory that names a manager decides, from the package.json's own upwards, so a workspace member
    takes the manager of its workspace root. In each directory, the corepack `packageManager` field of its
    package.json (e.g. `"pnpm@9.15.0"`) wins over a lockfile or a workspace file.
    """
    directory = package_json.path.parent
    if choice := _choice_in(directory, package_json.declared_manager()):
        return choice
    for ancestor in directory.parents:
        if choice := choice_in_directory(ancestor):
            return choice
    return _ManagerChoice("npm")


def choice_in_directory(directory: Path) -> _ManagerChoice | None:
    """Return the manager that the directory names, reading the package.json in it for the manager it declares."""
    return _choice_in(directory, _declared_manager_in(directory))


def _declared_manager_in(directory: Path) -> str:
    """Return the name of the manager that the package.json in the directory names, if any."""
    package_json = directory / "package.json"
    if not package_json.exists():
        return ""
    readable = PackageJson.readable(package_json, partial(_LOG.unreadable_file, content=FileContent.PACKAGE_MANAGER))
    return "" if readable is None else readable.declared_manager()


def _choice_in(directory: Path, declared_manager: str) -> _ManagerChoice | None:
    """Return the manager the package.json in the directory declares, or else the first its files name, if any."""
    if declared_manager:
        return _ManagerChoice(declared_manager)
    files = tuple(directory / name for name in _MANAGER_FILES if (directory / name).exists())
    return _ManagerChoice(_MANAGER_FILES[files[0].name], files) if files else None
