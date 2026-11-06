"""Unit tests for the inline-script-metadata updater (discovery and orchestration of the uv package manager)."""

from unittest.mock import ANY, Mock, patch

from update_time.io.log import Logger
from update_time.manifests.inline_script import InlineScript
from update_time.markers.directive import Reason
from update_time.markers.marker import Marker, Scope
from update_time.package_managers.uv import lock
from update_time.primitives.location import Location
from update_time.updaters.update_python_inline_script_metadata import update_python_inline_script_metadatas

from tests.helpers import FakeFiles, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.helpers import SCAN_ROOT, LoggingTestCase, message_of, published_version, uv_path_source
from tests.update_time.manifests.helpers import script
from tests.update_time.updaters.helpers import (
    LOCK_FAILS,
    NO_SOLUTION,
    PYPROJECT_TOML,
    UPLOAD_TIME_WITHOUT_CHANGELOG,
    UvLockTestCase,
    failing_uv_lock,
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
        update_python_inline_script_metadatas()


# The checks over the settled pins are stubbed out so these tests focus on the update flow: each check makes registry
# requests of its own, and what they report is `test_uv_pins.py`'s to pin, while being handed the scripts found is
# `CheckedInlineScriptPinsTest`'s below.
@patch("update_time.updaters.update_python_inline_script_metadata.warn_about_pins", Mock())
@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
@patch("requests.Session.get")
@patch("subprocess.run")
class ScriptLockTest(_ScriptLockTestCase):
    """Unit tests for the exact pins of a script, which take the versions uv locks for the script."""

    def updated_pin(self, run: Mock, get: Mock, glob: Mock, *, marker: str = "", latest: str = "1.1") -> FakeFiles:
        """Update a script whose only dependency pins the package on 1.0, which uv lock moves to the latest."""
        return self.updated_lock(run, get, glob, {_SCRIPT: script("package==1.0", marker=marker)}, _script_lock(latest))

    def updated_with_failing_lock(self, run: Mock, glob: Mock, contents: str) -> FakeFiles:
        """Update the script holding the contents, with every uv lock failing, and return the files."""
        files = FakeFiles({_SCRIPT: contents})
        run.side_effect = failing_uv_lock
        glob.return_value = [_SCRIPT]
        with files.patched():
            self.update()
        return files

    @kills(
        Mutation(
            lock._LockUpdate.update,
            "if self.target.keeps_the_lock:\n            self.log.path(self.target.uv_lock.path)",
            "self.log.path(self.target.uv_lock.path)",
            "the run reports checking a lock it removes again, which the user never sees",
        )
    )
    def test_a_script_without_a_lock_takes_the_locked_versions_and_is_left_without_a_lock(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that an exact pin moves to the version uv locks, with the cooldown passed to uv, and the lock goes."""
        left = self.updated_pin(run, get, glob)
        self.assertEqual(left[_SCRIPT], script("package==1.1"))
        self.assertEqual(self.text_locked_with_upgrade(), script("package>=1.0"))
        self.assertNotIn(_SCRIPT_LOCK, left)
        self.assert_none_logged(message_of(Logger.path), "path of the removed lock", location=Location(_SCRIPT_LOCK))
        as_written, upgrade = uv_locks(run)
        self.assertEqual(
            [command[:4] for command in (as_written, upgrade)], [("uv", "lock", "--script", str(_SCRIPT))] * 2
        )
        self.assertEqual(["--upgrade" in command for command in (as_written, upgrade)], [False, True])
        self.assertEqual(["--exclude-newer" in command for command in (as_written, upgrade)], [True, True])
        published = published_version("1.1", UPLOAD_TIME_WITHOUT_CHANGELOG)
        self.assert_new_version_logged("package", published, Location(_SCRIPT, 4))
        self.assert_no_warnings_logged()

    def test_a_script_with_a_lock_keeps_it_locked_again_on_the_new_pins_without_the_cooldown(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that the lock a script had before the run is locked again on the new pins, without the cutoff."""
        files = {_SCRIPT: script("package==1.0"), _SCRIPT_LOCK: _script_lock("1.0")}
        left = self.updated_lock(run, get, glob, files, _script_lock("1.1"))
        self.assertEqual(left[_SCRIPT], script("package==1.1"))
        self.assertEqual(left[_SCRIPT_LOCK], _script_lock("1.1"))
        self.assert_path_logged(_SCRIPT_LOCK)
        *_, (_, files_at_the_last_lock) = self.locks
        self.assertEqual(files_at_the_last_lock[_SCRIPT], script("package==1.1"))
        self.assertEqual(["--upgrade" in command for command in uv_locks(run)], [False, True, False])
        self.assertEqual(["--exclude-newer" in command for command in uv_locks(run)], [True, True, False])

    def test_a_dependency_locked_at_several_versions_is_pinned_and_reported_at_the_lowest(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a pin moves to the lowest version uv locks for the script, where it locks several."""
        left = self.updated_lock(run, get, glob, {_SCRIPT: script("package==1.0")}, _script_lock("1.1", "1.2"))
        self.assertEqual(left[_SCRIPT], script("package==1.1"))
        published = published_version("1.1", UPLOAD_TIME_WITHOUT_CHANGELOG)
        self.assert_new_version_logged("package", published, Location(_SCRIPT, 4))

    def test_the_version_an_earlier_lock_moves_for_a_dependency_without_an_exact_pin_is_reported(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that where a script had a lock, the version it moves for a dependency without a pin is reported."""
        files = {_SCRIPT: script("package>=1.0"), _SCRIPT_LOCK: _script_lock("1.0")}
        left = self.updated_lock(run, get, glob, files, _script_lock("1.1"))
        self.assertEqual(left[_SCRIPT], script("package>=1.0"))
        published = published_version("1.1", UPLOAD_TIME_WITHOUT_CHANGELOG)
        self.assert_new_version_logged("package", published, Location(_SCRIPT, 4))

    @kills(
        Mutation(
            lock._LockTarget.script,
            "uv_lock,\n            set(),",
            "uv_lock,\n            workspace_sources(path),",
            "a script takes on the sources of a workspace root above it, which `uv lock --script` ignores",
        )
    )
    def test_a_pin_the_workspace_root_beside_the_script_sources_is_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pin moves and is reported, though a workspace root beside the script sources its package."""
        root = "[tool.uv.workspace]\nmembers = []\n" + uv_path_source("package")
        files = {_SCRIPT: script("package==1.0"), PYPROJECT_TOML: root}
        left = self.updated_lock(run, get, glob, files, _script_lock("1.1"))
        self.assertEqual(left[_SCRIPT], script("package==1.1"))
        published = published_version("1.1", UPLOAD_TIME_WITHOUT_CHANGELOG)
        self.assert_new_version_logged("package", published, Location(_SCRIPT, 4))

    @kills(
        Mutation(
            lock._LockUpdate._restore_lock,
            "failure = self.log.lock_not_removed if self.target.uv_lock_text is None else self.log.file_not_restored",
            "failure = self.log.file_not_restored",
            "the run reports a lock it cannot remove as one it cannot give back the text it held before the run",
        ),
    )
    def test_a_lock_the_run_cannot_remove_is_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that a lock the run created and cannot remove is reported, and the script keeps its new pins."""
        files = FakeFiles({_SCRIPT: script("package==1.0")})
        error = PermissionError(13, "Permission denied", str(_SCRIPT_LOCK))
        files.remove_errors[_SCRIPT_LOCK] = error
        left = self.updated_lock(run, get, glob, files, _script_lock("1.1"))
        self.assertEqual(left[_SCRIPT], script("package==1.1"))
        self.assert_error_logged(message_of(Logger.lock_not_removed), location=Location(_SCRIPT_LOCK), error=str(error))

    def test_a_pin_uv_lock_keeps_leaves_the_script_as_it_was(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pin uv lock keeps on its version leaves the script as it was, and Update-time does not log it."""
        left = self.updated_pin(run, get, glob, latest="1.0")
        self.assertEqual(left[_SCRIPT], script("package==1.0"))
        get.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_without_an_earlier_lock_a_dependency_without_an_exact_pin_gets_no_new_version(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a dependency without an exact pin is left as it is, and its locked version is not reported."""
        left = self.updated_lock(run, get, glob, {_SCRIPT: script("package>=1.0")}, _script_lock("1.1"))
        self.assertEqual(left[_SCRIPT], script("package>=1.0"))
        get.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_a_marker_holds_the_update_back(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pin whose marker holds its update back keeps its version, and is reported as held back."""
        left = self.updated_pin(run, get, glob, marker="ignore[update]")
        location = Location(_SCRIPT, 4)
        self.assertNotIn(_SCRIPT, left.written_paths)
        get.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_recognised_marker_logged("package", location, Marker(ignored_scopes=Scope.UPDATE))
        self.assert_ignored_logged("package", location, "ignore[update]", among_others=True)

    def test_a_name_declared_twice_and_held_back_once_stays_on_the_held_back_version(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a name declared twice is relaxed on the line its marker leaves, and stays where uv locks it."""
        held_back = script("package==1.0", marker="ignore[stlae]")
        contents = held_back.replace("# ]\n", '#     "package==1.0",\n# ]\n')
        # uv locks the name at the version the held-back pin names, since the held-back line allows that version alone.
        left = self.updated_lock(run, get, glob, {_SCRIPT: contents}, _script_lock("1.0"))
        self.assertEqual(self.text_locked_with_upgrade(), held_back.replace("# ]\n", '#     "package>=1.0",\n# ]\n'))
        self.assertEqual(left[_SCRIPT], contents)
        self.assert_no_new_version_logged()
        self.assert_invalid_bracket_item_logged("package", Location(_SCRIPT, 4), "stlae")

    def test_a_failed_uv_lock_of_the_script_as_written_is_reported_at_error_and_nothing_locks_after_it(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a failed lock of the script as written is reported with its markers, and nothing locks after it."""
        contents = script("package==1.0", marker="ignore[update]")
        left = self.updated_with_failing_lock(run, glob, contents)
        self.assertEqual(["--upgrade" in command for command in uv_locks(run)], [False])
        self.assertEqual(left[_SCRIPT], contents)
        get.assert_not_called()
        self.assert_command_stderr_logged(stderr=NO_SOLUTION)
        self.assert_uv_dependencies_not_updated_logged(_SCRIPT, "uv lock")
        self.assert_recognised_marker_logged("package", Location(_SCRIPT, 4), Marker(ignored_scopes=Scope.UPDATE))

    @patch("shutil.which", Mock(return_value=None))
    def test_a_uv_missing_from_the_path_is_reported_in_one_line(self, run: Mock, get: Mock, glob: Mock):
        """Test that a missing uv is reported in one line naming the script, and that Update-time does not run it."""
        left = self.updated_lock(run, get, glob, {_SCRIPT: script("package==1.0")}, LOCK_FAILS)
        run.assert_not_called()
        self.assertEqual(left[_SCRIPT], script("package==1.0"))
        get.assert_not_called()
        self.assert_error_logged(message_of(Logger.uv_not_on_path), location=Location(_SCRIPT))

    def test_skip_py_file_without_script_block(self, run: Mock, get: Mock, glob: Mock):
        """Test that a .py file without a `# /// script` block is left untouched and does not invoke uv."""
        contents = "import os\n\nprint(os.getcwd())\n"
        left = self.updated_lock(run, get, glob, {_SCRIPT: contents}, LOCK_FAILS)
        run.assert_not_called()
        get.assert_not_called()
        self.assertEqual(left[_SCRIPT], contents)
        self.assert_no_new_version_logged()


@no_vulnerabilities
@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
@patch("requests.Session.get")
@patch("subprocess.run")
class ScriptRedundantDirectiveTest(_ScriptLockTestCase):
    """Unit tests for the directives a run reports as redundant in a script that uv locks."""

    def test_a_directive_steering_the_lock_is_not_reported_as_redundant(self, run: Mock, get: Mock, glob: Mock):
        """Test that an `ignore[update]` on a range in a script that has a lock goes unreported."""
        get.side_effect = pypi_answer
        files = {_SCRIPT: script("package>=0.9", marker="ignore[update]"), _SCRIPT_LOCK: _script_lock("1.0")}
        self.updated_lock(run, get, glob, files, _script_lock("1.0"))
        self.assert_none_logged(message_of(Logger.redundant_directive), "redundant directive")

    def test_an_ignore_update_on_a_dependency_without_an_exact_pin_is_reported_where_the_script_had_no_lock(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that the `ignore[update]` is reported, since the run removes the lock that would keep the version."""
        get.side_effect = pypi_answer
        contents = script("package>=0.9", marker="ignore[update]")
        self.updated_lock(run, get, glob, {_SCRIPT: contents}, _script_lock("1.0"), lock_as_written=_script_lock("1.0"))
        self.assert_redundant_directive_logged(
            Reason.MANAGER_RESOLVES_THE_VERSION, "package", Location(_SCRIPT, 4), "ignore[update]"
        )

    @kills(
        Mutation(
            lock._unkept_lock_bound_redundancy,
            "return None if declaration.pins_a_version else Reason.BOUND_DECIDES_NOTHING",
            "return None",
            "a bound on a range in a script without a lock goes unreported, though the run removes its lock",
        ),
        Mutation(
            lock._unkept_lock_bound_redundancy,
            "if reason := declaration.unnarrowable_reason:\n        return reason",
            "pass",
            "a bound Update-time does not narrow by is reported as one uv resolves past",
        ),
    )
    def test_a_bound_on_a_dependency_without_an_exact_pin_is_reported_where_the_script_had_no_lock(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a bound on a range is reported, since the run removes the lock the bound narrows."""
        url = "other @ https://example.com/other-1.0.tar.gz"
        cases = {
            "specifier bound": ("package>=0.9", "allow[update<2]", "package", Reason.BOUND_DECIDES_NOTHING),
            "bound on a direct URL": (url, "allow[update<2]", "other", Reason.URL_RATHER_THAN_VERSION),
        }
        for case, (spec, bound, dependency, reason) in cases.items():
            with self.subTest(case):
                get.side_effect = pypi_answer
                contents = script(spec, marker=bound)
                lock_after = _script_lock("1.0")
                self.updated_lock(run, get, glob, {_SCRIPT: contents}, lock_after, lock_as_written=_script_lock("1.0"))
                self.assert_redundant_directive_logged(reason, dependency, Location(_SCRIPT, 4), bound)


@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
@patch("requests.Session.get")
@patch("subprocess.run")
class CheckedInlineScriptPinsTest(LoggingTestCase):
    """Unit test for handing the discovered scripts to the checks uv-delegated updaters share."""

    @patch("update_time.updaters.update_python_inline_script_metadata.warn_about_pins")
    def test_the_discovered_scripts_are_checked(self, warn: Mock, run: Mock, get: Mock, glob: Mock):
        """Test that the checks are handed every script the scan found, whether uv updated it or not."""
        run.return_value = Mock(stdout="", stderr="")
        glob.return_value = [_SCRIPT]
        with FakeFiles({_SCRIPT: script("django==3.2.0")}).patched():
            update_python_inline_script_metadatas()
        get.assert_not_called()
        warn.assert_called_once_with([InlineScript(_SCRIPT)], ANY, ANY)
