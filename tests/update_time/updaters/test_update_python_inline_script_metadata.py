"""Unit tests for the inline-script-metadata updater (discovery and orchestration of the uv package manager)."""

from unittest.mock import Mock, patch

from update_time.domain.cooldown import COOLDOWN
from update_time.io.log import Logger
from update_time.markers.directive import Reason
from update_time.markers.marker import Marker, Scope
from update_time.package_managers.uv import lock, marker_policy
from update_time.package_managers.uv import uv as uv_module
from update_time.primitives.command import Command
from update_time.primitives.location import Location
from update_time.updaters import update_python_inline_script_metadata

from tests.helpers import FakeFiles, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.helpers import SCAN_ROOT, message_of, published_version, uv_path_source
from tests.update_time.manifests.helpers import script
from tests.update_time.package_managers.helpers import CONFIGURED_EXCLUDE_NEWER
from tests.update_time.updaters.helpers import (
    LOCK_FAILS,
    NO_SOLUTION,
    PYPROJECT_TOML,
    UPLOAD_TIME_WITHOUT_CHANGELOG,
    UvLockTestCase,
    locked_package,
    no_vulnerabilities,
    pypi_answer,
    uv_locks,
)

# The script at the root of the scan, and the lock uv writes beside it.
_SCRIPT = SCAN_ROOT / "tool.py"
_SCRIPT_LOCK = SCAN_ROOT / "tool.py.lock"


def _script_lock(*versions: str, package: str = "package") -> str:
    """Return a script's lock that lists the package among the script's requirements, and locks it at the versions.

    uv locks a package at several versions where it forks the resolution, one per Python version or platform.
    """
    requirements = f'[manifest]\nrequirements = [{{ name = "{package}", specifier = ">=1.0" }}]\n'
    packages = "\n".join(locked_package(package, version) for version in versions)
    return f"version = 1\n\n{requirements}\n{packages}"


class _ScriptLockTestCase(UvLockTestCase):
    """Base for the tests that run the inline script metadata updater with uv lock faked."""

    manifest = _SCRIPT
    lock_file = _SCRIPT_LOCK

    def update(self) -> None:
        """Run the inline script metadata updater."""
        update_python_inline_script_metadata.update_python_inline_script_metadatas()


