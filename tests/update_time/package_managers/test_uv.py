"""Unit tests for the uv package manager's operations (detection is covered via the pyproject.toml updater)."""

import unittest
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

from update_time.domain.cooldown import COOLDOWN
from update_time.io.log import Logger
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.package_managers import uv
from update_time.package_managers.uv import (
    _persist_exclude_newer,
    _workspace_root,
    _workspace_table,
    configure_cooldown,
    pypi_served,
)
from update_time.primitives.command import Command

from tests.helpers import mock_path, patch_environ
from tests.mutation import Mutation, kills
from tests.update_time.helpers import declaration, message_of, pyproject
from tests.update_time.package_managers.helpers import (
    CONFIGURED_EXCLUDE_NEWER,
    MANAGED_BY_COMMENT,
    UNREADABLE_SETTINGS,
    UvSettingsTestCase,
)


def _managed(cooldown: str) -> str:
    """Return a `[tool.uv] exclude-newer` block with the given cooldown, carrying the managed-by comment."""
    return f'\n[tool.uv]\nexclude-newer = "{cooldown}" # {MANAGED_BY_COMMENT}\n'


class PersistExcludeNewerTest(UvSettingsTestCase):
    """Unit tests for writing the cooldown into a single pyproject.toml, with the file I/O mocked."""

    def persist(self, contents: str) -> Mock:
        """Run `_persist_exclude_newer` on a mock pyproject.toml with the contents and return the mock."""
        pyproject_toml = mock_path(contents)
        _persist_exclude_newer(pyproject_toml)
        return pyproject_toml

    def test_writes_when_absent(self):
        """Test that the cooldown is written, with the managed-by comment, when the project configures none."""
        pyproject_toml = self.persist(pyproject("a==1.0"))
        written = pyproject_toml.write_text.call_args.args[0]
        self.assertIn(f'exclude-newer = "{COOLDOWN.default} days"', written)
        self.assertIn(MANAGED_BY_COMMENT, written)
        self.assert_logged(message_of(Logger.configured_uv_cooldown), cooldown=f"{COOLDOWN.default} days", location=ANY)

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
        self.assertIn(f'exclude-newer = "{COOLDOWN.default} days"', written)

    @kills(
        Mutation(
            uv._persist_exclude_newer,
            "_LOG.uv_cooldown_not_set(pyproject_toml)",
            "pass",
            "a pyproject.toml that cannot hold the cooldown is passed over without a word",
        )
    )
    def test_warns_about_a_file_whose_tool_tables_cannot_hold_the_cooldown(self):
        """Test that a pyproject.toml whose `tool` is not a table is warned about, and neither written nor logged."""
        pyproject_toml = self.persist('tool = "x"\n' + pyproject("a==1.0"))
        pyproject_toml.write_text.assert_not_called()
        self.assert_logged(message_of(Logger.uv_cooldown_not_set), location=ANY)

    def test_leaves_a_user_value_untouched(self):
        """Test that an exclude-newer without the managed-by comment is left alone, writing and logging nothing."""
        pyproject_toml = self.persist(pyproject("a==1.0") + '\n[tool.uv]\nexclude-newer = "2024-01-01"\n')
        pyproject_toml.write_text.assert_not_called()
        self.assert_no_info_logged()

    @kills(
        Mutation(
            uv._persist_exclude_newer,
            "pyproject_toml.parent, pyproject_toml",
            "Path.cwd(), pyproject_toml",
            "uv reads the settings of the directory Update-time runs in rather than those of the project",
        )
    )
    def test_leaves_an_exclude_newer_that_uvs_settings_hold_elsewhere(self):
        """Test that nothing is written where uv's settings, in the file's directory, hold an `exclude-newer`."""
        self.configured_exclude_newer = CONFIGURED_EXCLUDE_NEWER
        pyproject_toml = self.persist(pyproject("a==1.0"))
        pyproject_toml.write_text.assert_not_called()
        self.assert_no_info_logged()
        settings = Command("uv", "lock", "--show-settings")
        self.mock_run.assert_called_once_with(
            settings, capture_output=True, text=True, check=True, cwd=pyproject_toml.parent
        )

    def test_writes_the_cooldown_where_uvs_settings_cannot_be_read(self):
        """Test that Update-time warns, and writes the cooldown, where uv does not say whether its settings hold one."""
        for case, answer in UNREADABLE_SETTINGS.items():
            with self.subTest(case):
                self.settings_answer = answer
                pyproject_toml = self.persist(pyproject("a==1.0"))
                self.assertIn(f'exclude-newer = "{COOLDOWN.default} days"', pyproject_toml.write_text.call_args.args[0])
                self.assert_logged_among_others(message_of(Logger.uv_settings_unreadable), location=ANY)

    def test_does_not_rewrite_when_already_current(self):
        """Test that Update-time's own value is not rewritten where it already matches the cooldown."""
        pyproject_toml = self.persist(pyproject("a==1.0") + _managed(f"{COOLDOWN.default} days"))
        pyproject_toml.write_text.assert_not_called()

    @patch_environ({COOLDOWN.name: "14"})
    def test_syncs_own_value_to_the_cooldown(self):
        """Test that Update-time writes its `--cooldown` value over its own, whatever uv's settings say."""
        self.configured_exclude_newer = CONFIGURED_EXCLUDE_NEWER
        pyproject_toml = self.persist(pyproject("a==1.0") + _managed("7 days"))
        self.assertIn('exclude-newer = "14 days"', pyproject_toml.write_text.call_args.args[0])


