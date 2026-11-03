"""Unit tests for the pyproject.toml updater (discovery and orchestration of the uv package manager)."""

import subprocess  # nosec
from typing import TYPE_CHECKING
from unittest.mock import ANY, DEFAULT, Mock, patch

from update_time.io.log import Logger
from update_time.lock_files import uv_lock
from update_time.manifests import python_dependency_toml as python_dependency_toml_manifest
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.markers.marker import Marker, Scope, Threshold
from update_time.package_managers import uv
from update_time.primitives.location import Location
from update_time.updaters.update_pyproject_toml import update_pyproject_tomls

from tests.helpers import FakeFiles, mock_path, mock_response, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import CHANGELOG
from tests.update_time.helpers import SCAN_ROOT, LoggingTestCase, message_of, published_version, pyproject
from tests.update_time.sources.helpers import github_commits_json, github_release_json
from tests.update_time.updaters.helpers import (
    PYPROJECT_TOML,
    UPLOAD_TIME_WITH_CHANGELOG,
    UPLOAD_TIME_WITHOUT_CHANGELOG,
    pypi_metadata_without_changelog,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from update_time.sources.pypi import ReleaseMetadata


# What the log renders for the new version `updated_pyproject_toml` has uv and PyPI report.
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

# The run writes the cooldown into the pyproject.tomls, and then reads each workspace to give back where uv lock fails.
_COOLDOWN = "uv.configure_cooldown(projects_with_dependencies)"
_SNAPSHOT = "workspaces = uv.read_workspaces(projects_with_dependencies)"

# The lock after that `updated_lock` takes for a uv lock that fails.
_LOCK_FAILS = None

# What uv lock writes to stderr where the pins contradict each other.
_NO_SOLUTION = "error: No solution found when resolving dependencies"


def _uv_lock(
    version: str, package: str = "package", project_source: str = ".", group: tuple[str, str] | None = None
) -> str:
    """Return a uv.lock that locks the package at the version, for the project at the source the lock names.

    The project's entry lists the package among its dependencies, or under the table and the group given.
    """
    project = f'[[package]]\nname = "project"\nversion = "0"\nsource = {{ virtual = "{project_source}" }}\n'
    dependency = f'[{{ name = "{package}" }}]'
    listed = f"dependencies = {dependency}" if group is None else f"[package.{group[0]}]\n{group[1]} = {dependency}"
    return f"version = 1\n\n{project}{listed}\n\n{_locked(package, version)}"


def _forked_uv_lock(older: str, newer: str) -> str:
    """Return a uv.lock that locks `package` for the project at the older version below Python 3.12, and at the newer.

    It also locks `package` at 1.0 for `other`, a dependency of the project that needs that version.
    """
    project = '[[package]]\nname = "project"\nversion = "0"\nsource = { virtual = "." }\ndependencies = [\n'
    project += '    { name = "other" },\n'
    for version, marker in ((older, "python_full_version < '3.12'"), (newer, "python_full_version >= '3.12'")):
        project += f'    {{ name = "package", version = "{version}", marker = "{marker}" }},\n'
    other = '[[package]]\nname = "other"\nversion = "1"\ndependencies = [{ name = "package", version = "1.0" }]\n'
    packages = "\n".join(_locked("package", version) for version in ("1.0", older, newer))
    return f"version = 1\n\n{project}]\n\n{other}\n{packages}"


def _root_declaring(spec: str) -> str:
    """Return a workspace root that includes `member` and declares the dependency."""
    return f'[project]\nname = "root"\ndependencies = ["{spec}"]\n[tool.uv.workspace]\nmembers = ["member"]\n'


def _locked(package: str, version: str) -> str:
    """Return the entry of a uv.lock that locks the package at the version."""
    return f'[[package]]\nname = "{package}"\nversion = "{version}"\n'


def _failing_uv_lock(command: tuple[str, ...], **_options: object) -> object:
    """Fail `uv lock` with the error uv gives for pins that contradict each other, and let other commands answer."""
    if command[:2] == ("uv", "lock"):
        raise subprocess.CalledProcessError(cmd="", returncode=1, output="", stderr=_NO_SOLUTION)
    return DEFAULT


def _discovered_pyproject_toml(glob: Mock, spec: str) -> Mock:
    """Return the single mock pyproject.toml the scan discovers, pinning the given dependency."""
    pyproject_toml = mock_path(pyproject(spec), parent=SCAN_ROOT)
    glob.return_value = [pyproject_toml]
    return pyproject_toml


# Persisting the cooldown into config is exercised by the uv package manager's tests; stub it out here so these tests
# focus on the discovery/version-update flow (and don't try to write config to the mock pyproject.toml files). The
# checks over the settled pins are stubbed out for the same reason: each makes registry requests of its own, and
# what they report is `test_uv_pins.py`'s to pin, while being handed the files found is `CheckedPinsTest`'s below.
@patch("update_time.updaters.update_pyproject_toml.warn_about_pins", Mock())
@patch("update_time.package_managers.uv.configure_cooldown", Mock())
@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
@patch("requests.Session.get")
@patch("subprocess.run")
class UpdatePyprojectTomlsTest(LoggingTestCase):
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

    def create_pyproject_toml(self, contents: str) -> Mock:
        """Create a mock pyproject.toml file."""
        return mock_path(contents, parent=SCAN_ROOT)

    def mock_update_on_stdout(self, package: str, latest: str = "") -> Mock:
        """Mock stdout with optional package update."""
        update = f" (latest: {latest})" if latest else ""
        return Mock(stdout=f"| {package}{update}\n")

    def updated_pyproject_toml(  # noqa: PLR0913 — the patched mocks travel with the fixtures they are given
        self, run: Mock, get: Mock, glob: Mock, contents: str, *, package: str = "package", latest: str = "v1.1"
    ) -> Mock:
        """Update a mock pyproject.toml holding the contents, the only file the scan discovers, and return that mock.

        uv reports `latest` as the latest version of `package`, or no update at all when `latest` is empty. PyPI
        answers with metadata naming no changelog, so the log renders an available new version as `_NEW_VERSION`.
        """
        run.return_value = self.mock_update_on_stdout(package, latest)
        get.return_value = mock_response(pypi_metadata_without_changelog())
        pyproject_toml = self.create_pyproject_toml(contents)
        glob.return_value = [pyproject_toml]
        update_pyproject_tomls()
        return pyproject_toml

    def updated_lock(  # noqa: PLR0913 — the patched mocks travel with the fixtures they are given
        self,
        run: Mock,
        get: Mock,
        glob: Mock,
        files: Mapping[Path, str],
        lock_after: str | None,
        *,
        discovered: Sequence[Path] = (PYPROJECT_TOML,),
        package: str = "package",
        lock_after_tree: str | None = None,
    ) -> FakeFiles:
        """Update the pyproject.tomls among the files that the scan discovers, and return the files.

        uv lock writes the lock after to the uv.lock at the root, or fails for `_LOCK_FAILS`. uv tree writes the lock
        after the tree where one is given. uv writes past the write errors the files hold, since it is a process of its
        own. uv tree reports v1.2 as the latest version of the package, and PyPI does not name a changelog for it.
        """
        fake_files = files if isinstance(files, FakeFiles) else FakeFiles(files)
        written: dict[tuple[str, ...], str | None] = {("uv", "lock"): lock_after, ("uv", "tree"): lock_after_tree}

        def uv(command: tuple[str, ...], **_options: object) -> object:
            if (lock := written.get(command[:2])) is not None:
                fake_files[_UV_LOCK] = lock
            return DEFAULT if lock_after is not None else _failing_uv_lock(command)

        run.side_effect = uv
        run.return_value = self.mock_update_on_stdout(package, "v1.2")
        get.return_value = mock_response(pypi_metadata_without_changelog())
        glob.return_value = list(discovered)
        with fake_files.patched():
            update_pyproject_tomls()
        return fake_files

    def updated_marked_pin(
        self, run: Mock, get: Mock, glob: Mock, marker: str, latest: str = "v1.1"
    ) -> tuple[Mock, Location]:
        """Update a pyproject.toml whose only pin carries the marker, and return the file and the pin's location."""
        contents = pyproject("package==1.0", marker=marker)
        pyproject_toml = self.updated_pyproject_toml(run, get, glob, contents, latest=latest)
        return pyproject_toml, Location(pyproject_toml, 2)

    def assert_no_cli_cooldown(self, run: Mock) -> None:
        """Assert uv tree/lock carry no `--exclude-newer` flag (the cooldown lives in config), nor `--frozen`."""
        commands = [call.args[0] for call in run.call_args_list]
        uv_tree = next(command for command in commands if command[:2] == ("uv", "tree"))
        uv_lock = next(command for command in commands if command[:2] == ("uv", "lock"))
        self.assertNotIn("--frozen", uv_tree)  # --frozen would make uv tree --outdated ignore the cooldown
        for command in (uv_tree, uv_lock):
            with self.subTest(command=command[:2]):
                self.assertNotIn("--exclude-newer", command)

    def test_update(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml, with Update-time's cooldown passed to uv."""
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, pyproject("package==1.0"))
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged("package", _NEW_VERSION, Location(mock_pyproject_toml, 2))
        self.assert_no_cli_cooldown(run)
        self.assert_no_warnings_logged()

    def test_update_of_a_name_pinned_twice(self, run: Mock, get: Mock, glob: Mock):
        """Test that a name pinned in two arrays has its new version logged at each line pinning it."""
        contents = '[project]\ndependencies = ["package==1.0"]\n[dependency-groups]\ndev = ["package==1.0"]\n'
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, contents)
        self.assert_new_version_logged_among_others("package", _NEW_VERSION, Location(mock_pyproject_toml, 2), ANY)
        self.assert_new_version_logged_among_others("package", _NEW_VERSION, Location(mock_pyproject_toml, 4), ANY)

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
                left = self.updated_lock(run, get, glob, files, _uv_lock("1.1", group=group))
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
                left = self.updated_lock(run, get, glob, files, _uv_lock("1.2", reported), package=reported)
                self.assertEqual(left[PYPROJECT_TOML], contents.replace("==1.0", "==1.2"))
                self.assert_new_version_logged_among_others(reported, version, Location(PYPROJECT_TOML, 2), ANY)
                self.assert_new_version_logged_among_others(reported, version, Location(PYPROJECT_TOML, 4), ANY)
                self.assertEqual(len(self.new_version_records()), 2)

    @kills(
        Mutation(
            uv_lock.UvLock.locked_versions,
            'next(entries, lock.get("manifest", {}))',
            "next(entries, {})",
            "the dependency groups of a workspace root without `[project]` go unreported",
        ),
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
        lock_before, lock_after = (f"version = 1\n\n{manifest}{member}{_locked('package', v)}" for v in ("1.0", "1.1"))
        self.updated_lock(run, get, glob, {PYPROJECT_TOML: root, _UV_LOCK: lock_before}, lock_after)
        self.assert_new_version_logged("package", _NEW_VERSION, Location(PYPROJECT_TOML, 5))

    @kills(
        Mutation(
            uv_lock.UvLock.locked_versions,
            "if name not in versions or PEP_440.is_downgrade(versions[name], version):",
            "if True:",
            "a dependency locked at several versions is reported at the last the lock lists",
        ),
        Mutation(
            uv_lock.UvLock.locked_versions,
            'dependency.get("version", locked.get(name))',
            "locked.get(name)",
            "a dependency locked at several versions is reported at one the project may not use",
        ),
    )
    def test_a_dependency_locked_at_several_versions_is_reported_at_the_lowest_the_project_uses(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a dependency uv locks per Python version is reported at the lowest version the project uses."""
        files = {PYPROJECT_TOML: pyproject("package>=1.0"), _UV_LOCK: _forked_uv_lock("1.1", "1.4")}
        self.updated_lock(run, get, glob, files, _forked_uv_lock("1.2", "1.5"))
        version = published_version("1.2", UPLOAD_TIME_WITHOUT_CHANGELOG)
        self.assert_new_version_logged("package", version, Location(PYPROJECT_TOML, 2))

    @kills(
        Mutation(
            uv.Workspace.report_moved_versions,
            "pypi_served(pyproject_toml.declarations())",
            "pyproject_toml.declarations()",
            "a dependency from a git URL has its locked version reported, and its changelog asked of PyPI",
        )
    )
    def test_a_dependency_pypi_does_not_serve_is_not_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that a dependency PyPI does not serve, such as one from a git URL, is not reported."""
        contents = pyproject("other @ git+https://github.com/org/other", "package>=1.0")
        both = '[{ name = "other" }, { name = "package" }]'
        other = '[[package]]\nname = "other"\nversion = "{}"\nsource = {{ git = "https://github.com/org/other" }}\n'
        locks = [
            _uv_lock(package).replace('[{ name = "package" }]', both) + "\n" + other.format(other_version)
            for other_version, package in (("0.1", "1.0"), ("0.2", "1.1"))
        ]
        self.updated_lock(run, get, glob, {PYPROJECT_TOML: contents, _UV_LOCK: locks[0]}, locks[1])
        self.assert_new_version_logged("package", _NEW_VERSION, Location(PYPROJECT_TOML, 2))

    @kills(
        Mutation(
            uv.Workspace.report_moved_versions,
            "inherited = _workspace_sources(pyproject_toml.path)",
            "inherited = set()",
            "the run reports a sibling member the workspace root sources, and asks PyPI for its changelog",
        )
    )
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
        self.updated_lock(run, get, glob, files, locks[1], discovered=[_MEMBER])
        self.assert_new_version_logged("package", _NEW_VERSION, Location(_MEMBER, 2))

    def test_a_dependency_the_lock_records_no_version_for_is_passed_over(self, run: Mock, get: Mock, glob: Mock):
        """Test that a newly locked dependency without a version, such as a dynamic sibling member, is not reported."""
        root = _ROOT_SOURCING_OTHER
        other = '[[package]]\nname = "other"\nsource = { editable = "other" }\n'
        lock_before = _uv_lock("1.0", project_source="member")
        lock_after = _uv_lock("1.1", project_source="member").replace(
            '[{ name = "package" }]', '[{ name = "other" }, { name = "package" }]'
        )
        files = {PYPROJECT_TOML: root, _MEMBER: pyproject("other", "package>=1.0"), _UV_LOCK: lock_before}
        self.updated_lock(run, get, glob, files, f"{lock_after}\n{other}", discovered=[_MEMBER])
        self.assert_new_version_logged("package", _NEW_VERSION, Location(_MEMBER, 2))

    @kills(
        Mutation(
            uv.Workspace.report_moved_versions,
            "self.locked_before[pyproject_toml]",
            "self._locked_versions(pyproject_toml)",
            "the lock is read after uv tree locked again, so a version the cooldown moved back goes unreported",
        )
    )
    def test_a_version_uv_tree_moves_back_before_uv_lock_runs_is_reported_as_moved_back(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a version uv tree locks back for the cooldown is reported as moved back, without a changelog."""
        files = {PYPROJECT_TOML: pyproject("package>=1.0"), _UV_LOCK: _uv_lock("1.1")}
        self.updated_lock(run, get, glob, files, _uv_lock("1.0"), lock_after_tree=_uv_lock("1.0"))
        get.assert_not_called()
        self.assert_moved_back_logged("uv", "package", "1.1", "1.0", Location(PYPROJECT_TOML, 2))

    @kills(
        Mutation(
            update_pyproject_tomls,
            "continue",
            "pass",
            "a failed uv lock is followed by reports of versions the lock did not move",
        )
    )
    def test_a_failed_uv_lock_reads_no_moved_versions_from_the_lock(self, run: Mock, get: Mock, glob: Mock):
        """Test that a failed uv lock reads the locked versions before the run alone, and reports none as moved."""
        files = {PYPROJECT_TOML: pyproject("package>=1.0"), _UV_LOCK: _uv_lock("1.1")}
        read = uv_lock.UvLock.locked_versions
        with patch.object(uv_lock.UvLock, "locked_versions", autospec=True, side_effect=read) as locked_versions:
            self.updated_lock(run, get, glob, files, _LOCK_FAILS, lock_after_tree=_uv_lock("1.0"))
        self.assert_uv_dependencies_not_updated_logged(PYPROJECT_TOML, "uv lock")
        self.assertEqual(locked_versions.call_count, 1)
        self.assertEqual(self.records_of(message_of(Logger.version_moved_back)), [])

    @kills(
        Mutation(
            uv.Workspace.restore,
            "except OSError as error:",
            "except ValueError as error:",
            "a uv.lock that cannot be written back stops the run before the pyproject.tomls get their text back",
            raises="PermissionError: [Errno 13] Permission denied: '/uv.lock'",
        )
    )
    def test_a_uv_lock_that_cannot_be_written_back_is_reported_and_the_pyproject_toml_is_written_back(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a failed uv lock reports a uv.lock it cannot write back, and gives the pyproject.toml its text."""
        contents = pyproject("package==1.0")
        files = FakeFiles({PYPROJECT_TOML: contents, _UV_LOCK: _uv_lock("1.0")})
        error = PermissionError(13, "Permission denied", str(_UV_LOCK))
        files.write_errors[_UV_LOCK] = error
        left = self.updated_lock(run, get, glob, files, _LOCK_FAILS, lock_after_tree=_uv_lock("1.1"))
        self.assertEqual(left[PYPROJECT_TOML], contents)
        self.assert_errors_logged(
            (message_of(Logger.uv_dependencies_not_updated), {"location": Location(PYPROJECT_TOML), "tool": "uv lock"}),
            (message_of(Logger.file_not_restored), {"location": Location(_UV_LOCK), "error": str(error)}),
        )

    @kills(
        Mutation(
            update_pyproject_tomls,
            f"{_COOLDOWN}\n    {_SNAPSHOT}",
            f"{_SNAPSHOT}\n    {_COOLDOWN}",
            "a failed uv lock also takes back the cooldown the run wrote, which uv needs to lock reproducibly",
        )
    )
    def test_a_failed_uv_lock_keeps_the_cooldown_the_run_wrote(self, run: Mock, get: Mock, glob: Mock):
        """Test that a failed uv lock gives the pyproject.toml back its text with the cooldown the run wrote into it."""
        contents = pyproject("package==1.0")
        cooldown = '[tool.uv]\nexclude-newer = "7 days"\n'

        def configure_cooldown(pyproject_tomls: list[PyprojectToml]) -> None:
            for pyproject_toml in pyproject_tomls:
                pyproject_toml.write(pyproject_toml.read() + cooldown)

        with patch("update_time.package_managers.uv.configure_cooldown", side_effect=configure_cooldown):
            left = self.updated_lock(run, get, glob, {PYPROJECT_TOML: contents}, _LOCK_FAILS)
        self.assert_uv_dependencies_not_updated_logged(PYPROJECT_TOML, "uv lock")
        self.assertEqual(left[PYPROJECT_TOML], contents + cooldown)

    @kills(
        Mutation(
            uv_lock.UvLock.locked_versions,
            "return None",
            "return []",
            "a missing uv.lock reads as one locking nothing, so every locked version is reported as new",
        )
    )
    def test_without_an_earlier_lock_only_the_exact_pin_of_a_dependency_is_reported(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a run without an earlier uv.lock reports no new version for a dependency without an exact pin."""
        pyproject_toml_contents = (
            '[project]\nname = "a"\ndependencies = ["package>=1.0"]\n[dependency-groups]\ndev = ["package==1.0"]\n'
        )
        self.updated_lock(run, get, glob, {PYPROJECT_TOML: pyproject_toml_contents}, _uv_lock("1.2"))
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
        self.updated_lock(run, get, glob, files, _uv_lock("1.1", project_source="member"), discovered=[_MEMBER])
        self.assert_new_version_logged("package", _NEW_VERSION, Location(_MEMBER, 2))
        self.assert_path_logged(_UV_LOCK)

    def test_uv_lock_runs_once_per_workspace(self, run: Mock, get: Mock, glob: Mock):
        """Test that uv lock runs once per workspace, at its root, however many of its projects declare dependencies."""
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
                self.updated_lock(run, get, glob, files, _uv_lock("1.0"), discovered=list(files))
                uv_locks = [call.args[0] for call in run.call_args_list if call.args[0][:2] == ("uv", "lock")]
                directories = [command[command.index("--directory") + 1] for command in uv_locks]
                self.assertEqual(directories, [str(root) for root in roots])
                logged = [
                    self.records_of(message_of(Logger.path), location=Location(root / "uv.lock")) for root in roots
                ]
                self.assertEqual([len(records) for records in logged], [1] * len(roots))
                get.assert_not_called()

    def test_a_marker_on_a_pin_uv_reports_no_update_for_is_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that a marker is reported as recognised and as holding the update back, with no update available."""
        mock_pyproject_toml, location = self.updated_marked_pin(run, get, glob, "ignore[update]", latest="")
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_recognised_marker_logged("package", location, Marker(ignored_scopes=Scope.UPDATE))
        self.assert_ignored_logged("package", location, "ignore[update]", among_others=True)

    def test_no_update_is_reported_as_held_back_for_a_dependency_that_pins_no_version(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that an `ignore[update]` on a dependency that pins no version is not reported as holding it back."""
        contents = pyproject("package>=1.0", marker="ignore[update]")
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, contents)
        marker = Marker(ignored_scopes=Scope.UPDATE)
        self.assert_recognised_marker_logged("package", Location(mock_pyproject_toml, 2), marker)
        self.assert_none_logged(message_of(Logger.ignored), "update held back by a marker")

    def test_a_marker_holds_the_update_back(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pin whose marker holds its update back keeps its version, and is reported as held back."""
        mock_pyproject_toml, location = self.updated_marked_pin(run, get, glob, "ignore[update]")
        mock_pyproject_toml.write_text.assert_not_called()
        get.assert_not_called()  # a pin nothing updates costs no request for its changelog
        self.assert_no_new_version_logged()
        self.assert_recognised_marker_logged("package", location, Marker(ignored_scopes=Scope.UPDATE))
        self.assert_ignored_logged("package", location, "ignore[update]", among_others=True)

    @kills(
        Mutation(
            uv._update_dependencies,
            "declarations = by_package(report_file_markers(file, log))\n"
            "    if (outdated := _outdated(uv_tree, file, log)) is None:\n"
            "        return False\n",
            "if (outdated := _outdated(uv_tree, file, log)) is None:\n"
            "        return False\n"
            "    declarations = by_package(report_file_markers(file, log))\n",
            "a failed uv tree skips the marker report, so a marker on a pin reads as unrecognised",
        )
    )
    def test_a_marker_is_reported_when_uv_tree_fails(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pin's marker is reported as recognised, though uv tree fails."""
        run.side_effect = subprocess.CalledProcessError(cmd="", returncode=2, output="", stderr="offline")
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0", marker="ignore[update]"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        get.assert_not_called()
        marker = Marker(ignored_scopes=Scope.UPDATE)
        self.assert_recognised_marker_logged("package", Location(mock_pyproject_toml, 2), marker)
        self.assert_uv_dependencies_not_updated_logged(mock_pyproject_toml)

    def test_a_marker_holds_back_one_declaration_of_a_name_while_the_other_updates(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a name declared twice is held back on the line its marker steers, and updated on the other."""
        held_back = '[project]\ndependencies = ["package==1.0"]  # update-time: ignore[update]\n[dependency-groups]\n'
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, f'{held_back}dev = ["package==1.0"]\n')
        mock_pyproject_toml.write_text.assert_called_once_with(f'{held_back}dev = ["package==1.1"]\n')
        self.assert_new_version_logged("package", _NEW_VERSION, Location(mock_pyproject_toml, 4))
        self.assert_ignored_logged("package", Location(mock_pyproject_toml, 2), "ignore[update]", among_others=True)

    def test_an_invalid_bracket_item_leaves_the_pin_unchanged(self, run: Mock, get: Mock, glob: Mock):
        """Test that an item the marker language cannot read warns and keeps the pin, without reporting the marker."""
        for directive, item in {"ignore[stlae]": "stlae", "ignore[update": "[update"}.items():
            with self.subTest(directive=directive):
                mock_pyproject_toml, location = self.updated_marked_pin(run, get, glob, directive)
                mock_pyproject_toml.write_text.assert_not_called()
                get.assert_not_called()
                self.assert_no_new_version_logged()
                self.assert_invalid_bracket_item_logged("package", location, item)
                # The item is reported as invalid, so the marker is neither echoed back nor obeyed as a hold-back.
                self.assert_none_logged(message_of(Logger.recognised_marker), "recognised marker")
                self.assert_none_logged(message_of(Logger.ignored), "update held back by a marker")

    @kills(
        Mutation(
            uv._update_dependencies,
            "if declaration.updatable",
            "if declaration.updatable and not any(one.marker.invalid_item for one in declared)",
            "an unreadable item holds back every declaration of its name rather than the one carrying it",
        )
    )
    def test_an_invalid_item_holds_back_one_declaration_of_a_name_while_the_other_updates(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a name declared twice is held back where its item is unreadable, and updated on the other line."""
        held_back = '[project]\ndependencies = ["package==1.0"]  # update-time: ignore[stlae]\n[dependency-groups]\n'
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, f'{held_back}dev = ["package==1.0"]\n')
        mock_pyproject_toml.write_text.assert_called_once_with(f'{held_back}dev = ["package==1.1"]\n')
        self.assert_new_version_logged("package", _NEW_VERSION, Location(mock_pyproject_toml, 4))
        self.assert_invalid_bracket_item_logged("package", Location(mock_pyproject_toml, 2), "stlae")

    def test_update_with_changelog(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml with changelog."""
        run.return_value = self.mock_update_on_stdout("package_with_changelog", "v1.1")
        get.side_effect = [
            mock_response(self.pypi_metadata()),
            Mock(headers={"Content-Type": "text"}, text=CHANGELOG),
        ]
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_with_changelog==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_with_changelog==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            "package_with_changelog",
            published_version("1.1", UPLOAD_TIME_WITH_CHANGELOG),
            Location(mock_pyproject_toml, 2),
            changes=CHANGELOG,
        )
        self.assert_no_warnings_logged()

    def test_update_with_html_changelog(self, run: Mock, get: Mock, glob: Mock):
        """Test that updating a pyproject.toml with only a HTML changelog ignores the changelog."""
        run.return_value = self.mock_update_on_stdout("package_with_html_changelog", "v1.1")
        get.side_effect = [
            mock_response(self.pypi_metadata()),
            Mock(text=CHANGELOG, headers={"Content-Type": "text/html"}),
            mock_response([github_release_json("v1.1")]),
            mock_response([]),
        ]
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_with_html_changelog==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_with_html_changelog==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            "package_with_html_changelog",
            published_version("1.1", UPLOAD_TIME_WITH_CHANGELOG),
            Location(mock_pyproject_toml, 2),
        )
        self.assert_no_warnings_logged()

    def test_update_with_github_url(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml with GitHub releases."""
        run.return_value = self.mock_update_on_stdout("package_with_github_releases", "v1.1")
        get.side_effect = [
            mock_response(self.pypi_metadata(changelog_url="")),
            mock_response([github_release_json("v1.1", body=CHANGELOG)]),
            mock_response(github_commits_json()),
        ]
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_with_github_releases==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_with_github_releases==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            "package_with_github_releases",
            published_version("1.1", UPLOAD_TIME_WITH_CHANGELOG),
            Location(mock_pyproject_toml, 2),
            changes=CHANGELOG,
        )
        self.assert_no_warnings_logged()

    def test_update_without_github_url(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml without a GitHub URL."""
        run.return_value = self.mock_update_on_stdout("package_without_github_releases", "v1.1")
        get.return_value = mock_response(self.pypi_metadata(changelog_url="", repository="https://gitlab.com/org/repo"))
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_without_github_releases==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_without_github_releases==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            "package_without_github_releases",
            published_version("1.1", UPLOAD_TIME_WITH_CHANGELOG),
            Location(mock_pyproject_toml, 2),
        )
        self.assert_no_warnings_logged()

    def test_unchanged(self, run: Mock, get: Mock, glob: Mock):
        """Test that the pyproject.toml is not written if there are no changes."""
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, pyproject("package==1.0"), latest="")
        mock_pyproject_toml.write_text.assert_not_called()
        get.assert_not_called()
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_a_failed_uv_tree_skips_uv_lock_and_reports_the_dependencies_not_updated(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a failed uv tree (e.g. offline) skips the futile uv lock, and is reported at error."""
        run.side_effect = subprocess.CalledProcessError(cmd="", returncode=2, output="", stderr="error: offline")
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        commands = [call.args[0][:2] for call in run.call_args_list]
        self.assertIn(("uv", "tree"), commands)
        self.assertNotIn(("uv", "lock"), commands)  # uv lock is skipped because uv tree failed
        mock_pyproject_toml.write_text.assert_not_called()
        get.assert_not_called()
        self.assert_command_stderr_logged(stderr="error: offline")  # only uv tree's stderr is surfaced
        self.assert_uv_dependencies_not_updated_logged(mock_pyproject_toml)

    @kills(
        Mutation(
            python_dependency_toml_manifest.PythonDependencyToml.restore,
            "self.read() != contents",
            "True",
            "a failed uv lock writes back a file the run did not rewrite",
        )
    )
    def test_a_failed_uv_lock_is_reported_at_error_for_a_file_the_run_did_not_rewrite(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a failed uv lock is reported at error, also for a file without a pin the run rewrote."""
        run.side_effect = _failing_uv_lock
        run.return_value = self.mock_update_on_stdout("package")
        mock_pyproject_toml = _discovered_pyproject_toml(glob, "package>=1.0")
        update_pyproject_tomls()
        mock_pyproject_toml.write_text.assert_not_called()
        get.assert_not_called()
        self.assert_command_stderr_logged(stderr=_NO_SOLUTION)
        self.assert_uv_dependencies_not_updated_logged(mock_pyproject_toml, "uv lock")

    @kills(
        Mutation(
            uv_lock.UvLock.restore,
            "self.path.unlink(missing_ok=True)",
            "pass",
            "a failed uv lock leaves a uv.lock behind where the project had none",
        )
    )
    def test_a_failed_uv_lock_writes_the_files_the_run_rewrote_back(self, run: Mock, get: Mock, glob: Mock):
        """Test that a failed uv lock leaves the workspace's pyproject.tomls and uv.lock as they were before the run."""
        pyproject_tomls = {PYPROJECT_TOML: _root_declaring("package==1.0"), _MEMBER: pyproject("package==1.0")}
        message = message_of(Logger.uv_dependencies_not_updated)
        for case, files in {
            "an earlier uv.lock": {**pyproject_tomls, _UV_LOCK: _uv_lock("1.0")},
            "no earlier uv.lock": pyproject_tomls,
        }.items():
            with self.subTest(case):
                discovered = list(pyproject_tomls)
                left = self.updated_lock(
                    run, get, glob, files, _LOCK_FAILS, discovered=discovered, lock_after_tree=_uv_lock("1.1")
                )
                self.assertEqual(dict(left), files)
                self.assert_errors_logged(
                    (message, {"location": Location(PYPROJECT_TOML), "tool": "uv lock"}),
                    (message, {"location": Location(_MEMBER), "tool": "uv lock"}),
                )

    @kills(
        Mutation(
            uv._outdated,
            "log.uv_not_on_path(file.path)",
            "log.uv_dependencies_not_updated(file.path, uv_tree.name)",
            "a uv missing from the path is reported as a uv tree that failed",
        )
    )
    @patch("shutil.which", Mock(return_value=None))
    def test_a_uv_missing_from_the_path_is_reported_in_one_line(self, run: Mock, get: Mock, glob: Mock):
        """Test that a missing uv is reported in one line naming the file, and that Update-time does not run it."""
        run.side_effect = FileNotFoundError("uv")
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        run.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        get.assert_not_called()
        self.assert_error_logged(message_of(Logger.uv_not_on_path), location=Location(mock_pyproject_toml))

    def test_skip_non_uv_tool_section(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pyproject.toml with a non-uv tool section (e.g. [tool.poetry]) is skipped without running uv."""
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0") + '\n[tool.poetry]\nname = "x"\n')
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        run.assert_not_called()
        get.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_unsupported_package_manager_logged(mock_pyproject_toml, "poetry", "uv")
        self.assert_no_new_version_logged()

    @patch_pathlib_path(exists=True)
    def test_skip_non_uv_lockfile(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pyproject.toml beside a non-uv lockfile (e.g. poetry.lock) is reported as unsupported."""
        mock_pyproject_toml = self.create_pyproject_toml(_BUILD_REQUIREMENTS_ALONE)
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        run.assert_not_called()
        get.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_unsupported_package_manager_logged(mock_pyproject_toml, "poetry", "uv")
        self.assert_no_new_version_logged()

    def test_skip_invalid_pyproject_toml(self, run: Mock, get: Mock, glob: Mock):
        """Test that an unparsable pyproject.toml is skipped with a warning, without running uv or crashing."""
        not_utf_8 = self.create_pyproject_toml("")
        not_utf_8.read_text.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        for case, mock_pyproject_toml in (
            ("a missing bracket", self.create_pyproject_toml('[project]\ndependencies = ["package==1.0"\n')),
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
                mock_pyproject_toml = self.create_pyproject_toml(contents)
                glob.return_value = [mock_pyproject_toml]
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
                mock_pyproject_toml = self.create_pyproject_toml(contents)
                glob.return_value = [mock_pyproject_toml]
                with patch("update_time.package_managers.uv.configure_cooldown") as configure_cooldown:
                    update_pyproject_tomls()
                run.assert_not_called()
                get.assert_not_called()
                configure_cooldown.assert_called_once_with([])
                mock_pyproject_toml.write_text.assert_not_called()
                self.assert_no_dependencies_logged(mock_pyproject_toml, "uv", "resolve")

    def test_a_marker_in_a_file_uv_does_not_run_for_is_recognised(self, run: Mock, get: Mock, glob: Mock):
        """Test that the marker on a build requirement of a pyproject.toml that uv does not run for is reported."""
        contents = '[build-system]\nrequires = ["hatchling==1.20.0"]  # update-time: set[stale-after=0]\n'
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, contents, latest="")
        marker = Marker(stale=Threshold(value=0))
        self.assert_recognised_marker_logged("hatchling", Location(mock_pyproject_toml, 2), marker)

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
                self.updated_pyproject_toml(run, get, glob, contents, latest="")
                self.assertIn(("uv", "tree"), [call.args[0][:2] for call in run.call_args_list])


@patch("update_time.package_managers.uv.configure_cooldown", Mock())
@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
@patch("requests.Session.get")
@patch("subprocess.run")
class CheckedPinsTest(LoggingTestCase):
    """Unit test for handing the discovered manifests to the checks uv-delegated updaters share."""

    @kills(
        Mutation(
            update_pyproject_tomls,
            "warn_about_pins(uv_projects, _LOG)",
            "warn_about_pins(projects_with_dependencies, _LOG)",
            "a pyproject.toml that uv does not run for is not checked either",
        )
    )
    @patch("update_time.updaters.update_pyproject_toml.warn_about_pins")
    def test_the_discovered_manifests_are_checked(self, warn: Mock, run: Mock, get: Mock, glob: Mock):
        """Test that the checks are handed every manifest the scan found, whether uv ran for it or not."""
        run.return_value = Mock(stdout="| django\n")
        pyproject_toml = _discovered_pyproject_toml(glob, "django==3.2.0")
        build_requirements_alone = mock_path(_BUILD_REQUIREMENTS_ALONE, parent=SCAN_ROOT)
        glob.return_value.append(build_requirements_alone)
        update_pyproject_tomls()
        get.assert_not_called()
        warn.assert_called_once_with([PyprojectToml(pyproject_toml), PyprojectToml(build_requirements_alone)], ANY)
