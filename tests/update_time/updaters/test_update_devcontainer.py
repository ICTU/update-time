"""Unit tests for the devcontainer update script."""

import unittest
from unittest.mock import Mock, patch

from update_time.updaters.update_devcontainer import (
    DEVCONTAINER_GLOBS,
    FEATURE_RE,
    IMAGE_RE,
    update_devcontainers,
)

from tests.update_time import helpers
from tests.update_time.assertions import assert_success
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST3
from tests.update_time.helpers import docker_tag, mock_docker_hub_auth, mock_docker_registry, mock_path


@mock_docker_hub_auth
class UpdateDevcontainerTest(helpers.ImageUpdaterTestMixin):
    """Unit tests for the update devcontainer function."""

    def reference(self, image: str) -> str:
        """Return a devcontainer `"image"` value for the image."""
        return f'"image": "{image}"\n'

    def run_updater(self, mock_file: Mock) -> int:
        """Run the devcontainer updater with the mock file as the only discovered devcontainer.json."""
        with patch("update_time.updaters.update_devcontainer.glob", return_value=[mock_file]):
            return update_devcontainers()

    def test_feature_updated_and_pinned(self):
        """Test that a feature key (an OCI reference) is bumped and pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST1))
        devcontainer = mock_path('    "ghcr.io/devcontainers/features/node:1": {}\n')
        assert_success(self.run_updater(devcontainer))
        devcontainer.write_text.assert_called_once_with(
            f'    "ghcr.io/devcontainers/features/node:2.1@{DIGEST1}": {{}}\n'
        )
        self.assert_new_version_logged(devcontainer, "ghcr.io/devcontainers/features/node", "2.1")
        self.assert_no_warnings_logged()

    def test_feature_pinned_when_already_latest(self):
        """Test that an unpinned feature that is already at the latest version is still pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2", DIGEST3))
        devcontainer = mock_path('    "ghcr.io/devcontainers/features/node:2": {}\n')
        assert_success(self.run_updater(devcontainer))
        devcontainer.write_text.assert_called_once_with(
            f'    "ghcr.io/devcontainers/features/node:2@{DIGEST3}": {{}}\n'
        )
        self.assert_pinned_logged(devcontainer, "ghcr.io/devcontainers/features/node", "2", DIGEST3)
        self.assert_no_warnings_logged()

    def test_untagged_image_left_alone(self):
        """Test that an image without a version tag is left unchanged (there is no version to resolve)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        devcontainer = mock_path('  "image": "mcr.microsoft.com/devcontainers/typescript-node"\n')
        assert_success(self.run_updater(devcontainer))
        devcontainer.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_non_image_references_left_alone(self):
        """Test that Dockerfile builds, ports, and other `name:version`-looking values are not treated as images."""
        self.requests.side_effect = mock_docker_registry(docker_tag("9.9", DIGEST))
        devcontainer = mock_path(
            '  "build": { "dockerfile": "Dockerfile" },\n  "appPort": "3000:3000",\n  "forwardPorts": [3000]\n'
        )
        assert_success(self.run_updater(devcontainer))
        devcontainer.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


class ScannedDevcontainersTest(unittest.TestCase):
    """Unit tests for which devcontainer files are scanned and which references are updated in them."""

    @patch("update_time.updaters.update_devcontainer.glob")
    def test_standard_locations_are_scanned(self, mock_glob: Mock):
        """Test that the top-level, `.devcontainer/`, and per-configuration subfolder locations are all scanned."""
        mock_glob.return_value = []
        update_devcontainers()
        mock_glob.assert_called_once_with(*DEVCONTAINER_GLOBS)

    @patch("update_time.updaters.update_devcontainer.update_file", return_value=0)
    @patch("update_time.updaters.update_devcontainer.glob")
    def test_image_and_features_are_updated(self, mock_glob: Mock, mock_update_file: Mock):
        """Test that each devcontainer.json is scanned for both its image and its feature references."""
        mock_glob.return_value = [mock_path("{}")]
        update_devcontainers()
        regexes = [call.args[1] for call in mock_update_file.call_args_list]
        self.assertEqual([IMAGE_RE, FEATURE_RE], regexes)
