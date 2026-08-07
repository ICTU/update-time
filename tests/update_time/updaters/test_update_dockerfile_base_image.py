"""Unit tests for the Dockerfile base image update script."""

from unittest.mock import Mock, patch

from update_time.io.filesystem import DOCKERFILE_GLOB_PATTERNS
from update_time.primitives.location import Location
from update_time.updaters.update_dockerfile_base_image import update_dockerfiles

from tests.helpers import mock_path
from tests.update_time import registry
from tests.update_time.fixtures import DIGEST2
from tests.update_time.helpers import docker_tag, mock_docker_hub_auth
from tests.update_time.registry import mock_docker_registry


@mock_docker_hub_auth
class UpdateDockerfileTest(registry.ImageUpdaterTestMixin):
    """Unit tests for the update Dockerfile function."""

    def reference(self, image: str) -> str:
        """Return a Dockerfile `FROM` line for the image."""
        return f"FROM {image}\n"

    def run_updater(self, mock_file: Mock) -> None:
        """Run the Dockerfile updater with the mock file as the only discovered Dockerfile.

        The updater globs several Dockerfile patterns; return the file for the exact `Dockerfile` one only, so it is
        processed exactly once.
        """

        def rglob(pattern: str, *, case_sensitive: bool | None = None) -> list[Mock]:  # noqa: ARG001
            return [mock_file] if pattern == "Dockerfile" else []

        with patch("pathlib.Path.rglob", side_effect=rglob):
            update_dockerfiles()

    def test_alternate_filenames_are_scanned(self):
        """Test that `*.Dockerfile` and `Dockerfile.*` files are scanned, not only an exact `Dockerfile`."""
        with patch("update_time.updaters.update_dockerfile_base_image.update_files", return_value=0) as update_files:
            update_dockerfiles()
        self.assertEqual(DOCKERFILE_GLOB_PATTERNS, update_files.call_args.args)

    def test_stage_alias_is_preserved_when_pinning(self):
        """Test that a multi-stage `FROM image:tag AS name` alias is kept intact when the image is pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.4", DIGEST2))
        mock_dockerfile = mock_path("FROM ruby:3.3 AS build\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(f"FROM ruby:3.4@{DIGEST2} AS build\n")
        self.assert_new_version_logged("ruby", "3.4", Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()

    def test_platform_flag_with_build_arg_is_preserved(self):
        """Test that a `FROM --platform=$BUILDPLATFORM image:tag` line is updated with the flag left untouched."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        mock_dockerfile = mock_path("FROM --platform=$BUILDPLATFORM python:3.14\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(f"FROM --platform=$BUILDPLATFORM python:3.15@{DIGEST2}\n")
        self.assert_new_version_logged("python", "3.15", Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()

    def test_platform_flag_with_literal_value_and_stage_alias_is_preserved(self):
        """Test that a `FROM --platform=linux/amd64 image:tag AS name` line keeps both the flag and the stage alias."""
        self.requests.side_effect = mock_docker_registry(docker_tag("20", DIGEST2))
        mock_dockerfile = mock_path("FROM --platform=linux/amd64 node:18 AS build\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(f"FROM --platform=linux/amd64 node:20@{DIGEST2} AS build\n")
        self.assert_new_version_logged("node", "20", Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()

    def test_ignore_marker_leaves_base_image_untouched(self):
        """Test that a FROM line pinned by a preceding `# update-time: ignore` comment is not updated or queried."""
        mock_dockerfile = mock_path("# update-time: ignore\nFROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_ignored_logged("ghcr.io/astral-sh/uv", Location(mock_dockerfile, 2))
        self.assert_no_new_version_logged()
        self.assert_no_redundant_suppression_logged()
        self.assert_no_warnings_logged()

    def test_ignore_yanked_marker_is_reported_as_redundant(self):
        """Test that an `ignore[yanked]` marker on a base image is reported when an image has no yank to hold back."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        mock_dockerfile = mock_path("# update-time: ignore[yanked]\nFROM python:3.14\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(f"# update-time: ignore[yanked]\nFROM python:3.15@{DIGEST2}\n")
        self.assert_redundant_yank_scope_logged("python", Location(mock_dockerfile, 2), "ignore[yanked]")

    def test_vulnerable_scope_is_reported_as_redundant(self):
        """Test that each `vulnerable` marker is reported when an image has no vulnerability to hold back."""
        for directive in ("ignore[vulnerable]", "ignore[vulnerable=GHSA-2gwj-7jmv-h26r]", "ignore[vulnerable<high]"):
            with self.subTest(directive=directive):
                self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
                mock_dockerfile = mock_path(f"# update-time: {directive}\nFROM python:3.14\n")
                self.run_updater(mock_dockerfile)
                mock_dockerfile.write_text.assert_called_with(
                    f"# update-time: {directive}\nFROM python:3.15@{DIGEST2}\n"
                )
                self.assert_redundant_vulnerable_source_logged("python", Location(mock_dockerfile, 2), directive)
                self.mock_log.reset_mock()  # Judge each case on the records of its own run.

    def test_label_prefixed_base_image_bumped_and_pinned(self):
        """Test that a label-prefixed base image (ghcr.io/astral-sh/uv:python3.12-...) is bumped and pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("python3.13-bookworm-slim", DIGEST2))
        mock_dockerfile = mock_path("FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(f"FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@{DIGEST2}\n")
        self.assert_new_version_logged("ghcr.io/astral-sh/uv", "python3.13-bookworm-slim", Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()
