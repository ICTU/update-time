"""Unit tests for the devcontainer update script."""

import unittest
from unittest.mock import ANY, Mock, patch

from update_time.domain.file_type import DEVCONTAINER_CONFIGS
from update_time.primitives.location import Location
from update_time.updaters import update_devcontainer
from update_time.updaters.update_devcontainer import update_devcontainers

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time import registry
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST3
from tests.update_time.helpers import docker_tag, mock_docker_hub_auth
from tests.update_time.registry import mock_docker_registry


@mock_docker_hub_auth
class UpdateDevcontainerTest(registry.ImageUpdaterTestMixin):
    """Unit tests for the update devcontainer function."""

    def reference(self, image: str) -> str:
        """Return a devcontainer `"image"` value for the image."""
        return f'"image": "{image}"\n'

    def marker_line(self, directive: str) -> str:
        """Return the directive as a marker in the `//` comment devcontainer.json takes, being JSONC."""
        return f"// update-time: {directive}\n"

    def run_updater(self, mock_file: Mock) -> None:
        """Run the devcontainer updater with the mock file as the only discovered devcontainer.json."""
        with patch("update_time.updaters.update_devcontainer.glob_for", return_value=[mock_file]):
            update_devcontainers()

    def test_feature_updated_and_pinned(self):
        """Test that a feature key (an OCI reference) is bumped and pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST1))
        devcontainer = mock_path('    "ghcr.io/devcontainers/features/node:1": {}\n')
        self.run_updater(devcontainer)
        devcontainer.write_text.assert_called_once_with(
            f'    "ghcr.io/devcontainers/features/node:2.1@{DIGEST1}": {{}}\n'
        )
        self.assert_new_version_logged("ghcr.io/devcontainers/features/node", "2.1", Location(devcontainer, 1))
        self.assert_no_warnings_logged()

    def test_feature_pinned_when_already_latest(self):
        """Test that an unpinned feature that is already at the latest version is still pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2", DIGEST3))
        devcontainer = mock_path('    "ghcr.io/devcontainers/features/node:2": {}\n')
        self.run_updater(devcontainer)
        devcontainer.write_text.assert_called_once_with(
            f'    "ghcr.io/devcontainers/features/node:2@{DIGEST3}": {{}}\n'
        )
        self.assert_pinned_logged("ghcr.io/devcontainers/features/node", "2", DIGEST3, Location(devcontainer, 1))
        self.assert_no_warnings_logged()

    def test_pin_tagless_image(self):
        """Test that an `"image"` naming no tag is pinned to the version and digest `latest` serves."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        devcontainer = mock_path(self.reference("python"))
        self.run_updater(devcontainer)
        devcontainer.write_text.assert_called_once_with(self.reference(f"python:3.14.7@{DIGEST}"))
        self.assert_pinned_logged("python", "3.14.7", DIGEST, Location(devcontainer, 1))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_devcontainer,
            r"""_FEATURE_RE = rf'"{IMAGE_REFERENCE}":\s*{{'""",
            r"""_FEATURE_RE = r'"(?P<dependency>[\w./-]+)(?::(?=[\w.-]))?(?P<version>[\w.-]*)"""
            r"""(?:@(?P<sha>sha256:[0-9a-f]{64}))?":\s*{'""",
            "a JSON key naming no version is read as a feature reference without a tag",
        )
    )
    def test_json_key_naming_no_version_is_not_read_as_a_feature(self):
        """Test that a JSON key naming no version is left alone, a feature reference always naming one."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        devcontainer = mock_path('  "customizations": {\n')
        self.run_updater(devcontainer)
        devcontainer.write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_no_warnings_logged()

    def test_non_image_references_left_alone(self):
        """Test that Dockerfile builds, ports, and other `name:version`-looking values are not treated as images."""
        self.requests.side_effect = mock_docker_registry(docker_tag("9.9", DIGEST))
        devcontainer = mock_path(
            '  "build": { "dockerfile": "Dockerfile" },\n  "appPort": "3000:3000",\n  "forwardPorts": [3000]\n'
        )
        self.run_updater(devcontainer)
        devcontainer.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


class ScannedDevcontainersTest(unittest.TestCase):
    """Unit tests for which devcontainer files are scanned and which references are updated in them."""

    @patch("update_time.updaters.update_devcontainer.glob_for")
    def test_standard_locations_are_scanned(self, mock_glob_for: Mock):
        """Test that the top-level, `.devcontainer/`, and per-configuration subfolder locations are all scanned."""
        mock_glob_for.return_value = []
        update_devcontainers()
        mock_glob_for.assert_called_once_with(DEVCONTAINER_CONFIGS)
        locations = ".devcontainer.json", ".devcontainer/devcontainer.json", ".devcontainer/*/devcontainer.json"
        self.assertEqual(DEVCONTAINER_CONFIGS.patterns, locations)

    @patch("update_time.updaters.update_devcontainer.update_file", return_value=0)
    @patch("update_time.updaters.update_devcontainer.glob_for")
    def test_image_and_features_are_updated(self, mock_glob: Mock, mock_update_file: Mock):
        """Test that each devcontainer.json is scanned for both its image and its feature references in one pass."""
        mock_file = mock_path("{}")
        mock_glob.return_value = [mock_file]
        update_devcontainers()
        mock_update_file.assert_called_once_with(mock_file, ANY, ANY, get_new_version=ANY, logger=ANY)
        image_pattern, feature_pattern = mock_update_file.call_args.args[1:3]
        self.assertRegex('"image": "python:3.12"', image_pattern)
        self.assertRegex('"ghcr.io/devcontainers/features/node:1": {', feature_pattern)
