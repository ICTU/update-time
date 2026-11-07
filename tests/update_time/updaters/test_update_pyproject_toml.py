"""Unit tests for the pyproject.toml updater (discovery and orchestration of the uv package manager)."""

from itertools import repeat
from typing import TYPE_CHECKING
from unittest.mock import ANY, Mock, patch

from update_time.io.log import Logger
from update_time.lock_files import uv_lock
from update_time.manifests import pyproject_toml as pyproject_toml_manifest
from update_time.manifests import python_dependency_toml as python_dependency_toml_manifest
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.markers.directive import Reason
from update_time.markers.marker import Marker, Scope, Threshold
from update_time.package_managers.uv import lock, marker_policy
from update_time.primitives.location import Location
from update_time.updaters import uv_pins
from update_time.updaters.update_pyproject_toml import update_pyproject_tomls

from tests.helpers import FakeFiles, mock_path, mock_response, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import CHANGELOG
from tests.update_time.helpers import (
    SCAN_ROOT,
    LoggingTestCase,
    message_of,
    published_version,
    pyproject,
    uv_path_source,
)
from tests.update_time.sources.helpers import github_commits_json, github_release_json
from tests.update_time.updaters.helpers import (
    LOCK_FAILS,
    NO_SOLUTION,
    PYPROJECT_TOML,
    UPLOAD_TIME_WITH_CHANGELOG,
    UPLOAD_TIME_WITHOUT_CHANGELOG,
    UvLockTestCase,
    locked_package,
    no_vulnerabilities,
    pypi_answer,
    pyproject_per_line,
    uv_locks,
)

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.sources.pypi import ReleaseMetadata


# What the log renders for version 1.1, whose changelog PyPI does not name in `files_after_update`.
_NEW_VERSION = published_version("1.1", UPLOAD_TIME_WITHOUT_CHANGELOG)

# The pyproject.toml updater checks these tables. Each entry names the table's row, the case of the test below that
# pins it, and the error a run stops with once the row is gone. The `project` table is left out: another test guards
# its row too.
_CHECKED_TABLES = (
    (
        '("project", "optional-dependencies")',
        "project.optional-dependencies",
        "AttributeError: 'str' object has no attribute 'values'",
    ),
    ('("dependency-groups",)', "dependency-groups", "AttributeError: 'str' object has no attribute 'values'"),
    ('("tool",)', "tool", "AttributeError: 'str' object has no attribute 'get'"),
    ('("tool", "uv")', "tool.uv", "AttributeError: 'str' object has no attribute 'get'"),
    ('("build-system",)', "build-system", "AttributeError: 'str' object has no attribute 'get'"),
)

# A pyproject.toml that declares build requirements alone, which uv neither resolves nor locks.
_BUILD_REQUIREMENTS_ALONE = '[build-system]\nrequires = ["hatchling==1.20.0"]\n'


# The uv.lock beside the pyproject.toml at the root of the scan.
_UV_LOCK = SCAN_ROOT / "uv.lock"

# A workspace root at the root of the scan includes `member`, which declares its dependencies in this pyproject.toml.
_MEMBER = SCAN_ROOT / "member/pyproject.toml"

# A workspace root that includes `member` and `other`, and sources `other` from the workspace.
_ROOT_SOURCING_OTHER = (
    '[tool.uv.workspace]\nmembers = ["member", "other"]\n\n[tool.uv.sources]\nother = { workspace = true }\n'
)

# A uv.lock lists a dependency of the project in an extra, and one in a dependency group, under this table and group.
_EXTRA = ("optional-dependencies", "cli")
_GROUP = ("dev-dependencies", "dev")

# The run writes the cooldown into the pyproject.tomls, and then updates each workspace, so uv locks with the cooldown.
_COOLDOWN = "uv.configure_cooldown(projects_with_dependencies)"
_UPDATE = "warn_about_pins(uv_projects, _LOG, update_workspaces(projects_with_dependencies, _LOG))"


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


def _forked_uv_lock(below_python_3_12: str, from_python_3_12: str) -> str:
    """Return a uv.lock that locks `package` for the project at one version below Python 3.12, and at another from it.

    It also locks `package` at 1.0 for `other`, a dependency of the project that needs that version.
    """
    project = '[[package]]\nname = "project"\nversion = "0"\nsource = { virtual = "." }\ndependencies = [\n'
    project += '    { name = "other" },\n'
    forks = ((below_python_3_12, "python_full_version < '3.12'"), (from_python_3_12, "python_full_version >= '3.12'"))
    for version, marker in forks:
        project += f'    {{ name = "package", version = "{version}", marker = "{marker}" }},\n'
    other = '[[package]]\nname = "other"\nversion = "1"\ndependencies = [{ name = "package", version = "1.0" }]\n'
    packages = "\n".join(locked_package("package", version) for version in ("1.0", below_python_3_12, from_python_3_12))
    return f"version = 1\n\n{project}]\n\n{other}\n{packages}"


def _root_declaring(spec: str) -> str:
    """Return a workspace root that includes `member` and declares the dependency."""
    return f'[project]\nname = "root"\ndependencies = ["{spec}"]\n[tool.uv.workspace]\nmembers = ["member"]\n'


# Two ways the uv.lock before the run can leave out `package`: the project lacks a uv.lock, or its uv.lock locks another
# package.
_LOCKS_WITHOUT_THE_PACKAGE: dict[str, dict[Path, str]] = {
    "no uv.lock": {},
    "uv.lock without the package": {_UV_LOCK: _uv_lock("1.0", "other")},
}


def _discovered_pyproject_toml(glob: Mock, contents: str) -> Mock:
    """Return the single mock pyproject.toml the scan discovers, holding the contents."""
    pyproject_toml = mock_path(contents, parent=SCAN_ROOT)
    glob.return_value = [pyproject_toml]
    return pyproject_toml


class _PyprojectTomlLockTestCase(UvLockTestCase):
    """Base for the tests that run the pyproject.toml updater with uv lock faked."""

    manifest = PYPROJECT_TOML
    lock_file = _UV_LOCK

    def update(self) -> None:
        """Run the pyproject.toml updater."""
        update_pyproject_tomls()


