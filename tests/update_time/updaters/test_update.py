"""Unit tests for the update script that runs all updater scripts."""

import logging
import os
import platform
import signal
import sys
import threading
import unittest
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock, call, patch

from update_time.domain.archival import IGNORE_ARCHIVED
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency_type import DEPENDENCY_TYPES
from update_time.domain.vulnerability import IGNORE_VULNERABILITIES
from update_time.io import log as log_module
from update_time.io import process as process_module
from update_time.io.filesystem import EXCLUDE_PATHS
from update_time.io.log import LOG_LEVEL
from update_time.io.process import Processes
from update_time.markers.drift import ALLOW_HASH_DRIFT
from update_time.markers.floating import ALLOW_FLOATING_PIN
from update_time.updaters import update as update_module
from update_time.updaters.update import _SCRIPTS, _SRC, _Script, _Scripts, main

from tests.helpers import patch_environ, patch_pathlib_path
from tests.mutation import Mutation, kills

if TYPE_CHECKING:
    from update_time.primitives.command import Command

# The scripts that may not run beside another, spelled out so this pins which ones rather than echoing the registry
# back at itself: package_json rewrites the package.json files that node_engine rewrites too. The parallel ones need
# no list of their own, since which scripts exist at all is pinned against the files on disk.
_SEQUENTIAL_SCRIPT_NAMES = ("package_json",)

_PLATFORM = "macOS-26.7.1-arm64-arm-64bit-Mach-O"

# How long a stand-in updater runs when nothing stops it, so a run that fails to stop it cannot hang the tests.
_PATIENCE = 0.5

# How many stand-in updaters have started when the last of them sends the run a SIGTERM.
_STARTED_WHEN_SIGNALLED = 3


class _Updater:
    """A stand-in for an updater process, which runs until it is stopped."""

    def __init__(self, pid: int) -> None:
        """Start the stand-in."""
        self.pid = pid
        self.stopped = threading.Event()

    def wait(self) -> int:
        """Run until the stand-in is stopped, or until the patience runs out, and return the exit code."""
        return -signal.SIGTERM if self.stopped.wait(_PATIENCE) else 0


class _Updaters:
    """Stand-ins for the updaters a run starts, one of which sends the run a SIGTERM while others run."""

    def __init__(self) -> None:
        """Start with none."""
        self.started: list[_Updater] = []

    def start(self, _command: Command, **_kwargs: object) -> _Updater:
        """Start a stand-in for the command."""
        self.started.append(updater := _Updater(pid=len(self.started)))
        if len(self.started) == _STARTED_WHEN_SIGNALLED:
            os.kill(os.getpid(), signal.SIGTERM)
        return updater

    def stop_group(self, pid: int, _signal: int) -> None:
        """Stop the stand-in that leads the process group."""
        self.started[pid].stopped.set()


def _registration_problems(registered: _Scripts, on_disk: set[str]) -> list[str]:
    """Return a message for each way the registration, the dependency types, and the scripts on disk disagree."""
    names = {script.name for script in registered}
    problems = [
        f"no updater script is registered for '{dependency_type.name}'"
        for dependency_type in DEPENDENCY_TYPES
        if not registered.scripts.get(dependency_type)
    ]
    problems += [
        f"the updater script '{name}' is on disk but registered for no dependency type"
        for name in sorted(on_disk - names)
    ]
    problems += [f"the updater script '{name}' is registered but not on disk" for name in sorted(names - on_disk)]
    return problems


