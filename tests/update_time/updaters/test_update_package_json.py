"""Unit tests for the package.json updater (discovery and orchestration of the node package managers)."""

import json
import subprocess  # nosec
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

from update_time.domain.cooldown import COOLDOWN
from update_time.primitives.command import Command
from update_time.primitives.location import Location
from update_time.updaters.update_package_json import update_package_jsons

from tests.helpers import mock_path, mock_response, patch_pathlib_path
from tests.update_time.helpers import LoggingTestCase, github_commits_json, github_release_json, staleness_disabled

_NPM_COOLDOWN_OPTION = f"--min-release-age={COOLDOWN.default}"  # the cooldown npm option Update-time adds by default
_PNPM_COOLDOWN_OPTION = f"--config.minimumReleaseAge={COOLDOWN.default * 24 * 60}"  # pnpm's, in minutes
_NPM_UNSET = "null\n"  # what `npm config get <cooldown key>` prints when the key is not set
_PNPM_UNSET = "undefined\n"  # what `pnpm config get minimumReleaseAge` prints when the key is not set


def _package_lookup_responses() -> Mock:
    """Return a `requests.get` mock for the npmjs/GitHub lookups that describe an update to version 1.1.

    After a manager reports a package updated to 1.1, Update-time resolves its changelog and push date via four
    sequential requests: the package's repository URL (npm registry), the GitHub releases (the changelog), the npm
    registry publication time, and the release's commit SHA. A fresh mock is returned per call so each decorated
    test gets its own unexhausted `side_effect`.
    """
    return Mock(
        side_effect=[
            mock_response({"repository": {"url": "https://github.com/package/1.1"}}),
            mock_response([github_release_json("v1.1", body="Changelog")]),
            mock_response({"time": {"1.1": "20260530T10:26:45.543Z"}}),
            mock_response(github_commits_json()),
        ]
    )


def _outdated_error(package: str, current: str, latest: str, **extra: object) -> subprocess.CalledProcessError:
    """Build the `CalledProcessError` npm/pnpm `outdated` raises for an available update (exit 1 with a JSON body).

    Extra per-package fields (e.g. pnpm's `dependencyType`) are merged into the package's object.
    """
    output = json.dumps({package: {"current": current, "latest": latest, **extra}})
    return subprocess.CalledProcessError(cmd="", returncode=1, output=output, stderr="")


def _assert_manager_called(mock_run: Mock, outdated: Command, update: Command, list_cmd: Command) -> None:
    """Assert the manager's outdated, update, and list commands ran in order, each with the shared run kwargs."""
    run_kwargs = {"capture_output": True, "text": True, "check": True, "cwd": Path("/")}
    mock_run.assert_has_calls((call(outdated, **run_kwargs), call(update, **run_kwargs), call(list_cmd, **run_kwargs)))


