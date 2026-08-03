"""Unit tests for the uv package manager's operations (detection is covered via the pyproject.toml updater)."""

import unittest
from logging import INFO
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

from update_time.domain.cooldown import COOLDOWN
from update_time.io.log import Logger
from update_time.package_managers.uv import (
    EXCLUDE_NEWER_COMMENT,
    _persist_exclude_newer,
    _workspace_root,
    _workspace_table,
    configure_cooldown,
)

from tests.update_time.helpers import LoggingTestCase, mock_path, patch_environ, pyproject


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
        self.assertIn(f'exclude-newer = "{COOLDOWN.default} days"', written)
        self.assertIn(EXCLUDE_NEWER_COMMENT, written)
        self.assert_logged(Logger._MESSAGE_UV_COOLDOWN, cooldown=f"{COOLDOWN.default} days", location=ANY)

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

    def test_leaves_a_user_value_untouched(self):
        """Test that an exclude-newer without the marker (the user's own) is left alone, writing and logging nothing."""
        pyproject_toml = self.persist(pyproject("a==1.0") + '\n[tool.uv]\nexclude-newer = "2024-01-01"\n')
        pyproject_toml.write_text.assert_not_called()
        self.assertEqual(self.records(INFO), [])

    def test_does_not_rewrite_when_already_current(self):
        """Test that a marked value already matching the cooldown is not rewritten (no spurious file churn)."""
        pyproject_toml = self.persist(pyproject("a==1.0") + marked(f"{COOLDOWN.default} days"))
        pyproject_toml.write_text.assert_not_called()

    @patch_environ({COOLDOWN.name: "14"})
    def test_syncs_own_value_to_the_cooldown(self):
        """Test that a previously Update-time-written value is rewritten when --cooldown changes."""
        pyproject_toml = self.persist(pyproject("a==1.0") + marked("7 days"))
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
