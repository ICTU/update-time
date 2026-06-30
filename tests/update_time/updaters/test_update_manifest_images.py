"""Unit tests for the manifest image update script."""

import unittest
from unittest.mock import Mock, patch

from update_time.io.filesystem import YAML_GLOB_PATTERNS
from update_time.updaters.update_manifest_images import update_manifest_images

from tests.update_time.assertions import (
    assert_success,
)
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2, DIGEST3
from tests.update_time.helpers import (
    LoggingTestCase,
    RegistryRequestsMixin,
    docker_tag,
    mock_docker_hub_auth,
    mock_docker_registry,
    mock_path,
)


@patch("pathlib.Path.rglob")
@mock_docker_hub_auth
class UpdateManifestImagesTest(RegistryRequestsMixin, LoggingTestCase):
    """Unit tests for the update manifest images function."""

    def create_mock_manifest(self, mock_glob: Mock, image: str) -> Mock:
        """Create a mock manifest file with one image line and register it with the path glob mock.

        update_manifest_images globs multiple patterns, so rglob is called once per pattern. A real file matches
        only one glob, so return the mock for the Compose pattern only to avoid processing the same file twice.
        """
        mock_manifest = mock_path(f"    image: {image}\n")
        mock_glob.side_effect = lambda pattern: [mock_manifest] if pattern == "docker-compose*.yml" else []
        return mock_manifest

    def test_no_changes(self, mock_glob: Mock):
        """Test no changes are made when no newer tag is available."""
        self.requests.side_effect = mock_docker_registry()
        mock_manifest = self.create_mock_manifest(mock_glob, f"mongo-express:1.0.2@{DIGEST}")
        assert_success(update_manifest_images())
        mock_manifest.write_text.assert_not_called()
        self.assert_path_logged(mock_manifest)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_changes(self, mock_glob: Mock):
        """Test the image tag and digest are bumped when a newer version is available."""
        self.requests.side_effect = mock_docker_registry(docker_tag("149.0.3", DIGEST2))
        mock_manifest = self.create_mock_manifest(mock_glob, f"selenium/standalone-firefox:149.0.2@{DIGEST1}")
        assert_success(update_manifest_images())
        mock_manifest.write_text.assert_called_with(f"    image: selenium/standalone-firefox:149.0.3@{DIGEST2}\n")
        self.assert_path_logged(mock_manifest)
        self.assert_new_version_logged(mock_manifest, "selenium/standalone-firefox", "149.0.3")
        self.assert_no_warnings_logged()

    def test_pin_unpinned_image(self, mock_glob: Mock):
        """Test that an image referenced by tag only is automatically pinned with the latest tag and digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("11.1.0", DIGEST2))
        mock_manifest = self.create_mock_manifest(mock_glob, "grafana/grafana:11.0.0")
        assert_success(update_manifest_images())
        mock_manifest.write_text.assert_called_with(f"    image: grafana/grafana:11.1.0@{DIGEST2}\n")
        self.assert_path_logged(mock_manifest)
        self.assert_new_version_logged(mock_manifest, "grafana/grafana", "11.1.0")
        self.assert_no_warnings_logged()

    def test_pin_unpinned_image_already_at_latest(self, mock_glob: Mock):
        """Test that an unpinned image that is already at the latest version is still pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("7.2.0", DIGEST3))
        mock_manifest = self.create_mock_manifest(mock_glob, "redis:7.2.0")
        assert_success(update_manifest_images())
        mock_manifest.write_text.assert_called_with(f"    image: redis:7.2.0@{DIGEST3}\n")
        self.assert_path_logged(mock_manifest)
        self.assert_pinned_logged(mock_manifest, "redis", "7.2.0", DIGEST3)
        self.assert_no_warnings_logged()

    def test_variable_substitution_ignored(self, mock_glob: Mock):
        """Test that image tags using ${...} substitution are not modified."""
        self.requests.side_effect = mock_docker_registry(docker_tag("999.0", DIGEST))
        mock_manifest = self.create_mock_manifest(mock_glob, "ictu/quality-time_proxy:${QUALITY_TIME_VERSION}")
        assert_success(update_manifest_images())
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