class ConfigureCooldownTest(unittest.TestCase):
    """Unit tests for orchestrating the cooldown persistence across a run's projects (workspace routing mocked)."""

    @patch("update_time.package_managers.uv._persist_exclude_newer")
    @patch("update_time.package_managers.uv._workspace_root")
    def test_writes_once_per_workspace_root(self, workspace_root: Mock, persist: Mock):
        """Test that members sharing a workspace root yield a single write to that root."""
        root = Path("/ws/pyproject.toml")
        workspace_root.return_value = root  # Every member resolves to the same root.
        members = [Path("/ws/packages/a/pyproject.toml"), Path("/ws/packages/b/pyproject.toml"), root]
        configure_cooldown(members)
        persist.assert_called_once_with(root)

    @patch("update_time.package_managers.uv._persist_exclude_newer")
    @patch("update_time.package_managers.uv._workspace_root")
    def test_persists_each_distinct_root(self, workspace_root: Mock, persist: Mock):
        """Test that standalone projects (each its own root) are all persisted."""
        workspace_root.side_effect = lambda pyproject_toml: pyproject_toml
        first, second = Path("/a/pyproject.toml"), Path("/b/pyproject.toml")
        configure_cooldown([first, second])
        self.assertEqual(persist.call_args_list, [call(first), call(second)])

    @patch("update_time.package_managers.uv._persist_exclude_newer")
    @patch_environ({"UV_EXCLUDE_NEWER": "2024-01-01"})
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
            "update_time.package_managers.uv._workspace_table",
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
        with patch("update_time.package_managers.uv._workspace_table", return_value=None):
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
        self.assertEqual(_workspace_table(mock_path(contents)), {"members": ["packages/*"]})

    def test_no_workspace_table(self):
        """Test that a pyproject.toml without a workspace table yields None."""
        self.assertIsNone(_workspace_table(mock_path('[project]\nname = "solo"\n')))

    def test_unreadable_file(self):
        """Test that a missing/unreadable pyproject.toml (e.g. an ancestor with none) yields None, not an error."""
        self.assertIsNone(_workspace_table(Mock(read_text=Mock(side_effect=OSError))))

    def test_malformed_toml(self):
        """Test that an unrelated, malformed pyproject.toml up the tree yields None instead of crashing."""
        self.assertIsNone(_workspace_table(mock_path("this is not valid toml =")))


class PypiServedDependenciesTest(unittest.TestCase):
    """Unit tests for reading the dependencies PyPI serves a release for."""

    def test_a_dependency_with_a_uv_source_is_skipped_whether_or_not_it_pins(self):
        """Test that a dependency uv resolves from a source of its own is left out, pinned or not."""
        contents = (
            '[project]\ndependencies = ["local==1.0", "loose", "other==2.0"]\n'
            '[tool.uv.sources]\nlocal = {path = "../local"}\nloose = {path = "../loose"}\n'
        )
        path = mock_path(contents)
        self.assertEqual(pypi_served(PyprojectToml(path).declarations()), [declaration("other", "2.0", path, 2, 3)])

    def test_a_direct_reference_is_skipped(self):
        """Test that a dependency given as a URL is left out, since it resolves to no release on PyPI."""
        path = mock_path('dependencies = ["pkg @ git+https://github.com/org/repo.git", "other>=2.0"]\n')
        self.assertEqual(pypi_served(PyprojectToml(path).declarations()), [declaration("other", "", path, 1, 2)])
