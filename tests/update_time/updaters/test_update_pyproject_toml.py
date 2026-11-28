"""Unit tests for the pyproject.toml updater (discovery and orchestration of the uv package manager)."""

import tomllib
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, override
from unittest.mock import ANY, Mock, call, patch

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from mutation_time.mutation import kills
from update_time.domain.bound import BoundRedundancy
from update_time.domain.directive import Reason
from update_time.domain.marker import Marker, Scope, Threshold
from update_time.io.log import (
    Logger,
    NotRunCause,
    ToolFailure,
    ToolTask,
    _bound_reason,
    _Fields,  # simplint: ignore[private-import-path] # annotates with it
    _python_managers_reason,
    _unsupported_manager_reason,
)
from update_time.lock_files.uv_lock import UvLock, _lowest, _project_dependencies, _versions_of
from update_time.manifests import pyproject_config
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.manifests.python_dependency_toml import Declaration
from update_time.package_managers.policy import ManagerPolicy
from update_time.package_managers.uv import marker_policy
from update_time.package_managers.uv.lock import (
    _LockBeforeUpdate,
    _LockTarget,
    _LockUpdate,
    _Member,
    _read_workspace,
    _update_together,
)
from update_time.package_managers.uv.uv import (
    _LOG,
    _ManagerNames,
    _warn_about_the_managers,
    decision,
    settings_hold_exclude_newer,
)
from update_time.primitives.location import Location
from update_time.updaters.update_pyproject_toml import _update_pyproject_tomls
from update_time.updaters.uv_pins import warn_about_pins

from tests.file_mocks import FakeFiles, mock_path, patch_pathlib_path
from tests.http_mocks import mock_response, patch_get
from tests.update_time.base_test_cases import ExecutablesOnPathTestCase
from tests.update_time.fixtures import CHANGELOG, SCAN_ROOT
from tests.update_time.io.log_fields import not_run, published_version
from tests.update_time.io.log_messages import message_of
from tests.update_time.manifests.manifest_contents import pyproject, pyproject_per_line, uv_path_source
from tests.update_time.package_managers.uv_settings import CONFIGURED_EXCLUDE_NEWER
from tests.update_time.sources.osv_answers import no_vulnerabilities
from tests.update_time.sources.pypi_answers import (
    UPLOAD_TIME_WITH_CHANGELOG,
    UPLOAD_TIME_WITHOUT_CHANGELOG,
    pypi_answer,
)

from .scanning import PYPROJECT_TOML
from .uv_lock import LOCK_FAILS, NO_SOLUTION, UvLockTestCase, failing_uv_lock, locked_package, uv_locks

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from contextlib import AbstractContextManager

    from mutation_time.mutation import Registration
    from update_time.primitives.command import Command
    from update_time.sources.pypi import _ReleaseMetadata  # simplint: ignore[private-import-path] # annotates with it

    from tests.process_mocks import RunStub


# What the log renders for version 1.1, whose changelog PyPI does not name in `files_after_update`.
_NEW_VERSION: Final[str] = published_version("1.1", UPLOAD_TIME_WITHOUT_CHANGELOG)

# A run stops with one of these errors once the pyproject.toml updater stops checking that a table is a table.
_ARRAYS_NOT_IN_A_TABLE: Final[str] = "AttributeError: 'str' object has no attribute 'items'"
_KEYS_NOT_IN_A_TABLE: Final[str] = "AttributeError: 'str' object has no attribute 'get'"


def _kills_table_check(row: str, table: str, error: str) -> Registration:
    """Return a decorator registering the mutation that deletes the row checking that the table is a table."""
    return kills.delete(
        pyproject_config,
        f"    ({row}, _is_table),\n",
        f"a pyproject.toml whose {table} is not a table stops the run",
        raises=error,
        case=table,
    )


# A pyproject.toml that declares build requirements alone, which uv neither resolves nor locks.
_BUILD_REQUIREMENTS_ALONE: Final[str] = '[build-system]\nrequires = ["hatchling==1.20.0"]\n'

# An empty `[tool.uv]` table, which marks a pyproject.toml without a uv.lock beside it as a uv project.
_UV_TABLE: Final[str] = "[tool.uv]\n"

# A `[tool.uv]` table that tells uv not to manage the project.
_UV_TABLE_UNMANAGED: Final[str] = "[tool.uv]\nmanaged = false\n"

# A `[tool.pdm]` table and a `[tool.poetry]` table, which pdm-backend and poetry-core read their settings from.
_PDM_TABLE: Final[str] = "[tool.pdm]\ndistribution = true\n"
_POETRY_TABLE: Final[str] = '[tool.poetry]\nname = "x"\n'


# The lock files of uv, pdm, and Poetry beside the pyproject.toml at the root of the scan.
_UV_LOCK: Final[Path] = SCAN_ROOT / "uv.lock"
_PDM_LOCK: Final[Path] = SCAN_ROOT / "pdm.lock"
_POETRY_LOCK: Final[Path] = SCAN_ROOT / "poetry.lock"

# A workspace root at the root of the scan includes `member`, which declares its dependencies in this pyproject.toml.
_MEMBER: Final[Path] = SCAN_ROOT / "member/pyproject.toml"

# A workspace root that includes `member` and `other`, and sources `other` from the workspace.
_ROOT_SOURCING_OTHER: Final[str] = (
    '[tool.uv.workspace]\nmembers = ["member", "other"]\n\n[tool.uv.sources]\nother = { workspace = true }\n'
)

# A uv.lock lists a dependency of the project in an extra, and one in a dependency group, under this table and group.
_EXTRA: Final[tuple[str, str]] = ("optional-dependencies", "cli")
_GROUP: Final[tuple[str, str]] = ("dev-dependencies", "dev")


def _uv_lock(
    version: str, package: str = "package", project_source: str = ".", group: tuple[str, str] | None = None
) -> str:
    """Return a uv.lock that locks the package at the version, for the project at the source the lock names.

    The project's entry lists the package among its dependencies, or under the table and the group given.
    """
    project = f'[[package]]\nname = "project"\nversion = "0"\nsource = {{ virtual = "{project_source}" }}\n'
    dependency = f'[{{ name = "{package}" }}]'
    listed = f"dependencies = {dependency}" if group is None else f"[package.{group[0]}]\n{group[1]} = {dependency}"
    return f"version = 1\n\n{project}{listed}\n\n{locked_package(package, version)}"


# A uv.lock before the run that leaves out `package`, since it locks another package.
_LOCK_WITHOUT_THE_PACKAGE: Final[str] = _uv_lock("1.0", "other")

# Two projects, each the root of a workspace of its own. `app` sources `lib` by path, and both pin `package` exactly.
_LIB: Final[Path] = SCAN_ROOT / "lib/pyproject.toml"
_APP: Final[Path] = SCAN_ROOT / "app/pyproject.toml"
_LIB_AND_APP: Final[MappingProxyType[Path, str]] = MappingProxyType(
    {
        _LIB: pyproject("package==1.0"),
        _LIB.with_name("uv.lock"): _uv_lock("1.0"),
        _APP: pyproject("lib", "package==1.0") + uv_path_source("lib"),
        _APP.with_name("uv.lock"): _uv_lock("1.0"),
    }
)


class _PyprojectTomlLockTestCase(UvLockTestCase):
    """Base for the tests that run the pyproject.toml updater with uv lock faked."""

    manifest = PYPROJECT_TOML
    lock_file = _UV_LOCK

    @override
    def update(self) -> None:
        """Run the pyproject.toml updater."""
        _update_pyproject_tomls()


