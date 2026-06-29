"""Unit tests for the Dockerfile base image update script."""

from unittest.mock import Mock, patch

from update_time.updaters.update_dockerfile_base_image import update_dockerfiles

from tests.update_time.assertions import assert_success
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2
from tests.update_time.helpers import LoggingTestCase, docker_tag, mock_docker_hub_auth, mock_docker_registry, mock_path


@patch("pathlib.Path.rglob")
@patch("requests.get")
@mock_docker_hub_auth
class UpdateDockerfileTest(LoggingTestCase):
    """Unit tests for the update Dockerfile function."""

    def test_no_changes(self, mock_get: Mock, mock_glob: Mock):
        """Test no changes."""
        mock_get.side_effect = mock_docker_registry()
        mock_dockerfile = mock_path(f"FROM node:28.1@{DIGEST}")
        mock_glob.return_value = [mock_dockerfile]
        assert_success(update_dockerfiles())
        mock_dockerfile.write_text.assert_not_called()
        self.assert_path_logged(mock_dockerfile)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_changes(self, mock_get: Mock, mock_glob: Mock):
        """Test changes."""
        mock_get.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        mock_dockerfile = mock_path(f"FROM python:3.14@{DIGEST1}\n")
        mock_glob.return_value = [mock_dockerfile]
        assert_success(update_dockerfiles())
        mock_dockerfile.write_text.assert_called_with(f"FROM python:3.15@{DIGEST2}\n")
        self.assert_path_logged(mock_dockerfile)
        self.assert_new_version_logged(mock_dockerfile, "python", "3.15")
        self.assert_no_warnings_logged()

    def test_pin_unpinned_base_image(self, mock_get: Mock, mock_glob: Mock):
        """Test that a base image referenced by tag only is pinned with the latest tag and digest."""
        mock_get.side_effect = mock_docker_registry(docker_tag("1.22", DIGEST2))
        mock_dockerfile = mock_path("FROM golang:1.21\n")
        mock_glob.return_value = [mock_dockerfile]
        assert_success(update_dockerfiles())
        mock_dockerfile.write_text.assert_called_with(f"FROM golang:1.22@{DIGEST2}\n")
        self.assert_path_logged(mock_dockerfile)
        self.assert_new_version_logged(mock_dockerfile, "golang", "1.22")
        self.assert_no_warnings_logged()

    def test_stage_alias_is_preserved_when_pinning(self, mock_get: Mock, mock_glob: Mock):
        """Test that a multi-stage `FROM image:tag AS name` alias is kept intact when the image is pinned."""
        mock_get.side_effect = mock_docker_registry(docker_tag("3.4", DIGEST2))
        mock_dockerfile = mock_path("FROM ruby:3.3 AS build\n")
        mock_glob.return_value = [mock_dockerfile]
        assert_success(update_dockerfiles())
        mock_dockerfile.write_text.assert_called_with(f"FROM ruby:3.4@{DIGEST2} AS build\n")
        self.assert_path_logged(mock_dockerfile)
        self.assert_new_version_logged(mock_dockerfile, "ruby", "3.4")
        self.assert_no_warnings_logged()