class RegistrationTest(unittest.TestCase):
    """Unit tests for the agreement between the registered updater scripts and the dependency types."""

    @kills(
        Mutation(
            update_module,
            '        DEPENDENCY_TYPES.jsdelivr_npm_urls: (_Script("jsdelivr"),),\n',
            "",
            "an updater script is left out of the registry, so it never runs",
        )
    )
    def test_the_registration_agrees_with_the_scripts_on_disk(self):
        """Test that the registry names every dependency type and every `update_*.py` script beside it."""
        on_disk = {path.stem.removeprefix("update_") for path in _SRC.glob("update_*.py")}
        self.assertNotEqual(on_disk, set())  # An empty scan would pass the assertion below without examining anything
        self.assertEqual(_registration_problems(_SCRIPTS, on_disk), [])

    def test_dependency_type_without_a_script_is_reported(self):
        """Test that a dependency type no updater script is registered for is reported, naming the type."""
        scripts = dict.fromkeys(list(DEPENDENCY_TYPES)[:-1], (_Script("a_script"),))
        expected = [f"no updater script is registered for '{list(DEPENDENCY_TYPES)[-1].name}'"]
        self.assertEqual(_registration_problems(_Scripts(scripts), {"a_script"}), expected)

    def test_scripts_disagreeing_with_the_files_on_disk_are_reported(self):
        """Test that a script on disk nothing registers, and a registered script that is not on disk, are reported."""
        registered = dict.fromkeys(DEPENDENCY_TYPES, (_Script("registered"),))
        cases = {
            "on disk but registered for no dependency type": (
                {"registered", "stray"},
                ["the updater script 'stray' is on disk but registered for no dependency type"],
            ),
            "registered but not on disk": (set(), ["the updater script 'registered' is registered but not on disk"]),
        }
        for case, (on_disk, expected) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(_registration_problems(_Scripts(registered), on_disk), expected)


class ScriptTest(unittest.TestCase):
    """Unit tests for an updater script."""

    def test_command(self):
        """Test that a script's command runs the script's file with the interpreter running the run."""
        command = _Script("dockerfile_base_image").command
        self.assertEqual(command.executable, sys.executable)
        self.assertEqual(command[-1].split("/")[-1], "update_dockerfile_base_image.py")


@patch("subprocess.Popen")
class ScriptsTest(unittest.TestCase):
    """Unit tests for running every updater script."""

    def test_all_scripts_are_run(self, mock_popen: Mock):
        """Test that every updater script is run, and that the sequential ones run after the parallel ones."""
        mock_popen.return_value.wait.return_value = 0
        self.assertEqual(_SCRIPTS.run(Processes(), Mock()), 0)
        scripts_run = [run_call.args[0][-1].split("/")[-1] for run_call in mock_popen.call_args_list]
        expected = [f"update_{script.name}.py" for script in _SCRIPTS]
        self.assertEqual(sorted(scripts_run), sorted(expected))
        # The scripts that may not run beside another are the last ones run, in whichever order among themselves.
        sequential = {f"update_{name}.py" for name in _SEQUENTIAL_SCRIPT_NAMES}
        self.assertEqual(set(scripts_run[-len(sequential) :]), sequential)

    def test_highest_exit_code_is_returned(self, mock_popen: Mock):
        """Test that the highest exit code of all scripts is returned."""
        mock_popen.return_value.wait.return_value = 1
        self.assertEqual(_SCRIPTS.run(Processes(), Mock()), 1)

    @kills(
        Mutation(
            update_module._Scripts.run,
            "return 1",
            "return exit_code",
            "a signal ends an updater, and the run still exits as a success",
        )
    )
    def test_an_update_a_signal_stopped_fails_the_run_and_is_reported(self, mock_popen: Mock):
        """Test that an updater a signal stopped makes the run exit with 1, and names its dependency type and signal."""
        mock_popen.side_effect = lambda command, **_kwargs: Mock(
            wait=Mock(return_value=-signal.SIGKILL if command[-1].endswith("update_pom_xml.py") else 0)
        )
        mock_logger = Mock()
        self.assertEqual(_SCRIPTS.run(Processes(), mock_logger), 1)
        mock_logger.update_stopped.assert_called_once_with("Maven dependencies", signal.SIGKILL)


