"""Unit tests for updating the images in CI configs, manifests, and devcontainers."""

import unittest
from typing import TYPE_CHECKING, override
from unittest.mock import ANY, Mock, patch

from mutation_time.mutation import kills
from update_time.domain.dependency import FloatingPin, ImageNotLookedUp
from update_time.domain.directive import Consequence, ImageNotLookedUpReason
from update_time.domain.file_type import (
    CIRCLE_CI_CONFIGS,
    DEVCONTAINER_CONFIGS,
    DOCKER_COMPOSE_FILES,
    GITLAB_CI_CONFIG,
    HELM_CHARTS,
)
from update_time.formats import yaml
from update_time.io.log import Logger
from update_time.manifests import devcontainer_json
from update_time.manifests.circle_ci_config import machine_images
from update_time.manifests.docker_compose import built_images
from update_time.primitives.location import Location
from update_time.references.file import update_yaml_files
from update_time.references.resolve import _warn_about_redundant_update_directives, _why_the_pin_never_floats
from update_time.updaters.update_container_images import _update_container_images

from tests.file_mocks import mock_path
from tests.update_time import registry
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2, STALE_DATE
from tests.update_time.registry import mock_docker_registry
from tests.update_time.sources.docker_hub_answers import docker_hub_version, docker_tag

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.file_type import FileType


