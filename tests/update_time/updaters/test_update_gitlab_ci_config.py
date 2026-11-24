"""Unit tests for the GitLab CI config update script."""

from typing import override
from unittest.mock import Mock, patch

from update_time.primitives.location import Location
from update_time.updaters.update_gitlab_ci_config import _update_gitlab_ci_config

from tests.file_mocks import mock_path
from tests.update_time import registry
from tests.update_time.fixtures import DIGEST
from tests.update_time.registry import mock_docker_registry
from tests.update_time.sources.docker_hub_answers import docker_tag


class UpdateGitLabCIConfigTest(registry.ImageUpdaterTestMixin):
    """Unit tests for the update GitLab CI config function."""

    def test_pin_tagless_image(self):
        """Test that an `image:` naming no tag is pinned to the version and digest `latest` serves."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        config = mock_path(self.reference("python"))
        self.run_updater(config)
        config.write_text.assert_called_once_with(self.reference(f"python:3.14.7@{DIGEST}"))
        self.assert_pinned_logged("python", "3.14.7", DIGEST, Location(config, 1))
        self.assert_no_warnings_logged()

    @override
    def reference(self, image: str) -> str:
        """Return a GitLab CI `image:` line for the image."""
        return f"image: {image}\n"

    @override
    def run_updater(self, mock_file: Mock) -> None:
        """Run the GitLab CI updater on the mock config file."""
        with patch("update_time.updaters.update_gitlab_ci_config.glob_for", return_value=[mock_file]):
            _update_gitlab_ci_config()

    def test_missing_config_file(self):
        """Test that a repository without a .gitlab-ci.yml is handled gracefully."""
        with patch("update_time.updaters.update_gitlab_ci_config.glob_for", return_value=[]):
            _update_gitlab_ci_config()
        self.requests.assert_not_called()
        self.assert_no_debug_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()
