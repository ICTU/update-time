"""Unit tests for the Python version file update script."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from update_time.domain.bound import NO_BOUND
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion
from update_time.io.log import Logger
from update_time.markers.directive import Reason
from update_time.primitives.location import Location
from update_time.updaters import update_python_version_file
from update_time.updaters.update_python_version_file import update_python_version_files

from tests.helpers import mock_path, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import DIGEST
from tests.update_time.helpers import LoggingTestCase, docker_tag
from tests.update_time.registry import RegistryRequestsMixin, mock_docker_registry
from tests.update_time.updaters.helpers import mock_docker_hub_auth


def _python_tag(last_pushed: str | None = None) -> dict[str, object]:
    """Return a Docker Hub `python:3.13.2` tag with a digest, and optionally a push date, for the fallback tests."""
    return docker_tag("3.13.2", DIGEST, **({"tag_last_pushed": last_pushed} if last_pushed else {}))


class _VersionFileTestCase(LoggingTestCase):
    """Base for the version file tests: builds a mock `.python-version` file and runs the updater over it."""

    def create_version_file(self, contents: str = "3.12.6\n") -> Mock:
        """Create a mock .python-version file next to the (mocked) repository root."""
        return mock_path(contents, parent=Path("/"))

    def update_version_file(self, mock_glob: Mock, contents: str = "3.12.6\n") -> Mock:
        """Update the Python version in a version file holding these contents, and return the mock file."""
        version_file = self.create_version_file(contents)
        mock_glob.return_value = [version_file]
        update_python_version_files()
        return version_file


@patch_pathlib_path("rglob", cwd=Path("/"))
class UpdatePythonVersionFilesTest(_VersionFileTestCase):
    """Unit tests for the update Python version files function, deriving the version from a Dockerfile."""

    @patch_pathlib_path(exists=True, read_text="FROM python:3.12.6-slim")
    def test_unchanged(self, mock_glob: Mock):
        """Test that the version file is not written when it already matches the Dockerfile's Python base image."""
        version_file = self.update_version_file(mock_glob)
        version_file.write_text.assert_not_called()
        self.assert_path_logged(version_file)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM python:3.14.2-slim")
    def test_update_from_dockerfile(self, mock_glob: Mock):
        """Test that the version file follows a newer Python base image, adopting the tag's precision."""
        version_file = self.update_version_file(mock_glob)
        version_file.write_text.assert_called_once_with("3.14.2\n")
        self.assert_new_version_logged("python", "3.14.2", Location(version_file, 1))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_python_version_file,
            "    for dockerfile in (local_dockerfile, *glob_for(DOCKERFILES)):",
            "    for dockerfile in (local_dockerfile,):",
            "the base image is looked for beside the version file alone, not anywhere in the repository",
        )
    )
    @patch("update_time.updaters.update_python_version_file.get_latest_tag")
    @patch_pathlib_path(exists=False)
    def test_fallback_dockerfile(self, mock_get_latest_tag: Mock, mock_glob: Mock):
        """Test that a Python base image elsewhere in the repo is used when the version file has no local one."""
        # Docker Hub answers the version the entry already pins, so only the Dockerfile elsewhere can move it.
        mock_get_latest_tag.return_value = DependencyVersion(version="3.12.6")
        version_file = self.create_version_file()
        fallback_dockerfile = mock_path("FROM python:3.14.2-slim")

        def rglob(pattern: str, **_kwargs: object) -> list[Mock]:
            # The version-file glob finds the entry; the Dockerfile globs find only the one elsewhere in the repo.
            return [version_file] if pattern == ".python-version" else [fallback_dockerfile]

        mock_glob.side_effect = rglob
        update_python_version_files()
        version_file.write_text.assert_called_once_with("3.14.2\n")

    @patch_pathlib_path(exists=True, read_text="FROM python:3.14")
    def test_adopts_image_precision(self, mock_glob: Mock):
        """Test that a bare `python:3.14` image pins the entry to `3.14`, at the precision the tag provides."""
        version_file = self.update_version_file(mock_glob)
        version_file.write_text.assert_called_once_with("3.14\n")
        self.assert_new_version_logged("python", "3.14", Location(version_file, 1))
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text=f"FROM python:3.14.2-slim@{DIGEST}")
    def test_digest_pinned_base_image(self, mock_glob: Mock):
        """Test that the version is read from a digest-pinned base image (as the Dockerfile updater leaves it)."""
        version_file = self.update_version_file(mock_glob)
        version_file.write_text.assert_called_once_with("3.14.2\n")
        self.assert_new_version_logged("python", "3.14.2", Location(version_file, 1))
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM python:3.13")
    def test_no_downgrade(self, mock_glob: Mock):
        """Test that a version file ahead of the Dockerfile's Python base image is left unchanged, not downgraded."""
        version_file = self.update_version_file(mock_glob, "3.14.2\n")
        version_file.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM python:3.14.2-slim")
    def test_multiple_entries(self, mock_glob: Mock):
        """Test that a version file pinning several versions (as pyenv allows) updates each entry independently."""
        version_file = self.update_version_file(mock_glob, "3.11.9\n3.12.6\n")
        version_file.write_text.assert_called_once_with("3.14.2\n3.14.2\n")
        self.assertEqual(
            len(self.new_version_records()), 2
        )  # Both entries were reported (the second's changelog suppressed).
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM python:3.14.2")
    def test_non_cpython_entries_left_untouched(self, mock_glob: Mock):
        """Test that entries that are not a plain CPython version are left untouched, even next to a newer image."""
        contents = "pypy3.10-7.3.12\nsystem\n3.13t\ncpython@3.12\n>=3.10\n"
        version_file = self.update_version_file(mock_glob, contents)
        version_file.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM python:3.14")
    def test_marker_ignore_wins_over_dockerfile(self, mock_glob: Mock):
        """Test that an `# update-time: ignore` marker above an entry holds it back, even when the image is newer."""
        version_file = self.update_version_file(mock_glob, "# update-time: ignore\n3.12.6\n")
        version_file.write_text.assert_not_called()
        self.assert_ignored_logged("python", Location(version_file, 2))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM python:3.14")
    def test_marker_bound_blocks_dockerfile(self, mock_glob: Mock):
        """Test that a version bound above an entry keeps a Dockerfile jump it excludes from being adopted."""
        version_file = self.update_version_file(mock_glob, "# update-time: allow[update<3.13]\n3.12.6\n")
        version_file.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM python:3.12.9")
    def test_marker_bound_keeps_in_range_dockerfile(self, mock_glob: Mock):
        """Test that a bounded entry still adopts a Dockerfile version the bound admits (a patch within the range)."""
        version_file = self.update_version_file(mock_glob, "# update-time: allow[update<3.13]\n3.12.6\n")
        version_file.write_text.assert_called_once_with("# update-time: allow[update<3.13]\n3.12.9\n")
        self.assert_new_version_logged("python", "3.12.9", Location(version_file, 2))
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM python:3.14")
    def test_directives_that_hold_nothing_back_are_reported_as_redundant(self, mock_glob: Mock):
        """Test that a directive holding nothing back for the entry is reported, and the entry still updates."""
        for directive, reason in (
            ("ignore[cooldown<30]", Reason.NO_COOLDOWN_DATES),
            ("ignore[stale<90]", Reason.NO_STALENESS_DATES),
            ("ignore[archived]", Reason.NO_ARCHIVAL_SIGNAL),
            ("ignore[yanked]", Reason.NO_YANK_CONCEPT),
            ("ignore[vulnerable]", Reason.NO_VULNERABILITY_REPORTS),
        ):
            with self.subTest(directive=directive):
                version_file = self.update_version_file(mock_glob, f"# update-time: {directive}\n3.12.6\n")
                version_file.write_text.assert_called_once_with(f"# update-time: {directive}\n3.14\n")
                self.assert_redundant_directive_logged(reason, "python", Location(version_file, 2), directive)
                self.assert_new_version_logged("python", "3.14", Location(version_file, 2))

    @patch_pathlib_path(exists=True, read_text="FROM python:3.14")
    def test_the_reported_cooldown_directive_is_the_one_that_won(self, mock_glob: Mock):
        """Test that where both placements set a cooldown, the warning names the inline one, which wins."""
        above, inline = "# update-time: ignore[cooldown<90]", "# update-time: allow[cooldown>=30]"
        version_file = self.update_version_file(mock_glob, f"{above}\n3.12.6  {inline}\n")
        self.assert_redundant_directive_logged(
            Reason.NO_COOLDOWN_DATES, "python", Location(version_file, 2), "allow[cooldown>=30]"
        )

    @patch_pathlib_path(exists=True, read_text="FROM python:3.14")
    def test_inline_marker_ignore(self, mock_glob: Mock):
        """Test that an inline `# update-time: ignore` marker on the entry's own line holds it back too."""
        version_file = self.update_version_file(mock_glob, "3.12.6  # update-time: ignore\n")
        version_file.write_text.assert_not_called()
        self.assert_ignored_logged("python", Location(version_file, 1))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM python:3.12.9")
    def test_inline_marker_bound_preserves_comment(self, mock_glob: Mock):
        """Test that an inline bound is honoured and its comment is preserved when the version is rewritten."""
        version_file = self.update_version_file(mock_glob, "3.12.6  # update-time: allow[update<3.13]\n")
        version_file.write_text.assert_called_once_with("3.12.9  # update-time: allow[update<3.13]\n")
        self.assert_new_version_logged("python", "3.12.9", Location(version_file, 1))
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM python:3.14")
    def test_marker_invalid_specifier(self, mock_glob: Mock):
        """Test that an unparsable marker specifier is warned about and leaves the entry unchanged."""
        version_file = self.update_version_file(mock_glob, "# update-time: allow[update<<3.13]\n3.12.6\n")
        version_file.write_text.assert_not_called()
        # The marker sits on line 1 and the entry it governs on line 2; the warning points at the entry's line.
        self.assert_logged(
            Logger._MESSAGE_INVALID_BRACKET_ITEM,
            bracket_item="<<3.13",
            dependency="python",
            location=Location(version_file, 2),
        )
        self.assert_no_new_version_logged()


