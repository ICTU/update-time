"""Unit tests for the devcontainer update script."""

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from update_time.updaters.update_devcontainer import (
    DEVCONTAINER_GLOBS,
    FEATURE_RE,
    IMAGE_RE,
    update_devcontainers,
)

from tests.update_time.assertions import assert_success
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2, DIGEST3
from tests.update_time.helpers import (
    LoggingTestCase,
    RegistryRequestsMixin,
    docker_tag,
    mock_docker_hub_auth,
    mock_docker_registry,
    mock_path,
)


@patch("pathlib.Path.cwd", Mock(return_value=Path("/")))
@patch("pathlib.Path.glob")
@mock_docker_hub_auth
class UpdateDevcontainerTest(RegistryRequestsMixin, LoggingTestCase):
    """Unit tests for the update devcontainer function."""

    def create_devcontainer(self, mock_glob: Mock, contents: str) -> Mock:
        """Create a mock devcontainer.json file, discovered via the `.devcontainer/devcontainer.json` glob.

        Returning it for a single glob pattern (and nothing for the others) avoids processing the same file twice.
        """
        mock_devcontainer = mock_path(contents)
        pattern = ".devcontainer/devcontainer.json"
        mock_glob.side_effect = lambda glob: [mock_devcontainer] if glob == pattern else []
        return mock_devcontainer

    def test_image_updated_and_pinned(self, mock_glob: Mock):
        """Test that the base image tag is bumped and pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST2))
        devcontainer = self.create_devcontainer(
            mock_glob, '  "image": "mcr.microsoft.com/devcontainers/typescript-node:1.0"\n'
        )
        assert_success(update_devcontainers())
        devcontainer.write_text.assert_called_once_with(
            f'  "image": "mcr.microsoft.com/devcontainers/typescript-node:1.1@{DIGEST2}"\n'
        )
        self.assert_new_version_logged(devcontainer, "mcr.microsoft.com/devcontainers/typescript-node", "1.1")
        self.assert_no_warnings_logged()

    def test_feature_updated_and_pinned(self, mock_glob: Mock):
        """Test that a feature key (an OCI reference) is bumped and pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST1))
        devcontainer = self.create_devcontainer(mock_glob, '    "ghcr.io/devcontainers/features/node:1": {}\n')
        assert_success(update_devcontainers())
        devcontainer.write_text.assert_called_once_with(
            f'    "ghcr.io/devcontainers/features/node:2.1@{DIGEST1}": {{}}\n'
        )
        self.assert_new_version_logged(devcontainer, "ghcr.io/devcontainers/features/node", "2.1")
        self.assert_no_warnings_logged()

    def test_feature_pinned_when_already_latest(self, mock_glob: Mock):
        """Test that an unpinned feature that is already at the latest version is still pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2", DIGEST3))
        devcontainer = self.create_devcontainer(mock_glob, '    "ghcr.io/devcontainers/features/node:2": {}\n')
        assert_success(update_devcontainers())
        devcontainer.write_text.assert_called_once_with(
            f'    "ghcr.io/devcontainers/features/node:2@{DIGEST3}": {{}}\n'
        )
        self.assert_pinned_logged(devcontainer, "ghcr.io/devcontainers/features/node", "2", DIGEST3)
        self.assert_no_warnings_logged()

    def test_untagged_image_left_alone(self, mock_glob: Mock):
        """Test that an image without a version tag is left unchanged (there is no version to resolve)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        devcontainer = self.create_devcontainer(
            mock_glob, '  "image": "mcr.microsoft.com/devcontainers/typescript-node"\n'
        )
        assert_success(update_devcontainers())
        devcontainer.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_non_image_references_left_alone(self, mock_glob: Mock):
        """Test that Dockerfile builds, ports, and other `name:version`-looking values are not treated as images."""
        self.requests.side_effect = mock_docker_registry(docker_tag("9.9", DIGEST))
        devcontainer = self.create_devcontainer(
            mock_glob,
            '  "build": { "dockerfile": "Dockerfile" },\n  "appPort": "3000:3000",\n  "forwardPorts": [3000]\n',
        )
        assert_success(update_devcontainers())
        devcontainer.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


@patch("pathlib.Path.cwd", Mock(return_value=Path("/")))
@patch("pathlib.Path.glob")
class ScannedDevcontainersTest(unittest.TestCase):
    """Unit tests for which devcontainer files are scanned and which references are updated in them."""

    def test_standard_locations_are_scanned(self, mock_glob: Mock):
        """Test that the top-level, `.devcontainer/`, and per-configuration subfolder locations are all scanned."""
        mock_glob.return_value = []
        update_devcontainers()
        scanned = [call.args[0] for call in mock_glob.call_args_list]
        self.assertEqual(list(DEVCONTAINER_GLOBS), scanned)

    @patch("update_time.updaters.update_devcontainer.update_file", return_value=0)
    def test_image_and_features_are_updated(self, mock_update_file: Mock, mock_glob: Mock):
        """Test that each devcontainer.json is scanned for both its image and its feature references."""
        devcontainer = mock_path("{}")
        mock_glob.side_effect = lambda glob: [devcontainer] if glob == ".devcontainer.json" else []
        update_devcontainers()
        regexes = [call.args[1] for call in mock_update_file.call_args_list]
        self.assertEqual([IMAGE_RE, FEATURE_RE], regexes)
