"""Unit tests for the manifest image update script."""

import unittest
from unittest.mock import Mock, patch

from update_time.io.filesystem import YAML_GLOB_PATTERNS
from update_time.updaters.update_manifest_images import update_manifest_images

from tests.update_time import helpers
from tests.update_time.assertions import assert_success
from tests.update_time.fixtures import DIGEST
from tests.update_time.helpers import docker_tag, mock_docker_hub_auth, mock_docker_registry, mock_path


@mock_docker_hub_auth
class UpdateManifestImagesTest(helpers.ImageUpdaterTestMixin):
    """Unit tests for the update manifest images function."""

    def reference(self, image: str) -> str:
        """Return a Docker Compose / Helm `image:` line for the image."""
        return f"image: {image}\n"

    def run_updater(self, mock_file: Mock) -> int:
        """Run the manifest updater with the mock file as the only Docker Compose file.

        update_manifest_images globs the Compose pattern and then the Helm YAML patterns; returning the file for the
        Compose pattern only (and nothing for the Helm ones) processes it exactly once.
        """

        def rglob(pattern: str) -> list[Mock]:
            return [mock_file] if pattern == "docker-compose*.yml" else []

        with patch("pathlib.Path.rglob", side_effect=rglob):
            return update_manifest_images()

    def test_variable_substitution_ignored(self):
        """Test that image tags using ${...} substitution are not modified."""
        self.requests.side_effect = mock_docker_registry(docker_tag("999.0", DIGEST))
        mock_manifest = mock_path(self.reference("ictu/quality-time_proxy:${QUALITY_TIME_VERSION}"))
        assert_success(self.run_updater(mock_manifest))
        mock_manifest.write_text.assert_not_called()
        self.assert_path_logged(mock_manifest)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


@patch("update_time.updaters.update_manifest_images.update_files", return_value=0)
class ScannedManifestsTest(unittest.TestCase):
    """Unit tests for which manifest files are scanned for pinned images."""

    def test_docker_compose_files_are_scanned(self, mock_update_files: Mock):
        """Test that the Docker Compose files are scanned from the repository root."""
        update_manifest_images()
        compose_call = mock_update_files.call_args_list[0]
        self.assertIn("docker-compose*.yml", compose_call.args)
        self.assertIsNone(compose_call.kwargs.get("start"))

    def test_helm_yaml_files_are_scanned(self, mock_update_files: Mock):
        """Test all YAML files in the Helm folder are scanned, so pinned images stay in sync with Docker Compose."""
        update_manifest_images()
        helm_call = mock_update_files.call_args_list[1]
        self.assertEqual(YAML_GLOB_PATTERNS, helm_call.args)
        self.assertEqual("helm", helm_call.kwargs["start"].name)
