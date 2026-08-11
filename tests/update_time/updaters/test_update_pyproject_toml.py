"""Unit tests for the pyproject.toml updater (discovery and orchestration of the uv package manager)."""

import subprocess  # nosec
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import ANY, Mock, patch

from update_time.primitives.location import Location
from update_time.updaters.update_pyproject_toml import update_pyproject_tomls

from tests.helpers import mock_path, mock_response, patch_pathlib_path
from tests.update_time.helpers import (
    LoggingTestCase,
    github_commits_json,
    github_release_json,
    no_vulnerabilities,
    pyproject,
    staleness_disabled,
)

if TYPE_CHECKING:
    from update_time.sources.pypi import Release


def _discovered_pyproject_toml(glob: Mock, spec: str) -> Mock:
    """Return the single mock pyproject.toml the scan discovers, pinning the given dependency."""
    pyproject_toml = mock_path(pyproject(spec), parent=Path("/"))
    glob.return_value = [pyproject_toml]
    return pyproject_toml


# Persisting the cooldown into config is exercised by the uv package manager's tests; stub it out here so these tests
# focus on the discovery/version-update flow (and don't try to write config to the mock pyproject.toml files). The
# staleness pass is disabled here (it makes its own PyPI requests) and OSV answers with no advisory; each has its own
# tests in StaleDependencyTest and VulnerableDependencyTest below.
@no_vulnerabilities
@staleness_disabled
@patch("update_time.package_managers.uv.configure_cooldown", Mock())
@patch_pathlib_path("rglob", cwd=Path("/"))
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
        uv_tree = next(command for command in commands if command[:2] == ("uv", "tree"))
        uv_lock = next(command for command in commands if command[:2] == ("uv", "lock"))
        self.assertNotIn("--frozen", uv_tree)  # --frozen would make uv tree --outdated ignore the cooldown
        for command in (uv_tree, uv_lock):
            with self.subTest(command=command[:2]):
                self.assertNotIn("--exclude-newer", command)

    def test_update(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml, with Update-time's cooldown passed to uv."""
        run.return_value = self.mock_update_on_stdout("package", "v1.1")
        get.return_value = mock_response(
            {"info": {"description": "Package"}, "urls": [{"upload_time_iso_8601": "2026-05-30T12:08:53.123321Z"}]}
        )
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged("package", "1.1, published: 2026-05-30 12:08", Location(mock_pyproject_toml))
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
        update_pyproject_tomls()
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_with_changelog==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            "package_with_changelog",
            "1.1, published: 2026-05-30 12:07",
            Location(mock_pyproject_toml),
            self.changelog,
        )
        self.assert_no_warnings_logged()

    def test_update_with_html_changelog(self, run: Mock, get: Mock, glob: Mock):
        """Test that updating a pyproject.toml with only a HTML changelog ignores the changelog."""
        run.return_value = self.mock_update_on_stdout("package_with_html_changelog", "v1.1")
        get.side_effect = [
            mock_response(self.pypi_metadata()),
            Mock(text=self.changelog, headers={"Content-Type": "text/html"}),
            mock_response([github_release_json("v1.1")]),
        ]
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_with_html_changelog==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_with_html_changelog==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            "package_with_html_changelog", "1.1, published: 2026-05-30 12:07", Location(mock_pyproject_toml)
        )
        self.assert_no_warnings_logged()

    def test_update_with_github_url(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml with GitHub releases."""
        run.return_value = self.mock_update_on_stdout("package_with_github_releases", "v1.1")
        get.side_effect = [
            mock_response(self.pypi_metadata(changelog_url="")),
            mock_response([github_release_json("v1.1", body=self.changelog)]),
            mock_response(github_commits_json()),
        ]
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_with_github_releases==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_with_github_releases==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            "package_with_github_releases",
            "1.1, published: 2026-05-30 12:07",
            Location(mock_pyproject_toml),
            self.changelog,
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
            "1.1, published: 2026-05-30 12:07",
            Location(mock_pyproject_toml),
        )
        self.assert_no_warnings_logged()

    def test_unchanged(self, run: Mock, get: Mock, glob: Mock):
        """Test that the pyproject.toml is not written if there are no changes."""
        run.return_value = self.mock_update_on_stdout("package")
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
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
        update_pyproject_tomls()
        commands = [call.args[0][:2] for call in run.call_args_list]
        self.assertIn(("uv", "tree"), commands)
        self.assertNotIn(("uv", "lock"), commands)  # uv lock is skipped because uv tree failed
        mock_pyproject_toml.write_text.assert_not_called()
        get.assert_not_called()
        self.assert_command_stderr_logged(stderr="error: offline")  # only uv tree's stderr is surfaced

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
        """Test that a pyproject.toml with a non-uv lockfile (e.g. poetry.lock) is skipped without running uv."""
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        run.assert_not_called()
        get.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_unsupported_package_manager_logged(mock_pyproject_toml, "poetry", "uv")
        self.assert_no_new_version_logged()

    def test_skip_invalid_pyproject_toml(self, run: Mock, get: Mock, glob: Mock):
        """Test that an unparsable pyproject.toml is skipped with a warning, without running uv or crashing."""
        mock_pyproject_toml = self.create_pyproject_toml('[project]\ndependencies = ["package==1.0"\n')  # missing ]
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        run.assert_not_called()
        get.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_invalid_pyproject_toml_logged(mock_pyproject_toml)
        self.assert_no_new_version_logged()


@patch("update_time.package_managers.uv.configure_cooldown", Mock())
@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("requests.get")
@patch("subprocess.run")
class CheckedPinsTest(LoggingTestCase):
    """Unit test for handing the discovered manifests to the checks uv-delegated updaters share.

    What those checks then report is `WarnAboutPinsTest`'s to pin; what this updater owns is running them over the
    files it found, once uv has settled their pins.
    """

    @patch("update_time.updaters.update_pyproject_toml.warn_about_pins")
    def test_the_discovered_manifests_are_checked(self, warn: Mock, run: Mock, get: Mock, glob: Mock):
        """Test that the checks are handed every manifest the scan found, whether uv updated it or not."""
        run.return_value = Mock(stdout="| django\n")
        pyproject_toml = _discovered_pyproject_toml(glob, "django==3.2.0")
        update_pyproject_tomls()
        get.assert_not_called()
        warn.assert_called_once_with([pyproject_toml], ANY)