# Staleness makes its own npm registry requests; it is disabled here and covered by StaleDependencyTest below.
@staleness_disabled
@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("subprocess.run")
class UpdateNpmPackageJsonTest(LoggingTestCase):
    """Unit tests for updating an npm-managed package.json."""

    def create_package_json(self, contents: str = "{}") -> Mock:
        """Create a mock package.json file."""
        return mock_path(contents, parent=Path("/"))

    def npm_runs(self, *results: object) -> list:
        """Prepend the two `npm config get` cooldown probes (both unset) to the given npm command results."""
        return [Mock(stdout=_NPM_UNSET), Mock(stdout=_NPM_UNSET), *results]

    def assert_npm_called(self, mock_run: Mock, *, cooldown: bool = True) -> None:
        """Assert npm outdated, update, and list were called (with the cooldown option when expected)."""
        cooldown_option = [_NPM_COOLDOWN_OPTION] if cooldown else []
        _assert_manager_called(
            mock_run,
            Command("npm", "outdated", "--json", "--include=dev", "--silent", *cooldown_option),
            Command("npm", "update", "--save", "--include=dev", "--silent", *cooldown_option),
            Command("npm", "list", "--json", "--depth=0", "--include=dev", "--silent"),
        )

    def test_unchanged(self, mock_run: Mock, mock_glob: Mock):
        """Test that the package.json is not written if there are no outdated packages."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.npm_runs(Mock(stdout="{}"), Mock(stdout=""), Mock(stdout='{"dependencies": {}}'))
        update_package_jsons()
        self.assert_npm_called(mock_run)
        self.assert_path_logged(mock_package_json)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch("requests.get", _package_lookup_responses())
    def test_update(self, mock_run: Mock, mock_glob: Mock):
        """Test that the installed version is logged, even when it is older than the latest version."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        # npm outdated results in a subprocess.CalledProcessError if there are updates. npm installs 1.1 rather than
        # the latest 1.2, for example because min-release-age holds back the fresh 1.2 release:
        mock_run.side_effect = self.npm_runs(
            _outdated_error("package", "1.0", "1.2"),
            Mock(stdout=""),
            Mock(stdout='{"dependencies": {"package": {"version": "1.1"}}}'),
        )
        update_package_jsons()
        self.assert_npm_called(mock_run)
        self.assert_path_logged(mock_package_json)
        self.assert_new_version_logged(
            "package", "1.1, published: 2026-05-30 10:26", Location(mock_package_json), "Changelog"
        )
        self.assert_no_warnings_logged()

    @patch("requests.get", _package_lookup_responses())
    def test_new_version_is_logged_at_the_line_declaring_the_dependency(self, mock_run: Mock, mock_glob: Mock):
        """Test that the new version is logged at the line declaring the dependency npm updated."""
        mock_package_json = self.create_package_json('{\n  "dependencies": {\n    "package": "^1.0.0"\n  }\n}\n')
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.npm_runs(
            _outdated_error("package", "1.0", "1.1"),
            Mock(stdout=""),
            Mock(stdout='{"dependencies": {"package": {"version": "1.1"}}}'),
        )
        update_package_jsons()
        self.assert_new_version_logged(
            "package", "1.1, published: 2026-05-30 10:26", Location(mock_package_json, 3), "Changelog"
        )

    @patch("requests.get", _package_lookup_responses())
    def test_new_version_is_logged_at_each_line_declaring_the_dependency(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency two sections declare has its new version logged at each of those lines."""
        contents = (
            "{\n"
            '  "dependencies": {\n    "package": "^1.0.0"\n  },\n'
            '  "devDependencies": {\n    "package": "^1.0.0"\n  }\n'
            "}\n"
        )
        mock_package_json = self.create_package_json(contents)
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.npm_runs(
            _outdated_error("package", "1.0", "1.1"),
            Mock(stdout=""),
            Mock(stdout='{"dependencies": {"package": {"version": "1.1"}}}'),
        )
        update_package_jsons()
        published = "1.1, published: 2026-05-30 10:26"
        self.assert_new_version_logged_among_others("package", published, Location(mock_package_json, 3), ANY)
        self.assert_new_version_logged_among_others("package", published, Location(mock_package_json, 6), ANY)

    @patch("requests.get", _package_lookup_responses())
    def test_new_version_of_a_dependency_the_manifest_declares_without_a_registry_spec(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a new version for a dependency that resolves to no registry release is logged at the file."""
        contents = '{\n  "dependencies": {\n    "package": "git+https://github.com/org/package.git"\n  }\n}\n'
        mock_package_json = self.create_package_json(contents)
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.npm_runs(
            _outdated_error("package", "1.0", "1.1"),
            Mock(stdout=""),
            Mock(stdout='{"dependencies": {"package": {"version": "1.1"}}}'),
        )
        update_package_jsons()
        self.assert_new_version_logged(
            "package", "1.1, published: 2026-05-30 10:26", Location(mock_package_json), "Changelog"
        )

    def test_restore_git_url_dependencies(self, mock_run: Mock, mock_glob: Mock):
        """Test that git+https URLs npm normalized to the github: shorthand are restored."""
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
        # Four reads: package_manager detection, the original snapshot, then the npm-normalized contents twice —
        # once to locate the declarations, once to compare against the snapshot.
        mock_package_json.read_text = Mock(side_effect=[original, original, normalized, normalized])
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.npm_runs(Mock(stdout="{}"), Mock(stdout=""), Mock(stdout='{"dependencies": {}}'))
        update_package_jsons()
        mock_package_json.write_text.assert_called_once_with(original)
        self.assert_npm_called(mock_run)
        self.assert_path_logged(mock_package_json)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch("requests.get", _package_lookup_responses())
    def test_manifest_kept_when_a_dependency_is_updated(self, mock_run: Mock, mock_glob: Mock):
        """Test that npm's manifest (including any specs it normalized) is kept when a dependency is updated."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.npm_runs(
            _outdated_error("package", "1.0", "1.1"),
            Mock(stdout=""),
            Mock(stdout='{"dependencies": {"package": {"version": "1.1"}}}'),
        )
        update_package_jsons()
        mock_package_json.write_text.assert_not_called()
        self.assert_npm_called(mock_run)
        self.assert_path_logged(mock_package_json)
        self.assert_new_version_logged(
            "package", "1.1, published: 2026-05-30 10:26", Location(mock_package_json), "Changelog"
        )
        self.assert_no_warnings_logged()

    def test_outdated_but_not_updated(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package is not logged when npm update did not change its installed version."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.npm_runs(
            _outdated_error("held", "1.0", "1.2"),
            Mock(stdout=""),
            # "held" stayed at 1.0 (e.g. min-release-age held back every newer release), "untracked" has no version:
            Mock(stdout='{"dependencies": {"held": {"version": "1.0"}, "untracked": {"missing": true}}}'),
        )
        update_package_jsons()
        self.assert_npm_called(mock_run)
        self.assert_path_logged(mock_package_json)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_empty_npm_output(self, mock_run: Mock, mock_glob: Mock):
        """Test that empty npm output (e.g. from a failed command) is treated as no data rather than crashing."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        # npm outdated and npm list both return nothing, as a failed command does (its failure is logged by `run`):
        mock_run.side_effect = self.npm_runs(Mock(stdout=""), Mock(stdout=""), Mock(stdout=""))
        update_package_jsons()
        self.assert_npm_called(mock_run)
        mock_package_json.write_text.assert_not_called()
        self.assert_no_new_version_logged()

    def test_outdated_failure_skips_update_and_list(self, mock_run: Mock, mock_glob: Mock):
        """Test that a failed outdated check (e.g. offline) skips the futile update and list, logging only itself."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        # `npm outdated` fails with no output (as an offline registry does); a normal non-zero exit still has JSON.
        mock_run.side_effect = self.npm_runs(
            subprocess.CalledProcessError(cmd="", returncode=1, output="", stderr="error: offline"),
        )
        update_package_jsons()
        commands = [call.args[0][:2] for call in mock_run.call_args_list]
        self.assertIn(("npm", "outdated"), commands)
        self.assertNotIn(("npm", "update"), commands)  # skipped because outdated failed
        self.assertNotIn(("npm", "list"), commands)  # skipped because outdated failed
        mock_package_json.write_text.assert_not_called()
        self.assert_command_stderr_logged(stderr="error: offline")  # only the outdated failure is surfaced

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
        update_package_jsons()
        self.assert_npm_called(mock_run, cooldown=False)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


@staleness_disabled
@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("subprocess.run")
class UpdatePnpmPackageJsonTest(LoggingTestCase):
    """Unit tests for updating a pnpm-managed package.json (detected via the corepack `packageManager` field)."""

    def create_package_json(self, contents: str = '{"packageManager": "pnpm@9.15.0"}') -> Mock:
        """Create a mock package.json declaring pnpm as its package manager."""
        return mock_path(contents, parent=Path("/"))

    def pnpm_runs(self, *results: object) -> list:
        """Prepend the `pnpm config get minimumReleaseAge` cooldown probe (unset) to the given command results."""
        return [Mock(stdout=_PNPM_UNSET), *results]

    def assert_pnpm_called(self, mock_run: Mock, *, cooldown: bool = True) -> None:
        """Assert pnpm outdated, update, and list were called (with the cooldown option when expected)."""
        cooldown_option = [_PNPM_COOLDOWN_OPTION] if cooldown else []
        _assert_manager_called(
            mock_run,
            Command("pnpm", "outdated", "--format", "json", *cooldown_option),
            Command("pnpm", "update", *cooldown_option),
            Command("pnpm", "list", "--json", "--depth=0"),
        )

    def test_unchanged(self, mock_run: Mock, mock_glob: Mock):
        """Test that the package.json is not written if there are no outdated packages."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.pnpm_runs(Mock(stdout="{}"), Mock(stdout=""), Mock(stdout="[]"))
        update_package_jsons()
        self.assert_pnpm_called(mock_run)
        self.assert_path_logged(mock_package_json)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch("requests.get", _package_lookup_responses())
    def test_update(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dev dependency's installed version is logged, using pnpm's list and outdated JSON shapes."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        # pnpm outdated exits 1 when there are updates; pnpm installs 1.1 rather than the latest 1.2 (e.g. because
        # minimumReleaseAge holds back the fresh 1.2 release). pnpm list returns a list of projects and splits
        # dependencies over dependencies/devDependencies:
        mock_run.side_effect = self.pnpm_runs(
            _outdated_error("package", "1.0", "1.2", dependencyType="devDependencies"),
            Mock(stdout=""),
            Mock(stdout='[{"name": "root", "devDependencies": {"package": {"version": "1.1"}}}]'),
        )
        update_package_jsons()
        self.assert_pnpm_called(mock_run)
        self.assert_path_logged(mock_package_json)
        self.assert_new_version_logged(
            "package", "1.1, published: 2026-05-30 10:26", Location(mock_package_json), "Changelog"
        )
        self.assert_no_warnings_logged()

    def test_outdated_but_not_updated(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package is not logged when pnpm update did not change its installed version."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.pnpm_runs(
            _outdated_error("held", "1.0", "1.2"),
            Mock(stdout=""),
            # "held" stayed at 1.0 (e.g. minimumReleaseAge held back every newer release), "untracked" has no version:
            Mock(
                stdout='[{"name": "root", "dependencies": {"held": {"version": "1.0"}}, '
                '"devDependencies": {"untracked": {"from": "untracked"}}}]'
            ),
        )
        update_package_jsons()
        self.assert_pnpm_called(mock_run)
        self.assert_path_logged(mock_package_json)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True)
    def test_pnpm_detected_via_lockfile(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json with a sibling pnpm-lock.yaml (and no packageManager field) uses pnpm."""
        mock_package_json = self.create_package_json("{}")  # No packageManager field; detection falls to the lockfile.
        mock_glob.return_value = [mock_package_json]
        mock_run.side_effect = self.pnpm_runs(Mock(stdout="{}"), Mock(stdout=""), Mock(stdout="[]"))
        update_package_jsons()
        self.assert_pnpm_called(mock_run)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_project_cooldown_respected(self, mock_run: Mock, mock_glob: Mock):
        """Test that no cooldown option is added when the project's pnpm config already sets minimumReleaseAge."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        # The cooldown probe returns a value, so Update-time adds no cooldown of its own:
        mock_run.side_effect = [Mock(stdout="4320\n"), Mock(stdout="{}"), Mock(stdout=""), Mock(stdout="[]")]
        update_package_jsons()
        self.assert_pnpm_called(mock_run, cooldown=False)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("subprocess.run")
class SkipUnsupportedPackageManagerTest(LoggingTestCase):
    """Unit tests for skipping package.json files managed by an unsupported package manager (yarn, bun)."""

    def test_skip_unsupported_package_manager(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json with an unsupported `packageManager` field is skipped without running anything."""
        mock_package_json = mock_path('{"packageManager": "yarn@4.5.0"}', parent=Path("/"))
        mock_glob.return_value = [mock_package_json]
        update_package_jsons()
        mock_run.assert_not_called()
        mock_package_json.write_text.assert_not_called()
        self.assert_unsupported_package_manager_logged(mock_package_json, "yarn", "npm and pnpm")
        self.assert_no_new_version_logged()

    def test_skip_unsupported_lockfile(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json with an unsupported lockfile (e.g. yarn.lock) is skipped without running it."""
        mock_package_json = mock_path("{}", parent=Path("/"))
        mock_glob.return_value = [mock_package_json]
        # Only yarn.lock exists next to the manifest (no pnpm-lock.yaml), so yarn is detected and the file skipped:
        with patch("pathlib.Path.exists", lambda self: self.name == "yarn.lock"):
            update_package_jsons()
        mock_run.assert_not_called()
        mock_package_json.write_text.assert_not_called()
        self.assert_unsupported_package_manager_logged(mock_package_json, "yarn", "npm and pnpm")
        self.assert_no_new_version_logged()


@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("subprocess.run")
class StaleDependencyTest(LoggingTestCase):
    """Unit tests for the package.json staleness check, which makes its own npm registry pass over the deps."""

    @staticmethod
    def stub_no_update(mock_run: Mock) -> None:
        """Stub npm to report no updates: two unset cooldown probes, then empty outdated/update/list."""
        mock_run.side_effect = [
            Mock(stdout=_NPM_UNSET),
            Mock(stdout=_NPM_UNSET),
            Mock(stdout="{}"),
            Mock(stdout=""),
            Mock(stdout='{"dependencies": {}}'),
        ]

    @staticmethod
    def registry_doc(version: str, published: str) -> Mock:
        """Mock the npm registry document dating the version it names."""
        return mock_response({"time": {version: published}})

    def package_json(self, glob: Mock) -> Mock:
        """Discover a single mock package.json depending on `clipboard`, whose entry sits on line 4."""
        contents = '{\n  "name": "app",\n  "dependencies": {\n    "clipboard": "^2.0.11"\n  }\n}\n'
        package_json = mock_path(contents, parent=Path("/"))
        glob.return_value = [package_json]
        return package_json

    @patch("requests.get")
    def test_stale_dependency_warned(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a direct dependency whose newest release is old is warned about, at the line it is declared on."""
        self.stub_no_update(mock_run)
        get.return_value = self.registry_doc("2.0.11", (datetime.now(UTC) - timedelta(days=512)).isoformat())
        package_json = self.package_json(glob)
        update_package_jsons()
        self.assert_stale_dependency_logged("clipboard", "2.0.11", Location(package_json, 4))

    @patch("requests.get")
    def test_dependency_declared_in_two_sections(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a dependency two sections declare is warned about at each of the lines declaring it."""
        self.stub_no_update(mock_run)
        get.return_value = self.registry_doc("2.0.11", (datetime.now(UTC) - timedelta(days=512)).isoformat())
        contents = (
            "{\n"
            '  "dependencies": {\n    "clipboard": "^2.0.11"\n  },\n'
            '  "devDependencies": {\n    "clipboard": "^2.0.11"\n  }\n'
            "}\n"
        )
        package_json = mock_path(contents, parent=Path("/"))
        glob.return_value = [package_json]
        update_package_jsons()
        self.assert_stale_dependency_logged("clipboard", "2.0.11", Location(package_json, 3), Location(package_json, 6))

    @patch("requests.get")
    def test_recent_dependency_not_warned(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a direct dependency whose newest release is recent is not warned about as stale."""
        self.stub_no_update(mock_run)
        get.return_value = self.registry_doc("2.0.11", datetime.now(UTC).isoformat())
        self.package_json(glob)
        update_package_jsons()
        self.assert_no_warnings_logged()

    @patch("requests.get")
    def test_dependency_without_release_skipped(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a declared dependency the registry has no release for is skipped without warning or crashing."""
        self.stub_no_update(mock_run)
        get.return_value = mock_response({})  # the document dates no version, so newest_release returns None
        self.package_json(glob)
        update_package_jsons()
        self.assert_no_warnings_logged()

    @staleness_disabled
    @patch("requests.get")
    def test_disabled_makes_no_registry_request(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that `--stale-after 0` skips the staleness pass entirely, so it makes no npm registry request."""
        self.stub_no_update(mock_run)
        self.package_json(glob)
        update_package_jsons()
        get.assert_not_called()
        self.assert_no_warnings_logged()