@patch("os.chdir", Mock())
@patch("subprocess.Popen")
class UpdateMainTest(unittest.TestCase):
    """Unit tests for the main function."""

    def run_main(
        self, mock_popen: Mock, *argv: str, inside_git: bool = True, environment: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str]]:
        """Run `main()` with the given CLI arguments and return the environment it exported for the subprocesses.

        The returned snapshot is taken while main's exports (cooldown, log level, excluded paths) are still in place,
        so a caller can assert on them even though `patch.dict` restores the real environment afterwards.
        """
        mock_popen.return_value.wait.return_value = 0
        for signal_number in update_module._STOP_SIGNALS:
            self.addCleanup(signal.signal, signal_number, signal.getsignal(signal_number))
        root = logging.getLogger()
        self.addCleanup(setattr, root, "handlers", root.handlers[:])
        self.addCleanup(root.setLevel, root.level)
        with (
            patch_environ(environment, clear=True),
            patch("logging.Logger.log"),  # main configures the root logger for real, which would print to the tests
            patch("sys.argv", ["update-time", *argv]),
            # platform.platform runs `file` in a subprocess, which this class's Popen patch would answer.
            patch("platform.platform", Mock(return_value=_PLATFORM)),
            # parse_args (cli) gates on the repository check and main warns on it, so keep both patches in sync.
            patch("update_time.io.cli.inside_git_repository", Mock(return_value=inside_git)),
            patch("update_time.updaters.update.inside_git_repository", Mock(return_value=inside_git)),
        ):
            try:
                return main(), dict(os.environ)
            except SystemExit as system_exit:
                return cast(int, system_exit.code), dict(os.environ)

    def test_main_updates_dependencies(self, mock_popen: Mock):
        """Test that main parses the arguments and then updates the dependencies."""
        exit_code, environment = self.run_main(mock_popen)
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[COOLDOWN.name], "7")

    def test_main_passes_cooldown_to_subprocesses(self, mock_popen: Mock):
        """Test that main exports the cooldown in the environment so the updater subprocesses inherit it."""
        exit_code, environment = self.run_main(mock_popen, "--cooldown", "14")
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[COOLDOWN.name], "14")

    def test_main_passes_log_level_to_subprocesses(self, mock_popen: Mock):
        """Test that main exports the log level in the environment so the updater subprocesses inherit it."""
        exit_code, environment = self.run_main(mock_popen, "--log-level", "debug")
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[LOG_LEVEL.name], "DEBUG")

    def test_main_passes_allow_hash_drift_off_by_default(self, mock_popen: Mock):
        """Test that main exports the drift opt-in as off when --allow-hash-drift is not given."""
        exit_code, environment = self.run_main(mock_popen)
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[ALLOW_HASH_DRIFT.name], "0")

    def test_main_passes_allow_hash_drift_when_set(self, mock_popen: Mock):
        """Test that main exports the drift opt-in as on when --allow-hash-drift is given."""
        exit_code, environment = self.run_main(mock_popen, "--allow-hash-drift")
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[ALLOW_HASH_DRIFT.name], "1")

    def test_main_passes_allow_floating_pin_off_by_default(self, mock_popen: Mock):
        """Test that main exports the floating-pin opt-in as off when --allow-floating-pin is not given."""
        exit_code, environment = self.run_main(mock_popen)
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[ALLOW_FLOATING_PIN.name], "0")

    def test_main_passes_allow_floating_pin_when_set(self, mock_popen: Mock):
        """Test that main exports the floating-pin opt-in as on when --allow-floating-pin is given."""
        exit_code, environment = self.run_main(mock_popen, "--allow-floating-pin")
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[ALLOW_FLOATING_PIN.name], "1")

    def test_main_passes_ignore_archived_off_by_default(self, mock_popen: Mock):
        """Test that main exports the archival switch as off when --ignore-archived is not given."""
        exit_code, environment = self.run_main(mock_popen)
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[IGNORE_ARCHIVED.name], "0")

    def test_main_passes_ignore_archived_when_set(self, mock_popen: Mock):
        """Test that main exports the archival switch as on when --ignore-archived is given."""
        exit_code, environment = self.run_main(mock_popen, "--ignore-archived")
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[IGNORE_ARCHIVED.name], "1")

    def test_main_passes_ignored_vulnerabilities_to_subprocesses(self, mock_popen: Mock):
        """Test that main exports the advisories to ignore so the updater subprocesses inherit them."""
        advisories = "GHSA-2gwj-7jmv-h26r,CVE-2022-28346"
        exit_code, environment = self.run_main(mock_popen, "--ignore-vulnerability", advisories)
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[IGNORE_VULNERABILITIES.name], "CVE-2022-28346,GHSA-2gwj-7jmv-h26r")

    def test_main_passes_no_ignored_vulnerabilities_by_default(self, mock_popen: Mock):
        """Test that main exports an empty variable when --ignore-vulnerability is not given."""
        exit_code, environment = self.run_main(mock_popen)
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[IGNORE_VULNERABILITIES.name], "")

    @patch_pathlib_path(exists=True)
    def test_main_passes_excluded_paths_to_subprocesses(self, mock_popen: Mock):
        """Test that main exports the excluded paths in the environment so the updater subprocesses inherit them."""
        exit_code, environment = self.run_main(mock_popen, "--exclude-path", "vendor,packages/legacy")
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[EXCLUDE_PATHS.name], "vendor,packages/legacy")

    def test_main_passes_no_excluded_paths_by_default(self, mock_popen: Mock):
        """Test that main exports an empty excluded-paths variable when --exclude-path is not given."""
        exit_code, environment = self.run_main(mock_popen)
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[EXCLUDE_PATHS.name], "")

    def test_main_leaves_non_existing_excluded_paths_out_of_the_environment(self, mock_popen: Mock):
        """Test that a non-existing excluded path is warned about but not passed down to the subprocesses."""
        mock_logger = Mock()
        with (
            patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)),
            patch("pathlib.Path.exists", autospec=True, side_effect=lambda self: self == Path("vendor")),
        ):
            exit_code, environment = self.run_main(mock_popen, "--exclude-path", "vendor,missing")
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[EXCLUDE_PATHS.name], "vendor")
        mock_logger.missing_excluded_path.assert_called_once_with(Path("missing"))

    @patch_pathlib_path(exists=True)
    def test_main_logs_existing_excluded_path(self, mock_popen: Mock):
        """Test that main logs an existing excluded path once at DEBUG."""
        mock_logger = Mock()
        with patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)):
            self.run_main(mock_popen, "--exclude-path", "vendor")
        mock_logger.excluded_path.assert_called_once_with(Path("vendor"))
        mock_logger.missing_excluded_path.assert_not_called()

    @patch_pathlib_path(exists=False)
    def test_main_warns_about_missing_excluded_path(self, mock_popen: Mock):
        """Test that main warns about a non-existing excluded path instead of failing the run."""
        mock_logger = Mock()
        with patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)):
            self.run_main(mock_popen, "--exclude-path", "vendor")
        mock_logger.missing_excluded_path.assert_called_once_with(Path("vendor"))
        mock_logger.excluded_path.assert_not_called()

    def test_main_changes_to_the_default_directory(self, mock_popen: Mock):
        """Test that main changes to the current directory when no path is given."""
        with patch("os.chdir") as mock_chdir:
            self.run_main(mock_popen)
        mock_chdir.assert_called_once_with(Path())

    @patch_pathlib_path(is_dir=True)
    def test_main_changes_to_the_given_directory(self, mock_popen: Mock):
        """Test that main changes to the given path before spawning the updater subprocesses."""
        with patch("os.chdir") as mock_chdir:
            self.run_main(mock_popen, "some-directory")
        mock_chdir.assert_called_once_with(Path("some-directory"))

    def test_main_runs_without_warning_inside_git_repository(self, mock_popen: Mock):
        """Test that main updates the dependencies without warning when inside a git repository."""
        mock_logger = Mock()
        with patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)):
            exit_code, _ = self.run_main(mock_popen)
        self.assertEqual(exit_code, 0)
        mock_popen.assert_called()
        mock_logger.forced_outside_git_repository.assert_not_called()

    def test_main_refuses_to_run_outside_git_repository(self, mock_popen: Mock):
        """Test that main refuses to run outside a git repository."""
        with patch("sys.stderr.write") as mock_write:
            exit_code, _ = self.run_main(mock_popen, inside_git=False)
        self.assertEqual(exit_code, 2)
        mock_write.assert_called_with(
            "update-time: error: . is not inside a git repository; rerun inside a repository so changes can be "
            "reverted, or pass --force to run anyway\n"
        )
        mock_popen.assert_not_called()

    def test_main_warns_when_forced_to_run_outside_git_repository(self, mock_popen: Mock):
        """Test that main still updates the dependencies but warns when the scan root is outside a repository."""
        mock_logger = Mock()
        with patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)):
            exit_code, _ = self.run_main(mock_popen, "--force", inside_git=False)
        self.assertEqual(exit_code, 0)
        mock_popen.assert_called()
        mock_logger.forced_outside_git_repository.assert_called_once_with(Path.cwd())

    @kills(
        Mutation(
            update_module._report_context,
            "bool(os.environ.get(name))",
            "name in os.environ",
            "a credential variable set to the empty string is logged as set, though the run goes without it",
        )
    )
    def test_main_first_logs_the_context_of_the_run(self, mock_popen: Mock):
        """Test that main first logs the version, the OS and Python, the arguments, the credentials, and the network.

        A credential variable set to the empty string counts as not set.
        """
        mock_logger = Mock()
        arguments = ["--force", "--exclude-path", "vendor"]
        environment = {
            "GITHUB_TOKEN": "a-token",  # nosec B105
            "DOCKER_HUB_USERNAME": "",
            "HTTPS_PROXY": "http://proxy.acme.com:3128",
            "NO_PROXY": "localhost",
            "REQUESTS_CA_BUNDLE": "/etc/ssl/acme.pem",
        }
        credentials = {"GITHUB_TOKEN": True, "DOCKER_HUB_USERNAME": False, "DOCKER_HUB_TOKEN": False}  # nosec B105
        with patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)):
            self.run_main(mock_popen, *arguments, inside_git=False, environment=environment)
        mock_logger.running.assert_called_once()
        mock_logger.arguments.assert_called_once()
        mock_logger.credentials.assert_called_once()
        mock_logger.network_settings.assert_called_once()
        logged = [method_call for method_call in mock_logger.method_calls if method_call != call.logs_debug()]
        self.assertEqual(
            logged[:4],
            [
                call.running(version("update-time"), _PLATFORM, platform.python_version()),
                call.arguments(arguments),
                call.credentials(credentials),
                call.network_settings({"https": "http://proxy.acme.com:3128"}, "localhost", "/etc/ssl/acme.pem"),
            ],
        )

    def test_main_reports_the_signal_that_stopped_the_run(self, mock_popen: Mock):
        """Test that main logs which signal stopped the run, and exits with 128 plus the signal's number."""
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            with self.subTest(signal_number.name):
                # Swallow the signal where main lacks a handler of its own, so the signal cannot end the test run.
                self.addCleanup(signal.signal, signal_number, signal.signal(signal_number, Mock()))
                scripts = Mock()
                scripts.run.side_effect = lambda _processes, _logger, signal_number=signal_number: os.kill(
                    os.getpid(), signal_number
                )
                mock_logger = Mock()
                with (
                    patch("update_time.updaters.update._SCRIPTS", scripts),
                    patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)),
                ):
                    exit_code, _ = self.run_main(mock_popen)
                self.assertEqual(exit_code, 128 + signal_number)
                mock_logger.stopped.assert_called_once_with(signal_number)

    @kills(
        Mutation(
            update_module._Scripts.run,
            "exit_code >= 0 or processes.stop_signal is not None:",
            "exit_code >= 0:",
            "the run logs a stopped update for every updater it stopped itself",
        ),
        Mutation(
            process_module.Processes._start,
            "if self.stop_signal is not None:  #",
            "if False:  #",
            "a signal that lands while an updater starts leaves that updater running",
        ),
    )
    def test_a_stopped_run_stops_the_updaters_still_running(self, mock_popen: Mock):
        """Test that the run stops every updater it started on a signal, rather than waiting for them."""
        updaters = _Updaters()
        mock_logger = Mock()
        with (
            patch("subprocess.Popen", updaters.start),
            patch("os.killpg", updaters.stop_group),
            patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)),
        ):
            exit_code, _ = self.run_main(mock_popen)
        self.assertEqual(exit_code, 128 + signal.SIGTERM)
        self.assertGreaterEqual(len(updaters.started), _STARTED_WHEN_SIGNALLED)
        self.assertEqual([updater for updater in updaters.started if not updater.stopped.is_set()], [])
        mock_logger.update_stopped.assert_not_called()

    @kills(
        Mutation(
            log_module.set_log_level,
            "_configure_root_logger()",
            "pass",
            "the parent logs at the level of whichever module logged first, rather than at --log-level",
        )
    )
    def test_main_logs_at_the_level_the_command_line_sets(self, mock_popen: Mock):
        """Test that main logs at the level --log-level sets, whichever module configured the log before main ran."""
        for level in ("DEBUG", "WARNING"):
            with self.subTest(level):
                self.run_main(mock_popen, "--log-level", level)
                self.assertEqual(logging.getLevelName(logging.getLogger().level), level)
