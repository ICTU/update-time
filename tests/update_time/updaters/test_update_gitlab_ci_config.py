"""Unit tests for the GitLab CI config update script."""

from unittest.mock import Mock, patch

from update_time.updaters.update_gitlab_ci_config import update_gitlab_ci_config

from tests.update_time.assertions import assert_success
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2
from tests.update_time.helpers import LoggingTestCase, docker_tag, mock_docker_hub_auth, mock_docker_registry, mock_path


@patch("requests.get")
@mock_docker_hub_auth
class UpdateGitLabCIConfigTest(LoggingTestCase):
    """Unit tests for the update GitLab CI config function."""

    def test_no_changes(self, mock_get: Mock):
        """Test that an image already on the latest pinned tag is left unchanged."""
        mock_get.side_effect = mock_docker_registry()
        config = mock_path(f"image: python:3.14@{DIGEST}\n")
        assert_success(update_gitlab_ci_config(config))
        config.write_text.assert_not_called()
        self.assert_path_logged(config.relative_to())
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_changes(self, mock_get: Mock):
        """Test that the image tag and digest are bumped when a newer version is available."""
        mock_get.side_effect = mock_docker_registry(docker_tag("3.14.2", DIGEST2))
        config = mock_path(f"image: python:3.14.1@{DIGEST1}\n")
        assert_success(update_gitlab_ci_config(config))
        config.write_text.assert_called_with(f"image: python:3.14.2@{DIGEST2}\n")
        self.assert_path_logged(config.relative_to())
        self.assert_new_version_logged("python", "3.14.2")
        self.assert_no_warnings_logged()

    def test_pin_unpinned_image(self, mock_get: Mock):
        """Test that an image referenced by tag only is automatically pinned with the latest tag and digest."""
        mock_get.side_effect = mock_docker_registry(docker_tag("1.76", DIGEST2))
        config = mock_path("image: rust:1.75\n")
        assert_success(update_gitlab_ci_config(config))
        config.write_text.assert_called_with(f"image: rust:1.76@{DIGEST2}\n")
        self.assert_path_logged(config.relative_to())
        self.assert_new_version_logged("rust", "1.76")
        self.assert_no_warnings_logged()

    def test_variable_reference_ignored(self, mock_get: Mock):
        """Test that an image referenced through variable substitution is not modified."""
        mock_get.side_effect = mock_docker_registry(docker_tag("3.14.2", DIGEST))
        config = mock_path("image: $CI_REGISTRY_IMAGE:${CI_COMMIT_TAG}\n")
        assert_success(update_gitlab_ci_config(config))
        config.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_path_logged(config.relative_to())
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_missing_config_file(self, mock_get: Mock):
        """Test that a repository without a .gitlab-ci.yml is handled gracefully."""
        config = Mock(exists=Mock(return_value=False))
        assert_success(update_gitlab_ci_config(config))
        config.read_text.assert_not_called()
        config.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_no_path_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()
