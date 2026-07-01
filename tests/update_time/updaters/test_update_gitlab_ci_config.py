"""Unit tests for the GitLab CI config update script."""

from unittest.mock import Mock

from update_time.updaters.update_gitlab_ci_config import update_gitlab_ci_config

from tests.update_time import helpers
from tests.update_time.assertions import assert_success
from tests.update_time.fixtures import DIGEST
from tests.update_time.helpers import docker_tag, mock_docker_hub_auth, mock_docker_registry, mock_path


@mock_docker_hub_auth
class UpdateGitLabCIConfigTest(helpers.ImageUpdaterTestMixin):
    """Unit tests for the update GitLab CI config function."""

    def reference(self, image: str) -> str:
        """Return a GitLab CI `image:` line for the image."""
        return f"image: {image}\n"

    def run_updater(self, mock_file: Mock) -> int:
        """Run the GitLab CI updater on the mock config file (it is addressed directly, not discovered)."""
        return update_gitlab_ci_config(mock_file)

    def test_variable_reference_ignored(self):
        """Test that an image referenced through variable substitution is not modified."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.2", DIGEST))
        config = mock_path("image: $CI_REGISTRY_IMAGE:${CI_COMMIT_TAG}\n")
        assert_success(self.run_updater(config))
        config.write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_path_logged(config)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_missing_config_file(self):
        """Test that a repository without a .gitlab-ci.yml is handled gracefully."""
        config = Mock(exists=Mock(return_value=False))
        assert_success(self.run_updater(config))
        config.read_text.assert_not_called()
        config.write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_no_path_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()
