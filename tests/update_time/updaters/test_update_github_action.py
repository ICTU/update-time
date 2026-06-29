"""Unit tests for the GitHub Action update script."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import ANY, Mock, patch

import requests

from update_time.domain.version import DependencyVersion
from update_time.updaters.update_github_action import get_latest_version, update_github_actions

from tests.update_time.assertions import assert_success
from tests.update_time.helpers import LoggingTestCase, mock_path, mock_response, release_json


@patch("requests.get")
class UpdateGitHubActionTest(LoggingTestCase):
    """Unit tests for the get latest GitHub Action version function."""

    def test_unchanged(self, mock_get: Mock):
        """Test an unchanged version."""
        mock_get.side_effect = [
            mock_response([release_json("1.0", body="changelog")]),
            mock_response({"sha": "sha"}),
        ]
        latest_version = get_latest_version("docker/docker", "1.0")
        self.assertEqual("1.0", latest_version.version)
        self.assertEqual("changelog", latest_version.changes)

    def test_newer(self, mock_get: Mock):
        """Test an newer version."""
        mock_get.side_effect = [
            mock_response([release_json("1.1", body="changelog")]),
            mock_response({"sha": "sha"}),
        ]
        latest_version = get_latest_version("docker/hub", "1.0")
        self.assertEqual("1.1", latest_version.version)
        self.assertEqual("changelog", latest_version.changes)

    def test_publication_date(self, mock_get: Mock):
        """Test that the release's publication date is captured."""
        published = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        mock_get.side_effect = [
            mock_response([release_json("1.1", published_at=published)]),
            mock_response({"sha": "sha"}),
        ]
        self.assertEqual(datetime.fromisoformat(published), get_latest_version("docker/dated", "1.0").published)

    def test_older(self, mock_get: Mock):
        """Test an older version."""
        mock_get.side_effect = [
            mock_response([release_json("0.9")]),
            mock_response({"sha": "sha"}),
        ]
        self.assertEqual("1.0", get_latest_version("github/action", "1.0").version)

    def test_no_version(self, mock_get: Mock):
        """Test that an error is logged and the current version kept when there is no valid release."""
        mock_get.return_value = mock_response([])
        self.assertEqual("1.0", get_latest_version("docker/action", "1.0").version)
        self.mock_error.assert_called_once_with("No valid version found for %s", "docker/action", stacklevel=ANY)

    def test_no_commit_sha(self, mock_get: Mock):
        """Test that the version is not updated when the commit SHA can't be fetched for an eligible release."""
        mock_get.side_effect = [
            mock_response([release_json("1.1")]),
            Mock(raise_for_status=Mock(side_effect=requests.exceptions.HTTPError)),
        ]
        self.assertEqual("1.0", get_latest_version("docker/no-sha-action", "1.0").version)


GITHUB_DIR = Path("/repo/.github")
OLD_SHA = "a" * 40
NEW_SHA = "b" * 40


@patch("update_time.updaters.update_github_action.get_latest_version")
@patch("pathlib.Path.glob")
class UpdateGitHubActionsTest(LoggingTestCase):
    """Unit tests for the update GitHub Actions function."""

    def test_multiple_files(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that actions are updated in all YAML files under the GitHub directory, not just workflows."""
        mock_get_latest_version.return_value = DependencyVersion(version="1.1", sha=NEW_SHA)
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        composite_action_yaml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], [composite_action_yaml]]
        assert_success(update_github_actions(GITHUB_DIR))
        workflow_yml.write_text.assert_called_with(f"uses: action/action@{NEW_SHA} # v1.1\n")
        composite_action_yaml.write_text.assert_called_with(f"uses: action/action@{NEW_SHA} # v1.1\n")
        self.assert_path_logged(composite_action_yaml)
        self.assert_new_version_logged(
            composite_action_yaml,
            "action/action",
            "1.1",
            "Suppressing changelog already shown, see above",
        )
        self.assert_no_warnings_logged()

    def test_file_without_actions(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that YAML files without actions are left untouched."""
        dependabot_yml = mock_path("version: 2\n")
        mock_glob.side_effect = [[dependabot_yml], []]
        assert_success(update_github_actions(GITHUB_DIR))
        dependabot_yml.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_path_logged(dependabot_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_pinned_action_up_to_date(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an already pinned action that is up to date is left unchanged."""
        mock_get_latest_version.return_value = DependencyVersion(version="1.0", sha=OLD_SHA)
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], []]
        assert_success(update_github_actions(GITHUB_DIR))
        workflow_yml.write_text.assert_not_called()
        self.assert_path_logged(workflow_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_pin_unpinned_action(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an action referenced by version tag only is pinned to the commit SHA with a version comment."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.1.1", sha=NEW_SHA)
        workflow_yml = mock_path("uses: actions/checkout@v4\n")
        mock_glob.side_effect = [[workflow_yml], []]
        assert_success(update_github_actions(GITHUB_DIR))
        workflow_yml.write_text.assert_called_with(f"uses: actions/checkout@{NEW_SHA} # v4.1.1\n")
        mock_get_latest_version.assert_called_once_with("actions/checkout", "4")
        self.assert_path_logged(workflow_yml)
        self.assert_pinned_logged(workflow_yml, "actions/checkout", "4.1.1", NEW_SHA)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_unpinned_action_without_sha_is_left_alone(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an unpinned action is not changed when no commit SHA is available to pin it to."""
        mock_get_latest_version.return_value = DependencyVersion(version="4")
        workflow_yml = mock_path("uses: actions/checkout@v4\n")
        mock_glob.side_effect = [[workflow_yml], []]
        assert_success(update_github_actions(GITHUB_DIR))
        workflow_yml.write_text.assert_not_called()
        self.assert_path_logged(workflow_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_branch_reference_is_left_alone(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an action referenced by a branch (no resolvable version) is not touched."""
        workflow_yml = mock_path("uses: actions/checkout@main\n")
        mock_glob.side_effect = [[workflow_yml], []]
        assert_success(update_github_actions(GITHUB_DIR))
        workflow_yml.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_path_logged(workflow_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_v_prefixed_non_version_reference_is_left_alone(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a v-prefixed reference that isn't a version (e.g. a floating `@vnext` tag) is not touched."""
        workflow_yml = mock_path("uses: actions/checkout@vnext\n")
        mock_glob.side_effect = [[workflow_yml], []]
        assert_success(update_github_actions(GITHUB_DIR))
        workflow_yml.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_path_logged(workflow_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()
