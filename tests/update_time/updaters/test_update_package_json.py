"""Unit tests for the package.json update script."""

import subprocess  # nosec
from pathlib import Path
from unittest.mock import Mock, call, patch

from update_time.domain.cooldown import COOLDOWN_DAYS
from update_time.updaters.update_package_json import COMMON_NPM_OPTIONS, update_package_jsons

from tests.update_time.assertions import assert_success
from tests.update_time.helpers import LoggingTestCase, mock_path, mock_response, release_json

COOLDOWN_OPTION = f"--min-release-age={COOLDOWN_DAYS}"  # the cooldown npm option Update-time adds by default


@patch("pathlib.Path.cwd", Mock(return_value=Path("/")))
@patch("pathlib.Path.rglob")
@patch("subprocess.run")
class UpdatePackageJsonTest(LoggingTestCase):
    """Unit tests for the update package.jsons function."""

    def create_package_json(self, contents: str = "{}") -> Mock:
        """Create a mock package.json file."""
        return mock_path(contents, parent=Path("/"))

    def npm_runs(self, *results: object) -> list:
        """Prepend the two `npm config get` cooldown probes (both unset) to the given npm command results.

        Both probes return `null`, so Update-time finds no project cooldown and adds its own to outdated/update.
        """
        return [Mock(stdout="null\n"), Mock(stdout="null\n"), *results]

    def assert_npm_called(self, mock_run: Mock, *, cooldown: bool = True) -> None:
        """Assert npm outdated, update, and list were called (with the cooldown option when expected)."""
        cooldown_option = [COOLDOWN_OPTION] if cooldown else []
        npm_outdated = ["npm", "outdated", "--json", *COMMON_NPM_OPTIONS, *cooldown_option]
        npm_update = ["npm", "update", "--save", *COMMON_NPM_OPTIONS, *cooldown_option]
        npm_list = ["npm", "list", "--json", "--depth=0", *COMMON_NPM_OPTIONS]
        run_kwargs = {"capture_output": True, "text": True, "check": True, "cwd": Path("/")}
        mock_run.assert_has_calls(
            (call(npm_outdated, **run_kwargs), call(npm_update, **run_kwargs), call(npm_list, **run_kwargs))
        )

    def test_unchanged(self, mock_run: Mock, mock_glob: Mock):
        """Test that the package.json is not written if there are no outdated packages."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.npm_runs(Mock(stdout="{}"), Mock(stdout=""), Mock(stdout='{"dependencies": {}}'))
        assert_success(update_package_jsons())
        self.assert_npm_called(mock_run)
        self.assert_path_logged(mock_package_json)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch(
        "requests.get",
        Mock(
            side_effect=[
                mock_response({"repository": {"url": "https://github.com/package/1.1"}}),
                mock_response([release_json("v1.1", body="Changelog")]),
                mock_response({"time": {"1.1": "20260530T10:26:45.543Z"}}),
                mock_response({"sha": "sha"}),
            ]
        ),
    )
    def test_update(self, mock_run: Mock, mock_glob: Mock):
        """Test that the installed version is logged, even when it is older than the latest version."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        # npm outdated results in a subprocess.CalledProcessError if there are updates. npm installs 1.1 rather than
        # the latest 1.2, for example because min-release-age holds back the fresh 1.2 release:
        mock_run.side_effect = self.npm_runs(
            subprocess.CalledProcessError(
                cmd="", returncode=1, output='{"package": {"current": "1.0", "latest": "1.2"}}'
            ),
            Mock(stdout=""),
            Mock(stdout='{"dependencies": {"package": {"version": "1.1"}}}'),
        )
        assert_success(update_package_jsons())
        self.assert_npm_called(mock_run)
        self.assert_path_logged(mock_package_json)
        self.assert_new_version_logged(mock_package_json, "package", "1.1, published: 2026-05-30 10:26", "Changelog")
        self.assert_no_warnings_logged()

    def test_restore_git_url_dependencies(self, mock_run: Mock, mock_glob: Mock):
        """Test that git+https URLs npm normalized to the github: shorthand are restored (issue #27)."""
        original = (
            '{\n  "devDependencies": {\n'
            '    "bats": "git+https://github.com/calj/bats.git",\n'
            '    "bats-assert": "git+https://github.com/ztombol/bats-assert.git#v0.3.0"\n'
            "  }\n}\n"
        )
        normalized = (
            '{\n  "devDependencies": {\n'
            '    "bats": "github:calj/bats",\n'
            '    "bats-assert": "github:ztombol/bats-assert#v0.3.0"\n'
            "  }\n}\n"
        )
        mock_package_json = self.create_package_json()
        # Three reads: package_manager detection, the original snapshot, then the npm-normalized contents.
        mock_package_json.read_text = Mock(side_effect=[original, original, normalized])
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.npm_runs(Mock(stdout="{}"), Mock(stdout=""), Mock(stdout='{"dependencies": {}}'))
        assert_success(update_package_jsons())
        mock_package_json.write_text.assert_called_once_with(original)
        self.assert_npm_called(mock_run)
        self.assert_path_logged(mock_package_json)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch(
        "requests.get",
        Mock(
            side_effect=[
                mock_response({"repository": {"url": "https://github.com/package/1.1"}}),
                mock_response([release_json("v1.1", body="Changelog")]),
                mock_response({"time": {"1.1": "20260530T10:26:45.543Z"}}),
                mock_response({"sha": "sha"}),
            ]
        ),
    )
    def test_manifest_kept_when_a_dependency_is_updated(self, mock_run: Mock, mock_glob: Mock):
        """Test that npm's manifest (including any specs it normalized) is kept when a dependency is updated."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.npm_runs(
            subprocess.CalledProcessError(
                cmd="", returncode=1, output='{"package": {"current": "1.0", "latest": "1.1"}}'
            ),
            Mock(stdout=""),
            Mock(stdout='{"dependencies": {"package": {"version": "1.1"}}}'),
        )
        assert_success(update_package_jsons())
        mock_package_json.write_text.assert_not_called()
        self.assert_npm_called(mock_run)
        self.assert_path_logged(mock_package_json)
        self.assert_new_version_logged(mock_package_json, "package", "1.1, published: 2026-05-30 10:26", "Changelog")
        self.assert_no_warnings_logged()

    def test_outdated_but_not_updated(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package is not logged when npm update did not change its installed version."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.npm_runs(
            subprocess.CalledProcessError(cmd="", returncode=1, output='{"held": {"current": "1.0", "latest": "1.2"}}'),
            Mock(stdout=""),
            # "held" stayed at 1.0 (e.g. min-release-age held back every newer release), "untracked" has no version:
            Mock(stdout='{"dependencies": {"held": {"version": "1.0"}, "untracked": {"missing": true}}}'),
        )
        assert_success(update_package_jsons())
        self.assert_npm_called(mock_run)
        self.assert_path_logged(mock_package_json)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_project_cooldown_respected(self, mock_run: Mock, mock_glob: Mock):
        """Test that no cooldown option is added when the project's npm config already sets one."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        # The first cooldown probe (min-release-age) returns a value, so Update-time adds no cooldown of its own:
        mock_run.side_effect = [
            Mock(stdout="14\n"),
            Mock(stdout="{}"),
            Mock(stdout=""),
            Mock(stdout='{"dependencies": {}}'),
        ]
        assert_success(update_package_jsons())
        self.assert_npm_called(mock_run, cooldown=False)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_skip_non_npm_package_manager(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json with a non-npm `packageManager` field is skipped without running npm."""
        mock_package_json = self.create_package_json('{"packageManager": "pnpm@9.15.0"}')
        mock_glob.return_value = [mock_package_json]
        assert_success(update_package_jsons())
        mock_run.assert_not_called()
        mock_package_json.write_text.assert_not_called()
        self.assert_skipped_logged(mock_package_json, "pnpm is not supported, only npm")
        self.assert_no_warnings_logged()

    @patch("pathlib.Path.exists", Mock(return_value=True))
    def test_skip_non_npm_lockfile(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json with a non-npm lockfile (e.g. pnpm-lock.yaml) is skipped without running npm."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        assert_success(update_package_jsons())
        mock_run.assert_not_called()
        mock_package_json.write_text.assert_not_called()
        self.assert_skipped_logged(mock_package_json, "pnpm is not supported, only npm")
        self.assert_no_warnings_logged()