# Persisting the cooldown into config is exercised by the uv package manager's tests; stub it out here so these tests
# focus on the discovery/version-update flow (and don't try to write config to the mock pyproject.toml files). The
# checks over the settled pins are stubbed out for the same reason: each makes registry requests of its own, and
# what they report is `test_uv_pins.py`'s to pin, while being handed the files found is `CheckedPinsTest`'s below.
@patch("update_time.updaters.update_pyproject_toml.warn_about_pins", Mock())
@patch("update_time.package_managers.uv.uv.configure_cooldown", Mock())
@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
@patch("requests.Session.get")
@patch("subprocess.run")
class UpdatePyprojectTomlsTest(_PyprojectTomlLockTestCase):
    """Unit tests for the update pyproject.tomls function."""

    @staticmethod
    def pypi_metadata(
        changelog_url: str = "https://changelog",
        repository: str = "https://github.com/repo/package_with_github_releases",
    ) -> ReleaseMetadata:
        """Create PyPI release metadata fixture."""
        project_urls = {"Homepage": "https://home", "repository": repository}
        if changelog_url:
            project_urls["Changelog"] = changelog_url
        return {
            "info": {"description": "Package description", "project_urls": project_urls},
            "urls": [{"upload_time_iso_8601": UPLOAD_TIME_WITH_CHANGELOG}],
        }

    def updated_pin(  # noqa: PLR0913 — the patched mocks travel with the fixtures they are given
        self,
        run: Mock,
        get: Mock,
        glob: Mock,
        *,
        package: str = "package",
        marker: str = "",
        latest: str = "1.1",
    ) -> FakeFiles:
        """Update a pyproject.toml whose only dependency pins the package on 1.0, which uv lock moves to the latest."""
        files = {PYPROJECT_TOML: pyproject(f"{package}==1.0", marker=marker), _UV_LOCK: _uv_lock("1.0", package)}
        return self.files_after_update(run, get, glob, files, _uv_lock(latest, package))

    def assert_no_cli_cooldown(self, run: Mock) -> None:
        """Assert that the three uv locks run without an `--exclude-newer` flag, since the cooldown lives in config."""
        self.assertEqual(["--exclude-newer" in command for command in uv_locks(run)], [False, False, False])

    def test_an_exact_pin_takes_the_version_uv_lock_records(self, run: Mock, get: Mock, glob: Mock):
        """Test that an exact pin moves to the version uv lock records, with the cooldown left to the config."""
        left = self.updated_pin(run, get, glob)
        self.assertEqual(left[PYPROJECT_TOML], pyproject("package==1.1"))
        self.assert_path_logged(_UV_LOCK)
        self.assert_new_version_logged("package", _NEW_VERSION, Location(PYPROJECT_TOML, 2))
        self.assert_no_cli_cooldown(run)
        self.assert_no_warnings_logged()

    def test_uv_lock_runs_on_the_file_as_written_before_upgrading_and_again_on_the_rewritten_pins(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that uv locks without `--upgrade` before and after the `--upgrade` lock, on the text the file holds."""
        self.updated_pin(run, get, glob)
        self.assertEqual(["--upgrade" in command for command in uv_locks(run)], [False, True, False])
        (_, files_at_the_first_lock), *_, (_, files_at_the_last_lock) = self.locks
        self.assertEqual(files_at_the_first_lock[PYPROJECT_TOML], pyproject("package==1.0"))
        self.assertEqual(files_at_the_last_lock[PYPROJECT_TOML], pyproject("package==1.1"))

    @kills(
        Mutation(
            lock._MemberSnapshot.steered_member,
            "member.warn_about_redundant_bounds(log)",
            "pass",
            "a bound that decides nothing in a pyproject.toml is not warned about",
        ),
        Mutation(
            lock._Member.warn_about_redundant_bounds,
            "current_version=current_version",
            "current_version=declaration.current_version",
            "a bound on a dependency without an exact pin is not judged at the version locked before, so it goes "
            "unreported",
        ),
    )
    def test_a_bound_that_decides_nothing_at_the_current_version_is_redundant(self, run: Mock, get: Mock, glob: Mock):
        """Test that a bound that never has an effect or blocks every update at the current version is warned about."""
        cases = {
            "exact pin": ("package==1.0", "allow[update>=0.1]"),
            "version locked before": ("package>=0.9", "ignore[patch-update]"),
        }
        for case, (spec, directive) in cases.items():
            with self.subTest(case):
                files = {PYPROJECT_TOML: pyproject(spec, marker=directive), _UV_LOCK: _uv_lock("1.0")}
                self.files_after_update(run, get, glob, files, _uv_lock("1.0"))
                self.assert_logged_among_others(
                    message_of(Logger.warn_if_redundant_bound),
                    dependency="package",
                    location=Location(PYPROJECT_TOML, 2),
                    directive=directive,
                    version="1.0",
                    redundancy=ANY,
                )

    @kills(
        Mutation(
            marker_policy._versions_locked_for,
            "by_name.get(normalized_python_name(declaration.dependency), [])",
            "by_name.get(declaration.dependency, [])",
            "a declaration that spells its name its own way misses the version the uv.lock recorded",
        ),
    )
    def test_a_bound_narrows_the_requirement_uv_locks(self, run: Mock, get: Mock, glob: Mock):
        """Test that a bound joins the requirement uv locks, after which an exact pin takes the locked version.

        A declaration without an exact pin gets the text the file wrote back. Its level bound is anchored at the version
        uv locks for the file as written.
        """
        cases = {
            "specifier bound": ("package==1.0", "allow[update<2]", "", "package<2,>=1.0", "package==1.1"),
            "level bound": ("package==1.0", "ignore[major-update]", "", "package==1.*,>=1.0", "package==1.1"),
            "pin a uv source resolves": (
                "package==1.0",
                "allow[update<2]",
                uv_path_source("package"),
                "package<2,>=1.0",
                "package==1.1",
            ),
            "without an exact pin": ("package>=1.0", "allow[update<1.2]", "", "package<1.2,>=1.0", "package>=1.0"),
            "without a version locked before": ("other>=1.0", "allow[update<2]", "", "other<2,>=1.0", "other>=1.0"),
            "level bound without an exact pin": (
                "package>=0.9",
                "ignore[minor-update]",
                "",
                "package==1.0.*,>=0.9",
                "package>=0.9",
            ),
            "level bound on a name the file spells its own way": (
                "Package>=0.9",
                "ignore[minor-update]",
                "",
                "Package==1.0.*,>=0.9",
                "Package>=0.9",
            ),
        }
        for case, (spec, bound, sources, locked, left_on) in cases.items():
            with self.subTest(case):
                contents = pyproject(spec, marker=bound) + sources
                files = {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}
                left = self.files_after_update(run, get, glob, files, _uv_lock("1.1"))
                self.assertEqual(self.text_locked_with_upgrade(), contents.replace(spec, locked))
                self.assertEqual(left[PYPROJECT_TOML], contents.replace(spec, left_on))

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

    @kills(
        Mutation(
            lock._LockUpdate._report_new_pins,
            "served = pypi_served(moved_pins)",
            "served = list(moved_pins)",
            "a pin uv resolves from elsewhere than PyPI is reported, and its changelog asked of PyPI",
        ),
        Mutation(
            lock._LockUpdate._report_new_pins,
            'log.new_version(Reference(pin.name, "", declaration.location), DependencyVersion(pin.version))',
            "pass",
            "a pin uv resolves from elsewhere than PyPI moves without a word",
        ),
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

    @kills(
        Mutation(
            lock._Member.reportable_declarations,
            "if not declaration.build_requirement]",
            "]",
            "a version uv lock moves is also reported at a build requirement of its name, which uv does not lock",
        )
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

    @kills(
        Mutation(
            lock._LockUpdate._report_pins_with_environment_markers,
            "and not declaration.build_requirement:",
            ":",
            "a build requirement with an environment marker is reported as left, though uv does not lock it",
        )
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

    @kills(
        Mutation(
            uv_lock._project_dependencies,
            'entry.get("optional-dependencies", {})',
            "{}",
            "the versions locked for the project's extras go unreported",
        ),
        Mutation(
            uv_lock._project_dependencies,
            'entry.get("dev-dependencies", {})',
            "{}",
            "the versions locked for the project's dependency groups go unreported",
        ),
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

    @kills(
        Mutation(
            uv_lock._project_dependencies,
            'entry.get("dependency-groups", {})',
            "{}",
            "the run leaves the dependency groups in the manifest of a uv.lock unreported",
        ),
    )
    def test_the_locked_version_of_a_dependency_group_of_a_workspace_root_without_a_project_is_reported(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a group of a workspace root without `[project]`, which uv.lock lists in `[manifest]`, is read."""
        manifest = '[manifest]\nmembers = ["member"]\n\n[manifest.dependency-groups]\n'
        manifest += 'dev = [{ name = "package", specifier = ">=1.0" }]\n\n'
        member = '[[package]]\nname = "member"\nversion = "0"\nsource = { virtual = "member" }\n\n'
        root = '[tool.uv.workspace]\nmembers = ["member"]\n\n[dependency-groups]\ndev = ["package>=1.0"]\n'
        lock_before, lock_after = (
            f"version = 1\n\n{manifest}{member}{locked_package('package', v)}" for v in ("1.0", "1.1")
        )
        self.files_after_update(run, get, glob, {PYPROJECT_TOML: root, _UV_LOCK: lock_before}, lock_after)
        self.assert_new_version_logged("package", _NEW_VERSION, Location(PYPROJECT_TOML, 5))

    @kills(
        Mutation(
            uv_lock._versions_of,
            '[dependency["version"]] if "version" in dependency else locked.get(dependency["name"], [])',
            'locked.get(dependency["name"], [])',
            "a dependency locked at several versions is reported at one the project may not use",
        ),
    )
    def test_a_dependency_locked_at_several_versions_is_pinned_and_reported_at_the_lowest_the_project_uses(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a dependency uv locks per Python version is pinned and reported at the lowest the project uses."""
        contents = '[project]\ndependencies = ["package>=1.0"]\n[dependency-groups]\ndev = ["package==1.1"]\n'
        cases = {
            "the lower version listed first": (_forked_uv_lock("1.1", "1.4"), _forked_uv_lock("1.2", "1.5")),
            "the higher version listed first": (_forked_uv_lock("1.4", "1.1"), _forked_uv_lock("1.5", "1.2")),
        }
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
            _uv_lock(package).replace('[{ name = "package" }]', both) + "\n" + other.format(other_version)
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
            _uv_lock(package, project_source="member").replace('[{ name = "package" }]', both)
            + "\n"
            + other.format(other_version)
            for other_version, package in (("0.1", "1.0"), ("0.2", "1.1"))
        ]
        files = {PYPROJECT_TOML: root, _MEMBER: pyproject("other", "package>=1.0"), _UV_LOCK: locks[0]}
        self.files_after_update(run, get, glob, files, locks[1], discovered=[_MEMBER])
        self.assert_new_version_logged("package", _NEW_VERSION, Location(_MEMBER, 2))

    @kills(
        Mutation(
            uv_lock.UvLock.recorded_versions,
            'for package in packages if "version" in package',
            "for package in packages",
            "the run stops where the lock does not record a version for a dependency, such as a dynamic sibling member",
            raises="KeyError: 'version'",
        )
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

    @kills(
        Mutation(
            lock._LockUpdate._rewrite,
            "except OSError as error:",
            "except ValueError as error:",
            "a pyproject.toml that cannot be written stops the run, leaving the other files of its workspace relaxed",
            raises="PermissionError: [Errno 13] Permission denied: '/member/pyproject.toml'",
        )
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

    @kills(
        Mutation(
            lock._LockTarget._read,
            "except (OSError, UnicodeDecodeError) as error:",
            "except OSError as error:",
            "a uv.lock that is not UTF-8 stops the run",
            raises="UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
        )
    )
    def test_a_uv_lock_that_cannot_be_read_is_reported_and_its_workspace_is_left_as_it_is(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a uv.lock the run cannot read is reported, and that uv locks the other workspaces alone."""
        other = SCAN_ROOT / "other/pyproject.toml"
        other_lock = SCAN_ROOT / "other/uv.lock"
        for case, error in {
            "a uv.lock without read permission": PermissionError(13, "Permission denied", str(other_lock)),
            "a uv.lock that is not UTF-8": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        }.items():
            with self.subTest(case):
                contents = pyproject("package==1.0")
                files = FakeFiles({PYPROJECT_TOML: contents, other: contents, other_lock: _uv_lock("1.0")})
                files.read_errors[other_lock] = error
                discovered = [PYPROJECT_TOML, other]
                left = self.files_after_update(run, get, glob, files, _uv_lock("1.1"), discovered=discovered)
                self.assertEqual(left[PYPROJECT_TOML], pyproject("package==1.1"))
                self.assertEqual(left[other], contents)
                directories = {command[command.index("--directory") + 1] for command in uv_locks(run)}
                self.assertEqual(directories, {str(SCAN_ROOT)})
                self.assert_errors_logged(
                    (message_of(Logger.lock_not_read), {"location": Location(other_lock), "error": str(error)})
                )

    @kills(
        Mutation(
            lock._LockUpdate.update,
            "self._restore()\n            return",
            "self._restore()",
            "a failed uv lock is followed by reports of versions the lock did not move",
        ),
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
            (message_of(Logger.uv_dependencies_not_updated), {"location": Location(PYPROJECT_TOML), "tool": "uv lock"}),
            (message_of(Logger.file_not_restored), {"location": Location(_UV_LOCK), "error": str(error)}),
        )
        self.assert_no_new_version_logged()

    @kills(
        Mutation(
            update_pyproject_tomls,
            f"{_COOLDOWN}\n    {_UPDATE}",
            f"{_UPDATE}\n    {_COOLDOWN}",
            "uv locks the pyproject.toml before the run writes the cooldown uv reads from it",
        ),
        Mutation(
            uv_lock.UvLock.restore,
            "self.path.unlink(missing_ok=True)",
            "self.path.unlink()",
            "giving back a uv.lock that held nothing raises where uv did not write one either",
            raises="FileNotFoundError: [Errno 2] No such file or directory: '/uv.lock'",
        ),
    )
    def test_a_failed_uv_lock_keeps_the_cooldown_the_run_wrote(self, run: Mock, get: Mock, glob: Mock):
        """Test that uv locks the pyproject.toml with the cooldown the run wrote, and a failed lock leaves it there."""
        contents = pyproject("package==1.0")
        cooldown = '[tool.uv]\nexclude-newer = "7 days"\n'

        def configure_cooldown(pyproject_tomls: list[PyprojectToml]) -> None:
            for pyproject_toml in pyproject_tomls:
                pyproject_toml.write(pyproject_toml.read() + cooldown)

        with patch("update_time.package_managers.uv.uv.configure_cooldown", side_effect=configure_cooldown):
            left = self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents}, LOCK_FAILS)
        self.assert_uv_dependencies_not_updated_logged(PYPROJECT_TOML, "uv lock")
        (_, files_at_the_first_lock), *_ = self.locks
        self.assertEqual(files_at_the_first_lock[PYPROJECT_TOML], contents + cooldown)
        self.assertEqual(left[PYPROJECT_TOML], contents + cooldown)

    def test_without_an_earlier_lock_only_the_exact_pin_is_reported_and_the_new_uv_lock_stays(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a run without an earlier uv.lock keeps the one uv writes, and reports the exact pin's version."""
        pyproject_toml_contents = (
            '[project]\nname = "a"\ndependencies = ["package>=1.0"]\n[dependency-groups]\ndev = ["package==1.0"]\n'
        )
        left = self.files_after_update(run, get, glob, {PYPROJECT_TOML: pyproject_toml_contents}, _uv_lock("1.2"))
        self.assertEqual(left[_UV_LOCK], _uv_lock("1.2"))
        version = published_version("1.2", UPLOAD_TIME_WITHOUT_CHANGELOG)
        self.assert_new_version_logged("package", version, Location(PYPROJECT_TOML, 5))

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

    def test_uv_lock_upgrades_once_per_workspace(self, run: Mock, get: Mock, glob: Mock):
        """Test that uv lock upgrades each workspace once, at its root, however many projects declare dependencies."""
        other = SCAN_ROOT / "other/pyproject.toml"
        cases = {
            "one workspace": (
                {PYPROJECT_TOML: _root_declaring("package>=1.0"), _MEMBER: pyproject("package>=1.0")},
                [SCAN_ROOT],
            ),
            "two projects": (
                {PYPROJECT_TOML: pyproject("package>=1.0"), other: pyproject("package>=1.0")},
                [SCAN_ROOT, other.parent],
            ),
        }
        for case, (files, roots) in cases.items():
            with self.subTest(case):
                run.reset_mock()
                self.files_after_update(run, get, glob, files, _uv_lock("1.0"), discovered=list(files))
                upgrades = [command for command in uv_locks(run) if "--upgrade" in command]
                directories = [command[command.index("--directory") + 1] for command in upgrades]
                self.assertEqual(directories, [str(root) for root in roots])
                logged = [
                    self.records_of(message_of(Logger.path), location=Location(root / "uv.lock")) for root in roots
                ]
                self.assertEqual([len(records) for records in logged], [1] * len(roots))
                get.assert_not_called()

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

    def test_an_invalid_bracket_item_leaves_the_pin_unchanged(self, run: Mock, get: Mock, glob: Mock):
        """Test that an item the marker language cannot read warns and keeps the pin, without reporting the marker."""
        for directive, item in {"ignore[stlae]": "stlae", "ignore[update": "[update"}.items():
            with self.subTest(directive=directive):
                left = self.updated_pin(run, get, glob, marker=directive)
                self.assertNotIn(PYPROJECT_TOML, left.written_paths)
                get.assert_not_called()
                self.assert_no_new_version_logged()
                self.assert_invalid_bracket_item_logged("package", Location(PYPROJECT_TOML, 2), item)
                # The item is reported as invalid, so the marker is neither echoed back nor obeyed as a hold-back.
                self.assert_none_logged(message_of(Logger.recognised_marker), "recognised marker")
                self.assert_none_logged(message_of(Logger.ignored), "update held back by a marker")

    def test_update_with_changelog(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml with changelog."""
        get.side_effect = [
            mock_response(self.pypi_metadata()),
            Mock(headers={"Content-Type": "text"}, text=CHANGELOG),
        ]
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

    def test_update_with_html_changelog(self, run: Mock, get: Mock, glob: Mock):
        """Test that updating a pyproject.toml with only a HTML changelog ignores the changelog."""
        get.side_effect = [
            mock_response(self.pypi_metadata()),
            Mock(text=CHANGELOG, headers={"Content-Type": "text/html"}),
            mock_response([github_release_json("v1.1")]),
            mock_response([]),
        ]
        left = self.updated_pin(run, get, glob, package="package_with_html_changelog")
        self.assertEqual(left[PYPROJECT_TOML], pyproject("package_with_html_changelog==1.1"))
        self.assert_path_logged(_UV_LOCK)
        self.assert_new_version_logged(
            "package_with_html_changelog",
            published_version("1.1", UPLOAD_TIME_WITH_CHANGELOG),
            Location(PYPROJECT_TOML, 2),
        )
        self.assert_no_warnings_logged()

    def test_update_with_github_url(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml with GitHub releases."""
        get.side_effect = [
            mock_response(self.pypi_metadata(changelog_url="")),
            mock_response([github_release_json("v1.1", body=CHANGELOG)]),
            mock_response(github_commits_json()),
        ]
        left = self.updated_pin(run, get, glob, package="package_with_github_releases")
        self.assertEqual(left[PYPROJECT_TOML], pyproject("package_with_github_releases==1.1"))
        self.assert_path_logged(_UV_LOCK)
        self.assert_new_version_logged(
            "package_with_github_releases",
            published_version("1.1", UPLOAD_TIME_WITH_CHANGELOG),
            Location(PYPROJECT_TOML, 2),
            changes=CHANGELOG,
        )
        self.assert_no_warnings_logged()

    def test_update_without_github_url(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml without a GitHub URL."""
        get.side_effect = repeat(
            mock_response(self.pypi_metadata(changelog_url="", repository="https://gitlab.com/org/repo"))
        )
        left = self.updated_pin(run, get, glob, package="package_without_github_releases")
        self.assertEqual(left[PYPROJECT_TOML], pyproject("package_without_github_releases==1.1"))
        self.assert_path_logged(_UV_LOCK)
        self.assert_new_version_logged(
            "package_without_github_releases",
            published_version("1.1", UPLOAD_TIME_WITH_CHANGELOG),
            Location(PYPROJECT_TOML, 2),
        )
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            lock._LockUpdate._moved_pins,
            "PEP_440.order(locked[name].version) != PEP_440.order(declaration.current_version)",
            "locked[name].version != declaration.current_version",
            "where the lock spells a pinned 1.0 as 1.0.0, the pin becomes 1.0.0 and is reported as a new version",
        )
    )
    def test_a_pin_uv_lock_keeps_leaves_the_pyproject_toml_as_it_was(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pin uv lock keeps on its version leaves the file as it was, and Update-time does not log it."""
        for case, locked in {"the same spelling": "1.0", "a more precise spelling": "1.0.0"}.items():
            with self.subTest(case):
                left = self.updated_pin(run, get, glob, latest=locked)
                self.assertEqual(left[PYPROJECT_TOML], pyproject("package==1.0"))
                get.assert_not_called()
                self.assert_path_logged(_UV_LOCK)
                self.assert_no_new_version_logged()
                self.assert_no_warnings_logged()

    def test_a_failed_uv_lock_of_the_file_as_written_is_reported_at_error_and_nothing_locks_after_it(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that where uv cannot lock the file as written, the run reports that at error and stops there."""
        left = self.updated_with_failing_lock(run, glob, pyproject("package>=1.0"))
        self.assertEqual(["--upgrade" in command for command in uv_locks(run)], [False])
        self.assertNotIn(PYPROJECT_TOML, left.written_paths)
        get.assert_not_called()
        self.assert_command_stderr_logged(stderr=NO_SOLUTION)
        self.assert_uv_dependencies_not_updated_logged(PYPROJECT_TOML, "uv lock")

    def test_a_failed_uv_lock_writes_the_files_the_run_rewrote_back(self, run: Mock, get: Mock, glob: Mock):
        """Test that a uv lock failing on the new pins gives the workspace's files back, and logs the failure alone."""
        pyproject_tomls = {PYPROJECT_TOML: _root_declaring("package==1.0"), _MEMBER: pyproject("package==1.0")}
        message = message_of(Logger.uv_dependencies_not_updated)
        for case, files in {
            "an earlier uv.lock": {**pyproject_tomls, _UV_LOCK: _uv_lock("1.0")},
            "no earlier uv.lock": pyproject_tomls,
        }.items():
            with self.subTest(case):
                discovered = list(pyproject_tomls)
                lock_after = (_uv_lock("1.1"), LOCK_FAILS)
                left = self.files_after_update(run, get, glob, files, lock_after, discovered=discovered)
                self.assertEqual(dict(left), files)
                self.assert_errors_logged(
                    (message, {"location": Location(PYPROJECT_TOML), "tool": "uv lock"}),
                    (message, {"location": Location(_MEMBER), "tool": "uv lock"}),
                )
                self.assert_no_new_version_logged()

    @patch("shutil.which", Mock(return_value=None))
    def test_a_uv_missing_from_the_path_is_reported_in_one_line(self, run: Mock, get: Mock, glob: Mock):
        """Test that a missing uv is reported in one line naming the file, and that Update-time does not run it."""
        run.side_effect = FileNotFoundError("uv")
        mock_pyproject_toml = _discovered_pyproject_toml(glob, pyproject("package==1.0"))
        update_pyproject_tomls()
        run.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        get.assert_not_called()
        self.assert_error_logged(message_of(Logger.uv_not_on_path), location=Location(mock_pyproject_toml))

    def test_skip_non_uv_tool_section(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pyproject.toml with a non-uv tool section (e.g. [tool.poetry]) is skipped without running uv."""
        mock_pyproject_toml = _discovered_pyproject_toml(
            glob, pyproject("package==1.0") + '\n[tool.poetry]\nname = "x"\n'
        )
        update_pyproject_tomls()
        run.assert_not_called()
        get.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_unsupported_package_manager_logged(mock_pyproject_toml, "poetry", "uv")
        self.assert_no_new_version_logged()

    @patch_pathlib_path(exists=True)
    def test_skip_non_uv_lockfile(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pyproject.toml beside a non-uv lockfile (e.g. poetry.lock) is reported as unsupported."""
        mock_pyproject_toml = _discovered_pyproject_toml(glob, _BUILD_REQUIREMENTS_ALONE)
        update_pyproject_tomls()
        run.assert_not_called()
        get.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_unsupported_package_manager_logged(mock_pyproject_toml, "poetry", "uv")
        self.assert_no_new_version_logged()

    def test_skip_invalid_pyproject_toml(self, run: Mock, get: Mock, glob: Mock):
        """Test that an unparsable pyproject.toml is skipped with a warning, without running uv or crashing."""
        not_utf_8 = mock_path("", parent=SCAN_ROOT)
        not_utf_8.read_text.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        for case, mock_pyproject_toml in (
            ("a missing bracket", mock_path('[project]\ndependencies = ["package==1.0"\n', parent=SCAN_ROOT)),
            ("a file that is not UTF-8", not_utf_8),
        ):
            with self.subTest(case):
                glob.return_value = [mock_pyproject_toml]
                update_pyproject_tomls()
                run.assert_not_called()
                get.assert_not_called()
                mock_pyproject_toml.write_text.assert_not_called()
                self.assert_invalid_file_logged(mock_pyproject_toml, "TOML")
                self.assert_no_new_version_logged()

    @kills(
        *(
            Mutation(
                python_dependency_toml_manifest,
                f"    {row},\n",
                "",
                f"a pyproject.toml whose {table} is not a table stops the run",
                raises=error,
            )
            for row, table, error in _CHECKED_TABLES
        ),
        Mutation(
            python_dependency_toml_manifest.is_well_formed,
            "return all(isinstance(array, list) for array in _dependency_arrays(config))",
            "return True",
            "a pyproject.toml whose dependency array is not an array has uv run for it",
        ),
    )
    def test_skip_pyproject_toml_whose_tables_or_arrays_hold_another_value(self, run: Mock, get: Mock, glob: Mock):
        """Test that the updater warns about and skips a pyproject.toml whose tables or arrays hold another value."""
        cases = {
            "project": 'project = "x"\n',
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
                update_pyproject_tomls()
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
        run.return_value = Mock(stdout="", stderr="")
        for case, contents in cases.items():
            with self.subTest(case):
                mock_pyproject_toml = _discovered_pyproject_toml(glob, contents)
                with patch("update_time.package_managers.uv.uv.configure_cooldown") as configure_cooldown:
                    update_pyproject_tomls()
                run.assert_not_called()
                get.assert_not_called()
                configure_cooldown.assert_called_once_with([])
                mock_pyproject_toml.write_text.assert_not_called()
                self.assert_no_dependencies_logged(mock_pyproject_toml, "uv", "resolve")

    def test_a_marker_in_a_file_uv_does_not_run_for_is_recognised(self, run: Mock, get: Mock, glob: Mock):
        """Test that the marker on a build requirement of a pyproject.toml that uv does not run for is reported."""
        contents = '[build-system]\nrequires = ["hatchling==1.20.0"]  # update-time: set[stale-after=0]\n'
        mock_pyproject_toml = _discovered_pyproject_toml(glob, contents)
        update_pyproject_tomls()
        run.assert_not_called()
        get.assert_not_called()
        marker = Marker(stale=Threshold(value=0))
        self.assert_recognised_marker_logged("hatchling", Location(mock_pyproject_toml, 2), marker)

    @kills(
        Mutation(
            pyproject_toml_manifest,
            '("dependencies", "optional-dependencies")',
            '("dependencies",)',
            "uv does not run for a project whose extras its build backend declares",
        )
    )
    def test_uv_runs_for_each_array_it_resolves(self, run: Mock, get: Mock, glob: Mock):
        """Test that uv runs for a pyproject.toml whose only dependency sits in any of the arrays uv resolves."""
        cases = {
            "an extra": '[project]\nname = "a"\n\n[project.optional-dependencies]\ncli = ["package==1.0"]\n',
            "a dependency group without a project": '[dependency-groups]\ndev = ["package==1.0"]\n',
            "uv's legacy dev-dependencies": '[tool.uv]\ndev-dependencies = ["package==1.0"]\n',
            "dynamic dependencies": '[project]\nname = "a"\ndynamic = ["dependencies"]\n',
            "dynamic extras": '[project]\nname = "a"\ndynamic = ["optional-dependencies"]\n',
        }
        for case, contents in cases.items():
            with self.subTest(case):
                run.reset_mock()
                self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents}, _uv_lock("1.0"))
                self.assertTrue(uv_locks(run))


@no_vulnerabilities
@patch("update_time.package_managers.uv.uv.configure_cooldown", Mock())
@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
@patch("requests.Session.get", side_effect=pypi_answer)
@patch("subprocess.run")
class RedundantDirectiveTest(_PyprojectTomlLockTestCase):
    """Unit tests for the directives a run reports as redundant in a pyproject.toml that uv locks."""

    def test_a_directive_steering_the_lock_is_not_reported_as_redundant(self, run: Mock, get: Mock, glob: Mock):
        """Test that an `ignore[update]` and a bound on a dependency without an exact pin go unreported."""
        for directive in ("ignore[update]", "allow[update<2]"):
            with self.subTest(directive=directive):
                contents = pyproject("package>=0.9", marker=f"{directive} set[cooldown=30]")
                self.files_after_update(
                    run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}, _uv_lock("1.0")
                )
                self.assert_redundant_directive_logged(
                    Reason.COOLDOWN_PER_RUN, "package", Location(PYPROJECT_TOML, 2), "set[cooldown=30]"
                )

    @kills(
        *(
            Mutation(
                python_dependency_toml_manifest.Declaration.unnarrowable_reason,
                f"Reason.{reason}",
                "Reason.BOUND_DECIDES_NOTHING",
                f"a bound Update-time does not narrow for {case} is reported as if uv resolved the version",
            )
            for reason, case in (
                ("BUILD_REQUIREMENT_NOT_LOCKED", "a build requirement"),
                ("ENVIRONMENT_MARKER_PIN_KEPT", "a pin with an environment marker"),
                ("BOUND_ON_A_HELD_BACK_UPDATE", "a pin whose update is held back"),
            )
        ),
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

    @kills(
        Mutation(
            uv_pins.warn_about_pins,
            "declarations = uv.declarations_of(file)",
            "declarations = file.declarations()",
            "the run checks a member's dependency against PyPI where the workspace root sources it",
        )
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

    @kills(
        Mutation(
            marker_policy.kept_lock_policy,
            "if not declaration.updatable and hold_back_redundancy(declaration) is None:",
            "if False:",
            "a bound beside an `ignore[update]` keeping the locked version goes unreported, though it decides nothing",
        )
    )
    def test_a_bound_beside_an_ignore_update_keeping_the_locked_version_is_reported_as_deciding_nothing(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a bound beside an `ignore[update]` on a dependency without an exact pin is reported."""
        contents = pyproject("package>=0.9", marker="ignore[update] allow[update<2]")
        files = {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}
        self.files_after_update(run, get, glob, files, _uv_lock("1.0"))
        self.assertEqual(self.text_locked_with_upgrade(), contents.replace("package>=0.9", "package==1.0,>=0.9"))
        self.assert_redundant_directive_logged(
            Reason.BOUND_ON_A_HELD_BACK_UPDATE, "package", Location(PYPROJECT_TOML, 2), "allow[update<2]"
        )

    @kills(
        Mutation(
            marker_policy.kept_lock_policy,
            "if not declaration.updatable and hold_back_redundancy(declaration) is None:",
            "if not declaration.updatable:",
            "a bound beside an `ignore[update]` that keeps nothing is reported, and narrows the lock no longer",
        )
    )
    def test_a_bound_beside_an_ignore_update_without_a_locked_version_narrows_the_lock_and_goes_unreported(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a bound narrows the lock where the `ignore[update]` beside it does not have a version to keep."""
        contents = pyproject("package>=0.9", marker="ignore[update] allow[update<2]")
        lock_without_the_package = _uv_lock("1.0", "other")
        files = {PYPROJECT_TOML: contents, _UV_LOCK: lock_without_the_package}
        self.files_after_update(run, get, glob, files, lock_without_the_package)
        self.assertEqual(self.text_locked_with_upgrade(), contents.replace("package>=0.9", "package<2,>=0.9"))
        self.assert_redundant_directive_logged(
            Reason.NO_LOCKED_VERSION, "package", Location(PYPROJECT_TOML, 2), "ignore[update]"
        )

    @kills(
        Mutation(
            marker_policy.kept_lock_policy,
            "if declaration.pins_a_version or not declaration.narrowable:",
            "if declaration.pins_a_version:",
            "an `ignore[update]` reads as keeping a version that uv does not lock by a specifier",
        ),
        Mutation(
            marker_policy._hold_back_redundancy,
            "if declaration.build_requirement:",
            "if False:",
            "an `ignore[update]` on a pinned build requirement goes unreported, though Update-time leaves that pin",
        ),
    )
    def test_an_ignore_update_that_uv_does_not_lock_by_keeps_nothing(self, run: Mock, get: Mock, glob: Mock):
        """Test that an `ignore[update]` on a direct URL or a build requirement is reported, and holds nothing back."""

        def build_requirement(spec: str) -> str:
            return (
                '[project]\ndependencies = ["package>=0.9"]\n'
                f'[build-system]\nrequires = ["{spec}"]  # update-time: ignore[update]\n'
            )

        url = "other @ https://example.com/other-1.0.tar.gz"
        resolved = Reason.MANAGER_RESOLVES_THE_VERSION
        cases = {
            "build requirement": (build_requirement("package>=0.9"), _uv_lock("1.0"), "package", 4, resolved),
            "pinned build requirement": (
                build_requirement("package==0.9"),
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
                resolved,
            ),
        }
        for case, (contents, lock_text, dependency, line, reason) in cases.items():
            with self.subTest(case):
                self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: lock_text}, lock_text)
                self.assert_redundant_directive_logged(
                    reason, dependency, Location(PYPROJECT_TOML, line), "ignore[update]"
                )
                self.assert_none_logged(message_of(Logger.ignored), "update held back by a marker")

    @kills(
        Mutation(
            marker_policy._hold_back_redundancy,
            "return Reason.ENVIRONMENT_MARKER_PIN_LEFT if declaration.environment_marker else None",
            "return None",
            "an `ignore[update]` on a pin with an environment marker goes unreported, though the pin stays anyway",
        )
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
        self.assert_none_logged(message_of(Logger.ignored), "update held back by a marker")

    def test_a_directive_takes_the_version_a_lock_of_the_file_as_written_records(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that an `ignore[update]` and a level bound take the version uv locks, though the uv.lock had none."""
        locked_specs = {"ignore[update]": "package==1.0,>=0.9", "ignore[minor-update]": "package==1.0.*,>=0.9"}
        for directive, locked_spec in locked_specs.items():
            for lock_case, lock_before in _LOCKS_WITHOUT_THE_PACKAGE.items():
                with self.subTest(directive=directive, lock=lock_case):
                    contents = pyproject("package>=0.9", marker=f"{directive} set[cooldown=30]")
                    files = {PYPROJECT_TOML: contents, **lock_before}
                    self.files_after_update(run, get, glob, files, _uv_lock("1.0"), lock_as_written=_uv_lock("1.0"))
                    self.assertEqual(self.text_locked_with_upgrade(), contents.replace("package>=0.9", locked_spec))
                    self.assert_redundant_directive_logged(
                        Reason.COOLDOWN_PER_RUN, "package", Location(PYPROJECT_TOML, 2), "set[cooldown=30]"
                    )

    @kills(
        Mutation(
            marker_policy.current_version,
            'return versions.pop() if len(versions) == 1 else ""',
            'return versions.pop() if versions else ""',
            "an `ignore[update]` holds a dependency on one of the versions it is locked at, and moves it off the rest",
        ),
        Mutation(
            marker_policy._current_version_redundancy,
            "return Reason.SEVERAL_LOCKED_VERSIONS if several else",
            "return Reason.NO_LOCKED_VERSION if several else",
            "a directive on a dependency locked at several versions is reported as if none were locked",
        ),
    )
    def test_a_directive_on_a_dependency_locked_at_several_versions_narrows_nothing_and_is_reported(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that an `ignore[update]` and a level bound leave a dependency the lock records at two versions to uv."""
        forked = _forked_uv_lock("1.1", "1.4")
        for directive in ("ignore[update]", "ignore[minor-update]"):
            with self.subTest(directive=directive):
                contents = pyproject("package>=1.0", marker=directive)
                files = {PYPROJECT_TOML: contents, _UV_LOCK: forked}
                self.files_after_update(run, get, glob, files, forked, lock_as_written=forked)
                self.assertEqual(self.text_locked_with_upgrade(), contents)
                self.assert_redundant_directive_logged(
                    Reason.SEVERAL_LOCKED_VERSIONS, "package", Location(PYPROJECT_TOML, 2), directive
                )

    @kills(
        Mutation(
            marker_policy.current_version,
            "if declaration.current_version:",
            "if False:",
            "a bound on an exact pin narrows nothing where the lock records the package at several versions",
        )
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
        self.assert_redundant_directive_logged(
            Reason.COOLDOWN_PER_RUN, "package", Location(PYPROJECT_TOML, 2), "set[cooldown=30]"
        )

    @patch("shutil.which", Mock(return_value=None))
    def test_without_uv_an_ignore_update_is_obeyed_and_not_judged_by_the_uv_lock_before_the_run(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that, without uv on the path, an `ignore[update]` holds the update back, unjudged by the uv.lock."""
        contents = pyproject("package>=0.9", marker="ignore[update] set[cooldown=30]")
        self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")}, _uv_lock("1.0"))
        self.assert_redundant_directive_logged(
            Reason.COOLDOWN_PER_RUN, "package", Location(PYPROJECT_TOML, 2), "set[cooldown=30]"
        )
        self.assert_ignored_logged("package", Location(PYPROJECT_TOML, 2), "ignore[update]", among_others=True)
        self.assert_error_logged(message_of(Logger.uv_not_on_path), location=Location(PYPROJECT_TOML))

    @patch("shutil.which", Mock(return_value=None))
    @kills(
        Mutation(
            lock._LockSnapshot.update,
            "self.target.uv_lock.recorded_versions(member.project_directory) if locked else None",
            "self.target.uv_lock.recorded_versions(member.project_directory)",
            "without uv, a directive is judged by a lock uv did not write for the files as they are",
        ),
        Mutation(
            marker_policy.kept_lock_policy,
            "if (reason := MARKER_POLICY.bound_redundancy(declaration)) or locked_as_written is None:",
            "if reason := MARKER_POLICY.bound_redundancy(declaration):",
            "without uv, a bound beside an `ignore[update]` is reported as if the lock kept a version for it",
        ),
    )
    def test_without_uv_a_directive_needing_a_locked_version_is_not_judged(self, run: Mock, get: Mock, glob: Mock):
        """Test that, without uv on the path, an `ignore[update]` and a bound go unjudged, as uv locks nothing."""
        for directive in ("ignore[update]", "ignore[minor-update]", "ignore[update] allow[update<2]"):
            for lock_case, lock_before in _LOCKS_WITHOUT_THE_PACKAGE.items():
                with self.subTest(directive=directive, lock=lock_case):
                    contents = pyproject("package>=0.9", marker=f"{directive} set[cooldown=30]")
                    self.files_after_update(run, get, glob, {PYPROJECT_TOML: contents, **lock_before}, _uv_lock("1.0"))
                    self.assert_redundant_directive_logged(
                        Reason.COOLDOWN_PER_RUN, "package", Location(PYPROJECT_TOML, 2), "set[cooldown=30]"
                    )
                    self.assert_error_logged(message_of(Logger.uv_not_on_path), location=Location(PYPROJECT_TOML))


@patch("update_time.package_managers.uv.uv.configure_cooldown", Mock())
@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
@patch("requests.Session.get")
@patch("subprocess.run")
class CheckedPinsTest(LoggingTestCase):
    """Unit test for handing the discovered manifests to the checks uv-delegated updaters share."""

    @kills(
        Mutation(
            update_pyproject_tomls,
            "warn_about_pins(uv_projects,",
            "warn_about_pins(projects_with_dependencies,",
            "a pyproject.toml that uv does not run for is not checked either",
        )
    )
    @patch("update_time.updaters.update_pyproject_toml.warn_about_pins")
    def test_the_discovered_manifests_are_checked(self, warn: Mock, _run: Mock, get: Mock, glob: Mock):
        """Test that the checks are handed every manifest the scan found, whether uv ran for it or not."""
        pyproject_toml = _discovered_pyproject_toml(glob, pyproject("django==3.2.0"))
        build_requirements_alone = mock_path(_BUILD_REQUIREMENTS_ALONE, parent=SCAN_ROOT)
        glob.return_value.append(build_requirements_alone)
        update_pyproject_tomls()
        get.assert_not_called()
        warn.assert_called_once_with([PyprojectToml(pyproject_toml), PyprojectToml(build_requirements_alone)], ANY, ANY)
