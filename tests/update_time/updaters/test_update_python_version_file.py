"""Unit tests for the Python version file update script."""

from typing import TYPE_CHECKING

from update_time.io.log import Logger
from update_time.manifests import python_dependency_toml
from update_time.markers.directive import Reason
from update_time.package_managers.uv import uv
from update_time.primitives.location import Location
from update_time.updaters import update_python_version_file
from update_time.updaters.update_python_version_file import update_python_version_files

from tests.helpers import FakeFiles, mock_path, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import DIGEST, PAST_COOLDOWN_DATE, STALE_DATE
from tests.update_time.helpers import SCAN_ROOT, LoggingTestCase, docker_hub_version, docker_tag, message_of
from tests.update_time.registry import RegistryRequestsMixin, mock_docker_registry
from tests.update_time.updaters.helpers import PYPROJECT_TOML, mock_docker_hub_auth

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path
    from unittest.mock import Mock


@mock_docker_hub_auth
@patch_pathlib_path("rglob", cwd=SCAN_ROOT)
class UpdatePythonVersionFilesTest(RegistryRequestsMixin, LoggingTestCase):
    """Unit tests for the update Python version files function."""

    def setUp(self) -> None:
        """Start each test with an empty file system in memory."""
        super().setUp()
        self.files = FakeFiles()
        self.enterContext(self.files.patched())

    def write_pyproject_toml(self, requires_python: str) -> None:
        """Write a pyproject.toml with this `requires-python` into the root directory."""
        self.files[PYPROJECT_TOML] = f'[project]\nrequires-python = "{requires_python}"\n'

    def write_workspace(self, members: dict[str, str], *, exclude: str = "") -> None:
        """Write a uv workspace whose root allows `>=3.10`, with a member under `packages/` per `requires-python`."""
        excluded = f'exclude = ["packages/{exclude}"]\n' if exclude else ""
        self.files[PYPROJECT_TOML] = (
            f'[project]\nrequires-python = ">=3.10"\n\n[tool.uv.workspace]\nmembers = ["packages/*"]\n{excluded}'
        )
        for member, requires_python in members.items():
            self.files[SCAN_ROOT / f"packages/{member}/pyproject.toml"] = (
                f'[project]\nrequires-python = "{requires_python}"\n'
            )

    def update_version_file(
        self,
        mock_glob: Mock,
        contents: str = "3.12.6\n",
        *,
        latest: tuple[str, ...] = ("3.13.2",),
        pushed: datetime = PAST_COOLDOWN_DATE,
        directory: Path = SCAN_ROOT,
    ) -> Mock:
        """Update the Python version in a version file holding these contents in the directory, and return the file."""
        self.requests.side_effect = mock_docker_registry(
            *(docker_tag(tag, DIGEST, tag_last_pushed=pushed) for tag in latest)
        )
        version_file = mock_path(contents, parent=directory)
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

    def test_requires_python_bounds_the_update(self, mock_glob: Mock):
        """Test that the `requires-python` of the `pyproject.toml` beside the file bounds the entry's update."""
        self.write_pyproject_toml(">=3.10,<3.12")
        version_file = self.update_version_file(mock_glob, "3.10\n", latest=("3.11.9", "3.15.1"))
        version_file.write_text.assert_called_once_with("3.11.9\n")
        self.assert_new_version_logged("python", docker_hub_version("3.11.9"), Location(version_file, 1))
        self.assert_logged_among_others(
            message_of(Logger.held_back_by_requires_python),
            dependency="python",
            location=Location(version_file, 1),
            requires_python=">=3.10,<3.12",
            pyproject_toml=Location(PYPROJECT_TOML, 2),
        )
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_python_version_file._report_requires_python,
            " and not marker.ignores(Scope.UPDATE)",
            "",
            "an entry its marker holds back is logged as held back by the `requires-python` as well",
        )
    )
    def test_a_requires_python_that_holds_nothing_back_is_not_logged(self, mock_glob: Mock):
        """Test that a `requires-python` is not logged as holding an entry back where it holds nothing back."""
        for case, requires_python, contents, written in (
            ("it admits every version above the entry's", ">=3.10", "3.10\n", "3.15.1\n"),
            ("the entry's marker holds the update back", ">=3.10,<3.12", "3.10  # update-time: ignore[update]\n", ""),
        ):
            with self.subTest(case):
                self.write_pyproject_toml(requires_python)
                version_file = self.update_version_file(mock_glob, contents, latest=("3.11.9", "3.15.1"))
                if written:
                    version_file.write_text.assert_called_once_with(written)
                else:
                    version_file.write_text.assert_not_called()
                self.assertEqual(self.records_of(message_of(Logger.held_back_by_requires_python)), [])

    def test_requires_python_above_the_file_bounds_the_update(self, mock_glob: Mock):
        """Test that the `requires-python` of the nearest `pyproject.toml` above the file bounds the entry's update."""
        self.write_pyproject_toml(">=3.10,<3.12")
        version_file = self.update_version_file(
            mock_glob, "3.10\n", latest=("3.11.9", "3.15.1"), directory=SCAN_ROOT / "src/lib"
        )
        version_file.write_text.assert_called_once_with("3.11.9\n")

    def test_a_workspace_member_bounds_the_entry_at_the_root(self, mock_glob: Mock):
        """Test that the `requires-python` of a uv workspace member bounds a `.python-version` at the root."""
        self.write_workspace({"b": ">=3.10,<3.12"})
        version_file = self.update_version_file(mock_glob, "3.10\n", latest=("3.11.9", "3.15.1"))
        version_file.write_text.assert_called_once_with("3.11.9\n")

    @kills(
        Mutation(
            uv.workspace_pyproject_tomls,
            "root = _workspace_root(pyproject_toml)",
            "root = pyproject_toml",
            "a member's own `.python-version` is bounded by that member alone, though uv checks the whole workspace",
        )
    )
    def test_a_workspace_member_bounds_the_entry_of_another_member(self, mock_glob: Mock):
        """Test that the `requires-python` of one workspace member bounds the `.python-version` of another."""
        self.write_workspace({"a": ">=3.10,<3.13", "b": ">=3.10,<3.12"})
        version_file = self.update_version_file(
            mock_glob, "3.10\n", latest=("3.11.9", "3.12.5", "3.15.1"), directory=SCAN_ROOT / "packages/a"
        )
        version_file.write_text.assert_called_once_with("3.11.9\n")

    @kills(
        Mutation(
            uv.workspace_pyproject_tomls,
            " and _workspace_includes(root_dir, workspace, member)",
            "",
            "the excluded members of a workspace bound the entry, though uv leaves them out",
        )
    )
    def test_an_excluded_workspace_member_does_not_bound_the_entry(self, mock_glob: Mock):
        """Test that the `requires-python` of a member the workspace excludes does not bound the entry."""
        self.write_workspace({"old": "<3.11"}, exclude="old")
        version_file = self.update_version_file(mock_glob, "3.10\n", latest=("3.10.14", "3.15.1"))
        version_file.write_text.assert_called_once_with("3.15.1\n")

    @kills(
        Mutation(
            uv.workspace_pyproject_tomls,
            'if (member / "pyproject.toml").exists() and _workspace_includes(root_dir, workspace, member)',
            "if _workspace_includes(root_dir, workspace, member)",
            "a directory without a pyproject.toml counts as a member, so a warning names a file that is not there",
        )
    )
    def test_a_member_glob_matching_a_directory_without_a_pyproject_toml_passes_it_over(self, mock_glob: Mock):
        """Test that a member glob passes over a directory that does not hold a `pyproject.toml`."""
        self.write_workspace({"b": ">=3.10,<3.12"})
        self.files[SCAN_ROOT / "packages/docs/README.md"] = "notes\n"
        version_file = self.update_version_file(mock_glob, "3.10\n", latest=("3.11.9", "3.15.1"))
        version_file.write_text.assert_called_once_with("3.11.9\n")
        self.assert_no_warnings_logged()

    def test_each_member_that_holds_the_entry_back_is_logged(self, mock_glob: Mock):
        """Test that each workspace member whose `requires-python` holds the entry back gets a line of its own."""
        self.write_workspace({"a": ">=3.10,<3.13", "b": ">=3.10,<3.12"})
        version_file = self.update_version_file(mock_glob, "3.10\n", latest=("3.11.9", "3.12.5", "3.15.1"))
        version_file.write_text.assert_called_once_with("3.11.9\n")
        held_back = message_of(Logger.held_back_by_requires_python)
        self.assertEqual(len(self.records_of(held_back)), 2)
        member_files = {member: Location(SCAN_ROOT / f"packages/{member}/pyproject.toml", 2) for member in ("a", "b")}
        lines = {member: len(self.records_of(held_back, pyproject_toml=file)) for member, file in member_files.items()}
        self.assertEqual(lines, {"a": 1, "b": 1})

    def test_the_nearest_pyproject_toml_decides_even_without_requires_python(self, mock_glob: Mock):
        """Test that a `requires-python` above the nearest `pyproject.toml` does not bound the entry's update."""
        self.write_pyproject_toml(">=3.10,<3.12")
        self.files[SCAN_ROOT / "src/lib/pyproject.toml"] = "[project]\n"
        version_file = self.update_version_file(
            mock_glob, "3.10\n", latest=("3.11.9", "3.15.1"), directory=SCAN_ROOT / "src/lib"
        )
        version_file.write_text.assert_called_once_with("3.15.1\n")

    @kills(
        Mutation(
            update_python_version_file._report_requires_python,
            "if not bound.keeps(Version(version), version):",
            "if False:",
            "the run does not warn about an entry outside the `requires-python`, though uv refuses to run with it",
        )
    )
    def test_an_entry_outside_the_requires_python_is_warned_about(self, mock_glob: Mock):
        """Test that an entry the `requires-python` excludes, and the run leaves there, is warned about."""
        for case, entry in (
            ("an entry without a marker", "3.13.2\n"),
            ("an entry its marker holds back", "3.13.2  # update-time: ignore[update]\n"),
        ):
            with self.subTest(case):
                self.write_pyproject_toml(">=3.10,<3.12")
                version_file = self.update_version_file(mock_glob, entry, latest=("3.11.9", "3.13.2", "3.15.1"))
                version_file.write_text.assert_not_called()
                self.assert_logged(
                    message_of(Logger.python_version_outside_requires_python),
                    dependency="python",
                    location=Location(version_file, 1),
                    version="3.13.2",
                    requires_python=">=3.10,<3.12",
                    pyproject_toml=Location(PYPROJECT_TOML, 2),
                )

    def test_a_requires_python_that_is_not_a_version_specifier_is_warned_about(self, mock_glob: Mock):
        """Test that a `requires-python` that is not a version specifier is warned about and ignored."""
        self.files[PYPROJECT_TOML] = "[project]\nrequires-python = 3.10\n"
        version_file = self.update_version_file(mock_glob, "3.10\n", latest=("3.11.9", "3.15.1"))
        version_file.write_text.assert_called_once_with("3.15.1\n")
        self.assert_logged(message_of(Logger.invalid_requires_python), location=Location(PYPROJECT_TOML, 2))

    @kills(
        Mutation(
            update_python_version_file._requires_python,
            "except (OSError, UnicodeDecodeError) as error:",
            "except UnicodeDecodeError as error:",
            "a `pyproject.toml` that cannot be read stops the run for every `.python-version` file",
            raises="PermissionError: [Errno 13] Permission denied: '/pyproject.toml'",
        ),
        Mutation(
            update_python_version_file._requires_python,
            "except (OSError, UnicodeDecodeError) as error:",
            "except OSError as error:",
            "a `pyproject.toml` that is not UTF-8 stops the run for every `.python-version` file",
            raises="UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
        ),
        Mutation(
            update_python_version_file._requires_python,
            "_LOG.unreadable_requires_python(pyproject_toml, error)",
            "pass",
            "a `pyproject.toml` that cannot be read goes unreported, so its `requires-python` drops out silently",
        ),
    )
    def test_a_pyproject_toml_that_cannot_be_read_is_warned_about_and_bounds_nothing(self, mock_glob: Mock):
        """Test that a `pyproject.toml` that cannot be read is warned about, and the entry updates as without it."""
        self.write_pyproject_toml(">=3.10,<3.12")
        for case, error in (
            ("a file it may not read", PermissionError(13, "Permission denied", str(PYPROJECT_TOML))),
            ("a file that is not UTF-8", UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")),
        ):
            with self.subTest(case):
                self.files.read_errors[PYPROJECT_TOML] = error
                version_file = self.update_version_file(mock_glob, "3.10\n", latest=("3.11.9", "3.15.1"))
                version_file.write_text.assert_called_once_with("3.15.1\n")
                self.assert_logged(
                    message_of(Logger.unreadable_requires_python),
                    location=Location(PYPROJECT_TOML),
                    error=str(error),
                )

    @kills(
        Mutation(
            python_dependency_toml.parsed_config,
            " or not is_well_formed(config)",
            "",
            "a `pyproject.toml` whose `project` is not a table stops the run for every `.python-version` file",
            raises="AttributeError: 'str' object has no attribute 'get'",
        )
    )
    def test_a_pyproject_toml_whose_project_is_not_a_table_bounds_nothing(self, mock_glob: Mock):
        """Test that a `pyproject.toml` whose `project` is not a table leaves the entry to update as without it."""
        self.files[PYPROJECT_TOML] = 'project = "x"\n'
        version_file = self.update_version_file(mock_glob, "3.10\n", latest=("3.11.9", "3.15.1"))
        version_file.write_text.assert_called_once_with("3.15.1\n")
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_python_version_file,
            "@cache\ndef _requires_python(",
            "def _requires_python(",
            "every `.python-version` file under a `pyproject.toml` reads it again, and repeats its warning",
        )
    )
    def test_a_pyproject_toml_several_version_files_share_is_warned_about_once(self, mock_glob: Mock):
        """Test that a `requires-python` two `.python-version` files share is warned about once, not once per file."""
        self.files[PYPROJECT_TOML] = "[project]\nrequires-python = 3.10\n"
        self.requests.side_effect = mock_docker_registry(
            docker_tag("3.15.1", DIGEST, tag_last_pushed=PAST_COOLDOWN_DATE)
        )
        mock_glob.return_value = [mock_path("3.10\n", parent=SCAN_ROOT / name) for name in ("a", "b")]
        update_python_version_files()
        self.assert_logged(message_of(Logger.invalid_requires_python), location=Location(PYPROJECT_TOML, 2))

    def test_requires_python_and_the_marker_bound_both_bound_the_update(self, mock_glob: Mock):
        """Test that a marker bound tighter than the `requires-python` holds the entry below the marker's bound."""
        self.write_pyproject_toml(">=3.10,<3.12")
        version_file = self.update_version_file(
            mock_glob, "3.10.1  # update-time: allow[update<3.11]\n", latest=("3.10.14", "3.11.9", "3.15.1")
        )
        version_file.write_text.assert_called_once_with("3.10.14  # update-time: allow[update<3.11]\n")

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
        latest = ("3.12.9", "3.13.2", "3.14.1", "3.16.0")
        for case, bound, updated in (
            ("a ceiling", "allow[update<3.13]", "3.12.9"),
            ("a range to skip", "ignore[update>=3.13,<3.15]", "3.16.0"),
        ):
            with self.subTest(case):
                version_file = self.update_version_file(mock_glob, f"3.12.6  # update-time: {bound}\n", latest=latest)
                version_file.write_text.assert_called_once_with(f"{updated}  # update-time: {bound}\n")
                self.assert_new_version_logged("python", docker_hub_version(updated), Location(version_file, 1))
                self.assert_no_warnings_logged()

    def test_an_invalid_specifier_is_warned_about_and_the_entry_updates(self, mock_glob: Mock):
        """Test that an unparsable marker specifier is warned about, and the entry updates as usual."""
        version_file = self.update_version_file(mock_glob, "# update-time: allow[update<<3.13]\n3.12.6\n")
        version_file.write_text.assert_called_once_with("# update-time: allow[update<<3.13]\n3.13.2\n")
        # The marker sits on line 1 and the entry it governs on line 2; the warning points at the entry's line.
        self.assert_invalid_bracket_item_logged("python", Location(version_file, 2), "<<3.13")

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