@mock_docker_hub_auth
@patch_pathlib_path("rglob", cwd=Path("/"), exists=False)
class UpdatePythonVersionFilesFallbackTest(RegistryRequestsMixin, _VersionFileTestCase):
    """Unit tests for the fallback to the latest `python` release on Docker Hub when no Dockerfile derives it."""

    @patch("update_time.updaters.update_python_version_file.get_latest_tag")
    def test_fallback_updates_to_latest(self, mock_get_latest_tag: Mock, mock_glob: Mock):
        """Test that the entry is updated to the latest `python` release on Docker Hub when no Dockerfile derives it."""
        mock_get_latest_tag.return_value = DependencyVersion(version="3.13.2")
        version_file = self.update_version_file(mock_glob)
        version_file.write_text.assert_called_once_with("3.13.2\n")
        mock_get_latest_tag.assert_called_once_with("python", "3.12.6", NO_BOUND, COOLDOWN.default)
        self.assert_new_version_logged("python", "3.13.2", Location(version_file, 1))
        self.assert_no_warnings_logged()

    # Docker Hub is patched out because this test is about the Dockerfile path: without it the updater would go
    # there, which is another test's subject.
    @patch("update_time.updaters.update_python_version_file.get_latest_tag")
    def test_fallback_pins_bare_entry_to_full_version(self, mock_get_latest_tag: Mock, mock_glob: Mock):
        """Test that a less precise entry (`3.12`) is pinned to the full latest version."""
        mock_get_latest_tag.return_value = DependencyVersion(version="3.13.2")
        version_file = self.update_version_file(mock_glob, "3.12\n")
        version_file.write_text.assert_called_once_with("3.13.2\n")
        self.assert_new_version_logged("python", "3.13.2", Location(version_file, 1))
        self.assert_no_warnings_logged()

    def test_fallback_via_docker_hub(self, mock_glob: Mock):
        """Test the full Docker Hub fallback path: the entry follows the latest `python` tag, resolved for real."""
        self.requests.side_effect = mock_docker_registry(_python_tag())
        version_file = self.update_version_file(mock_glob)
        version_file.write_text.assert_called_once_with("3.13.2\n")
        self.assert_new_version_logged("python", "3.13.2", Location(version_file, 1))
        self.assert_no_warnings_logged()

    def test_fallback_cooldown_marker_is_not_reported_as_redundant(self, mock_glob: Mock):
        """Test that a `cooldown` marker on an entry following Docker Hub holds something back, since tags are dated."""
        marker = "# update-time: ignore[cooldown<30]"
        self.requests.side_effect = mock_docker_registry(_python_tag())
        version_file = self.update_version_file(mock_glob, f"{marker}\n3.12.6\n")
        version_file.write_text.assert_called_once_with(f"{marker}\n3.13.2\n")
        self.assert_no_warnings_logged()

    def test_fallback_scopes_docker_hub_cannot_answer_are_reported_as_redundant(self, mock_glob: Mock):
        """Test that a scope Docker Hub cannot answer is reported for an entry following it, which still updates."""
        for directive, reason in (
            ("ignore[archived]", Reason.NO_ARCHIVAL_SIGNAL),
            ("ignore[yanked]", Reason.NO_YANK_CONCEPT),
            ("ignore[vulnerable]", Reason.NO_VULNERABILITY_REPORTS),
        ):
            with self.subTest(directive=directive):
                self.requests.side_effect = mock_docker_registry(_python_tag())
                version_file = self.update_version_file(mock_glob, f"# update-time: {directive}\n3.12.6\n")
                version_file.write_text.assert_called_once_with(f"# update-time: {directive}\n3.13.2\n")
                self.assert_redundant_directive_logged(reason, "python", Location(version_file, 2), directive)

    def test_fallback_stale_warned(self, mock_glob: Mock):
        """Test that a stale `python` release (newest tag pushed long ago) is warned about via the fallback."""
        old = (datetime.now(UTC) - timedelta(days=512)).isoformat()
        self.requests.side_effect = mock_docker_registry(_python_tag(last_pushed=old))
        version_file = self.update_version_file(mock_glob, "3.13.2\n")
        version_file.write_text.assert_not_called()
        self.assert_stale_dependency_logged("python", "3.13.2", Location(version_file, 1))
