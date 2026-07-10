"""Unit tests for the Circle CI config update script."""

from pathlib import Path
from unittest.mock import Mock, patch

from update_time.io.log import Logger
from update_time.updaters.update_circle_ci_config import update_circle_ci_config

from tests.update_time import registry
from tests.update_time.assertions import assert_success
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2
from tests.update_time.helpers import docker_tag, mock_docker_hub_auth, mock_path
from tests.update_time.registry import mock_docker_registry

CIRCLE_CI_DIR = Path("/repo/.circleci")


@mock_docker_hub_auth
class UpdateCircleCIConfigTest(registry.ImageUpdaterTestMixin):
    """Unit tests for the update Circle CI config function."""

    def reference(self, image: str) -> str:
        """Return a CircleCI `image:` line for the image."""
        return f"image: {image}\n"

    def run_updater(self, mock_file: Mock) -> int:
        """Run the CircleCI updater with the mock file as the only YAML file under the CircleCI directory."""
        with patch("pathlib.Path.glob", side_effect=[[mock_file], []]):
            return update_circle_ci_config(CIRCLE_CI_DIR)

    def test_multiple_files(self):
        """Test that images are updated in all YAML files under the CircleCI directory, not just config.yml."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.26.2", DIGEST2))
        config_yml = mock_path(f"image: cimg/go:1.26.1@{DIGEST1}\n")
        continue_yaml = mock_path(f"image: cimg/go:1.26.1@{DIGEST1}\n")
        with patch("pathlib.Path.glob", side_effect=[[config_yml], [continue_yaml]]):
            assert_success(update_circle_ci_config(CIRCLE_CI_DIR))
        config_yml.write_text.assert_called_with(f"image: cimg/go:1.26.2@{DIGEST2}\n")
        continue_yaml.write_text.assert_called_with(f"image: cimg/go:1.26.2@{DIGEST2}\n")
        self.assert_path_logged(continue_yaml)
        self.assert_new_version_logged(continue_yaml, "cimg/go", "1.26.2", Logger._SUPPRESSING_CHANGELOG, once=False)
        self.assert_no_warnings_logged()

    def test_machine_executor_alias_ignored(self):
        """Test that machine-executor 'image: default' aliases without a tag are not modified."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.2", DIGEST))
        config_yml = mock_path("image: default\n")
        assert_success(self.run_updater(config_yml))
        config_yml.write_text.assert_not_called()
        self.assert_path_logged(config_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_machine_image_skipped(self):
        """Test that a machine-executor image is left unchanged and not looked up on Docker Hub (no warning)."""
        config_yml = mock_path("jobs:\n  build:\n    machine:\n      image: ubuntu-2204:2024.01.1\n")
        assert_success(self.run_updater(config_yml))
        config_yml.write_text.assert_not_called()
        # The machine image is recognised by parsing the YAML, so no registry is queried for it.
        self.requests.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_docker_image_with_auth_before_image(self):
        """Test that a Docker image is updated even when its list item lists `auth:` before `image:`."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST2))
        config = "jobs:\n  build:\n    docker:\n      - auth:\n          username: u\n        image: cimg/python:3.13\n"
        config_yml = mock_path(config)
        assert_success(self.run_updater(config_yml))
        config_yml.write_text.assert_called_with(config.replace("cimg/python:3.13", f"cimg/python:3.14@{DIGEST2}"))
        self.assert_new_version_logged(config_yml, "cimg/python", "3.14")
        self.assert_no_warnings_logged()