# The checks over the settled pins are stubbed out so these tests focus on the update flow: each check makes registry
# requests of its own, and what they report is `test_uv_pins.py`'s to pin, while `ScriptRedundantDirectiveTest` below
# runs them.
@patch("update_time.updaters.update_python_inline_script_metadata.warn_about_pins", Mock())
@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
@patch("requests.Session.get")
@patch("subprocess.run")
class ScriptLockTest(_ScriptLockTestCase):
    """Unit tests for the exact pins of a script, which take the versions uv locks for the script."""

    @kills(
        Mutation(
            lock._LockUpdate.update,
            "if self.before.target.keeps_the_lock:\n            self.before.log.path(self.before.target.uv_lock.path)",
            "self.before.log.path(self.before.target.uv_lock.path)",
            "the run reports checking a lock it removes again, which the user never sees",
        ),
        Mutation(
            lock._with_cutoff,
            "cooldown_cutoff(COOLDOWN.get())",
            "cooldown_cutoff(0)",
            "uv locks a script, or a workspace without a lock, without the cooldown `--cooldown` sets",
        ),
    )
    def test_a_script_without_a_lock_takes_the_locked_versions_and_is_left_without_a_lock(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that an exact pin moves to the version uv locks, with the cooldown passed to uv, and the lock goes."""
        with patch("update_time.package_managers.uv.lock.cooldown_cutoff", Mock(return_value="cutoff")) as cutoff:
            left = self.files_after_update(run, get, glob, {_SCRIPT: script("package==1.0")}, _script_lock("1.1"))
        cutoff.assert_called_once_with(COOLDOWN.get())
        self.assertEqual(left[_SCRIPT], script("package==1.1"))
        self.assertEqual(self.text_locked_with_upgrade(), script("package>=1.0"))
        self.assertNotIn(_SCRIPT_LOCK, left)
        self.assert_none_logged(message_of(Logger.path), "path of the removed lock", location=Location(_SCRIPT_LOCK))
        as_written, upgrade = uv_locks(run)
        self.assertEqual(
            [command[:4] for command in (as_written, upgrade)], [("uv", "lock", "--script", str(_SCRIPT))] * 2
        )
        self.assertEqual(["--upgrade" in command for command in (as_written, upgrade)], [False, True])
        cutoffs = [command[command.index("--exclude-newer") + 1] for command in (as_written, upgrade)]
        self.assertEqual(cutoffs, ["cutoff", "cutoff"])
        published = published_version("1.1", UPLOAD_TIME_WITHOUT_CHANGELOG)
        self.assert_new_version_logged("package", published, Location(_SCRIPT, 4))
        self.assert_no_warnings_logged()

    def test_a_script_with_a_lock_keeps_it_locked_again_on_the_new_pins_without_the_cooldown(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that the lock a script had before the run is locked again on the new pins, without the cutoff."""
        files = {_SCRIPT: script("package==1.0"), _SCRIPT_LOCK: _script_lock("1.0")}
        left = self.files_after_update(run, get, glob, files, _script_lock("1.1"))
        self.assertEqual(left[_SCRIPT], script("package==1.1"))
        self.assertEqual(left[_SCRIPT_LOCK], _script_lock("1.1"))
        self.assert_path_logged(_SCRIPT_LOCK)
        *_, (_, files_at_the_last_lock) = self.locks
        self.assertEqual(files_at_the_last_lock[_SCRIPT], script("package==1.1"))
        self.assertEqual(["--upgrade" in command for command in uv_locks(run)], [False, True, False])
        self.assertEqual(["--exclude-newer" in command for command in uv_locks(run)], [True, True, False])

    @kills(
        Mutation(
            lock._with_cutoff,
            "settings_hold_exclude_newer(target.command,",
            "settings_hold_exclude_newer(_UV_LOCK,",
            "Update-time asks uv's settings for the directory, leaving out the script's own exclude-newer",
        )
    )
    def test_a_script_locks_without_a_cutoff_where_uvs_settings_for_the_script_hold_an_exclude_newer(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that uv locks the script with the exclude-newer its settings hold, rather than with a cutoff."""
        self.configured_exclude_newer = CONFIGURED_EXCLUDE_NEWER
        left = self.files_after_update(run, get, glob, {_SCRIPT: script("package==1.0")}, _script_lock("1.1"))
        self.assertEqual(left[_SCRIPT], script("package==1.1"))
        self.assertEqual(["--exclude-newer" in command for command in uv_locks(run)], [False, False])
        settings = Command("uv", "lock", "--script", str(_SCRIPT), "--show-settings")
        self.assertIn(settings, [executed.args[0] for executed in run.call_args_list])

    def test_a_dependency_locked_at_several_versions_is_pinned_and_reported_at_the_lowest(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a pin moves to the lowest version uv locks for the script, where it locks several."""
        for case, versions in {
            "the lower version listed first": ("1.1", "1.2"),
            "the higher version listed first": ("1.2", "1.1"),
        }.items():
            with self.subTest(case):
                left = self.files_after_update(
                    run, get, glob, {_SCRIPT: script("package==1.0")}, _script_lock(*versions)
                )
                self.assertEqual(left[_SCRIPT], script("package==1.1"))
                published = published_version("1.1", UPLOAD_TIME_WITHOUT_CHANGELOG)
                self.assert_new_version_logged("package", published, Location(_SCRIPT, 4))

    def test_the_version_an_earlier_lock_moves_for_a_dependency_without_an_exact_pin_is_reported(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that where a script had a lock, the version it moves for a dependency without a pin is reported."""
        files = {_SCRIPT: script("package>=1.0"), _SCRIPT_LOCK: _script_lock("1.0")}
        left = self.files_after_update(run, get, glob, files, _script_lock("1.1"))
        self.assertEqual(left[_SCRIPT], script("package>=1.0"))
        published = published_version("1.1", UPLOAD_TIME_WITHOUT_CHANGELOG)
        self.assert_new_version_logged("package", published, Location(_SCRIPT, 4))

    @kills(
        Mutation(
            uv_module.declarations_of,
            "if isinstance(file, PyprojectToml) else set()",
            "",
            "a script takes on the sources of a workspace root above it, which `uv lock --script` ignores",
        )
    )
    def test_a_pin_the_workspace_root_beside_the_script_sources_is_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pin moves and is reported, though a workspace root beside the script sources its package."""
        root = "[tool.uv.workspace]\nmembers = []\n" + uv_path_source("package")
        files = {_SCRIPT: script("package==1.0"), PYPROJECT_TOML: root}
        left = self.files_after_update(run, get, glob, files, _script_lock("1.1"))
        self.assertEqual(left[_SCRIPT], script("package==1.1"))
        published = published_version("1.1", UPLOAD_TIME_WITHOUT_CHANGELOG)
        self.assert_new_version_logged("package", published, Location(_SCRIPT, 4))

    @kills(
        Mutation(
            lock._LockUpdate._restore_lock,
            "failure = log.lock_not_removed if target.uv_lock_text is None else log.file_not_restored",
            "failure = log.file_not_restored",
            "the run reports a lock it cannot remove as one it cannot give back the text it held before the run",
        ),
    )
    def test_a_lock_the_run_cannot_remove_is_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that a lock the run created and cannot remove is reported, and the script keeps its new pins."""
        files = FakeFiles({_SCRIPT: script("package==1.0")})
        error = PermissionError(13, "Permission denied", str(_SCRIPT_LOCK))
        files.remove_errors[_SCRIPT_LOCK] = error
        left = self.files_after_update(run, get, glob, files, _script_lock("1.1"))
        self.assertEqual(left[_SCRIPT], script("package==1.1"))
        self.assert_error_logged(message_of(Logger.lock_not_removed), location=Location(_SCRIPT_LOCK), error=str(error))

    def test_a_lock_that_cannot_be_read_is_reported_and_its_script_is_left_as_it_is(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a script's lock the run cannot read is reported, and that uv locks the other scripts alone."""
        other = SCAN_ROOT / "other.py"
        other_lock = SCAN_ROOT / "other.py.lock"
        contents = script("package==1.0")
        files = FakeFiles({_SCRIPT: contents, other: contents, other_lock: _script_lock("1.0")})
        error = PermissionError(13, "Permission denied", str(other_lock))
        files.read_errors[other_lock] = error
        left = self.files_after_update(run, get, glob, files, _script_lock("1.1"), discovered=[_SCRIPT, other])
        self.assertEqual(left[_SCRIPT], script("package==1.1"))
        self.assertEqual(left[other], contents)
        self.assertEqual({command[3] for command in uv_locks(run)}, {str(_SCRIPT)})
        self.assert_errors_logged(
            (message_of(Logger.lock_not_read), {"location": Location(other_lock), "error": str(error)})
        )

    @kills(
        Mutation(
            update_python_inline_script_metadata._scripts,
            "except (OSError, UnicodeDecodeError) as error:",
            "except OSError as error:",
            "a .py file that is not UTF-8 stops the update of every script",
            raises="UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
        ),
        Mutation(
            update_python_inline_script_metadata._scripts,
            "_LOG.unreadable_script(path, error)",
            "_ = error",
            "the run skips an unreadable .py file without a word",
        ),
    )
    def test_a_py_file_that_cannot_be_read_is_reported_and_skipped(self, run: Mock, get: Mock, glob: Mock):
        """Test that a .py file the run cannot read is reported, and that uv locks the other scripts alone."""
        other = SCAN_ROOT / "other.py"
        for case, error in {
            "a .py file without read permission": PermissionError(13, "Permission denied", str(other)),
            "a .py file that is not UTF-8": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        }.items():
            with self.subTest(case):
                run.reset_mock()
                files = FakeFiles({other: script("package==1.0"), _SCRIPT: script("package==1.0")})
                files.read_errors[other] = error
                left = self.files_after_update(run, get, glob, files, _script_lock("1.1"), discovered=[other, _SCRIPT])
                self.assertEqual(left[_SCRIPT], script("package==1.1"))
                self.assertEqual({command[3] for command in uv_locks(run)}, {str(_SCRIPT)})
                self.assert_logged(message_of(Logger.unreadable_script), location=Location(other), error=str(error))

    def test_without_an_earlier_lock_a_dependency_without_an_exact_pin_gets_no_new_version(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a dependency without an exact pin is left as it is, and its locked version is not reported."""
        left = self.files_after_update(run, get, glob, {_SCRIPT: script("package>=1.0")}, _script_lock("1.1"))
        self.assertEqual(left[_SCRIPT], script("package>=1.0"))
        get.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_python_inline_script_metadata._scripts,
            "_LOG.path(path)",
            "pass",
            "a script is not logged as checked before the lines about it",
        )
    )
    def test_a_failed_uv_lock_of_the_script_as_written_is_reported_at_error_and_nothing_locks_after_it(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a script's failed first lock is reported after its checked line, and nothing locks after it."""
        contents = script("package==1.0", marker="ignore[update]")
        left = self.updated_with_failing_lock(run, glob, contents)
        self.assertEqual(["--upgrade" in command for command in uv_locks(run)], [False])
        self.assertEqual(left[_SCRIPT], contents)
        get.assert_not_called()
        self.assert_command_stderr_logged(stderr=NO_SOLUTION)
        self.assert_uv_dependencies_not_updated_logged(_SCRIPT, "uv lock")
        self.assert_path_logged_first(_SCRIPT)
        self.assert_recognised_marker_logged("package", Location(_SCRIPT, 4), Marker(ignored_scopes=Scope.UPDATE))

    @kills(
        Mutation(
            update_python_inline_script_metadata._scripts,
            "if carries_a_block:\n            _LOG.path(path)",
            "_LOG.path(path)\n        if carries_a_block:",
            "every .py file is logged as checked, whether or not it carries a script block",
        )
    )
    def test_skip_py_file_without_script_block(self, run: Mock, get: Mock, glob: Mock):
        """Test that a .py file without a `# /// script` block is left untouched and not logged as checked."""
        contents = "import os\n\nprint(os.getcwd())\n"
        left = self.files_after_update(run, get, glob, {_SCRIPT: contents}, LOCK_FAILS)
        run.assert_not_called()
        get.assert_not_called()
        self.assertEqual(left[_SCRIPT], contents)
        self.assert_no_new_version_logged()
        self.assert_no_debug_logged()


@no_vulnerabilities
@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
@patch("requests.Session.get", side_effect=pypi_answer)
@patch("subprocess.run")
class ScriptRedundantDirectiveTest(_ScriptLockTestCase):
    """Unit tests for the directives a run reports as redundant in a script that uv locks."""

    def test_a_directive_steering_the_lock_is_not_reported_as_redundant(self, run: Mock, get: Mock, glob: Mock):
        """Test that an `ignore[update]` on a range in a script that has a lock goes unreported."""
        files = {
            _SCRIPT: script("package>=0.9", marker="ignore[update] set[cooldown=30]"),
            _SCRIPT_LOCK: _script_lock("1.0"),
        }
        self.files_after_update(run, get, glob, files, _script_lock("1.0"))
        self.assert_redundant_directive_logged(
            Reason.COOLDOWN_PER_RUN, "package", Location(_SCRIPT, 4), "set[cooldown=30]"
        )

    def test_an_ignore_update_on_a_dependency_without_an_exact_pin_is_reported_where_the_script_had_no_lock(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that the `ignore[update]` is reported, since the run removes the lock that would keep the version."""
        contents = script("package>=0.9", marker="ignore[update]")
        self.files_after_update(
            run, get, glob, {_SCRIPT: contents}, _script_lock("1.0"), lock_as_written=_script_lock("1.0")
        )
        self.assert_redundant_directive_logged(
            Reason.MANAGER_RESOLVES_THE_VERSION, "package", Location(_SCRIPT, 4), "ignore[update]"
        )

    @kills(
        Mutation(
            marker_policy._unkept_lock_bound_redundancy,
            "return None if declaration.pins_a_version else Reason.BOUND_DECIDES_NOTHING",
            "return None",
            "a bound on a range in a script without a lock goes unreported, though the run removes its lock",
        ),
        Mutation(
            lock._Member.bounded_declarations,
            "if self.policy.bound_redundancy(declaration) is None]",
            "if declaration.narrowable]",
            "a bound on a range narrows the lock the run removes, and is judged at the version that lock records",
        ),
    )
    def test_a_bound_on_a_dependency_without_an_exact_pin_narrows_nothing_and_is_reported_where_the_script_had_no_lock(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a bound on a range narrows nothing, and is reported as deciding nothing alone, as the lock goes."""
        url = "other @ https://example.com/other-1.0.tar.gz"
        cases = {
            "specifier bound": ("package>=0.9", "allow[update>=1]", "package", Reason.BOUND_DECIDES_NOTHING),
            "bound on a direct URL": (url, "allow[update<2]", "other", Reason.URL_RATHER_THAN_VERSION),
        }
        for case, (spec, bound, dependency, reason) in cases.items():
            with self.subTest(case):
                contents = script(spec, marker=bound)
                lock_after = _script_lock("1.0")
                self.files_after_update(
                    run, get, glob, {_SCRIPT: contents}, lock_after, lock_as_written=_script_lock("1.0")
                )
                self.assertEqual(self.text_locked_with_upgrade(), contents)
                self.assert_redundant_directive_logged(reason, dependency, Location(_SCRIPT, 4), bound)
