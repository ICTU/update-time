"""Unit tests for the Circle CI config update script."""

from unittest.mock import Mock, patch

from update_time.domain.dependency import FloatingPin, Unserved
from update_time.io.log import Logger
from update_time.primitives.location import Location
from update_time.updaters import update_circle_ci_config as circle_ci
from update_time.updaters.update_circle_ci_config import update_circle_ci_config

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time import registry
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2
from tests.update_time.helpers import docker_tag
from tests.update_time.registry import mock_docker_registry
from tests.update_time.updaters.helpers import mock_docker_hub_auth


@mock_docker_hub_auth
class UpdateCircleCIConfigTest(registry.ImageUpdaterTestMixin):
    """Unit tests for the update Circle CI config function."""

    def reference(self, image: str) -> str:
        """Return a CircleCI `image:` line for the image."""
        return f"image: {image}\n"

    def run_updater(self, mock_file: Mock) -> None:
        """Run the CircleCI updater with the mock file as the only YAML file under the CircleCI directory."""
        with patch("pathlib.Path.glob", side_effect=[[mock_file], []]):
            update_circle_ci_config()

    def test_multiple_files(self):
        """Test that images are updated in all YAML files under the CircleCI directory, not just config.yml."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.26.2", DIGEST2))
        config_yml = mock_path(f"image: cimg/go:1.26.1@{DIGEST1}\n")
        next_yml = mock_path(f"image: cimg/go:1.26.1@{DIGEST1}\n")
        with patch("pathlib.Path.glob", side_effect=[[config_yml], [next_yml]]):
            update_circle_ci_config()
        config_yml.write_text.assert_called_with(f"image: cimg/go:1.26.2@{DIGEST2}\n")
        next_yml.write_text.assert_called_with(f"image: cimg/go:1.26.2@{DIGEST2}\n")
        self.assert_path_logged(next_yml)
        self.assert_last_new_version_logged("cimg/go", "1.26.2", Location(next_yml, 1), Logger._SUPPRESSING_CHANGELOG)
        self.assert_no_warnings_logged()

    def test_alias_that_no_registry_serves_is_left_alone(self):
        """Test that an `image: default` alias, which no tag of a registry image serves, is left as it is."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.2", DIGEST))
        config_yml = mock_path("image: default\n")
        self.run_updater(config_yml)
        config_yml.write_text.assert_not_called()
        location = Location(config_yml, 1)
        # The reference names no tag, so the report names the `latest` that was looked up for it.
        self.assert_unpinned_floating_tag_logged("default", "latest", location, FloatingPin.NOT_LISTED)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_pin_tagless_image(self):
        """Test that an `image:` naming no tag is pinned to the version and digest `latest` serves."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        config_yml = mock_path(self.reference("python"))
        self.run_updater(config_yml)
        config_yml.write_text.assert_called_once_with(self.reference(f"python:3.14.7@{DIGEST}"))
        self.assert_pinned_logged("python", "3.14.7", DIGEST, Location(config_yml, 1))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            circle_ci,
            "            images.add(image)\n",
            '            if ":" in image:\n                images.add(image)\n',
            "a machine-executor image naming no tag is not collected, so it is looked up on a registry",
        )
    )
    def test_machine_image_without_a_tag_is_skipped(self):
        """Test that a machine-executor image naming no tag is left unchanged and looked up nowhere."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        config_yml = mock_path("jobs:\n  build:\n    machine:\n      image: default\n")
        self.run_updater(config_yml)
        config_yml.write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            circle_ci,
            "    return tag_getter_excluding(_machine_images(config), Unserved.MACHINE_EXECUTOR_IMAGE)\n",
            "    return tag_getter_excluding(_machine_images(config), Unserved.BUILT_IMAGE)\n",
            "a machine-executor image is reported as one a Compose file builds",
        )
    )
    def test_machine_image_skipped(self):
        """Test that a machine-executor image is left unchanged, not looked up on Docker Hub, and reported."""
        config_yml = mock_path("jobs:\n  build:\n    machine:\n      image: ubuntu-2204:2024.01.1\n")
        self.run_updater(config_yml)
        config_yml.write_text.assert_not_called()
        # The machine image is recognised by parsing the YAML, so no registry is queried for it.
        self.requests.assert_not_called()
        location = Location(config_yml, 4)
        self.assert_unserved_reference_logged("ubuntu-2204", "2024.01.1", location, Unserved.MACHINE_EXECUTOR_IMAGE)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_docker_image_with_auth_before_image(self):
        """Test that a Docker image is updated even when its list item lists `auth:` before `image:`."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST))
        config = "jobs:\n  build:\n    docker:\n      - auth:\n          username: u\n        image: cimg/python:3.13\n"
        config_yml = mock_path(config)
        self.run_updater(config_yml)
        config_yml.write_text.assert_called_with(config.replace("cimg/python:3.13", f"cimg/python:3.14@{DIGEST}"))
        self.assert_new_version_logged("cimg/python", "3.14", Location(config_yml, 6))
        self.assert_no_warnings_logged()