# What `configure_cooldown` writes is the uv package manager's tests' to pin, so it is stubbed out here, and these tests
# pin where the run calls it. The checks over the settled pins are stubbed out as well: each makes registry requests of
# its own, and what they report is `test_uv_pins.py`'s to pin, while being handed the files found is `CheckedPinsTest`'s
# below.
@patch("update_time.updaters.update_pyproject_toml.warn_about_pins", Mock())
@patch("update_time.package_managers.uv.lock.configure_cooldown", Mock())
@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
@patch_get()
@patch("subprocess.run")
class UpdatePyprojectTomlsTest(_PyprojectTomlLockTestCase):
    """Unit tests for the update pyproject.tomls function."""

    def test_an_exact_pin_takes_the_version_uv_lock_records(self, run: Mock, get: Mock, glob: Mock):
        """Test that an exact pin moves to the version uv lock records, with the cooldown left to the config."""
        self.assertEqual(self.updated_pin(run, get, glob)[PYPROJECT_TOML], pyproject("package==1.1"))
        self.assert_path_logged(_UV_LOCK)
        self.assert_new_version_logged("package", _NEW_VERSION, Location(PYPROJECT_TOML, 2))
        self.assert_no_cli_cooldown(run)
        self.assert_no_warnings_logged()

    def updated_pin(  # noqa: PLR0913 — the patched mocks travel with the fixtures they are given
        self, run: Mock, get: Mock, glob: Mock, *, package: str = "package", marker: str = "", latest: str = "1.1"
    ) -> FakeFiles:
        """Update a pyproject.toml whose only dependency pins the package on 1.0, which uv lock moves to the latest."""
        files = {PYPROJECT_TOML: pyproject(f"{package}==1.0", marker=marker), _UV_LOCK: _uv_lock("1.0", package)}
        return self.files_after_update(run, get, glob, files, _uv_lock(latest, package))

    def assert_no_cli_cooldown(self, run: Mock) -> None:
        """Assert that the three uv locks run without an `--exclude-newer` flag, since the cooldown lives in config."""
        self.assertEqual(["--exclude-newer" in command for command in uv_locks(run)], [False, False, False])

    def test_uv_lock_runs_on_the_file_as_written_before_upgrading_and_again_on_the_rewritten_pins(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that uv locks without `--upgrade` before and after the `--upgrade` lock, on the text the file holds."""
        self.updated_pin(run, get, glob)
        self.assertEqual(["--upgrade" in command for command in uv_locks(run)], [False, True, False])
        (_, files_at_the_first_lock), *_, (_, files_at_the_last_lock) = self.locks
        self.assertEqual(files_at_the_first_lock[PYPROJECT_TOML], pyproject("package==1.0"))
        self.assertEqual(files_at_the_last_lock[PYPROJECT_TOML], pyproject("package==1.1"))

    @kills.delete(
        _LockBeforeUpdate.steered_update,
        "member.warn_about_redundant_bounds(self.log)",
        "a bound that decides nothing in a pyproject.toml is not warned about",
    )
    @kills.replace(
        _Member.warn_about_redundant_bounds,
        "current_version=version",
        "current_version=declaration.current_version",
        "a bound on a dependency without an exact pin is not judged at the version locked before, so it goes "
        "unreported",
    )
    def test_a_bound_that_decides_nothing_at_the_current_version_is_redundant(self, run: Mock, get: Mock, glob: Mock):
        """Test that a bound blocking every update at the version locked before is warned about."""
        directive = "ignore[patch-update]"
        files = {PYPROJECT_TOML: pyproject("package>=0.9", marker=directive), _UV_LOCK: _uv_lock("1.0")}
        self.files_after_update(run, get, glob, files, _uv_lock("1.0"))
        self.assert_logged_among_others(
            message_of(Logger.redundant_directive),
            dependency="package",
            location=Location(PYPROJECT_TOML, 2),
            directive=directive,
            reason=_bound_reason(BoundRedundancy.BLOCKS_ALL, "1.0"),
        )

    @kills.replace(
        marker_policy._versions_locked_for,
        "by_name.get(normalized_python_name(declaration.dependency), [])",
        "by_name.get(declaration.dependency, [])",
        "a declaration that spells its name its own way misses the version the uv.lock recorded",
    )
    def test_a_bound_narrows_the_requirement_uv_locks(self, run: Mock, get: Mock, glob: Mock):
        """Test that a level bound on a name the file spells its own way joins the requirement uv locks.

        The bound is anchored at the version uv locks for the file as written, and the file keeps the text it wrote.
        """
        contents = pyproject("Package>=0.9", marker="ignore[minor-update]")
        files = {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}
        left = self.files_after_update(run, get, glob, files, _uv_lock("1.1"))
        self.assertEqual(self.text_locked_with_upgrade(), contents.replace("Package>=0.9", "Package==1.0.*,>=0.9"))
        self.assertEqual(left[PYPROJECT_TOML], contents)

    def test_a_build_requirement_keeps_its_pin_where_a_dependency_of_the_same_name_moves(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a build requirement, which uv does not lock, keeps its pin while a dependency of its name moves."""
        contents = '[project]\ndependencies = ["package==1.0"]\n[build-system]\nrequires = ["package==1.0"]\n'
        files = {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}
        left = self.files_after_update(run, get, glob, files, _uv_lock("1.1"))
        self.assertEqual(self.text_locked_with_upgrade(), contents.replace("==1.0", ">=1.0", 1))
        self.assertEqual(left[PYPROJECT_TOML], contents.replace("==1.0", "==1.1", 1))
        self.assert_new_version_logged("package", _NEW_VERSION, Location(PYPROJECT_TOML, 2))

    @kills.replace(
        _LockUpdate._report_new_pins,
        "served = pypi_served(moved_pins)",
        "served = list(moved_pins)",
        "a pin uv resolves from elsewhere than PyPI is reported, and its changelog asked of PyPI",
    )
    @kills.delete_statement(
        _LockUpdate._report_new_pins,
        'log.new_version(Reference(pin.name, "", declaration.location), DependencyVersion(pin.version))',
        "a pin uv resolves from elsewhere than PyPI moves without a word",
    )
    def test_a_pin_uv_sources_from_elsewhere_than_pypi_moves_and_is_reported_without_asking_pypi(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a pin the workspace root sources takes the locked version, reported without a PyPI lookup."""
        files = {
            PYPROJECT_TOML: _ROOT_SOURCING_OTHER,
            _MEMBER: pyproject("other==0.1"),
            _UV_LOCK: _uv_lock("0.1", "other", project_source="member"),
        }
        lock_after = _uv_lock("0.2", "other", project_source="member")
        left = self.files_after_update(run, get, glob, files, lock_after, discovered=[_MEMBER])
        self.assertEqual(left[_MEMBER], pyproject("other==0.2"))
        get.assert_not_called()
        self.assert_new_version_logged("other", "0.2", Location(_MEMBER, 2))

    @kills.delete(
        _Member.reportable_declarations,
        "if not declaration.build_requirement",
        "a version uv lock moves is also reported at a build requirement of its name, which uv does not lock",
    )
    def test_a_version_the_lock_moves_is_reported_at_the_dependency_and_not_at_a_build_requirement(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a version uv lock moves is reported at the dependency, as uv does not lock a build requirement."""
        contents = '[project]\ndependencies = ["package>=1.0"]\n[build-system]\nrequires = ["package>=1.0"]\n'
        self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}, _uv_lock("1.1"))
        self.assert_new_version_logged("package", _NEW_VERSION, Location(PYPROJECT_TOML, 2))

    def test_a_pin_with_an_environment_marker_is_left_as_it_is_and_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pin with an environment marker keeps its version, and that Update-time reports that at info."""
        contents = pyproject("package==1.0; python_version < '3.13'")
        files = {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}
        left = self.files_after_update(run, get, glob, files, _uv_lock("1.1"))
        self.assertEqual(self.text_locked_with_upgrade(), contents)
        self.assertEqual(left[PYPROJECT_TOML], contents)
        message = message_of(Logger.pin_with_environment_marker_left_as_is)
        self.assert_logged(message, dependency="package", location=Location(PYPROJECT_TOML, 2))

    @kills.delete(
        _LockUpdate._report_pins_with_environment_markers,
        "and not declaration.build_requirement",
        "a build requirement with an environment marker is reported as left, though uv does not lock it",
    )
    def test_a_build_requirement_with_an_environment_marker_is_not_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pinned build requirement with an environment marker goes without the report."""
        marked = "package==1.0; python_version < '3.13'"
        contents = f'[project]\ndependencies = ["other"]\n[build-system]\nrequires = ["{marked}"]\n'
        self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}, _uv_lock("1.1"))
        self.assert_path_logged(_UV_LOCK)
        self.assert_none_logged(message_of(Logger.pin_with_environment_marker_left_as_is), "pin left as it is")

    def test_a_name_pinned_twice_is_reported_at_each_line_pinning_it(self, run: Mock, get: Mock, glob: Mock):
        """Test that a name pinned in two arrays has its new version logged at each line pinning it."""
        contents = '[project]\ndependencies = ["package==1.0"]\n[dependency-groups]\ndev = ["package==1.0"]\n'
        self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}, _uv_lock("1.1"))
        self.assert_new_version_logged_among_others("package", _NEW_VERSION, Location(PYPROJECT_TOML, 2), ANY)
        self.assert_new_version_logged_among_others("package", _NEW_VERSION, Location(PYPROJECT_TOML, 4), ANY)

    @kills.replace(
        _project_dependencies,
        'entry.get("optional-dependencies", {})',
        "{}",
        "the versions locked for the project's extras go unreported",
        case="an extra",
    )
    @kills.replace(
        _project_dependencies,
        'entry.get("dev-dependencies", {})',
        "{}",
        "the versions locked for the project's dependency groups go unreported",
        case="a dependency group",
    )
    def test_the_locked_version_of_a_dependency_without_an_exact_pin_is_reported_where_it_moved_up(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a dependency without an exact pin is reported at its line with the version uv lock moved it to."""
        spec = '"package>=1.0,<1.2"'
        cases = {
            "an extra": (f'[project]\nname = "a"\n[project.optional-dependencies]\ncli = [{spec}]\n', _EXTRA),
            "a dependency group": (f'[project]\nname = "a"\n[dependency-groups]\ndev = [{spec}]\n', _GROUP),
        }
        for case, (contents, group) in cases.items():
            with self.subTest(case):
                files = {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0", group=group)}
                left = self.files_after_update(run, get, glob, files, _uv_lock("1.1", group=group))
                self.assertEqual(left[PYPROJECT_TOML], contents)
                self.assert_new_version_logged("package", _NEW_VERSION, Location(PYPROJECT_TOML, 4))
                self.assert_no_warnings_logged()

    def test_a_dependency_the_file_spells_its_own_way_is_reported_at_its_lines_and_keeps_its_spelling(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a name uv spells otherwise is reported at its exact pin and its range, and keeps its spelling."""
        version = published_version("1.2", UPLOAD_TIME_WITHOUT_CHANGELOG)
        for spelling, reported in (("Jinja2", "jinja2"), ("typing_extensions", "typing-extensions")):
            with self.subTest(spelling=spelling):
                contents = (
                    f'[project]\ndependencies = ["{spelling}==1.0"]\n[dependency-groups]\ndev = ["{spelling}>=1.0"]\n'
                )
                files = {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0", reported)}
                left = self.files_after_update(run, get, glob, files, _uv_lock("1.2", reported))
                self.assertEqual(left[PYPROJECT_TOML], contents.replace("==1.0", "==1.2"))
                self.assert_new_version_logged_among_others(reported, version, Location(PYPROJECT_TOML, 2), ANY)
                self.assert_new_version_logged_among_others(reported, version, Location(PYPROJECT_TOML, 4), ANY)
                self.assertEqual(len(self.new_version_records()), 2)

    @kills.replace(
        _project_dependencies,
        'entry.get("dependency-groups", {})',
        "{}",
        "the run leaves the dependency groups in the manifest of a uv.lock unreported",
    )
    def test_the_locked_version_of_a_dependency_group_of_a_workspace_root_without_a_project_is_reported(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a group of a workspace root without `[project]`, which uv.lock lists in `[manifest]`, is read."""
        manifest = '[manifest]\nmembers = ["member"]\n\n[manifest.dependency-groups]\n'
        manifest += 'dev = [{ name = "package", specifier = ">=1.0" }]\n\n'
        member = '[[package]]\nname = "member"\nversion = "0"\nsource = { virtual = "member" }\n\n'
        lock_before, lock_after = (
            f"version = 1\n\n{manifest}{member}{locked_package('package', v)}" for v in ("1.0", "1.1")
        )
        root = '[tool.uv.workspace]\nmembers = ["member"]\n\n[dependency-groups]\ndev = ["package>=1.0"]\n'
        self.files_after_update(run, get, glob, {PYPROJECT_TOML: root, _UV_LOCK: lock_before}, lock_after)
        self.assert_new_version_logged("package", _NEW_VERSION, Location(PYPROJECT_TOML, 5))

    @kills.delete(
        _versions_of,
        '[dependency["version"]] if "version" in dependency else ',
        "a dependency locked at several versions is reported at one the project may not use",
        case="the lower version listed first",
    )
    @kills.force_false(
        _lowest,
        "PEP_440.is_downgrade(lowest, version)",
        "a dependency locked at several versions is pinned at the one the lock lists first",
        case="the higher version listed first",
    )
    @kills.force_true(
        _lowest,
        "PEP_440.is_downgrade(lowest, version)",
        "a dependency locked at several versions is pinned at the one the lock lists last",
        case="the lower version listed first",
    )
    def test_a_dependency_locked_at_several_versions_is_pinned_and_reported_at_the_lowest_the_project_uses(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a dependency uv locks per Python version is pinned and reported at the lowest the project uses."""
        cases = {
            "the lower version listed first": (_forked_uv_lock("1.1", "1.4"), _forked_uv_lock("1.2", "1.5")),
            "the higher version listed first": (_forked_uv_lock("1.4", "1.1"), _forked_uv_lock("1.5", "1.2")),
        }
        contents = '[project]\ndependencies = ["package>=1.0"]\n[dependency-groups]\ndev = ["package==1.1"]\n'
        for case, (lock_before, lock_after) in cases.items():
            with self.subTest(case):
                left = self.files_after_update(
                    run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: lock_before}, lock_after
                )
                self.assertEqual(left[PYPROJECT_TOML], contents.replace("==1.1", "==1.2"))
                version = published_version("1.2", UPLOAD_TIME_WITHOUT_CHANGELOG)
                self.assert_new_version_logged_among_others("package", version, Location(PYPROJECT_TOML, 2), ANY)
                self.assert_new_version_logged_among_others("package", version, Location(PYPROJECT_TOML, 4), ANY)
                self.assertEqual(len(self.new_version_records()), 2)

    def test_a_dependency_pypi_does_not_serve_is_not_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that a dependency PyPI does not serve, such as one from a git URL, is not reported."""
        contents = pyproject("other @ git+https://github.com/org/other", "package>=1.0")
        both = '[{ name = "other" }, { name = "package" }]'
        other = '[[package]]\nname = "other"\nversion = "{}"\nsource = {{ git = "https://github.com/org/other" }}\n'
        locks = [
            f"{_uv_lock(package).replace('[{ name = "package" }]', both)}\n{other.format(other_version)}"
            for other_version, package in (("0.1", "1.0"), ("0.2", "1.1"))
        ]
        self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: locks[0]}, locks[1])
        self.assert_new_version_logged("package", _NEW_VERSION, Location(PYPROJECT_TOML, 2))

    def test_a_sibling_member_the_workspace_root_sources_is_not_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that a member's dependency on a sibling, sourced by the workspace root, is not reported."""
        root = _ROOT_SOURCING_OTHER
        both = '[{ name = "other" }, { name = "package" }]'
        other = '[[package]]\nname = "other"\nversion = "{}"\nsource = {{ editable = "other" }}\n'
        locks = [
            f"{_uv_lock(package, project_source='member').replace('[{ name = "package" }]', both)}\n"
            f"{other.format(other_version)}"
            for other_version, package in (("0.1", "1.0"), ("0.2", "1.1"))
        ]
        files = {PYPROJECT_TOML: root, _MEMBER: pyproject("other", "package>=1.0"), _UV_LOCK: locks[0]}
        self.files_after_update(run, get, glob, files, locks[1], discovered=[_MEMBER])
        self.assert_new_version_logged("package", _NEW_VERSION, Location(_MEMBER, 2))

    @kills.delete(
        UvLock.recorded_versions,
        ' if "version" in package',
        "the run stops where the lock does not record a version for a dependency, such as a dynamic sibling member",
        raises="KeyError: 'version'",
    )
    def test_a_dependency_the_lock_records_no_version_for_is_passed_over(self, run: Mock, get: Mock, glob: Mock):
        """Test that a newly locked dependency without a version, such as a dynamic sibling member, is not reported."""
        root = _ROOT_SOURCING_OTHER
        other = '[[package]]\nname = "other"\nsource = { editable = "other" }\n'
        lock_before = _uv_lock("1.0", project_source="member")
        lock_after = _uv_lock("1.1", project_source="member").replace(
            '[{ name = "package" }]', '[{ name = "other" }, { name = "package" }]'
        )
        files = {PYPROJECT_TOML: root, _MEMBER: pyproject("other", "package>=1.0"), _UV_LOCK: lock_before}
        self.files_after_update(run, get, glob, files, f"{lock_after}\n{other}", discovered=[_MEMBER])
        self.assert_new_version_logged("package", _NEW_VERSION, Location(_MEMBER, 2))

    def test_a_version_the_lock_of_the_file_as_written_moves_is_reported_against_the_uv_lock_before_the_run(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a version a stale uv.lock is moved to is reported, though the upgrade does not move it further."""
        files = {PYPROJECT_TOML: pyproject("package>=1.1"), _UV_LOCK: _uv_lock("1.0")}
        self.files_after_update(run, get, glob, files, _uv_lock("1.2"), lock_as_written=_uv_lock("1.2"))
        version = published_version("1.2", UPLOAD_TIME_WITHOUT_CHANGELOG)
        self.assert_new_version_logged("package", version, Location(PYPROJECT_TOML, 2))

    def test_a_version_uv_lock_moves_back_is_reported_as_moved_back(self, run: Mock, get: Mock, glob: Mock):
        """Test that a version uv lock moves back for the cooldown is reported as moved back, without a changelog."""
        files = {PYPROJECT_TOML: pyproject("package>=1.0"), _UV_LOCK: _uv_lock("1.1")}
        self.files_after_update(run, get, glob, files, _uv_lock("1.0"))
        get.assert_not_called()
        self.assert_moved_back_logged("uv", "package", "1.1", "1.0", Location(PYPROJECT_TOML, 2))

    @kills.replace(
        _LockUpdate._rewrite,
        "except OSError as error:",
        "except ValueError as error:",
        "a pyproject.toml that cannot be written stops the run, leaving the other files of its workspace relaxed",
        raises="PermissionError: [Errno 13] Permission denied: '/member/pyproject.toml'",
    )
    def test_a_pyproject_toml_that_cannot_be_written_gives_the_workspace_its_files_back(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a pyproject.toml the run cannot write is reported, and its workspace gets its files back."""
        files = FakeFiles(
            {
                PYPROJECT_TOML: _root_declaring("package==1.0"),
                _MEMBER: pyproject("package==1.0"),
                _UV_LOCK: _uv_lock("1.0"),
            }
        )
        error = PermissionError(13, "Permission denied", str(_MEMBER))
        files.write_errors[_MEMBER] = error
        left = self.files_after_update(run, get, glob, files, _uv_lock("1.1"), discovered=[PYPROJECT_TOML, _MEMBER])
        self.assertEqual(left[PYPROJECT_TOML], _root_declaring("package==1.0"))
        self.assertEqual(["--upgrade" in command for command in uv_locks(run)], [False])
        self.assert_errors_logged(
            (message_of(Logger.file_not_written), {"location": Location(_MEMBER), "error": str(error)})
        )

    @kills.delete(
        _LockTarget._read,
        "UnicodeDecodeError",
        "a uv.lock that is not UTF-8 stops the run",
        raises="UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
        case="a uv.lock that is not UTF-8",
    )
    @kills.delete(
        _LockTarget._read,
        "OSError, ",
        "a uv.lock without read permission stops the run",
        raises="PermissionError: [Errno 13] Permission denied: '/other/uv.lock'",
        case="a uv.lock without read permission",
    )
    def test_a_uv_lock_that_cannot_be_read_is_reported_and_its_workspace_is_left_as_it_is(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that an unreadable uv.lock is reported, and that only the other workspaces get uv and a cooldown."""
        other = SCAN_ROOT / "other/pyproject.toml"
        other_lock = SCAN_ROOT / "other/uv.lock"
        for case, error in {
            "a uv.lock without read permission": PermissionError(13, "Permission denied", str(other_lock)),
            "a uv.lock that is not UTF-8": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        }.items():
            with self.subTest(case):
                contents = pyproject("package==1.0")
                files = FakeFiles(
                    {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0"), other: contents, other_lock: ""}
                )
                files.read_errors[other_lock] = error
                discovered = [PYPROJECT_TOML, other]
                with patch("update_time.package_managers.uv.lock.configure_cooldown") as configure_cooldown:
                    left = self.files_after_update(run, get, glob, files, _uv_lock("1.1"), discovered=discovered)
                self.assertEqual(configure_cooldown.call_args_list, [call(PYPROJECT_TOML)])
                self.assertEqual(left[PYPROJECT_TOML], pyproject("package==1.1"))
                self.assertEqual(left[other], contents)
                directories = {command[command.index("--directory") + 1] for command in uv_locks(run)}
                self.assertEqual(directories, {str(SCAN_ROOT)})
                self.assert_errors_logged(
                    (message_of(Logger.lock_not_read), {"location": Location(other_lock), "error": str(error)})
                )

    def test_a_uv_lock_that_cannot_be_written_back_is_reported_and_the_pyproject_toml_is_written_back(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a failed uv lock reports a uv.lock it cannot write back, and gives the pyproject.toml its text.

        The uv.lock keeps the version the `--upgrade` lock moved the pin to, which Update-time does not report.
        """
        contents = pyproject("package==1.0")
        files = FakeFiles({PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")})
        error = PermissionError(13, "Permission denied", str(_UV_LOCK))
        files.write_errors[_UV_LOCK] = error
        left = self.files_after_update(run, get, glob, files, (_uv_lock("1.1"), LOCK_FAILS))
        self.assertEqual(left[PYPROJECT_TOML], contents)
        self.assertEqual(left[_UV_LOCK], _uv_lock("1.1"))
        self.assert_errors_logged(
            (
                message_of(Logger.tool_failed),
                {
                    "location": Location(PYPROJECT_TOML),
                    "task": ToolTask.UPDATE,
                    "tool": "uv lock",
                    "failure": ToolFailure.FAILED,
                },
            ),
            (message_of(Logger.file_not_restored), {"location": Location(_UV_LOCK), "error": str(error)}),
        )
        self.assert_no_new_version_logged()

    def test_a_failed_uv_lock_keeps_the_cooldown_the_run_wrote(self, run: Mock, get: Mock, glob: Mock):
        """Test that uv locks the pyproject.toml with the cooldown the run wrote, and a failed lock leaves it there."""
        contents = pyproject("package==1.0") + _UV_TABLE
        cooldown = 'exclude-newer = "7 days"\n'

        def configure_cooldown(root: Path) -> None:
            PyprojectToml(root).write(PyprojectToml(root).text() + cooldown)

        with patch("update_time.package_managers.uv.lock.configure_cooldown", side_effect=configure_cooldown):
            files = {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}
            left = self.files_after_update(run, get, glob, files, LOCK_FAILS)
        self.assert_uv_dependencies_not_updated_logged(PYPROJECT_TOML, "uv lock")
        (_, files_at_the_first_lock), *_ = self.locks
        self.assertEqual(files_at_the_first_lock[PYPROJECT_TOML], contents + cooldown)
        self.assertEqual(left[PYPROJECT_TOML], contents + cooldown)

    def test_a_pyproject_toml_is_logged_as_checked_before_any_other_line_about_it(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that the line saying the pyproject.toml is checked comes before the cooldown the run sets in it."""

        def configure_cooldown(root: Path) -> None:
            _LOG.configured_cooldown(root, root, "7 days", "uv exclude-newer")

        with patch("update_time.package_managers.uv.lock.configure_cooldown", side_effect=configure_cooldown):
            self.updated_pin(run, get, glob)
        self.assert_path_logged_first(PYPROJECT_TOML)

    @kills.delete(
        _read_workspace,
        "if target.keeps_the_lock:\n        ",
        "the run writes the cooldown into the root of a workspace that it leaves without a lock",
    )
    def test_without_an_earlier_lock_the_pin_moves_the_cooldown_goes_to_uv_alone_and_the_uv_lock_goes(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that an exact pin moves to the locked version, the cooldown goes to uv alone, and the uv.lock goes."""
        contents = (
            '[project]\nname = "a"\ndependencies = ["package>=1.0"]\n[dependency-groups]\ndev = ["package==1.0"]\n'
            f"{_UV_TABLE}"
        )
        with (
            patch("update_time.package_managers.uv.lock.cooldown_cutoff", Mock(return_value="cutoff")),
            patch("update_time.package_managers.uv.lock.configure_cooldown") as configure_cooldown,
        ):
            left = self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents}, _uv_lock("1.2"))
        configure_cooldown.assert_not_called()
        self.assertEqual(left[PYPROJECT_TOML], contents.replace("package==1.0", "package==1.2"))
        self.assertNotIn(_UV_LOCK, left)
        self.assert_none_logged(message_of(Logger.path), "path of the removed lock", location=Location(_UV_LOCK))
        self.assertEqual(["--upgrade" in command for command in uv_locks(run)], [False, True])
        self.assertEqual(["--exclude-newer" in command for command in uv_locks(run)], [True, True])
        cutoffs = [command[command.index("--exclude-newer") + 1] for command in uv_locks(run)]
        self.assertEqual(cutoffs, ["cutoff", "cutoff"])
        version = published_version("1.2", UPLOAD_TIME_WITHOUT_CHANGELOG)
        self.assert_new_version_logged("package", version, Location(PYPROJECT_TOML, 5))

    def test_without_an_earlier_lock_uv_locks_without_a_cutoff_where_uvs_settings_hold_an_exclude_newer(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that uv locks with the exclude-newer the project's settings hold, rather than with a cutoff."""
        self.configured_exclude_newer = CONFIGURED_EXCLUDE_NEWER
        contents = pyproject("package==1.0") + _UV_TABLE
        left = self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents}, _uv_lock("1.1"))
        self.assertEqual(left[PYPROJECT_TOML], pyproject("package==1.1") + _UV_TABLE)
        self.assertEqual(["--exclude-newer" in command for command in uv_locks(run)], [False, False])
        self.assert_project_cooldown_logged(PYPROJECT_TOML, "uv exclude-newer")

    def test_the_locked_version_of_a_workspace_members_dependency_is_read_from_the_workspace_root(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a member's dependency is reported with the version the root's lock moved, and that lock logged."""
        files = {
            PYPROJECT_TOML: '[tool.uv.workspace]\nmembers = ["member"]\n',
            _MEMBER: pyproject("package>=1.0"),
            _UV_LOCK: _uv_lock("1.0", project_source="member"),
        }
        self.files_after_update(run, get, glob, files, _uv_lock("1.1", project_source="member"), discovered=[_MEMBER])
        self.assert_new_version_logged("package", _NEW_VERSION, Location(_MEMBER, 2))
        self.assert_path_logged(_UV_LOCK)

    def test_each_workspace_gets_the_cooldown_and_one_upgrade_at_its_root(self, run: Mock, get: Mock, glob: Mock):
        """Test that each workspace gets the cooldown and one upgrade at its root, however many projects it holds."""
        other = SCAN_ROOT / "other/pyproject.toml"
        cases = {
            "one workspace": (
                {
                    PYPROJECT_TOML: _root_declaring("package>=1.0"),
                    _MEMBER: pyproject("package>=1.0"),
                    _UV_LOCK: _uv_lock("1.0"),
                },
                [SCAN_ROOT],
            ),
            "two projects": (
                {
                    PYPROJECT_TOML: pyproject("package>=1.0"),
                    other: pyproject("package>=1.0"),
                    _UV_LOCK: _uv_lock("1.0"),
                    other.with_name("uv.lock"): _uv_lock("1.0"),
                },
                [SCAN_ROOT, other.parent],
            ),
        }
        for case, (files, roots) in cases.items():
            with self.subTest(case):
                run.reset_mock()
                discovered = [path for path in files if path.name == "pyproject.toml"]
                with patch("update_time.package_managers.uv.lock.configure_cooldown") as configure_cooldown:
                    self.files_after_update(run, get, glob, files, _uv_lock("1.0"), discovered=discovered)
                self.assertEqual(configure_cooldown.call_args_list, [call(root / "pyproject.toml") for root in roots])
                upgrades = [command for command in uv_locks(run) if "--upgrade" in command]
                directories = [command[command.index("--directory") + 1] for command in upgrades]
                self.assertEqual(directories, [str(root) for root in roots])
                logged = [
                    self.records_of(message_of(Logger.path), location=Location(root / "uv.lock")) for root in roots
                ]
                self.assertEqual([len(records) for records in logged], [1] * len(roots))
                get.assert_not_called()

    def test_a_dependent_found_first_moves_the_pin_it_shares_with_its_path_dependency_along_with_it(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a dependent found before a project it sources by path moves the pin they share along with it."""
        files = self.updated_lib_and_app(run, get, glob, {}, [_APP, _LIB])
        self.assertEqual(files[_LIB], pyproject("package==1.1"))
        self.assertEqual(files[_APP], pyproject("lib", "package==1.1") + uv_path_source("lib"))

    def updated_lib_and_app(  # noqa: PLR0913 — the patched mocks travel with the fixtures they are given
        self,
        run: Mock,
        get: Mock,
        glob: Mock,
        changed: Mapping[Path, str],
        discovered: list[Path],
        *,
        unreachable_index: Path | None = None,
    ) -> FakeFiles:
        """Update `lib` and `app`, with the changed files replacing theirs, and return the files.

        The scan finds the files in the order `discovered` gives. The uv stub fails to upgrade the lock in the
        directory `unreachable_index`, and records each lock in `locks`.
        """
        files = FakeFiles({**_LIB_AND_APP, **changed})
        self.locks = []
        run.side_effect = _uv_lock_resolving_path_sources(files, self.locks, unreachable_index)
        get.side_effect = pypi_answer
        glob.return_value = discovered
        with files.patched():
            _update_pyproject_tomls()
        return files

    @kills.delete_statement(
        _update_together,
        "lock_update.relax()",
        "each workspace of a cycle upgrades while the others still pin the version they share, so none moves",
    )
    @kills.delete(
        _update_together,
        "(lock_update, lock_update.lock) for lock_update in lock_updates[:-1]",
        "an earlier workspace of a cycle keeps a lock that records the others' pins from before they moved",
    )
    def test_workspaces_whose_projects_source_each_other_by_path_move_the_pin_they_share_together(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that projects sourcing each other by path move a shared pin together, and that each lock records it."""
        lib = pyproject("app", "package==1.0") + uv_path_source("app")
        files = self.updated_lib_and_app(run, get, glob, {_LIB: lib}, [_LIB, _APP])
        app = pyproject("lib", "package==1.1") + uv_path_source("lib")
        self.assertEqual(files[_LIB], lib.replace("package==1.0", "package==1.1"))
        self.assertEqual(files[_APP], app)
        self.assertEqual([locked for command, locked in self.locks if str(_LIB.parent) in command][-1][_APP], app)

    def test_a_cycle_of_path_sources_is_rolled_back_where_a_later_workspace_cannot_be_locked(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a workspace whose cycle partner cannot be locked gets its files back and is reported."""
        changed = {_LIB: (pyproject("app", "package==1.0") + uv_path_source("app"))}
        files = self.updated_lib_and_app(run, get, glob, changed, [_LIB, _APP], unreachable_index=_APP.parent)
        self.assertEqual(dict(files), {**_LIB_AND_APP, **changed})
        self.assert_no_new_version_logged()
        self.assert_errors_logged(
            (
                message_of(Logger.tool_failed),
                {"location": Location(_APP), "task": ToolTask.UPDATE, "tool": "uv lock", "failure": ToolFailure.FAILED},
            ),
            (message_of(Logger.rolled_back_with_cycle), {"location": Location(_LIB), "failed": (Location(_APP),)}),
        )

    @kills.replace(
        _LockUpdate.move_pins,
        "if not self.lock_as_written_succeeded:\n            return False",
        "if not self.lock_as_written_succeeded:\n            return True",
        "a workspace of a cycle whose files as written uv cannot lock keeps the requirements relaxed for the cycle",
    )
    def test_a_cycle_of_path_sources_whose_files_as_written_cannot_be_locked_stays_as_it_was(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a cycle stays as it was where uv cannot lock the files of its workspaces as written."""
        changed = {
            _LIB: pyproject("app", "package==1.0") + uv_path_source("app"),
            _APP: pyproject("lib", "package==1.1") + uv_path_source("lib"),
        }
        files = self.updated_lib_and_app(run, get, glob, changed, [_LIB, _APP])
        self.assertEqual(dict(files), {**_LIB_AND_APP, **changed})
        not_updated = message_of(Logger.tool_failed)
        self.assert_errors_logged(
            (
                not_updated,
                {"location": Location(_LIB), "task": ToolTask.UPDATE, "tool": "uv lock", "failure": ToolFailure.FAILED},
            ),
            (
                not_updated,
                {"location": Location(_APP), "task": ToolTask.UPDATE, "tool": "uv lock", "failure": ToolFailure.FAILED},
            ),
            (message_of(Logger.rolled_back_with_cycle), {"location": Location(_APP), "failed": (Location(_LIB),)}),
        )

    def test_a_dependent_holding_back_the_pin_its_path_dependency_moves_is_reported_as_not_updated(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that where a dependent's marker keeps a pin its path dependency moves, uv cannot lock the dependent."""
        app = pyproject("lib", "package==1.0", marker="ignore[update]") + uv_path_source("lib")
        files = self.updated_lib_and_app(run, get, glob, {_APP: app}, [_LIB, _APP])
        self.assertEqual(files[_LIB], pyproject("package==1.1"))
        self.assertEqual(files[_APP], app)
        self.assert_uv_dependencies_not_updated_logged(_APP, "uv lock")

    def test_a_bound_that_cannot_narrow_the_requirement_holds_the_declaration_on_its_current_version(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that uv locks the current version where a bound leaves it out or a specifier cannot carry the bound."""
        cases = {
            "exact pin left out": ("package==1.0", "allow[update<1]", "package==1.0,>=1.0"),
            "version locked before left out": ("package>=0.9", "allow[update<1]", "package==1.0,>=0.9"),
            "bound without an allow form": ("package==1.0", "ignore[update>=1.1,<1.3]", "package==1.0,>=1.0"),
        }
        for case, (spec, bound, locked) in cases.items():
            with self.subTest(case):
                contents = pyproject(spec, marker=bound)
                files = {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}
                left = self.files_after_update(run, get, glob, files, _uv_lock("1.0"))
                self.assertEqual(self.text_locked_with_upgrade(), contents.replace(spec, locked))
                self.assertEqual(left[PYPROJECT_TOML], contents)

    def test_an_ignore_update_holds_a_dependency_on_the_version_a_lock_of_the_file_as_written_records(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that an `ignore[update]` holds the version uv locks for the file as written, not a stale lock's."""
        contents = pyproject("package>=1.1", marker="ignore[update]")
        files = {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}
        left = self.files_after_update(run, get, glob, files, _uv_lock("1.2"), lock_as_written=_uv_lock("1.2"))
        self.assertEqual(self.text_locked_with_upgrade(), contents.replace("package>=1.1", "package==1.2,>=1.1"))
        self.assertEqual(left[PYPROJECT_TOML], contents)
        location = Location(PYPROJECT_TOML, 2)
        self.assert_recognised_marker_logged("package", location, Marker(ignored_scopes=Scope.UPDATE))
        self.assert_ignored_logged("package", location, "ignore[update]", among_others=True)

    def test_a_marker_holds_the_update_back(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pin whose marker holds its update back keeps its version, and is reported as held back."""
        left = self.updated_pin(run, get, glob, marker="ignore[update]")
        location = Location(PYPROJECT_TOML, 2)
        self.assertNotIn(PYPROJECT_TOML, left.written_paths)
        get.assert_not_called()  # a pin nothing updates costs no request for its changelog
        self.assert_no_new_version_logged()
        self.assert_recognised_marker_logged("package", location, Marker(ignored_scopes=Scope.UPDATE))
        self.assert_ignored_logged("package", location, "ignore[update]", among_others=True)

    def test_a_name_declared_twice_and_held_back_once_stays_on_the_held_back_version(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a name declared twice is relaxed on the line its marker leaves, and stays where uv locks it."""
        held_back = '[project]\ndependencies = ["package==1.0"]  # update-time: ignore[update]\n[dependency-groups]\n'
        contents = f'{held_back}dev = ["package==1.0"]\n'
        # uv locks the name at the version the held-back pin names, since the held-back line allows that version alone.
        lock_text = _uv_lock("1.0")
        left = self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: lock_text}, lock_text)
        self.assertEqual(self.text_locked_with_upgrade(), f'{held_back}dev = ["package>=1.0"]\n')
        self.assertEqual(left[PYPROJECT_TOML], contents)
        self.assert_no_new_version_logged()

    def test_an_invalid_bracket_item_is_warned_about_and_the_pin_updates(self, run: Mock, get: Mock, glob: Mock):
        """Test that an item the marker language cannot read warns, and the pin updates as usual."""
        for directive, item in {"ignore[stlae]": "stlae", "ignore[update": "[update"}.items():
            with self.subTest(directive=directive):
                left = self.updated_pin(run, get, glob, marker=directive)
                self.assertEqual(left[PYPROJECT_TOML], pyproject("package==1.1", marker=directive))
                self.assert_new_version_logged("package", _NEW_VERSION, Location(PYPROJECT_TOML, 2))
                self.assert_invalid_bracket_item_logged("package", Location(PYPROJECT_TOML, 2), item)
                self.assert_no_ignored_logged()

    def test_update_with_changelog(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml with changelog."""
        get.side_effect = [mock_response(self.pypi_metadata()), Mock(headers={"Content-Type": "text"}, text=CHANGELOG)]
        left = self.updated_pin(run, get, glob, package="package_with_changelog")
        self.assertEqual(left[PYPROJECT_TOML], pyproject("package_with_changelog==1.1"))
        self.assert_path_logged(_UV_LOCK)
        self.assert_new_version_logged(
            "package_with_changelog",
            published_version("1.1", UPLOAD_TIME_WITH_CHANGELOG),
            Location(PYPROJECT_TOML, 2),
            changes=CHANGELOG,
        )
        self.assert_no_warnings_logged()

    @staticmethod
    def pypi_metadata() -> _ReleaseMetadata:
        """Create PyPI release metadata fixture."""
        return {
            "info": {"description": "Package description", "project_urls": {"Changelog": "https://changelog"}},
            "urls": [{"upload_time_iso_8601": UPLOAD_TIME_WITH_CHANGELOG}],
        }

    @kills.replace(
        _LockUpdate._moved_pins,
        "PEP_440.order(locked[name].version) != PEP_440.order(declaration.current_version)",
        "locked[name].version != declaration.current_version",
        "where the lock spells a pinned 1.0 as 1.0.0, the pin becomes 1.0.0 and is reported as a new version",
    )
    def test_a_pin_uv_lock_keeps_leaves_the_pyproject_toml_as_it_was(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pin uv lock keeps, spelled more precisely, leaves the file as it was, and goes unlogged."""
        self.assertEqual(self.updated_pin(run, get, glob, latest="1.0.0")[PYPROJECT_TOML], pyproject("package==1.0"))
        get.assert_not_called()
        self.assert_path_logged(_UV_LOCK)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_a_failed_uv_lock_of_the_file_as_written_is_reported_at_error_and_nothing_locks_after_it(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that where uv cannot lock the file as written, the run reports that at error and stops there."""
        left = self.updated_with_failing_lock(run, glob, pyproject("package>=1.0") + _UV_TABLE)
        self.assertEqual(["--upgrade" in command for command in uv_locks(run)], [False])
        self.assertNotIn(PYPROJECT_TOML, left.written_paths)
        get.assert_not_called()
        self.assert_command_stderr_logged(stderr=NO_SOLUTION)
        self.assert_uv_dependencies_not_updated_logged(PYPROJECT_TOML, "uv lock")

    @kills.delete(
        UvLock.restore,
        "missing_ok=True",
        "giving back a uv.lock that held nothing fails where uv did not write one either",
    )
    def test_a_failed_uv_lock_writes_the_files_the_run_rewrote_back(self, run: Mock, get: Mock, glob: Mock):
        """Test that a uv lock failing without an earlier uv.lock gives the workspace's files back, and logs that."""
        files = {PYPROJECT_TOML: _root_declaring("package==1.0"), _MEMBER: pyproject("package==1.0")}
        message = message_of(Logger.tool_failed)
        left = self.files_after_update(run, get, glob, files, (LOCK_FAILS,), discovered=list(files))
        self.assertEqual(dict(left), files)
        self.assert_errors_logged(
            (
                message,
                {
                    "location": Location(PYPROJECT_TOML),
                    "task": ToolTask.UPDATE,
                    "tool": "uv lock",
                    "failure": ToolFailure.FAILED,
                },
            ),
            (
                message,
                {
                    "location": Location(_MEMBER),
                    "task": ToolTask.UPDATE,
                    "tool": "uv lock",
                    "failure": ToolFailure.FAILED,
                },
            ),
        )
        self.assert_no_new_version_logged()

    @kills.delete_statement(
        settings_hold_exclude_newer,
        'if not is_installed("uv"):\n        return False',
        "a missing uv is asked for its settings, for a project without a lock, and reported twice",
    )
    def test_a_uv_missing_from_the_path_is_reported_in_one_line(self, run: Mock, get: Mock, glob: Mock):
        """Test that a missing uv is reported in one line naming the file, and that Update-time does not run it."""
        self.leave_off_the_path("uv")
        run.side_effect = FileNotFoundError("uv")
        mock_pyproject_toml = _discovered_pyproject_toml(glob, pyproject("package==1.0") + _UV_TABLE)
        _update_pyproject_tomls()
        run.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        get.assert_not_called()
        self.assert_tool_not_on_path_logged(mock_pyproject_toml, "uv")

    def test_skip_invalid_pyproject_toml(self, run: Mock, get: Mock, glob: Mock):
        """Test that an unparsable pyproject.toml is logged as checked, then skipped with a warning, and uv not run."""
        not_utf_8 = mock_path("", parent=SCAN_ROOT)
        not_utf_8.read_text.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        for case, mock_pyproject_toml in (
            ("a missing bracket", mock_path('[project]\ndependencies = ["package==1.0"\n', parent=SCAN_ROOT)),
            ("a file that is not UTF-8", not_utf_8),
        ):
            with self.subTest(case):
                glob.return_value = [mock_pyproject_toml]
                _update_pyproject_tomls()
                run.assert_not_called()
                get.assert_not_called()
                mock_pyproject_toml.write_text.assert_not_called()
                self.assert_invalid_file_logged(mock_pyproject_toml, "TOML")
                self.assert_path_logged_first(mock_pyproject_toml)
                self.assert_no_new_version_logged()

    @_kills_table_check('("project", "optional-dependencies")', "project.optional-dependencies", _ARRAYS_NOT_IN_A_TABLE)
    @_kills_table_check('("dependency-groups",)', "dependency-groups", _ARRAYS_NOT_IN_A_TABLE)
    @_kills_table_check('("tool",)', "tool", _KEYS_NOT_IN_A_TABLE)
    @_kills_table_check('("tool", "uv")', "tool.uv", _KEYS_NOT_IN_A_TABLE)
    @_kills_table_check('("build-system",)', "build-system", _KEYS_NOT_IN_A_TABLE)
    @kills.force_true(
        pyproject_config._is_well_formed,
        "all(isinstance(array, list) for _, array in [*_uv_arrays(table), *_build_requirements(table)])",
        "a pyproject.toml whose dependency array is not an array has uv run for it",
        case="project.dependencies",
    )
    @kills.delete(
        pyproject_config._is_well_formed,
        ", *_build_requirements(table)",
        "a pyproject.toml whose build requirements are not an array is not warned about",
        case="build-system.requires",
    )
    @kills.replace(
        pyproject_config._is_well_formed,
        "[*_uv_arrays(table), *_build_requirements(table)]",
        "[*_build_requirements(table)]",
        "a pyproject.toml whose dependency array is not an array has uv run for it, and stops the run",
        case="project.dependencies",
    )
    def test_skip_pyproject_toml_whose_tables_or_arrays_hold_another_value(self, run: Mock, get: Mock, glob: Mock):
        """Test that the updater warns about and skips a pyproject.toml whose tables or arrays hold another value."""
        cases = {
            "tool": 'tool = "x"\n',
            "tool.uv": '[tool]\nuv = "x"\n',
            "project.optional-dependencies": '[project]\noptional-dependencies = "x"\n',
            "dependency-groups": 'dependency-groups = "x"\n',
            "build-system": 'build-system = "x"\n',
            "project.dependencies": '[project]\ndependencies = "x"\n',
            "build-system.requires": '[build-system]\nrequires = "x"\n',
        }
        for case, contents in cases.items():
            with self.subTest(case):
                run.reset_mock()
                get.reset_mock()
                mock_pyproject_toml = _discovered_pyproject_toml(glob, contents)
                _update_pyproject_tomls()
                run.assert_not_called()
                get.assert_not_called()
                mock_pyproject_toml.write_text.assert_not_called()
                self.assert_invalid_file_logged(mock_pyproject_toml, "pyproject.toml")

    def test_uv_not_run_for_a_file_that_declares_nothing_to_lock(self, run: Mock, get: Mock, glob: Mock):
        """Test that uv does not run, nor get its cooldown, for a pyproject.toml that declares nothing to lock."""
        cases = {
            "tool configuration alone": "[tool.ruff]\nline-length = 100\n",
            "empty dependency arrays": "[project]\ndependencies = []\n\n[dependency-groups]\ndev = []\n",
            "build requirements alone": _BUILD_REQUIREMENTS_ALONE,
        }
        for case, contents in cases.items():
            with self.subTest(case):
                self.assert_uv_not_run_for(run, get, glob, {PYPROJECT_TOML: contents})
                self.assert_debug_logged_after_path(
                    PYPROJECT_TOML,
                    message_of(Logger.manager_not_run),
                    **not_run("uv", NotRunCause.NO_DEPENDENCIES_TO_RESOLVE),
                )

    def assert_uv_not_run_for(
        self, run: Mock, get: Mock, glob: Mock, files: Mapping[Path, str], discovered: Sequence[Path] = ()
    ) -> None:
        """Assert that updating the files does not run uv, ask PyPI, or configure the cooldown, and leaves the files."""
        run.reset_mock()
        get.reset_mock()
        with patch("update_time.package_managers.uv.lock.configure_cooldown") as configure_cooldown:
            left = self.files_after_update(run, get, glob, files, _uv_lock("1.1"), discovered=discovered)
        run.assert_not_called()
        get.assert_not_called()
        configure_cooldown.assert_not_called()
        self.assertEqual(dict(left), files)

    @kills.delete_statement(
        decision,
        '_LOG.manager_not_run(pyproject_toml, "uv", NotRunCause.NOT_A_UV_PROJECT, UvLock.beside(root).path)',
        "a pyproject.toml that uv does not run for, though it declares dependencies, goes unreported",
        case="neither a uv.lock nor a [tool.uv] table",
    )
    @kills.delete_statement(
        decision,
        '_LOG.manager_not_run(pyproject_toml, "uv", NotRunCause.MARKED_UNMANAGED, root)',
        "a pyproject.toml whose [tool.uv] table sets managed = false goes unreported",
        case="a [tool.uv] table that sets managed = false",
    )
    def test_uv_not_run_for_a_file_uv_does_not_manage(self, run: Mock, get: Mock, glob: Mock):
        """Test that uv does not run, nor get its cooldown, for a pyproject.toml that uv does not manage."""
        marked_unmanaged = pyproject("package==1.0") + _UV_TABLE_UNMANAGED
        cases = {
            "neither a uv.lock nor a [tool.uv] table": (
                {PYPROJECT_TOML: pyproject("package==1.0")},
                not_run("uv", NotRunCause.NOT_A_UV_PROJECT, _UV_LOCK),
            ),
            "a [tool.uv] table that sets managed = false": (
                {PYPROJECT_TOML: marked_unmanaged, _UV_LOCK: _uv_lock("1.0")},
                not_run("uv", NotRunCause.MARKED_UNMANAGED, PYPROJECT_TOML),
            ),
        }
        for case, (files, fields) in cases.items():
            with self.subTest(case):
                self.assert_uv_not_run_for(run, get, glob, files)
                self.assert_debug_logged_after_path(PYPROJECT_TOML, message_of(Logger.manager_not_run), **fields)

    def test_uv_not_run_for_a_file_pdm_or_poetry_manages(self, run: Mock, get: Mock, glob: Mock):
        """Test that uv does not run, nor get its cooldown, for a file pdm or Poetry manages, and the log names that."""
        cases = {
            "a [tool.poetry] table": ({PYPROJECT_TOML: pyproject("package==1.0") + _POETRY_TABLE}, "poetry"),
            "a [tool.pdm] table": ({PYPROJECT_TOML: pyproject("package==1.0") + _PDM_TABLE}, "pdm"),
            "a pdm.lock": ({PYPROJECT_TOML: pyproject("package==1.0"), _PDM_LOCK: ""}, "pdm"),
            "a poetry.lock beside a [tool.uv] table": (
                {PYPROJECT_TOML: pyproject("package==1.0") + _UV_TABLE, _POETRY_LOCK: ""},
                "poetry",
            ),
            "a pdm.lock beside a [tool.poetry] table": (
                {PYPROJECT_TOML: pyproject("package==1.0") + _POETRY_TABLE, _PDM_LOCK: ""},
                "pdm",
            ),
            "a poetry.lock beside a file that declares its dependencies under [tool.poetry.dependencies]": (
                {PYPROJECT_TOML: '[tool.poetry.dependencies]\npackage = "1.0"\n', _POETRY_LOCK: ""},
                "poetry",
            ),
            "a [tool.pdm] table beside [tool.uv] managed = false": (
                {PYPROJECT_TOML: pyproject("package==1.0") + _PDM_TABLE + _UV_TABLE_UNMANAGED},
                "pdm",
            ),
        }
        for case, (files, manager) in cases.items():
            with self.subTest(case):
                self.assert_uv_not_run_for(run, get, glob, files)
                self.assert_path_logged_first(PYPROJECT_TOML)
                self.assert_unsupported_package_manager_logged(PYPROJECT_TOML, manager, "uv")

    def test_uv_not_run_for_a_file_whose_signals_name_more_than_one_manager(self, run: Mock, get: Mock, glob: Mock):
        """Test that uv does not run, nor get its cooldown, for a file whose tables name two managers."""
        files = {PYPROJECT_TOML: pyproject("package==1.0") + _POETRY_TABLE + _PDM_TABLE}
        self.assert_uv_not_run_for(run, get, glob, files)
        self.assert_path_logged_first(PYPROJECT_TOML)
        reason = _python_managers_reason([], ["poetry", "pdm"])
        self.assert_logged(
            message_of(Logger.manager_cannot_run), location=Location(PYPROJECT_TOML), manager="uv", reason=reason
        )

    @kills.force_false(
        _warn_about_the_managers,
        "pyproject_toml != root and is_scanned(root)",
        "each member of a workspace repeats the warning about its root",
    )
    def test_uv_not_run_for_a_workspace_whose_root_names_another_manager_and_the_root_alone_is_warned_about(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a root beside two managers' lock files gets the warning, and its member a pointer to it."""
        files = {
            PYPROJECT_TOML: _root_declaring("other==1.0"),
            _MEMBER: pyproject("package==1.0"),
            _UV_LOCK: _uv_lock("1.0"),
            _PDM_LOCK: "",
        }
        self.assert_uv_not_run_for(run, get, glob, files, discovered=[PYPROJECT_TOML, _MEMBER])
        reason = _python_managers_reason([_UV_LOCK, _PDM_LOCK], [])
        fields: _Fields = {"location": Location(PYPROJECT_TOML), "manager": "uv", "reason": reason}
        self.assert_warnings_logged((message_of(Logger.manager_cannot_run), fields))
        self.assert_logged_among_others(
            message_of(Logger.manager_not_run),
            location=Location(_MEMBER),
            **not_run("uv", NotRunCause.LEFT_WITH_THE_WORKSPACE_ROOT, PYPROJECT_TOML),
        )

    def test_uv_runs_for_a_workspace_whose_member_has_the_lock_file_and_the_table_of_another_manager(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that the lock file and the table of a workspace member do not decide which manager manages it."""
        cases = {
            "a member beside a pdm.lock, the root beside a uv.lock": {
                PYPROJECT_TOML: _root_declaring("other==1.0"),
                _MEMBER: pyproject("package==1.0"),
                _MEMBER.with_name("pdm.lock"): "",
                _UV_LOCK: _uv_lock("1.0"),
            },
            "a member with a [tool.pdm] table, the root without a lock file": {
                PYPROJECT_TOML: _root_declaring("other==1.0"),
                _MEMBER: pyproject("package==1.0") + _PDM_TABLE,
            },
        }
        for case, files in cases.items():
            with self.subTest(case):
                run.reset_mock()
                self.files_after_update(run, get, glob, files, _uv_lock("1.0"), discovered=[PYPROJECT_TOML, _MEMBER])
                self.assertTrue(uv_locks(run))
                self.assert_no_warnings_logged()

    @kills.delete(
        _warn_about_the_managers,
        " and is_scanned(root)",
        "a member whose workspace root lies outside the scan points at a warning nobody gives",
    )
    def test_the_lock_file_beside_a_workspace_root_outside_the_scan_names_the_manager_of_a_member(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a member whose root lies outside the scan, beside a pdm.lock, is reported as pdm's."""
        files = {**_workspace_above_the_scan(pyproject("package==1.0")), _PDM_LOCK: ""}
        with _scan_from_the_member():
            self.assert_uv_not_run_for(run, get, glob, files, discovered=[_MEMBER])
            self.assert_unsupported_package_manager_logged(_MEMBER, "pdm", "uv")

    def test_uv_not_run_for_a_member_whose_workspace_root_lies_outside_the_scan_root(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that uv does not run, nor get its cooldown, for a member whose workspace root lies outside the scan."""
        files = _workspace_above_the_scan(pyproject("package==1.0"))
        with _scan_from_the_member():
            self.assert_uv_not_run_for(run, get, glob, files, discovered=[_MEMBER])
            fields = not_run("uv", NotRunCause.WORKSPACE_ROOT_OUTSIDE_THE_SCAN, PYPROJECT_TOML)
            self.assert_debug_logged_after_path(_MEMBER, message_of(Logger.manager_not_run), **fields)

    @kills.delete(
        _update_pyproject_tomls,
        "report_file_markers(pyproject_toml, _LOG, policy)",
        "the markers of a pyproject.toml uv does not manage go unreported",
    )
    def test_a_marker_in_a_file_uv_does_not_run_for_is_recognised(self, run: Mock, get: Mock, glob: Mock):
        """Test that the marker on a build requirement of a pyproject.toml that uv does not run for is reported."""
        build_requirement = '[build-system]\nrequires = ["hatchling==1.20.0"]  # update-time: set[stale-after=0]\n'
        mock_pyproject_toml = _discovered_pyproject_toml(glob, build_requirement)
        _update_pyproject_tomls()
        run.assert_not_called()
        get.assert_not_called()
        marker = Marker(stale=Threshold(value=0))
        self.assert_recognised_marker_logged("hatchling", Location(mock_pyproject_toml, 2), marker)

    @kills.replace(
        pyproject_config,
        '("dependencies", "optional-dependencies")',
        '("dependencies",)',
        "uv does not run for a project whose extras its build backend declares",
        case="dynamic extras",
    )
    @kills.replace(
        pyproject_config,
        '_DYNAMIC_DEPENDENCY_FIELDS: Final[tuple[str, ...]] = ("dependencies", "optional-dependencies")',
        '_DYNAMIC_DEPENDENCY_FIELDS: Final[tuple[str, ...]] = ("optional-dependencies",)',
        "uv does not run for a project whose dependencies its build backend declares",
        case="dynamic dependencies",
    )
    @kills.delete(
        pyproject_config._uv_arrays,
        '*_declared(table, ("tool", "uv", "dev-dependencies")),',
        "uv does not run for a project whose dependencies sit in uv's legacy dev-dependencies alone",
        case="uv's legacy dev-dependencies",
    )
    def test_uv_runs_for_each_array_it_resolves(self, run: Mock, get: Mock, glob: Mock):
        """Test that uv runs for a pyproject.toml whose only dependency sits in an array or a field uv resolves."""
        cases = {
            "uv's legacy dev-dependencies": '[tool.uv]\ndev-dependencies = ["package==1.0"]\n',
            "dynamic dependencies": '[project]\nname = "a"\ndynamic = ["dependencies"]\n',
            "dynamic extras": '[project]\nname = "a"\ndynamic = ["optional-dependencies"]\n',
        }
        for case, contents in cases.items():
            with self.subTest(case):
                run.reset_mock()
                files = {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}
                self.files_after_update(run, get, glob, files, _uv_lock("1.0"))
                self.assertTrue(uv_locks(run))

    @kills.delete(
        _ManagerNames.managers,
        '("uv",) if "uv" in self.tables else ',
        "the tables of uv and Poetry tie in a project without a lock file",
    )
    def test_uv_runs_for_a_project_it_manages_whose_build_backend_has_a_table_of_its_own(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a `[tool.poetry]` table beside a `[tool.uv]` table, without a lock file, has uv move the pin."""
        files = {PYPROJECT_TOML: pyproject("package==1.0") + _POETRY_TABLE + _UV_TABLE}
        left = self.files_after_update(run, get, glob, files, _uv_lock("1.1"))
        self.assertTrue(uv_locks(run))
        self.assertIn("package==1.1", left[PYPROJECT_TOML])
        self.assert_no_warnings_logged()


def _forked_uv_lock(below_python_3_12: str, from_python_3_12: str) -> str:
    """Return a uv.lock that locks `package` for the project at one version below Python 3.12, and at another from it.

    It also locks `package` at 1.0 for `other`, a dependency of the project that needs that version.
    """
    project = '[[package]]\nname = "project"\nversion = "0"\nsource = { virtual = "." }\ndependencies = [\n'
    project += '    { name = "other" },\n'
    forks = ((below_python_3_12, "python_full_version < '3.12'"), (from_python_3_12, "python_full_version >= '3.12'"))
    for version, marker in forks:
        project += f'    {{ name = "package", version = "{version}", marker = "{marker}" }},\n'
    packages = "\n".join(locked_package("package", version) for version in ("1.0", below_python_3_12, from_python_3_12))
    other = '[[package]]\nname = "other"\nversion = "1"\ndependencies = [{ name = "package", version = "1.0" }]\n'
    return f"version = 1\n\n{project}]\n\n{other}\n{packages}"


def _root_declaring(spec: str) -> str:
    """Return a workspace root that includes `member` and declares the dependency."""
    return f'[project]\nname = "root"\ndependencies = ["{spec}"]\n[tool.uv.workspace]\nmembers = ["member"]\n'


def _uv_lock_resolving_path_sources(
    files: FakeFiles, locks: list[tuple[Command, dict[Path, str]]], unreachable_index: Path | None
) -> RunStub:
    """Return a stub of uv lock that locks `package` at a version every pin on it admits, as uv does.

    A lock admits what the pins of each project the locked project declares admit as well, since these tests source
    `lib` and `app` by path.
    """
    sourced_manifests = {"lib": _LIB, "app": _APP}

    def requirements(manifest: Path) -> list[Requirement]:
        return [Requirement(spec) for spec in tomllib.loads(files[manifest])["project"]["dependencies"]]

    def stub(command: Command, *_arguments: object, **_options: object) -> Mock:
        locks.append((command, dict(files)))
        directory = Path(command[command.index("--directory") + 1])
        locked_manifest = directory / "pyproject.toml"
        names = [requirement.name for requirement in requirements(locked_manifest)]
        declared_manifests = [sourced_manifests[name] for name in names if name in sourced_manifests]
        specifier = SpecifierSet()
        for manifest in [locked_manifest, *declared_manifests]:
            for requirement in requirements(manifest):
                if requirement.name == "package":
                    specifier &= requirement.specifier
        admitted = list(specifier.filter(["1.0", "1.1"]))
        if not admitted or (directory == unreachable_index and "--upgrade" in command):
            failing_uv_lock()
        lock = directory / "uv.lock"
        locked = tomllib.loads(files[lock])["package"][1]["version"]
        files[lock] = _uv_lock(locked if locked in admitted and "--upgrade" not in command else admitted[-1])
        return Mock(stdout="", stderr="")

    return stub


def _discovered_pyproject_toml(glob: Mock, contents: str) -> Mock:
    """Return the single mock pyproject.toml the scan discovers, holding the contents."""
    pyproject_toml = mock_path(contents, parent=SCAN_ROOT)
    glob.return_value = [pyproject_toml]
    return pyproject_toml


def _workspace_above_the_scan(member: str) -> dict[Path, str]:
    """Return the pyproject.toml of `member`, and of a workspace root that includes it and declares a dependency."""
    return {PYPROJECT_TOML: _root_declaring("other==1.0"), _MEMBER: member}


def _scan_from_the_member() -> AbstractContextManager[Mock]:
    """Return a patch that makes the directory of `member` the scan root, which leaves the workspace root out."""
    return patch("pathlib.Path.cwd", return_value=_MEMBER.parent)


@no_vulnerabilities()
@patch("update_time.package_managers.uv.lock.configure_cooldown", Mock())
@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
@patch_get()
@patch("subprocess.run")
class RedundantDirectiveTest(_PyprojectTomlLockTestCase):
    """Unit tests for the directives a run reports as redundant in a pyproject.toml."""

    def test_a_directive_steering_the_lock_is_not_reported_as_redundant(self, run: Mock, get: Mock, glob: Mock):
        """Test that an `ignore[update]` and a bound on a dependency without an exact pin go unreported."""
        for directive in ("ignore[update]", "allow[update<2]"):
            with self.subTest(directive=directive):
                contents = pyproject("package>=0.9", marker=f"{directive} set[cooldown=30]")
                self.files_after_update(
                    run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}, _uv_lock("1.0")
                )
                self.assert_cooldown_per_run_logged()

    def assert_cooldown_per_run_logged(self) -> None:
        """Assert that the `set[cooldown=30]` on the dependency `pyproject()` declares was warned about as redundant."""
        self.assert_redundant_directive_logged(
            Reason.COOLDOWN_PER_RUN, "package", Location(PYPROJECT_TOML, 2), "set[cooldown=30]"
        )

    @kills.replace(
        Declaration.unnarrowable_reason,
        "Reason.BUILD_REQUIREMENT_NOT_LOCKED",
        "Reason.BOUND_DECIDES_NOTHING",
        "a bound Update-time does not narrow for a build requirement is reported as if uv resolved the version",
        case="build requirement",
    )
    @kills.replace(
        Declaration.unnarrowable_reason,
        "Reason.URL_RATHER_THAN_VERSION",
        "Reason.BOUND_DECIDES_NOTHING",
        "a bound Update-time does not narrow for a direct URL is reported as if uv resolved the version",
        case="direct URL",
    )
    @kills.replace(
        Declaration.unnarrowable_reason,
        "Reason.ENVIRONMENT_MARKER_PIN_KEPT",
        "Reason.BOUND_DECIDES_NOTHING",
        "a bound Update-time does not narrow for a pin with an environment marker is reported as if uv resolved the "
        "version",
        case="exact pin with an environment marker",
    )
    @kills.replace(
        Declaration.unnarrowable_reason,
        "Reason.BOUND_ON_A_HELD_BACK_UPDATE",
        "Reason.BOUND_DECIDES_NOTHING",
        "a bound Update-time does not narrow for a pin whose update is held back is reported as if uv resolved the "
        "version",
        case="exact pin beside an ignore[update]",
    )
    def test_a_bound_update_time_does_not_narrow_by_is_reported_as_deciding_nothing(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a bound stays out of the lock and is reported where Update-time does not narrow the requirement."""
        build_requirement = (
            '[project]\ndependencies = ["package>=0.9"]\n'
            '[build-system]\nrequires = ["hatchling>=1.0"]  # update-time: allow[update<2]\n'
        )
        url = "other @ https://example.com/other-1.0.tar.gz"
        cases = {
            "build requirement": (build_requirement, "hatchling", 4, Reason.BUILD_REQUIREMENT_NOT_LOCKED),
            "direct URL": (
                pyproject_per_line(url, "package>=0.9", marker="allow[update<2]"),
                "other",
                3,
                Reason.URL_RATHER_THAN_VERSION,
            ),
            "exact pin with an environment marker": (
                pyproject_per_line("other==1.0; python_version < '3.13'", marker="allow[update<2]"),
                "other",
                3,
                Reason.ENVIRONMENT_MARKER_PIN_KEPT,
            ),
            "exact pin beside an ignore[update]": (
                pyproject_per_line("other==1.0", marker="ignore[update] allow[update<2]"),
                "other",
                3,
                Reason.BOUND_ON_A_HELD_BACK_UPDATE,
            ),
        }
        for case, (contents, dependency, line, reason) in cases.items():
            with self.subTest(case):
                files = {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}
                self.files_after_update(run, get, glob, files, _uv_lock("1.0"))
                self.assertEqual(self.text_locked_with_upgrade(), contents)
                self.assert_redundant_directive_logged(
                    reason, dependency, Location(PYPROJECT_TOML, line), "allow[update<2]"
                )

    @kills.replace(
        marker_policy,
        "Reason.UNMANAGED_FILE_LEFT, Reason.UNMANAGED_FILE_KEPT",
        "Reason.UNMANAGED_FILE_KEPT, Reason.UNMANAGED_FILE_LEFT",
        "a directive steering the update in a file uv does not manage is reported for the other one's reason",
        case="ignore[update]",
    )
    @kills.replace(
        marker_policy,
        "UNMANAGED_FILE_KEPT",
        "UNMANAGED_FILE_LEFT",
        "a bound in a file uv does not manage is said to leave the dependency as it is",
        case="allow[update<2]",
    )
    def test_a_directive_steering_the_update_in_a_file_uv_does_not_manage_is_reported(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that an `ignore[update]` and a bound in a file uv does not manage are reported."""
        cases = {"ignore[update]": Reason.UNMANAGED_FILE_LEFT, "allow[update<2]": Reason.UNMANAGED_FILE_KEPT}
        for directive, reason in cases.items():
            with self.subTest(directive=directive):
                contents = pyproject("package==1.0", marker=directive)
                left = self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents}, None)
                self.assertEqual(dict(left), {PYPROJECT_TOML: contents})
                self.assert_redundant_directive_logged(reason, "package", Location(PYPROJECT_TOML, 2), directive)
                self.assert_no_ignored_logged()

    @kills.replace(
        decision,
        "else Decision.UNMANAGED",
        "else Decision.NOTHING_TO_RESOLVE",
        "the markers of a file pdm manages are judged as if uv ran for it",
    )
    def test_a_directive_steering_the_update_in_a_file_pdm_manages_is_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that an `ignore[update]` in a file pdm manages is reported, after the warning that pdm manages it."""
        contents = pyproject("package==1.0", marker="ignore[update]") + _PDM_TABLE
        left = self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents}, None)
        self.assertEqual(dict(left), {PYPROJECT_TOML: contents})
        self.assert_warnings_logged(
            (
                message_of(Logger.manager_cannot_run),
                {"location": Location(PYPROJECT_TOML), "manager": "pdm", "reason": _unsupported_manager_reason("uv")},
            ),
            (message_of(Logger.redundant_directive), _redundant_update_fields(Reason.UNMANAGED_FILE_LEFT)),
        )
        self.assert_no_ignored_logged()

    @kills.replace(
        ManagerPolicy.policy_for_more_than_one_manager,
        "Reason.MORE_THAN_ONE_MANAGER_LEFT, Reason.MORE_THAN_ONE_MANAGER_KEPT",
        "Reason.MORE_THAN_ONE_MANAGER_KEPT, Reason.MORE_THAN_ONE_MANAGER_LEFT",
        "a directive steering the update beside the lock files of two managers gets the other one's reason",
        case="ignore[update]",
    )
    @kills.replace(
        ManagerPolicy.policy_for_more_than_one_manager,
        "MORE_THAN_ONE_MANAGER_KEPT",
        "MORE_THAN_ONE_MANAGER_LEFT",
        "a bound beside the lock files of two managers is said to leave the dependency as it is",
        case="allow[update<2]",
    )
    def test_a_directive_steering_the_update_in_a_file_whose_lock_files_name_more_than_one_manager_is_reported(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that an `ignore[update]` and a bound beside a uv.lock and a poetry.lock follow the warning on both."""
        several_managers: _Fields = {
            "location": Location(PYPROJECT_TOML),
            "manager": "uv",
            "reason": _python_managers_reason([_UV_LOCK, _POETRY_LOCK], []),
        }
        cases = {
            "ignore[update]": Reason.MORE_THAN_ONE_MANAGER_LEFT,
            "allow[update<2]": Reason.MORE_THAN_ONE_MANAGER_KEPT,
        }
        for directive, reason in cases.items():
            with self.subTest(directive=directive):
                files = {
                    PYPROJECT_TOML: pyproject("package==1.0", marker=directive),
                    _UV_LOCK: _uv_lock("1.0"),
                    _POETRY_LOCK: "",
                }
                self.assertEqual(dict(self.files_after_update(run, get, glob, files, None)), files)
                self.assert_warnings_logged(
                    (message_of(Logger.manager_cannot_run), several_managers),
                    (message_of(Logger.redundant_directive), _redundant_update_fields(reason, directive)),
                )
                self.assert_no_ignored_logged()

    @kills.replace(
        ManagerPolicy.policy_for_a_workspace_root_outside_the_scan,
        "Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_LEFT, Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_KEPT",
        "Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_KEPT, Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_LEFT",
        "a directive steering the update in a member whose root lies outside the scan gets the other one's reason",
        case="ignore[update]",
    )
    @kills.replace(
        ManagerPolicy.policy_for_a_workspace_root_outside_the_scan,
        "WORKSPACE_ROOT_OUTSIDE_THE_SCAN_KEPT",
        "WORKSPACE_ROOT_OUTSIDE_THE_SCAN_LEFT",
        "a bound in a member whose root lies outside the scan is said to leave the dependency as it is",
        case="allow[update<2]",
    )
    def test_a_directive_steering_the_update_of_a_member_whose_workspace_root_lies_outside_the_scan_is_reported(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a directive steering the update is reported in a member whose root lies outside the scan."""
        cases = {
            "ignore[update]": Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_LEFT,
            "allow[update<2]": Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_KEPT,
        }
        for directive, reason in cases.items():
            with self.subTest(directive=directive), _scan_from_the_member():
                files = _workspace_above_the_scan(pyproject("package==1.0", marker=directive))
                left = self.files_after_update(run, get, glob, files, None, discovered=[_MEMBER])
                self.assertEqual(dict(left), files)
                self.assert_redundant_directive_logged(reason, "package", Location(_MEMBER, 2), directive)
                self.assert_no_ignored_logged()

    @kills.replace(
        warn_about_pins,
        "declarations = declarations_of(file)",
        "declarations = file.declarations()",
        "the run checks a member's dependency against PyPI where the workspace root sources it",
    )
    def test_a_scope_on_a_dependency_the_workspace_root_sources_is_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that a warning scope on a member's dependency that the workspace root sources is reported."""
        lock_text = _uv_lock("1.0", "other", project_source="member")
        member = pyproject("other>=1.0", marker="ignore[archived]")
        files = {PYPROJECT_TOML: _ROOT_SOURCING_OTHER, _MEMBER: member, _UV_LOCK: lock_text}
        self.files_after_update(run, get, glob, files, lock_text, discovered=[_MEMBER])
        self.assert_redundant_directive_logged(
            Reason.NO_PYPI_RELEASE, "other", Location(_MEMBER, 2), "ignore[archived]"
        )

    @kills.force_false(
        marker_policy.kept_lock_policy,
        "not declaration.updatable and hold_back_redundancy(declaration) is None",
        "a bound beside an `ignore[update]` keeping the locked version goes unreported, though it decides nothing",
    )
    def test_a_bound_beside_an_ignore_update_keeping_the_locked_version_is_reported_as_deciding_nothing(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a bound beside an `ignore[update]` on a dependency without an exact pin is reported."""
        contents = pyproject("package>=0.9", marker="ignore[update] allow[update<2]")
        self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}, _uv_lock("1.0"))
        self.assertEqual(self.text_locked_with_upgrade(), contents.replace("package>=0.9", "package==1.0,>=0.9"))
        self.assert_redundant_directive_logged(
            Reason.BOUND_ON_A_HELD_BACK_UPDATE, "package", Location(PYPROJECT_TOML, 2), "allow[update<2]"
        )

    @kills.delete(
        marker_policy.kept_lock_policy,
        " and hold_back_redundancy(declaration) is None",
        "a bound beside an `ignore[update]` that keeps nothing is reported, and narrows the lock no longer",
    )
    def test_a_bound_beside_an_ignore_update_without_a_locked_version_narrows_the_lock_and_goes_unreported(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a bound narrows the lock where the `ignore[update]` beside it does not have a version to keep."""
        contents = pyproject("package>=0.9", marker="ignore[update] allow[update<2]")
        files = {PYPROJECT_TOML: contents, _UV_LOCK: _LOCK_WITHOUT_THE_PACKAGE}
        self.files_after_update(run, get, glob, files, _LOCK_WITHOUT_THE_PACKAGE)
        self.assertEqual(self.text_locked_with_upgrade(), contents.replace("package>=0.9", "package<2,>=0.9"))
        self.assert_redundant_directive_logged(
            Reason.NO_LOCKED_VERSION, "package", Location(PYPROJECT_TOML, 2), "ignore[update]"
        )

    @kills.delete(
        marker_policy.kept_lock_policy,
        " or not declaration.narrowable",
        "an `ignore[update]` reads as keeping a version that uv does not lock by a specifier",
        case="direct URL",
    )
    @kills.force_false(
        marker_policy._hold_back_redundancy,
        "declaration.build_requirement",
        "an `ignore[update]` on a pinned build requirement goes unreported, though Update-time leaves that pin",
        case="pinned build requirement",
    )
    def test_an_ignore_update_that_uv_does_not_lock_by_keeps_nothing(self, run: Mock, get: Mock, glob: Mock):
        """Test that an `ignore[update]` on a direct URL or a build requirement is reported, and holds nothing back."""
        pinned_build_requirement = (
            '[project]\ndependencies = ["package>=0.9"]\n'
            '[build-system]\nrequires = ["package==0.9"]  # update-time: ignore[update]\n'
        )
        url = "other @ https://example.com/other-1.0.tar.gz"
        cases = {
            "pinned build requirement": (
                pinned_build_requirement,
                _uv_lock("1.0"),
                "package",
                4,
                Reason.BUILD_REQUIREMENT_KEPT,
            ),
            "direct URL": (
                pyproject_per_line(url, marker="ignore[update]"),
                _uv_lock("1.0", "other"),
                "other",
                3,
                Reason.MANAGER_RESOLVES_THE_VERSION,
            ),
        }
        for case, (contents, lock_text, dependency, line, reason) in cases.items():
            with self.subTest(case):
                self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: lock_text}, lock_text)
                self.assert_redundant_directive_logged(
                    reason, dependency, Location(PYPROJECT_TOML, line), "ignore[update]"
                )
                self.assert_no_ignored_logged()

    @kills.delete(
        marker_policy._hold_back_redundancy,
        "Reason.ENVIRONMENT_MARKER_PIN_LEFT if declaration.environment_marker else ",
        "an `ignore[update]` on a pin with an environment marker goes unreported, though the pin stays anyway",
    )
    def test_an_ignore_update_on_a_pin_with_an_environment_marker_is_reported_and_the_pin_left_as_it_is(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that an `ignore[update]` on a pin with an environment marker is reported, as the pin stays anyway."""
        contents = pyproject("package==1.0; python_version < '3.13'", marker="ignore[update]")
        self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}, _uv_lock("1.1"))
        location = Location(PYPROJECT_TOML, 2)
        self.assert_redundant_directive_logged(
            Reason.ENVIRONMENT_MARKER_PIN_LEFT, "package", location, "ignore[update]"
        )
        message = message_of(Logger.pin_with_environment_marker_left_as_is)
        self.assert_logged(message, dependency="package", location=location)
        self.assert_no_ignored_logged()

    def test_a_directive_takes_the_version_a_lock_of_the_file_as_written_records(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that an `ignore[update]` and a level bound take the version uv locks, though the uv.lock had none."""
        locked_specs = {"ignore[update]": "package==1.0,>=0.9", "ignore[minor-update]": "package==1.0.*,>=0.9"}
        for directive, locked_spec in locked_specs.items():
            with self.subTest(directive=directive):
                contents = pyproject("package>=0.9", marker=f"{directive} set[cooldown=30]")
                files = {PYPROJECT_TOML: contents, _UV_LOCK: _LOCK_WITHOUT_THE_PACKAGE}
                self.files_after_update(run, get, glob, files, _uv_lock("1.0"), lock_as_written=_uv_lock("1.0"))
                self.assertEqual(self.text_locked_with_upgrade(), contents.replace("package>=0.9", locked_spec))
                self.assert_cooldown_per_run_logged()

    @kills.replace(
        marker_policy.current_version,
        'return versions.pop() if len(versions) == 1 else ""',
        'return versions.pop() if versions else ""',
        "an `ignore[update]` holds a dependency on one of the versions it is locked at, and moves it off the rest",
    )
    @kills.replace(
        marker_policy._current_version_redundancy,
        "return Reason.SEVERAL_LOCKED_VERSIONS if several else",
        "return Reason.NO_LOCKED_VERSION if several else",
        "a directive on a dependency locked at several versions is reported as if none were locked",
    )
    @kills.replace(
        marker_policy.kept_lock_policy,
        "return None\n        return _current_version_redundancy(declaration, locked_as_written)",
        "return None\n        return None",
        "a level bound without a version to anchor at, the lock recording several, goes unreported",
    )
    def test_a_directive_on_a_dependency_locked_at_several_versions_narrows_nothing_and_is_reported(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a level bound leaves a dependency the lock records at two versions to uv, and is reported."""
        forked = _forked_uv_lock("1.1", "1.4")
        directive = "ignore[minor-update]"
        contents = pyproject("package>=1.0", marker=directive)
        files = {PYPROJECT_TOML: contents, _UV_LOCK: forked}
        self.files_after_update(run, get, glob, files, forked, lock_as_written=forked)
        self.assertEqual(self.text_locked_with_upgrade(), contents)
        self.assert_redundant_directive_logged(
            Reason.SEVERAL_LOCKED_VERSIONS, "package", Location(PYPROJECT_TOML, 2), directive
        )

    @kills.replace(
        marker_policy.current_version,
        "if declaration.current_version:",
        "if False:",
        "a bound on an exact pin narrows nothing where the lock records the package at several versions",
    )
    def test_a_bound_on_an_exact_pin_takes_the_pinned_version_where_the_lock_records_several(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a level bound on an exact pin narrows from the pinned version, though the lock records two."""
        contents = pyproject("package==1.1", marker="ignore[minor-update] set[cooldown=30]")
        forked = _forked_uv_lock("1.1", "1.4")
        self.files_after_update(
            run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: forked}, forked, lock_as_written=forked
        )
        self.assertEqual(self.text_locked_with_upgrade(), contents.replace("package==1.1", "package==1.1.*,>=1.1"))
        self.assert_cooldown_per_run_logged()

    def test_without_uv_an_ignore_update_is_obeyed_and_not_judged_by_the_uv_lock_before_the_run(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that, without uv on the path, an `ignore[update]` holds the update back, unjudged by the uv.lock."""
        self.leave_off_the_path("uv")
        contents = pyproject("package>=0.9", marker="ignore[update] set[cooldown=30]")
        self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}, _uv_lock("1.0"))
        self.assert_cooldown_per_run_logged()
        self.assert_ignored_logged("package", Location(PYPROJECT_TOML, 2), "ignore[update]", among_others=True)
        self.assert_tool_not_on_path_logged(PYPROJECT_TOML, "uv")

    @kills.delete(
        _LockBeforeUpdate.steered_update,
        " if locked else None",
        "without uv, a directive is judged by a lock uv did not write for the files as they are",
    )
    @kills.replace(
        marker_policy.kept_lock_policy,
        "if (reason := MARKER_POLICY.bound_redundancy(declaration)) or locked_as_written is None:",
        "if reason := MARKER_POLICY.bound_redundancy(declaration):",
        "without uv, a bound beside an `ignore[update]` is reported as if the lock kept a version for it",
    )
    def test_without_uv_a_directive_needing_a_locked_version_is_not_judged(self, run: Mock, get: Mock, glob: Mock):
        """Test that, without uv on the path, a bound beside an `ignore[update]` goes unjudged, as uv locks nothing."""
        self.leave_off_the_path("uv")
        contents = pyproject("package>=0.9", marker="ignore[update] allow[update<2] set[cooldown=30]")
        files = {PYPROJECT_TOML: contents, _UV_LOCK: _LOCK_WITHOUT_THE_PACKAGE}
        self.files_after_update(run, get, glob, files, _uv_lock("1.0"))
        self.assert_cooldown_per_run_logged()
        self.assert_tool_not_on_path_logged(PYPROJECT_TOML, "uv")


def _redundant_update_fields(reason: Reason, directive: str = "ignore[update]") -> _Fields:
    """Return the fields of the warning that the directive on the dependency `pyproject()` declares is redundant."""
    return {"directive": directive, "dependency": "package", "location": Location(PYPROJECT_TOML, 2), "reason": reason}


@patch("update_time.package_managers.uv.lock.configure_cooldown", Mock())
@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
@patch_get()
@patch("subprocess.run")
class CheckedPinsTest(ExecutablesOnPathTestCase):
    """Unit test for handing the discovered manifests to the checks uv-delegated updaters share."""

    @patch("update_time.updaters.update_pyproject_toml.warn_about_pins")
    def test_the_discovered_manifests_are_checked(self, warn: Mock, run: Mock, get: Mock, glob: Mock):
        """Test that the checks are handed every manifest the scan found, whether uv manages and runs for it or not."""
        build_requirements_alone = SCAN_ROOT / "tools/pyproject.toml"
        not_a_uv_project = SCAN_ROOT / "legacy/pyproject.toml"
        files = FakeFiles(
            {
                PYPROJECT_TOML: pyproject("django==3.2.0"),
                _UV_LOCK: _uv_lock("3.2.0", "django"),
                build_requirements_alone: _BUILD_REQUIREMENTS_ALONE,
                not_a_uv_project: pyproject("django==3.2.0"),
            }
        )
        run.return_value = Mock(stdout="", stderr="")
        glob.return_value = [PYPROJECT_TOML, build_requirements_alone, not_a_uv_project]
        with files.patched():
            _update_pyproject_tomls()
        get.assert_not_called()
        warn.assert_called_once_with([PyprojectToml(path) for path in glob.return_value], ANY, ANY)
