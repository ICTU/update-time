"""Unit tests for the pyproject.toml update script."""

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import Mock, patch

from update_time.domain.cooldown import COOLDOWN_DAYS
from update_time.updaters.update_pyproject_toml import update_pyproject_tomls

from tests.update_time.assertions import assert_success
from tests.update_time.helpers import LoggingTestCase, mock_path, mock_response, release_json

if TYPE_CHECKING:
    from update_time.sources.pypi import Release

EXCLUDE_NEWER = ["--exclude-newer", f"{COOLDOWN_DAYS} days"]  # the uv cooldown option Update-time adds by default


def pyproject(spec: str, *, exclude_newer: bool = False) -> str:
    """Return a minimal valid pyproject.toml pinning the given dependency, optionally with its own uv cooldown."""
    contents = f'[project]\ndependencies = ["{spec}"]\n'
    return contents + '\n[tool.uv]\nexclude-newer = "2024-01-01"\n' if exclude_newer else contents


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

    def assert_exclude_newer(self, run: Mock, *, expected: bool) -> None:
        """Assert the uv tree and uv lock commands carry (or omit) Update-time's exclude-newer cooldown option."""
        commands = [call.args[0] for call in run.call_args_list]
        uv_tree = next(command for command in commands if command[:2] == ["uv", "tree"])
        uv_lock = next(command for command in commands if command[:2] == ["uv", "lock"])
        self.assertNotIn("--frozen", uv_tree)  # --frozen would make uv tree --outdated ignore the cooldown
        for command in (uv_tree, uv_lock):
            if expected:
                self.assertEqual(EXCLUDE_NEWER, command[-2:])
            else:
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
        self.assert_exclude_newer(run, expected=True)
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

    def test_project_exclude_newer_respected(self, run: Mock, get: Mock, glob: Mock):
        """Test that no cooldown option is added when the pyproject.toml sets its own uv exclude-newer."""
        run.return_value = self.mock_update_on_stdout("package")
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0", exclude_newer=True))
        glob.return_value = [mock_pyproject_toml]
        assert_success(update_pyproject_tomls())
        self.assert_exclude_newer(run, expected=False)
        get.assert_not_called()
        self.assert_no_warnings_logged()

    @patch.dict("os.environ", {"UV_EXCLUDE_NEWER": "2024-01-01"})
    def test_uv_exclude_newer_env_respected(self, run: Mock, get: Mock, glob: Mock):
        """Test that no cooldown option is added when the UV_EXCLUDE_NEWER environment variable is set."""
        run.return_value = self.mock_update_on_stdout("package")
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0"))
        glob.return_value = [mock_pyproject_toml]
        assert_success(update_pyproject_tomls())
        self.assert_exclude_newer(run, expected=False)
        get.assert_not_called()
        self.assert_no_warnings_logged()

    def test_skip_non_uv_tool_section(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pyproject.toml with a non-uv tool section (e.g. [tool.poetry]) is skipped without running uv."""
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0") + '\n[tool.poetry]\nname = "x"\n')
        glob.return_value = [mock_pyproject_toml]
        assert_success(update_pyproject_tomls())
        run.assert_not_called()
        get.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_skipped_logged(mock_pyproject_toml, "poetry is not supported, only uv")
        self.assert_no_warnings_logged()

    @patch("pathlib.Path.exists", Mock(return_value=True))
    def test_skip_non_uv_lockfile(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pyproject.toml with a non-uv lockfile (e.g. poetry.lock) is skipped without running uv."""
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0"))
        glob.return_value = [mock_pyproject_toml]
        assert_success(update_pyproject_tomls())
        run.assert_not_called()
        get.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_skipped_logged(mock_pyproject_toml, "poetry is not supported, only uv")
        self.assert_no_warnings_logged()