class UpdateContainerImagesTest(registry.ImageUpdaterTestMixin):
    """Unit tests for updating the images in CI configs, manifests, and devcontainers.

    The shared tests of the mixin run over Docker Compose files, the format that is parsed and checked for built images.
    """

    @override
    def run_updater(self, mock_file: Mock) -> None:
        """Run the container image updater with the mock file as the only Docker Compose file."""
        _update_container_images_in(DOCKER_COMPOSE_FILES, mock_file)

    def test_repository_without_image_files(self):
        """Test that a repository without files holding images is handled gracefully."""
        _update_container_images_in(GITLAB_CI_CONFIG)
        self.requests.assert_not_called()
        self.assert_no_debug_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @kills.delete(
        _update_container_images,
        "glob_for(GITLAB_CI_CONFIG), ",
        "the GitLab CI config is not walked, so its images are never updated",
    )
    def test_gitlab_ci_config_is_updated(self):
        """Test that an image in the GitLab CI config is updated."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        gitlab_ci_config = self.updated_file_of(GITLAB_CI_CONFIG, "image: python:3.14\n")
        gitlab_ci_config.write_text.assert_called_once_with(f"image: python:3.15@{DIGEST}\n")
        self.assert_new_version_logged("python", docker_hub_version("3.15"), Location(gitlab_ci_config, 1))
        self.assert_no_warnings_logged()

    def updated_file_of(self, file_type: FileType, contents: str) -> Mock:
        """Run the container image updater with a file holding the contents as the only file of the file type."""
        mock_file = mock_path(contents)
        _update_container_images_in(file_type, mock_file)
        return mock_file

    def test_every_circle_ci_config_is_updated(self):
        """Test that images are updated in every CircleCI config the walk finds, not just in the first."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.26.2", DIGEST2))
        config_yml = mock_path(f"image: cimg/go:1.26.1@{DIGEST1}\n")
        next_yml = mock_path(f"image: cimg/go:1.26.1@{DIGEST1}\n")
        _update_container_images_in(CIRCLE_CI_CONFIGS, config_yml, next_yml)
        config_yml.write_text.assert_called_with(f"image: cimg/go:1.26.2@{DIGEST2}\n")
        next_yml.write_text.assert_called_with(f"image: cimg/go:1.26.2@{DIGEST2}\n")
        self.assert_path_logged(next_yml)
        self.assert_last_new_version_logged(
            "cimg/go", docker_hub_version("1.26.2"), Location(next_yml, 1), Logger._SUPPRESSING_CHANGELOG
        )
        self.assert_no_warnings_logged()

    def test_alias_that_no_registry_serves_is_left_alone(self):
        """Test that a CircleCI `image: default` alias, which no tag of a registry image serves, is left as it is."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.2", DIGEST))
        config_yml = self.updated_file_of(CIRCLE_CI_CONFIGS, "image: default\n")
        config_yml.write_text.assert_not_called()
        location = Location(config_yml, 1)
        self.assert_unpinned_floating_tag_logged("default", "", location, FloatingPin.NOT_LISTED, looked_up="latest")
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_reference_through_the_variable_syntax_of_its_format_is_left_alone_and_reported(self):
        """Test that a reference whose image name or tag holds a variable in its format's syntax is not looked up."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        image_line, feature_line = "image: {}\n", '    "{}": {{}}\n'
        node_feature = "ghcr.io/devcontainers/features/node"
        cases = {
            "a CircleCI tag that is a parameter": (CIRCLE_CI_CONFIGS, image_line, "cimg/node", "<< parameters.tag >>"),
            "a CircleCI image name ending in a parameter": (
                CIRCLE_CI_CONFIGS,
                image_line,
                "cimg/<< parameters.image >>",
                "3.12",
            ),
            "a Helm tag that is a template": (HELM_CHARTS, image_line, "app", "{{ .Values.tag }}"),
            "a Helm image name ending in a template": (HELM_CHARTS, image_line, "registry/{{ .Values.app }}", "1.0"),
            "a feature tag that is a variable": (
                DEVCONTAINER_CONFIGS,
                feature_line,
                node_feature,
                "${localEnv:NODE_FEATURE}",
            ),
            "a feature name holding a variable": (DEVCONTAINER_CONFIGS, feature_line, "ghcr.io/${ORG}/node", "1"),
        }
        for case, (file_type, line, image, tag) in cases.items():
            with self.subTest(case):
                mock_file = self.updated_file_of(file_type, line.format(f"{image}:{tag}"))
                self.assert_left_alone_unqueried(mock_file, image, tag, 1, ImageNotLookedUp.THROUGH_A_VARIABLE)

    @kills.append(
        machine_images,
        '(image := node["machine"]["image"].string()) is not None',
        ' and ":" in image',
        "a machine-executor image naming no tag is not collected, so it is looked up on a registry",
    )
    def test_machine_image_without_a_tag_is_skipped(self):
        """Test that a machine-executor image naming no tag is left unchanged and looked up nowhere."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        config = "jobs:\n  build:\n    machine:\n      image: default\n"
        self.updated_file_of(CIRCLE_CI_CONFIGS, config).write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_no_warnings_logged()

    @kills.replace(
        _update_container_images,
        "MACHINE_EXECUTOR_IMAGE",
        "BUILT_IMAGE",
        "a machine-executor image is reported as one a Compose file builds",
    )
    def test_machine_image_skipped(self):
        """Test that a machine-executor image is left unchanged, not looked up on Docker Hub, and reported."""
        config = "jobs:\n  build:\n    machine:\n      image: ubuntu-2204:2024.01.1\n"
        config_yml = self.updated_file_of(CIRCLE_CI_CONFIGS, config)
        reason = ImageNotLookedUp.MACHINE_EXECUTOR_IMAGE
        self.assert_left_alone_unqueried(config_yml, "ubuntu-2204", "2024.01.1", 4, reason)

    def test_docker_image_with_auth_before_image(self):
        """Test that a CircleCI Docker image is updated even when its list item lists `auth:` before `image:`."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST))
        config = "jobs:\n  build:\n    docker:\n      - auth:\n          username: u\n        image: cimg/python:{}\n"
        config_yml = self.updated_file_of(CIRCLE_CI_CONFIGS, config.format("3.13"))
        config_yml.write_text.assert_called_once_with(config.format(f"3.14@{DIGEST}"))
        self.assert_new_version_logged("cimg/python", docker_hub_version("3.14"), Location(config_yml, 6))
        self.assert_no_warnings_logged()

    def test_image_whose_tag_the_registry_does_not_serve_is_not_stale(self):
        """Test that an `image:` whose tag the registry does not list is not reported stale."""
        self.requests.side_effect = mock_docker_registry(docker_tag("v4.7.0", DIGEST, tag_last_pushed=STALE_DATE))
        mock_manifest = self.updated_file(self.reference("acme/api:ci"))
        mock_manifest.write_text.assert_not_called()
        self.assert_unpinned_floating_tag_logged("acme/api", "ci", Location(mock_manifest, 1), FloatingPin.NOT_LISTED)
        self.assert_no_warnings_logged()

    @override
    def reference(self, image: str) -> str:
        """Return an `image:` line for the image."""
        return f"image: {image}\n"

    @kills.append(
        built_images,
        'for service in document["services"].values()',
        "[:1]",
        "only the first service is read, so an image a later service builds is looked up on a registry",
    )
    def test_every_reference_to_a_built_image_is_left_alone(self):
        """Test that a built image is left as it is, looked up nowhere, and reported, in each service naming it."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.3.0", DIGEST))
        compose = (
            "services:\n"
            "  api:\n"
            "    build: .\n"
            "    image: acme/api:1.2.3\n"
            "  worker:\n"  # Runs what the api service built, without building anything itself.
            "    image: acme/api:1.2.3\n"
            "  db:\n"  # Builds an image of its own, so the worker is not the only service without a build.
            "    build: ./db\n"
            "    image: acme/db:2.0.0\n"
        )
        mock_manifest = self.updated_file(compose)
        built = ImageNotLookedUp.BUILT_IMAGE
        self.assert_left_alone_unqueried(mock_manifest, "acme/api", "1.2.3", 4, built)
        self.assert_left_alone_unqueried(mock_manifest, "acme/api", "1.2.3", 6, built)
        self.assert_left_alone_unqueried(mock_manifest, "acme/db", "2.0.0", 9, built)

    def test_a_built_image_pinned_to_a_digest_is_left_alone(self):
        """Test that a built image is left as it is when its reference carries the digest an earlier run pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.3.0", DIGEST))
        mock_manifest = self.updated_file(f"services:\n  api:\n    build: .\n    image: acme/api:1.2.3@{DIGEST}\n")
        self.assert_left_alone_unqueried(mock_manifest, "acme/api", "1.2.3", 4, ImageNotLookedUp.BUILT_IMAGE)

    @kills.replace(
        built_images,
        '(image := service["image"].string()) is not None\n',
        '((image := service["image"].string()) or True)\n',
        "a service that builds an image without tagging one aborts the run",
        raises="AttributeError: 'NoneType' object has no attribute 'partition'",
    )
    def test_image_the_file_does_not_build_is_updated(self):
        """Test that an image a service pulls is updated beside a service that builds without tagging its build."""
        compose = "services:\n  api:\n    build: .\n  db:\n    image: postgres:{}\n"
        self.assert_update_written("17", compose.format("16"), compose.format(f"17@{DIGEST}"), image="postgres", line=5)

    @kills.delete(
        yaml.Document.__contains__,
        "isinstance(self._value, dict) and ",
        "a service declared with no body is read as a mapping, ending the run",
        raises="TypeError: argument of type 'NoneType' is not a container or iterable",
        case="a service has no body",
    )
    @kills.delete(
        yaml.Document.__getitem__,
        " if isinstance(self._value, dict) else Document()",
        "a Compose file that is a list rather than a mapping ends the run",
        raises="AttributeError: 'list' object has no attribute 'get'",
        case="the document is a list",
    )
    @kills.delete(
        yaml.Document.values,
        " if isinstance(self._value, dict) else []",
        "a Compose file listing its services rather than mapping them ends the run",
        raises="AttributeError: 'list' object has no attribute 'values'",
        case="the services are a list",
    )
    def test_a_malformed_compose_file_is_updated(self):
        """Test that Update-time updates the images in a malformed Compose file instead of ending the run."""
        shapes = {
            "the document is a list": "- image: python:3.14\n",
            "the services are a list": "services:\n  - image: python:3.14\n",
            "a service has no body": "services:\n  api:\n  db:\n    image: python:3.14\n",
        }
        for case, compose in shapes.items():
            with self.subTest(case=case):
                self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
                mock_manifest = self.updated_file(compose)
                expected = compose.replace("python:3.14", f"python:3.15@{DIGEST}")
                mock_manifest.write_text.assert_called_once_with(expected)
                self.assert_no_warnings_logged()

    def test_other_tag_of_a_built_repository_is_updated(self):
        """Test that a service pulling another tag of the repository the file builds has that tag updated."""
        compose = "services:\n  api:\n    build: .\n    image: acme/api:dev\n  legacy:\n    image: acme/api:{}\n"
        self.assert_update_written(
            "3.0.0", compose.format("2.0.0"), compose.format(f"3.0.0@{DIGEST}"), image="acme/api", line=6
        )

    @kills.append(
        update_yaml_files,
        "tag_source = ",
        'tag_source = tag_source if "tag_source" in dir() else ',
        "the source built for one file judges the files after it, so what one file builds reaches them all",
    )
    def test_a_file_builds_an_image_for_itself_only(self):
        """Test that an image one Compose file builds is updated in another Compose file that pulls it."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.3.0", DIGEST))
        builder = mock_path("services:\n  api:\n    build: .\n    image: acme/api:1.2.3\n")
        puller = mock_path("services:\n  api:\n    image: acme/api:1.2.3\n")
        _update_container_images_in(DOCKER_COMPOSE_FILES, builder, puller)
        builder.write_text.assert_not_called()
        puller.write_text.assert_called_once_with(f"services:\n  api:\n    image: acme/api:1.3.0@{DIGEST}\n")
        self.assert_new_version_logged("acme/api", docker_hub_version("1.3.0"), Location(puller, 3))
        self.assert_no_warnings_logged()

    @kills.delete(
        _why_the_pin_never_floats,
        "if image_not_looked_up is not None:\n"
        "        return ImageNotLookedUpReason(image_not_looked_up, Consequence.TAG_NEVER_PINNED)\n"
        "    ",
        "the warning names a pin that does not float, where it should name the image the file does not pull",
    )
    def test_a_floating_pin_opt_in_on_a_built_image_names_why_the_image_is_not_pulled(self):
        """Test that `allow[floating-pin]` on an image the Compose file builds is reported as redundant, naming why."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.3.0", DIGEST))
        marker = "# update-time: allow[floating-pin]"
        mock_manifest = self.updated_file(f"services:\n  api:\n    build: .\n    image: acme/api:dev  {marker}\n")
        mock_manifest.write_text.assert_not_called()
        self.assert_redundant_directive_logged(
            ImageNotLookedUpReason(ImageNotLookedUp.BUILT_IMAGE, Consequence.TAG_NEVER_PINNED),
            "acme/api",
            Location(mock_manifest, 4),
            "allow[floating-pin]",
        )

    def test_a_drift_opt_in_beside_a_hold_back_on_a_built_image_names_the_build(self):
        """Test that `allow[hash-drift]` beside `ignore[update]` on a built image is redundant because of the build."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.3.0", DIGEST))
        marker = "# update-time: ignore[update] allow[hash-drift]"
        mock_manifest = self.updated_file(f"services:\n  api:\n    build: .\n    image: acme/api:1.2.3  {marker}\n")
        mock_manifest.write_text.assert_not_called()
        self.assert_redundant_directive_logged(
            ImageNotLookedUpReason(ImageNotLookedUp.BUILT_IMAGE, Consequence.DIGEST_NEVER_CHECKED),
            "acme/api",
            Location(mock_manifest, 4),
            "allow[hash-drift]",
            among_others=True,
        )

    @kills.replace(
        _warn_about_redundant_update_directives,
        "else:\n        warn_if_the_bound_is_redundant(reference, log)",
        "warn_if_the_bound_is_redundant(reference, log)",
        "a bound on an image never looked up is reported twice, once as never having an effect",
    )
    def test_a_bound_on_a_built_image_is_redundant_once_because_of_the_build(self):
        """Test that a bound on a built image is reported once, naming the build, rather than judged at its version."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.3.0", DIGEST))
        marker = "# update-time: allow[update>=1]"
        mock_manifest = self.updated_file(f"services:\n  api:\n    build: .\n    image: acme/api:1.2.3  {marker}\n")
        mock_manifest.write_text.assert_not_called()
        self.assert_redundant_directive_logged(
            ImageNotLookedUpReason(ImageNotLookedUp.BUILT_IMAGE, Consequence.NEVER_UPDATED),
            "acme/api",
            Location(mock_manifest, 4),
            "allow[update>=1]",
        )

    def test_helm_chart_that_does_not_parse_is_updated(self):
        """Test that a Helm chart whose YAML does not parse is updated, a Go-templated chart being no valid YAML."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        chart = "spec:\n  {{- if .Values.enabled }}\n  image: python:3.14\n  {{- end }}\n"
        mock_chart = self.updated_file_of(HELM_CHARTS, chart)
        mock_chart.write_text.assert_called_once_with(chart.replace("python:3.14", f"python:3.15@{DIGEST}"))
        self.assert_new_version_logged("python", docker_hub_version("3.15"), Location(mock_chart, 3))
        self.assert_no_warnings_logged()

    def test_reference_in_a_flow_mapping_is_updated(self):
        """Test that a reference a flow mapping or sequence closes or continues right after it is still updated."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        cases = {
            "followed by a comma": "containers: [{image: python:3.14, name: app}]\n",
            "followed by a closing brace": "containers: [{name: app, image: python:3.14}]\n",
            "followed by a closing bracket": "containers: [image: python:3.14]\n",
        }
        for case, chart in cases.items():
            with self.subTest(case):
                mock_chart = self.updated_file_of(HELM_CHARTS, chart)
                mock_chart.write_text.assert_called_once_with(chart.replace("python:3.14", f"python:3.15@{DIGEST}"))
                self.assert_new_version_logged("python", docker_hub_version("3.15"), Location(mock_chart, 1))

    def test_feature_updated_and_pinned(self):
        """Test that a devcontainer feature key (an OCI reference) is bumped and pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST))
        devcontainer = self.updated_file_of(DEVCONTAINER_CONFIGS, '    "ghcr.io/devcontainers/features/node:1": {}\n')
        devcontainer.write_text.assert_called_once_with(
            f'    "ghcr.io/devcontainers/features/node:2.1@{DIGEST}": {{}}\n'
        )
        self.assert_new_version_logged("ghcr.io/devcontainers/features/node", "2.1", Location(devcontainer, 1))
        self.assert_no_warnings_logged()

    def test_feature_pinned_when_already_latest(self):
        """Test that an unpinned feature that is already at the latest version is still pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2", DIGEST))
        devcontainer = self.updated_file_of(DEVCONTAINER_CONFIGS, '    "ghcr.io/devcontainers/features/node:2": {}\n')
        devcontainer.write_text.assert_called_once_with(f'    "ghcr.io/devcontainers/features/node:2@{DIGEST}": {{}}\n')
        self.assert_pinned_logged("ghcr.io/devcontainers/features/node", "2", DIGEST, Location(devcontainer, 1))
        self.assert_no_warnings_logged()

    @kills.replace(
        devcontainer_json,
        r"""re.compile(rf'"{IMAGE_REFERENCE}":\s*{{')""",
        r"""re.compile(r'"(?P<dependency>[\w./-]+)(?::(?=[\w.-]))?(?P<version>[\w.-]*)"""
        r"""(?:@(?P<sha>sha256:[0-9a-f]{64}))?":\s*{')""",
        "a JSON key naming no version is read as a feature reference without a tag",
    )
    def test_json_key_naming_no_version_is_not_read_as_a_feature(self):
        """Test that a JSON key naming no version is left alone, a feature reference always naming one."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        self.updated_file_of(DEVCONTAINER_CONFIGS, '  "customizations": {\n').write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_no_warnings_logged()

    def test_non_image_devcontainer_values_are_left_alone(self):
        """Test that Dockerfile builds, ports, and other `name:version`-looking values are not treated as images."""
        self.requests.side_effect = mock_docker_registry(docker_tag("9.9", DIGEST))
        devcontainer = self.updated_file_of(
            DEVCONTAINER_CONFIGS,
            '  "build": { "dockerfile": "Dockerfile" },\n  "appPort": "3000:3000",\n  "forwardPorts": [3000]\n',
        )
        devcontainer.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


def _update_container_images_in(file_type: FileType, *paths: Path) -> None:
    """Run the container image updater with the paths as the files of the file type, and every other file type empty."""

    def walk(walked: FileType) -> list[Path]:
        return list(paths) if walked is file_type else []

    with (
        patch("update_time.updaters.update_container_images.glob_for", side_effect=walk),
        patch("update_time.references.file.glob_for", side_effect=walk),
    ):
        _update_container_images()


class ScannedContainerImageFilesTest(unittest.TestCase):
    """Unit tests for which files are scanned for images and which references are updated in them."""

    def test_docker_compose_files_are_declared(self):
        """Test that the Docker Compose files are scanned from the repository root."""
        self.assertEqual(DOCKER_COMPOSE_FILES.patterns, ("docker-compose*.yml",))
        self.assertEqual(DOCKER_COMPOSE_FILES.start, "")

    def test_helm_yaml_files_are_declared(self):
        """Test all YAML files in the Helm folder are scanned, so pinned images stay in sync with Docker Compose."""
        self.assertEqual(HELM_CHARTS.patterns, ("*.yml", "*.yaml"))
        self.assertEqual(HELM_CHARTS.start, "helm")

    def test_devcontainer_locations_are_declared(self):
        """Test that the top-level, `.devcontainer/`, and per-configuration subfolder locations are all declared."""
        locations = ".devcontainer.json", ".devcontainer/devcontainer.json", ".devcontainer/*/devcontainer.json"
        self.assertEqual(DEVCONTAINER_CONFIGS.patterns, locations)

    @patch("update_time.updaters.update_container_images.update_file", return_value=0)
    def test_devcontainer_image_and_features_are_updated(self, mock_update_file: Mock):
        """Test that each devcontainer.json is scanned for both its image and its feature references in one pass."""
        mock_file = mock_path("{}")
        _update_container_images_in(DEVCONTAINER_CONFIGS, mock_file)
        mock_update_file.assert_called_once_with(mock_file, ANY, ANY, source=ANY, logger=ANY)
        image_pattern, feature_pattern = mock_update_file.call_args.args[1:3]
        self.assertRegex('"image": "python:3.12"', image_pattern)
        self.assertRegex('"ghcr.io/devcontainers/features/node:1": {', feature_pattern)
