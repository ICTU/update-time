"""Unit tests for the Circle CI config update script."""

from pathlib import Path
from unittest.mock import Mock, patch

from update_time.io.log import Logger
from update_time.primitives.location import Location
from update_time.updaters.update_circle_ci_config import update_circle_ci_config

from tests.helpers import mock_path
from tests.update_time import registry
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2
from tests.update_time.helpers import docker_tag, mock_docker_hub_auth
from tests.update_time.registry import mock_docker_registry

_CIRCLE_CI_DIR = Path("/repo/.circleci")


@mock_docker_hub_auth
class UpdateCircleCIConfigTest(registry.ImageUpdaterTestMixin):
    """Unit tests for the update Circle CI config function."""

    def reference(self, image: str) -> str:
        """Return a CircleCI `image:` line for the image."""
        return f"image: {image}\n"

    def run_updater(self, mock_file: Mock) -> None:
        """Run the CircleCI updater with the mock file as the only YAML file under the CircleCI directory."""
        with patch("pathlib.Path.glob", side_effect=[[mock_file], []]):
            update_circle_ci_config(_CIRCLE_CI_DIR)

    def test_multiple_files(self):
        """Test that images are updated in all YAML files under the CircleCI directory, not just config.yml."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.26.2", DIGEST2))
        config_yml = mock_path(f"image: cimg/go:1.26.1@{DIGEST1}\n")
        next_yml = mock_path(f"image: cimg/go:1.26.1@{DIGEST1}\n")
        with patch("pathlib.Path.glob", side_effect=[[config_yml], [next_yml]]):
            update_circle_ci_config(_CIRCLE_CI_DIR)
        config_yml.write_text.assert_called_with(f"image: cimg/go:1.26.2@{DIGEST2}\n")
        next_yml.write_text.assert_called_with(f"image: cimg/go:1.26.2@{DIGEST2}\n")
        self.assert_path_logged(next_yml)
        self.assert_last_new_version_logged("cimg/go", "1.26.2", Location(next_yml, 1), Logger._SUPPRESSING_CHANGELOG)
        self.assert_no_warnings_logged()

    def test_machine_executor_alias_ignored(self):
        """Test that machine-executor 'image: default' aliases without a tag are not modified."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.2", DIGEST))
        config_yml = mock_path("image: default\n")
        self.run_updater(config_yml)
        config_yml.write_text.assert_not_called()
        self.assert_path_logged(config_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_machine_image_skipped(self):
        """Test that a machine-executor image is left unchanged and not looked up on Docker Hub (no warning)."""
        config_yml = mock_path("jobs:\n  build:\n    machine:\n      image: ubuntu-2204:2024.01.1\n")
        self.run_updater(config_yml)
        config_yml.write_text.assert_not_called()
        # The machine image is recognised by parsing the YAML, so no registry is queried for it.
        self.requests.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_cooldown_marker_on_a_machine_image_is_reported_as_redundant(self):
        """Test that a `cooldown` marker on a machine-executor image is reported, since no registry dates it."""
        marker = "      # update-time: ignore[cooldown<30]\n"
        config_yml = mock_path(f"jobs:\n  build:\n    machine:\n{marker}      image: ubuntu-2204:2024.01.1\n")
        self.run_updater(config_yml)
        config_yml.write_text.assert_not_called()
        self.assert_redundant_cooldown_item_logged("ubuntu-2204", Location(config_yml, 5), "ignore[cooldown<30]")

    def test_stale_marker_on_a_machine_image_is_reported_as_redundant(self):
        """Test that a `stale` marker on a machine-executor image is reported, since no registry dates it."""
        marker = "      # update-time: ignore[stale<90]\n"
        config_yml = mock_path(f"jobs:\n  build:\n    machine:\n{marker}      image: ubuntu-2204:2024.01.1\n")
        self.run_updater(config_yml)
        config_yml.write_text.assert_not_called()
        self.assert_redundant_stale_source_logged("ubuntu-2204", Location(config_yml, 5), "ignore[stale<90]")

    def test_docker_image_with_auth_before_image(self):
        """Test that a Docker image is updated even when its list item lists `auth:` before `image:`."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST2))
        config = "jobs:\n  build:\n    docker:\n      - auth:\n          username: u\n        image: cimg/python:3.13\n"
        config_yml = mock_path(config)
        self.run_updater(config_yml)
        config_yml.write_text.assert_called_with(config.replace("cimg/python:3.13", f"cimg/python:3.14@{DIGEST2}"))
        self.assert_new_version_logged("cimg/python", "3.14", Location(config_yml, 6))
        self.assert_no_warnings_logged()
