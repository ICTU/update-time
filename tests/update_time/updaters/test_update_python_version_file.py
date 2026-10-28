"""Unit tests for the Python version file update script."""

from pathlib import Path
from typing import TYPE_CHECKING

from update_time.markers.directive import Reason
from update_time.primitives.location import Location
from update_time.updaters.update_python_version_file import update_python_version_files

from tests.helpers import mock_path, patch_pathlib_path
from tests.update_time.fixtures import DIGEST, PAST_COOLDOWN_DATE, STALE_DATE
from tests.update_time.helpers import LoggingTestCase, docker_hub_version, docker_tag
from tests.update_time.registry import RegistryRequestsMixin, mock_docker_registry
from tests.update_time.updaters.helpers import mock_docker_hub_auth

if TYPE_CHECKING:
    from datetime import datetime
    from unittest.mock import Mock


@mock_docker_hub_auth
@patch_pathlib_path("rglob", cwd=Path("/"))
class UpdatePythonVersionFilesTest(RegistryRequestsMixin, LoggingTestCase):
    """Unit tests for the update Python version files function."""

    def update_version_file(
        self,
        mock_glob: Mock,
        contents: str = "3.12.6\n",
        *,
        latest: tuple[str, ...] = ("3.13.2",),
        pushed: datetime = PAST_COOLDOWN_DATE,
    ) -> Mock:
        """Update the Python version in a version file holding these contents, and return the mock file."""
        self.requests.side_effect = mock_docker_registry(
            *(docker_tag(tag, DIGEST, tag_last_pushed=pushed) for tag in latest)
        )
        version_file = mock_path(contents, parent=Path("/"))
        mock_glob.return_value = [version_file]
        update_python_version_files()
        return version_file

    def test_unchanged(self, mock_glob: Mock):
        """Test that the version file is not written when it already pins the latest `python` release."""
        version_file = self.update_version_file(mock_glob, "3.13.2\n")
        version_file.write_text.assert_not_called()
        self.assert_path_logged(version_file)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_update(self, mock_glob: Mock):
        """Test that the entry is updated to the latest `python` release on Docker Hub."""
        version_file = self.update_version_file(mock_glob)
        version_file.write_text.assert_called_once_with("3.13.2\n")
        self.assert_new_version_logged("python", docker_hub_version("3.13.2"), Location(version_file, 1))
        self.assert_no_warnings_logged()

    def test_pins_bare_entry_to_full_version(self, mock_glob: Mock):
        """Test that a less precise entry (`3.12`) is pinned to the full latest version."""
        version_file = self.update_version_file(mock_glob, "3.12\n")
        version_file.write_text.assert_called_once_with("3.13.2\n")
        self.assert_new_version_logged("python", docker_hub_version("3.13.2"), Location(version_file, 1))
        self.assert_no_warnings_logged()

    def test_multiple_entries(self, mock_glob: Mock):
        """Test that a version file pinning several versions (as pyenv allows) updates each entry independently."""
        version_file = self.update_version_file(mock_glob, "3.11.9\n3.12.6\n")
        version_file.write_text.assert_called_once_with("3.13.2\n3.13.2\n")
        self.assertEqual(
            len(self.new_version_records()), 2
        )  # Both entries were reported (the second's changelog suppressed).
        self.assert_no_warnings_logged()

    def test_non_cpython_entries_left_untouched(self, mock_glob: Mock):
        """Test that entries that are not a plain CPython version are left untouched, even with a newer release."""
        contents = "pypy3.10-7.3.12\nsystem\n3.13t\ncpython@3.12\n>=3.10\n"
        version_file = self.update_version_file(mock_glob, contents)
        version_file.write_text.assert_not_called()
        self.assert_path_logged(version_file)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_marker_ignore_holds_the_entry_back(self, mock_glob: Mock):
        """Test that an `# update-time: ignore[update]` marker above an entry holds it back, despite a newer release."""
        version_file = self.update_version_file(mock_glob, "# update-time: ignore[update]\n3.12.6\n")
        version_file.write_text.assert_not_called()
        self.assert_ignored_logged("python", Location(version_file, 2))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_inline_marker_ignore(self, mock_glob: Mock):
        """Test that an inline `# update-time: ignore[update]` marker on the entry's own line holds it back too."""
        version_file = self.update_version_file(mock_glob, "3.12.6  # update-time: ignore[update]\n")
        version_file.write_text.assert_not_called()
        self.assert_ignored_logged("python", Location(version_file, 1))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_inline_marker_bound_preserves_comment(self, mock_glob: Mock):
        """Test that an inline bound is honoured and its comment is preserved when the version is rewritten."""
        version_file = self.update_version_file(
            mock_glob, "3.12.6  # update-time: allow[update<3.13]\n", latest=("3.12.9", "3.13.2")
        )
        version_file.write_text.assert_called_once_with("3.12.9  # update-time: allow[update<3.13]\n")
        self.assert_new_version_logged("python", docker_hub_version("3.12.9"), Location(version_file, 1))
        self.assert_no_warnings_logged()

    def test_marker_invalid_specifier(self, mock_glob: Mock):
        """Test that an unparsable marker specifier is warned about and leaves the entry unchanged."""
        version_file = self.update_version_file(mock_glob, "# update-time: allow[update<<3.13]\n3.12.6\n")
        version_file.write_text.assert_not_called()
        # The marker sits on line 1 and the entry it governs on line 2; the warning points at the entry's line.
        self.assert_invalid_bracket_item_logged("python", Location(version_file, 2), "<<3.13")
        self.assert_no_new_version_logged()

    def test_the_cooldown_scope_is_not_reported_as_redundant(self, mock_glob: Mock):
        """Test that a `cooldown` marker on an entry is not reported as redundant."""
        version_file = self.update_version_file(mock_glob, "# update-time: set[cooldown=30]\n3.12.6\n")
        version_file.write_text.assert_called_once_with("# update-time: set[cooldown=30]\n3.13.2\n")
        self.assert_no_warnings_logged()

    def test_the_stale_scope_warns_at_its_own_threshold(self, mock_glob: Mock):
        """Test that a `stale` threshold on an entry warns at that threshold, rather than being redundant."""
        version_file = self.update_version_file(mock_glob, "# update-time: set[stale-after=90]\n3.12.6\n")
        version_file.write_text.assert_called_once_with("# update-time: set[stale-after=90]\n3.13.2\n")
        self.assert_stale_dependency_logged("python", "3.13.2", Location(version_file, 2))

    def test_the_yanked_vulnerable_and_archived_scopes_are_reported_as_redundant(self, mock_glob: Mock):
        """Test that a `yanked`, `vulnerable`, or `archived` marker on an entry is reported as redundant."""
        for directive, reason in (
            ("ignore[archived]", Reason.NO_ARCHIVAL_SIGNAL),
            ("ignore[yanked]", Reason.NO_YANK_CONCEPT),
            ("set[vulnerability-level=none]", Reason.NO_VULNERABILITY_REPORTS),
        ):
            with self.subTest(directive=directive):
                version_file = self.update_version_file(mock_glob, f"# update-time: {directive}\n3.12.6\n")
                version_file.write_text.assert_called_once_with(f"# update-time: {directive}\n3.13.2\n")
                self.assert_redundant_directive_logged(reason, "python", Location(version_file, 2), directive)

    def test_stale_warned(self, mock_glob: Mock):
        """Test that a stale `python` release (newest tag pushed long ago) is warned about."""
        version_file = self.update_version_file(mock_glob, "3.13.2\n", pushed=STALE_DATE)
        version_file.write_text.assert_not_called()
        self.assert_stale_dependency_logged("python", "3.13.2", Location(version_file, 1))
