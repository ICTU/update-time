"""Unit tests for the manifest image update script."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from update_time.domain.dependency import AccountedFor, FloatingPin
from update_time.domain.file_type import DOCKER_COMPOSE_FILES, HELM_CHARTS
from update_time.markers.directive import Reason
from update_time.primitives.location import Location
from update_time.references import file as references_file
from update_time.references import resolve as references_resolve
from update_time.sources import oci
from update_time.updaters import update_manifest_images as manifest_images
from update_time.updaters.update_manifest_images import update_manifest_images

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time import registry
from tests.update_time.fixtures import DIGEST
from tests.update_time.helpers import docker_tag
from tests.update_time.registry import mock_docker_registry
from tests.update_time.updaters.helpers import mock_docker_hub_auth


@mock_docker_hub_auth
class UpdateManifestImagesTest(registry.ImageUpdaterTestMixin):
    """Unit tests for the update manifest images function."""

    def reference(self, image: str) -> str:
        """Return a Docker Compose / Helm `image:` line for the image."""
        return f"image: {image}\n"

    def run_updater_over(self, mock_files: list[Mock], glob_pattern: str) -> None:
        """Run the manifest updater with the mock files as the only files the glob pattern finds.

        update_manifest_images globs the Compose pattern and then the Helm YAML patterns; answering one of them with
        the files and the rest with nothing processes each of them exactly once.
        """

        def rglob(pattern: str, *, case_sensitive: bool | None = None) -> list[Mock]:  # noqa: ARG001
            return mock_files if pattern == glob_pattern else []

        with patch("pathlib.Path.rglob", side_effect=rglob):
            update_manifest_images()

    def run_compose_updater(self, *mock_files: Mock) -> None:
        """Run the manifest updater with the mock files as the only Docker Compose files."""
        self.run_updater_over(list(mock_files), "docker-compose*.yml")

    def run_helm_updater(self, mock_file: Mock) -> None:
        """Run the manifest updater with the mock file as the only Helm chart."""
        self.run_updater_over([mock_file], "*.yml")

    def run_updater(self, mock_file: Mock) -> None:
        """Run the Compose half, which is the one the shared tests of the mixin exercise."""
        self.run_compose_updater(mock_file)

    def test_pin_tagless_image(self):
        """Test that an `image:` naming no tag is pinned to the version and digest `latest` serves."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_manifest = mock_path(self.reference("python"))
        self.run_compose_updater(mock_manifest)
        mock_manifest.write_text.assert_called_once_with(self.reference(f"python:3.14.7@{DIGEST}"))
        self.assert_pinned_logged("python", "3.14.7", DIGEST, Location(mock_manifest, 1))
        self.assert_no_warnings_logged()

    def test_image_whose_tag_the_registry_does_not_serve_is_not_stale(self):
        """Test that an `image:` whose tag the registry does not list is not reported stale."""
        pushed = (datetime.now(UTC) - timedelta(days=512)).isoformat()
        self.requests.side_effect = mock_docker_registry(docker_tag("v4.7.0", DIGEST, tag_last_pushed=pushed))
        mock_manifest = mock_path(self.reference("acme/api:ci"))
        self.run_compose_updater(mock_manifest)
        mock_manifest.write_text.assert_not_called()
        location = Location(mock_manifest, 1)
        self.assert_unpinned_floating_tag_logged("acme/api", "ci", location, FloatingPin.NOT_LISTED)
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            manifest_images,
            "        for service in services.values()\n",
            "        for service in list(services.values())[:1]\n",
            "only the first service is read, so an image a later service builds is looked up on a registry",
        )
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
        mock_manifest = mock_path(compose)
        self.run_compose_updater(mock_manifest)
        mock_manifest.write_text.assert_not_called()
        self.requests.assert_not_called()
        built = AccountedFor.BUILT_IMAGE
        self.assert_accounted_for_reference_logged("acme/api", "1.2.3", Location(mock_manifest, 4), built)
        self.assert_accounted_for_reference_logged("acme/api", "1.2.3", Location(mock_manifest, 6), built)
        self.assert_accounted_for_reference_logged("acme/db", "2.0.0", Location(mock_manifest, 9), built)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            oci,
            '    without_digests = {image.partition("@")[0] for image in images}\n',
            "    without_digests = set(images)\n",
            "a digest the file records beside a built image's tag survives into the match, so the image is looked up",
        )
    )
    def test_a_built_image_pinned_to_a_digest_is_left_alone(self):
        """Test that a built image is left as it is when its reference carries the digest an earlier run pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.3.0", DIGEST))
        mock_manifest = mock_path(f"services:\n  api:\n    build: .\n    image: acme/api:1.2.3@{DIGEST}\n")
        self.run_compose_updater(mock_manifest)
        mock_manifest.write_text.assert_not_called()
        self.requests.assert_not_called()
        location = Location(mock_manifest, 4)
        self.assert_accounted_for_reference_logged("acme/api", "1.2.3", location, AccountedFor.BUILT_IMAGE)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            manifest_images,
            '        if isinstance(service, dict) and "build" in service and isinstance(service.get("image"), str)\n',
            '        if isinstance(service, dict) and "build" in service\n',
            "a service that builds an image without tagging one aborts the run",
            raises="KeyError: 'image'",
        ),
    )
    def test_image_the_file_does_not_build_is_updated(self):
        """Test that an image a service pulls is updated, whatever the service beside it builds."""
        builders = {
            "the service beside it tags its build": "  api:\n    build: .\n    image: acme/api:1.2.3\n",
            "the service beside it tags no build": "  api:\n    build: .\n",
        }
        for case, builder in builders.items():
            with self.subTest(case=case):
                self.requests.side_effect = mock_docker_registry(docker_tag("17", DIGEST))
                compose = f"services:\n{builder}  db:\n    image: postgres:16\n"
                mock_manifest = mock_path(compose)
                self.run_compose_updater(mock_manifest)
                expected = compose.replace("postgres:16", f"postgres:17@{DIGEST}")
                mock_manifest.write_text.assert_called_once_with(expected)
                postgres_line = len(compose.splitlines())  # The `db` service's image is the file's last line.
                self.assert_new_version_logged("postgres", "17", Location(mock_manifest, postgres_line))
                self.assert_no_warnings_logged()

    @kills(
        Mutation(
            manifest_images,
            '    services = document.get("services") if isinstance(document, dict) else None\n',
            '    services = document.get("services")\n',
            "a Compose file that is not a mapping is read as one, ending the run",
            raises="AttributeError: 'list' object has no attribute 'get'",
        ),
        Mutation(
            manifest_images,
            "    if not isinstance(services, dict):\n",
            "    if services is None:\n",
            "a `services:` that is not a mapping is read as one, ending the run",
            raises="AttributeError: 'list' object has no attribute 'values'",
        ),
        Mutation(
            manifest_images,
            '        if isinstance(service, dict) and "build" in service and isinstance(service.get("image"), str)\n',
            '        if "build" in service and isinstance(service.get("image"), str)\n',
            "a service declared with no body is read as a mapping, ending the run",
            raises="TypeError: argument of type 'NoneType' is not a container or iterable",
        ),
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
                mock_manifest = mock_path(compose)
                self.run_compose_updater(mock_manifest)
                expected = compose.replace("python:3.14", f"python:3.15@{DIGEST}")
                mock_manifest.write_text.assert_called_once_with(expected)
                self.assert_no_warnings_logged()

    @kills(
        Mutation(
            oci,
            '        as_written = f"{pinned.name}{tag_of(pinned.version)}"\n'
            "        return reason if as_written in without_digests else None\n",
            '        return reason if pinned.name in {name.split(":", maxsplit=1)[0] for name in without_digests}'
            " else None\n",
            "a reference is matched on its name alone, so every tag of a built repository is left alone",
        )
    )
    def test_other_tag_of_a_built_repository_is_updated(self):
        """Test that a service pulling another tag of the repository the file builds has that tag updated."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.0.0", DIGEST))
        compose = "services:\n  api:\n    build: .\n    image: acme/api:dev\n  legacy:\n    image: acme/api:2.0.0\n"
        mock_manifest = mock_path(compose)
        self.run_compose_updater(mock_manifest)
        mock_manifest.write_text.assert_called_once_with(compose.replace("2.0.0", f"3.0.0@{DIGEST}"))
        self.assert_new_version_logged("acme/api", "3.0.0", Location(mock_manifest, 6))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            references_file,
            "            update_file(path, regexp, get_new_version=get_new_version_for(document), logger=logger)\n",
            '            getter = getter if "getter" in dir() else get_new_version_for(document)\n'
            "            update_file(path, regexp, get_new_version=getter, logger=logger)\n",
            "the getter built for one file judges the files after it, so what one file builds reaches them all",
        )
    )
    def test_a_file_builds_an_image_for_itself_only(self):
        """Test that an image one Compose file builds is updated in another Compose file that pulls it."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.3.0", DIGEST))
        builder = mock_path("services:\n  api:\n    build: .\n    image: acme/api:1.2.3\n")
        puller = mock_path("services:\n  api:\n    image: acme/api:1.2.3\n")
        self.run_compose_updater(builder, puller)
        builder.write_text.assert_not_called()
        puller.write_text.assert_called_once_with(f"services:\n  api:\n    image: acme/api:1.3.0@{DIGEST}\n")
        self.assert_new_version_logged("acme/api", "1.3.0", Location(puller, 3))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            references_resolve,
            "    if not asked:\n        return Reason.NO_REGISTRY_ASKED\n    if floats is False:\n",
            "    if floats is False:\n",
            "a reference no registry is asked about is reported as one whose pin does not float, which it may do",
        )
    )
    def test_floating_pin_marker_on_a_built_image_says_no_registry_is_asked(self):
        """Test that `allow[floating-pin]` on a built image says no registry is asked, not that its pin is fixed."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.3.0", DIGEST))
        marker = "# update-time: allow[floating-pin]"
        mock_manifest = mock_path(f"services:\n  api:\n    build: .\n    image: acme/api:dev  {marker}\n")
        self.run_compose_updater(mock_manifest)
        mock_manifest.write_text.assert_not_called()
        self.assert_redundant_directive_logged(
            Reason.NO_REGISTRY_ASKED, "acme/api", Location(mock_manifest, 4), "allow[floating-pin]"
        )

    def test_helm_chart_that_does_not_parse_is_updated(self):
        """Test that a Helm chart whose YAML does not parse is updated, a Go-templated chart being no valid YAML."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        chart = "spec:\n  {{- if .Values.enabled }}\n  image: python:3.14\n  {{- end }}\n"
        mock_chart = mock_path(chart)
        self.run_helm_updater(mock_chart)
        mock_chart.write_text.assert_called_once_with(chart.replace("python:3.14", f"python:3.15@{DIGEST}"))
        self.assert_new_version_logged("python", "3.15", Location(mock_chart, 3))
        self.assert_no_warnings_logged()

    def test_variable_substitution_ignored(self):
        """Test that image tags using ${...} substitution are not modified."""
        self.requests.side_effect = mock_docker_registry(docker_tag("999.0", DIGEST))
        mock_manifest = mock_path(self.reference("ictu/quality-time_proxy:${QUALITY_TIME_VERSION}"))
        self.run_compose_updater(mock_manifest)
        mock_manifest.write_text.assert_not_called()
        self.assert_path_logged(mock_manifest)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


class ScannedManifestsTest(unittest.TestCase):
    """Unit tests for which manifest files are scanned for pinned images."""

    def scan(self) -> tuple[Mock, Mock]:
        """Run the updater over no files, and return the mocks standing in for its two passes."""
        with (
            patch("update_time.updaters.update_manifest_images.update_yaml_files") as update_yaml_files,
            patch("update_time.updaters.update_manifest_images.glob_for", return_value=[]) as glob_for,
        ):
            update_manifest_images()
        return update_yaml_files, glob_for

    def test_docker_compose_files_are_scanned(self):
        """Test that the Docker Compose files are scanned from the repository root."""
        update_yaml_files, _glob_for = self.scan()
        self.assertEqual(update_yaml_files.call_args.args, (DOCKER_COMPOSE_FILES,))
        self.assertEqual(DOCKER_COMPOSE_FILES.patterns, ("docker-compose*.yml",))
        self.assertEqual(DOCKER_COMPOSE_FILES.start, "")

    def test_helm_yaml_files_are_scanned(self):
        """Test all YAML files in the Helm folder are scanned, so pinned images stay in sync with Docker Compose."""
        _update_yaml_files, glob_for = self.scan()
        glob_for.assert_called_once_with(HELM_CHARTS)
        self.assertEqual(HELM_CHARTS.patterns, ("*.yml", "*.yaml"))
        self.assertEqual(HELM_CHARTS.start, "helm")
