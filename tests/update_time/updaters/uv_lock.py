"""A faked uv lock, for the tests of the updaters that delegate to uv, and the test case that fakes it."""

import subprocess  # nosec
from itertools import repeat
from typing import TYPE_CHECKING, ClassVar, Final, NoReturn
from unittest.mock import DEFAULT, Mock

from tests.file_mocks import FakeFiles
from tests.http_mocks import mock_response
from tests.update_time.base_test_cases import ExecutablesOnPathTestCase
from tests.update_time.package_managers.uv_settings import UNSET_EXCLUDE_NEWER, uv_settings_answer
from tests.update_time.sources.pypi_answers import pypi_metadata_without_changelog

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from update_time.primitives.command import Command

    from tests.process_mocks import RunStub


# The lock after that `UvLockTestCase.files_after_update` takes for a uv lock that fails.
LOCK_FAILS: Final[None] = None


# What uv lock writes to stderr where the pins contradict each other.
NO_SOLUTION: Final[str] = "error: No solution found when resolving dependencies"


def locked_package(package: str, version: str) -> str:
    """Return the entry of a lock that locks the package at the version."""
    return f'[[package]]\nname = "{package}"\nversion = "{version}"\n'


def uv_locks(run: Mock) -> list[Command]:
    """Return the uv lock commands the mock of `subprocess.run` received, in the order of the calls."""
    return [
        command
        for command in _executed_commands(run)
        if command[:2] == ("uv", "lock") and not _asks_for_settings(command)
    ]


def _executed_commands(run: Mock) -> list[Command]:
    """Return the command of each call the mock of `subprocess.run` received, in the order of the calls."""
    return [call.args[0] for call in run.call_args_list]


def _asks_for_settings(command: Command) -> bool:
    """Return whether the command asks uv for its settings rather than to lock."""
    return "--show-settings" in command


class UvLockTestCase(ExecutablesOnPathTestCase):
    """Base for the tests that run an updater delegating to uv, with uv lock faked.

    A subclass names the file the scan discovers, the lock uv writes for it, and the updater. `locks` holds each faked
    uv lock of the last update: its command, and the files as they were when it ran. uv's settings hold the
    `exclude-newer` that `configured_exclude_newer` spells, none unless a test sets one.
    """

    manifest: ClassVar[Path]
    lock_file: ClassVar[Path]
    locks: list[tuple[Command, dict[Path, str]]]
    configured_exclude_newer = UNSET_EXCLUDE_NEWER

    def files_after_update(  # noqa: PLR0913 — the patched mocks travel with the fixtures they are given
        self,
        run: Mock,
        get: Mock,
        glob: Mock,
        files: Mapping[Path, str],
        lock_after: str | tuple[str | None, ...] | None,
        *,
        discovered: Sequence[Path] = (),
        lock_as_written: str | None = None,
    ) -> FakeFiles:
        """Update the files that the scan discovers, the manifest where the test names none, and return the files.

        The uv lock of the files as written writes the lock as written. Where the test gives none, it leaves the lock
        as it is, or absent where there was none. Each uv lock after it writes the lock after, or
        fails for `LOCK_FAILS`. A tuple gives the lock after of each of those in turn. uv writes past the write errors
        the files hold, since it is a process of its own. PyPI does not name a changelog for a version.
        """
        fake_files = files if isinstance(files, FakeFiles) else FakeFiles(files)
        locks_after = iter(lock_after) if isinstance(lock_after, tuple) else repeat(lock_after)
        self.locks = []

        def uv_lock(command: Command) -> Mock:
            as_written = not any("--upgrade" in locked for locked, _files in self.locks) and "--upgrade" not in command
            self.locks.append((command, dict(fake_files)))
            if as_written:
                if lock_as_written is not None:
                    fake_files[self.lock_file] = lock_as_written
                return DEFAULT
            if (lock := next(locks_after)) is None:
                failing_uv_lock()
            fake_files[self.lock_file] = lock
            return DEFAULT

        run.side_effect = self._answering_settings(uv_lock)
        run.return_value = Mock(stdout="", stderr="")
        get.return_value = mock_response(pypi_metadata_without_changelog())
        glob.return_value = list(discovered) or [self.manifest]
        with fake_files.patched():
            self.update()
        return fake_files

    def _answering_settings(self, uv_lock: Callable[[Command], Mock]) -> RunStub:
        """Return the stub of uv lock, answering a command that asks for uv's settings as those settings do."""

        def stub(command: Command, *_arguments: object, **_options: object) -> Mock:
            if _asks_for_settings(command):
                return uv_settings_answer(self.configured_exclude_newer)
            return uv_lock(command)

        return stub

    def update(self) -> None:
        """Run the updater."""
        raise NotImplementedError

    def updated_with_failing_lock(self, run: Mock, glob: Mock, contents: str) -> FakeFiles:
        """Update the manifest holding the contents, with every uv lock failing, and return the files."""
        files = FakeFiles({self.manifest: contents})
        run.side_effect = self._answering_settings(failing_uv_lock)
        glob.return_value = [self.manifest]
        with files.patched():
            self.update()
        return files

    def text_locked_with_upgrade(self) -> str:
        """Return the text the manifest held when the last update ran `uv lock --upgrade`."""
        return next(files[self.manifest] for command, files in self.locks if "--upgrade" in command)


def failing_uv_lock(*_arguments: object, **_options: object) -> NoReturn:
    """Fail `uv lock` with the error uv gives for pins that contradict each other."""
    raise subprocess.CalledProcessError(cmd="", returncode=1, output="", stderr=NO_SOLUTION)
