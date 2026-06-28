"""Unit tests for the Circle CI config update script."""

from pathlib import Path
from unittest.mock import Mock, patch

from update_time.updaters.update_circle_ci_config import update_circle_ci_config

from tests.update_time.assertions import assert_success
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2
from tests.update_time.helpers import LoggingTestCase, docker_hub_response, docker_tag, mock_docker_hub_auth, mock_path

CIRCLE_CI_DIR = Path("/repo/.circleci")


@patch("requests.get")
@mock_docker_hub_auth
@patch("pathlib.Path.glob")
class UpdateCircleCIConfigTest(LoggingTestCase):
    """Unit tests for the update Circle CI config function."""

    def test_no_changes(self, mock_glob: Mock, mock_get: Mock):
        """Test no changes."""
        mock_get.return_value = docker_hub_response()
        config_yml = mock_path(f"image: cimg/node:26.8@{DIGEST}\n")
        mock_glob.side_effect = [[config_yml], []]
        assert_success(update_circle_ci_config(CIRCLE_CI_DIR))
        config_yml.write_text.assert_not_called()
        self.assert_path_logged(config_yml.relative_to())
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_changes(self, mock_glob: Mock, mock_get: Mock):
        """Test the image tag and digest are bumped when a newer version is available."""
        mock_get.return_value = docker_hub_response(docker_tag("3.14.2", DIGEST2))
        config_yml = mock_path(f"image: cimg/py:3.14.1@{DIGEST1}\n")
        mock_glob.side_effect = [[config_yml], []]
        assert_success(update_circle_ci_config(CIRCLE_CI_DIR))
        config_yml.write_text.assert_called_with(f"image: cimg/py:3.14.2@{DIGEST2}\n")
        self.assert_path_logged(config_yml.relative_to())
        self.assert_new_version_logged("cimg/py", "3.14.2")
        self.assert_no_warnings_logged()

    def test_multiple_files(self, mock_glob: Mock, mock_get: Mock):
        """Test that images are updated in all YAML files under the CircleCI directory, not just config.yml."""
        mock_get.return_value = docker_hub_response(docker_tag("1.26.2", DIGEST2))
        config_yml = mock_path(f"image: cimg/go:1.26.1@{DIGEST1}\n")
        continue_yaml = mock_path(f"image: cimg/go:1.26.1@{DIGEST1}\n")
        mock_glob.side_effect = [[config_yml], [continue_yaml]]
        assert_success(update_circle_ci_config(CIRCLE_CI_DIR))
        config_yml.write_text.assert_called_with(f"image: cimg/go:1.26.2@{DIGEST2}\n")
        continue_yaml.write_text.assert_called_with(f"image: cimg/go:1.26.2@{DIGEST2}\n")
        self.assertEqual(2, self.mock_info.call_count)
        self.assert_path_logged(continue_yaml.relative_to())
        self.assert_new_version_logged("cimg/go", "1.26.2", "Suppressing changelog already shown, see above")
        self.assert_no_warnings_logged()

    def test_pin_unpinned_image(self, mock_glob: Mock, mock_get: Mock):
        """Test that an image referenced by tag only is automatically pinned with the latest tag and digest."""
        mock_get.return_value = docker_hub_response(docker_tag("1.76", DIGEST2))
        config_yml = mock_path("image: cimg/rust:1.75\n")
        mock_glob.side_effect = [[config_yml], []]
        assert_success(update_circle_ci_config(CIRCLE_CI_DIR))
        config_yml.write_text.assert_called_with(f"image: cimg/rust:1.76@{DIGEST2}\n")
        self.assert_path_logged(config_yml.relative_to())
        self.assert_new_version_logged("cimg/rust", "1.76")
        self.assert_no_warnings_logged()

    def test_machine_executor_alias_ignored(self, mock_glob: Mock, mock_get: Mock):
        """Test that machine-executor 'image: default' aliases without a tag are not modified."""
        mock_get.return_value = docker_hub_response(docker_tag("3.14.2", DIGEST))
        config_yml = mock_path("image: default\n")
        mock_glob.side_effect = [[config_yml], []]
        assert_success(update_circle_ci_config(CIRCLE_CI_DIR))
        config_yml.write_text.assert_not_called()
        self.assert_path_logged(config_yml.relative_to())
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()
