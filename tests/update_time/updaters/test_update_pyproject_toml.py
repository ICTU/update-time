"""Unit tests for the pyproject.toml update script."""

import os
import subprocess  # nosec
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import ANY, Mock, call, patch

from update_time.domain.cooldown import COOLDOWN_DAYS
from update_time.updaters.update_pyproject_toml import (
    EXCLUDE_NEWER_COMMENT,
    _persist_exclude_newer,
    _workspace_root,
    _workspace_table,
    configure_cooldown,
    update_pyproject_tomls,
)

from tests.update_time.assertions import assert_success
from tests.update_time.helpers import LoggingTestCase, mock_path, mock_response, release_json

if TYPE_CHECKING:
    from update_time.sources.pypi import Release


def pyproject(spec: str) -> str:
    """Return a minimal valid pyproject.toml pinning the given dependency."""
    return f'[project]\ndependencies = ["{spec}"]\n'


# Persisting the cooldown into config is exercised by ConfigureCooldownTest; stub it out here so these tests focus on
# the version-update flow (and don't try to write config to the mock pyproject.toml files).
@patch("update_time.updaters.update_pyproject_toml.configure_cooldown", Mock())
@patch("pathlib.Path.cwd", Mock(return_value=Path("/")))
@patch("pathlib.Path.rglob")
@patch("requests.get")
@patch("subprocess.run")
class UpdatePyprojectTomlsTest(LoggingTestCase):
    """Unit tests for the update pyproject.tomls function."""

    changelog: ClassVar = "Changelog"

    @staticmethod
    def pypi_metadata(
        changelog_url: str = "https://changelog",
        repository: str = "https://github.com/repo/package_with_github_releases",
    ) -> Release:
        """Create PyPI release metadata fixture."""
        project_urls = {"Homepage": "https://home", "repository": repository}
        if changelog_url:
            project_urls["Changelog"] = changelog_url
        return {
            "info": {"description": "Package description", "project_urls": project_urls},
            "urls": [{"upload_time_iso_8601": "2026-05-30T12:07:03.123456Z"}],
        }

    def create_pyproject_toml(self, contents: str) -> Mock:
        """Create a mock pyproject.toml file."""
        return mock_path(contents, parent=Path("/"))

    def mock_update_on_stdout(self, package: str, latest: str = "") -> Mock:
        """Mock stdout with optional package update."""
        update = f" (latest: {latest})" if latest else ""
        return Mock(stdout=f"| {package}{update}\n")

    def assert_no_cli_cooldown(self, run: Mock) -> None:
        """Assert uv tree/lock carry no `--exclude-newer` flag (the cooldown lives in config now), nor `--frozen`."""
        commands = [call.args[0] for call in run.call_args_list]
        uv_tree = next(command for command in commands if command[:2] == ["uv", "tree"])
        uv_lock = next(command for command in commands if command[:2] == ["uv", "lock"])
        self.assertNotIn("--frozen", uv_tree)  # --frozen would make uv tree --outdated ignore the cooldown
        for command in (uv_tree, uv_lock):
            self.assertNotIn("--exclude-newer", command)

    def test_update(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml, with Update-time's cooldown passed to uv."""
        run.return_value = self.mock_update_on_stdout("package", "v1.1")
        get.return_value = mock_response(
            {"info": {"description": "Package"}, "urls": [{"upload_time_iso_8601": "2026-05-30T12:08:53.123321Z"}]}
        )
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0"))
        glob.return_value = [mock_pyproject_toml]
        assert_success(update_pyproject_tomls())
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(mock_pyproject_toml, "package", "1.1, published: 2026-05-30 12:08")
        self.assert_no_cli_cooldown(run)
        self.assert_no_warnings_logged()

    def test_update_with_changelog(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml with changelog."""
        run.return_value = self.mock_update_on_stdout("package_with_changelog", "v1.1")
        get.side_effect = [
            mock_response(self.pypi_metadata()),
            Mock(headers={"Content-Type": "text"}, text=self.changelog),
        ]
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_with_changelog==1.0"))
        glob.return_value = [mock_pyproject_toml]
        assert_success(update_pyproject_tomls())
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_with_changelog==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            mock_pyproject_toml,
            "package_with_changelog",
            "1.1, published: 2026-05-30 12:07",
            self.changelog,
        )
        self.assert_no_warnings_logged()

    def test_update_with_html_changelog(self, run: Mock, get: Mock, glob: Mock):
        """Test that updating a pyproject.toml with only a HTML changelog ignores the changelog."""
        run.return_value = self.mock_update_on_stdout("package_with_html_changelog", "v1.1")
        get.side_effect = [
            mock_response(self.pypi_metadata()),
            Mock(text=self.changelog, headers={"Content-Type": "text/html"}),
            mock_response([{"tag_name": "v1.1"}]),
        ]
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_with_html_changelog==1.0"))
        glob.return_value = [mock_pyproject_toml]
        assert_success(update_pyproject_tomls())
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_with_html_changelog==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            mock_pyproject_toml, "package_with_html_changelog", "1.1, published: 2026-05-30 12:07"
        )
        self.assert_no_warnings_logged()

    def test_update_with_github_url(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml with GitHub releases."""
        run.return_value = self.mock_update_on_stdout("package_with_github_releases", "v1.1")
        get.side_effect = [
            mock_response(self.pypi_metadata(changelog_url="")),
            mock_response([release_json("v1.1", body=self.changelog)]),
            mock_response({"sha": "sha"}),
        ]
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_with_github_releases==1.0"))
        glob.return_value = [mock_pyproject_toml]
        assert_success(update_pyproject_tomls())
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_with_github_releases==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            mock_pyproject_toml,
            "package_with_github_releases",
            "1.1, published: 2026-05-30 12:07",
            self.changelog,
        )
        self.assert_no_warnings_logged()

    def test_update_without_github_url(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml without a GitHub URL."""
        run.return_value = self.mock_update_on_stdout("package_without_github_releases", "v1.1")
        get.return_value = mock_response(self.pypi_metadata(changelog_url="", repository="https://gitlab.com/org/repo"))
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_without_github_releases==1.0"))
        glob.return_value = [mock_pyproject_toml]
        assert_success(update_pyproject_tomls())
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_without_github_releases==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            mock_pyproject_toml,
            "package_without_github_releases",
            "1.1, published: 2026-05-30 12:07",
        )
        self.assert_no_warnings_logged()

    def test_unchanged(self, run: Mock, get: Mock, glob: Mock):
        """Test that the pyproject.toml is not written if there are no changes."""
        run.return_value = self.mock_update_on_stdout("package")
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0"))
        glob.return_value = [mock_pyproject_toml]
        assert_success(update_pyproject_tomls())
        mock_pyproject_toml.write_text.assert_not_called()
        get.assert_not_called()
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_uv_lock_skipped_when_uv_tree_fails(self, run: Mock, get: Mock, glob: Mock):
        """Test that a failed uv tree (e.g. offline) skips the futile uv lock, logging only uv tree's failure."""
        run.side_effect = subprocess.CalledProcessError(cmd="", returncode=2, output="", stderr="error: offline")
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0"))
        glob.return_value = [mock_pyproject_toml]
        assert_success(update_pyproject_tomls())
        commands = [call.args[0][:2] for call in run.call_args_list]
        self.assertIn(["uv", "tree"], commands)
        self.assertNotIn(["uv", "lock"], commands)  # uv lock is skipped because uv tree failed
        mock_pyproject_toml.write_text.assert_not_called()
        get.assert_not_called()
        self.mock_warning.assert_called_once()  # uv tree's stderr is surfaced

    def test_skip_non_uv_tool_section(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pyproject.toml with a non-uv tool section (e.g. [tool.poetry]) is skipped without running uv."""
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0") + '\n[tool.poetry]\nname = "x"\n')
        glob.return_value = [mock_pyproject_toml]
        assert_success(update_pyproject_tomls())
        run.assert_not_called()
        get.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_unsupported_package_manager_logged(mock_pyproject_toml, "poetry", "uv")
        self.assert_no_new_version_logged()

    @patch("pathlib.Path.exists", Mock(return_value=True))
    def test_skip_non_uv_lockfile(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pyproject.toml with a non-uv lockfile (e.g. poetry.lock) is skipped without running uv."""
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0"))
        glob.return_value = [mock_pyproject_toml]
        assert_success(update_pyproject_tomls())
        run.assert_not_called()
        get.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_unsupported_package_manager_logged(mock_pyproject_toml, "poetry", "uv")
        self.assert_no_new_version_logged()


def marked(cooldown: str) -> str:
    """Return a `[tool.uv] exclude-newer` block with the given cooldown, carrying Update-time's marker comment."""
    return f'\n[tool.uv]\nexclude-newer = "{cooldown}" # {EXCLUDE_NEWER_COMMENT}\n'


class PersistExcludeNewerTest(LoggingTestCase):
    """Unit tests for writing the cooldown into a single pyproject.toml, with the file I/O mocked."""

    def persist(self, contents: str) -> Mock:
        """Run `_persist_exclude_newer` on a mock pyproject.toml with the contents and return the mock."""
        pyproject_toml = mock_path(contents)
        _persist_exclude_newer(pyproject_toml)
        return pyproject_toml

    def test_writes_when_absent(self):
        """Test that the cooldown is written, with the marker comment, when the project configures none."""
        pyproject_toml = self.persist(pyproject("a==1.0"))
        written = pyproject_toml.write_text.call_args.args[0]
        self.assertIn(f'exclude-newer = "{COOLDOWN_DAYS} days"', written)
        self.assertIn(EXCLUDE_NEWER_COMMENT, written)
        message = "Set uv exclude-newer to %r in %s to apply the cooldown"
        self.mock_info.assert_called_once_with(message, f"{COOLDOWN_DAYS} days", ANY, stacklevel=ANY)

    def test_creates_tool_uv_section_when_absent(self):
        """Test that a `[tool.uv]` section is created when the pyproject.toml has none at all."""
        pyproject_toml = self.persist(pyproject("a==1.0"))
        self.assertIn("[tool.uv]", pyproject_toml.write_text.call_args.args[0])

    def test_preserves_other_tool_uv_keys(self):
        """Test that writing exclude-newer preserves an existing exclude-newer-package table."""
        pyproject_toml = self.persist(
            pyproject("a==1.0") + "\n[tool.uv]\nexclude-newer-package = { msgpack = false }\n"
        )
        written = pyproject_toml.write_text.call_args.args[0]
        self.assertIn("exclude-newer-package = { msgpack = false }", written)
        self.assertIn(f'exclude-newer = "{COOLDOWN_DAYS} days"', written)

    def test_leaves_a_user_value_untouched(self):
        """Test that an exclude-newer without the marker (the user's own) is left alone, writing and logging nothing."""
        pyproject_toml = self.persist(pyproject("a==1.0") + '\n[tool.uv]\nexclude-newer = "2024-01-01"\n')
        pyproject_toml.write_text.assert_not_called()
        self.mock_info.assert_not_called()

    def test_does_not_rewrite_when_already_current(self):
        """Test that a marked value already matching the cooldown is not rewritten (no spurious file churn)."""
        pyproject_toml = self.persist(pyproject("a==1.0") + marked(f"{COOLDOWN_DAYS} days"))
        pyproject_toml.write_text.assert_not_called()

    def test_syncs_own_value_to_the_cooldown(self):
        """Test that a previously Update-time-written value is rewritten when --cooldown changes."""
        with patch.dict(os.environ, {"_UPDATE_TIME_COOLDOWN_DAYS": "14"}):
            pyproject_toml = self.persist(pyproject("a==1.0") + marked("7 days"))
        self.assertIn('exclude-newer = "14 days"', pyproject_toml.write_text.call_args.args[0])


class ConfigureCooldownTest(unittest.TestCase):
    """Unit tests for orchestrating the cooldown persistence across a run's projects (workspace routing mocked)."""

    @patch("update_time.updaters.update_pyproject_toml._persist_exclude_newer")
    @patch("update_time.updaters.update_pyproject_toml._workspace_root")
    def test_writes_once_per_workspace_root(self, workspace_root: Mock, persist: Mock):
        """Test that members sharing a workspace root yield a single write to that root."""
        root = Path("/ws/pyproject.toml")
        workspace_root.return_value = root  # Every member resolves to the same root.
        members = [Path("/ws/packages/a/pyproject.toml"), Path("/ws/packages/b/pyproject.toml"), root]
        configure_cooldown(members)
        persist.assert_called_once_with(root)

    @patch("update_time.updaters.update_pyproject_toml._persist_exclude_newer")
    @patch("update_time.updaters.update_pyproject_toml._workspace_root")
    def test_persists_each_distinct_root(self, workspace_root: Mock, persist: Mock):
        """Test that standalone projects (each its own root) are all persisted."""
        workspace_root.side_effect = lambda pyproject_toml: pyproject_toml
        first, second = Path("/a/pyproject.toml"), Path("/b/pyproject.toml")
        configure_cooldown([first, second])
        self.assertEqual([call(first), call(second)], persist.call_args_list)

    @patch("update_time.updaters.update_pyproject_toml._persist_exclude_newer")
    @patch.dict(os.environ, {"UV_EXCLUDE_NEWER": "2024-01-01"})
    def test_environment_override_writes_nothing(self, persist: Mock):
        """Test that nothing is persisted when the user sets the UV_EXCLUDE_NEWER environment variable."""
        configure_cooldown([Path("/proj/pyproject.toml")])
        persist.assert_not_called()


class WorkspaceRootTest(unittest.TestCase):
    """Unit tests for resolving a project's uv workspace root, with the pyproject.toml reads mocked."""

    ROOT = Path("/ws/pyproject.toml")
    MEMBER = Path("/ws/packages/lib/pyproject.toml")

    def workspace_at_root(self, **workspace: object):
        """Patch `_workspace_table` so only the root declares the given `[tool.uv.workspace]` table."""
        return patch(
            "update_time.updaters.update_pyproject_toml._workspace_table",
            side_effect=lambda pyproject_toml: workspace if pyproject_toml == self.ROOT else None,
        )

    def test_member_resolves_to_root(self):
        """Test that a member is resolved to the workspace root that lists it."""
        with self.workspace_at_root(members=["packages/*"]):
            self.assertEqual(self.ROOT, _workspace_root(self.MEMBER))

    def test_root_is_its_own_member(self):
        """Test that the root project, which declares the workspace, resolves to itself."""
        with self.workspace_at_root(members=["packages/*"]):
            self.assertEqual(self.ROOT, _workspace_root(self.ROOT))

    def test_standalone_resolves_to_self(self):
        """Test that a project in no workspace resolves to itself."""
        with patch("update_time.updaters.update_pyproject_toml._workspace_table", return_value=None):
            self.assertEqual(self.MEMBER, _workspace_root(self.MEMBER))

    def test_excluded_member_resolves_to_self(self):
        """Test that a nested project excluded from the workspace is treated as a standalone root."""
        vendor = Path("/ws/packages/vendor/pyproject.toml")
        with self.workspace_at_root(members=["packages/*"], exclude=["packages/vendor"]):
            self.assertEqual(vendor, _workspace_root(vendor))

    def test_member_not_matching_a_glob_resolves_to_self(self):
        """Test that a nested project not matched by any members glob is treated as a standalone root."""
        outsider = Path("/ws/tools/lib/pyproject.toml")
        with self.workspace_at_root(members=["packages/*"]):
            self.assertEqual(outsider, _workspace_root(outsider))


class WorkspaceTableTest(unittest.TestCase):
    """Unit tests for reading the `[tool.uv.workspace]` table, with the pyproject.toml contents mocked."""

    def test_returns_the_workspace_table(self):
        """Test that a declared `[tool.uv.workspace]` table is returned."""
        contents = '[project]\nname = "root"\n\n[tool.uv.workspace]\nmembers = ["packages/*"]\n'
        self.assertEqual({"members": ["packages/*"]}, _workspace_table(mock_path(contents)))

    def test_no_workspace_table(self):
        """Test that a pyproject.toml without a workspace table yields None."""
        self.assertIsNone(_workspace_table(mock_path('[project]\nname = "solo"\n')))

    def test_unreadable_file(self):
        """Test that a missing/unreadable pyproject.toml (e.g. an ancestor with none) yields None, not an error."""
        self.assertIsNone(_workspace_table(Mock(read_text=Mock(side_effect=OSError))))

    def test_malformed_toml(self):
        """Test that an unrelated, malformed pyproject.toml up the tree yields None instead of crashing."""
        self.assertIsNone(_workspace_table(mock_path("this is not valid toml =")))
