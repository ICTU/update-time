"""Unit tests for the Dockerfile base image update script."""

import unittest
from unittest.mock import Mock, patch

from update_time.updaters.update_dockerfile_base_image import update_dockerfiles

from tests.update_time.assertions import assert_new_version_logged, assert_path_logged, assert_success
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2
from tests.update_time.helpers import docker_hub_response, docker_tag, mock_path


@patch("logging.Logger.warning")
@patch("logging.Logger.info")
@patch("pathlib.Path.rglob")
@patch("requests.get")
class UpdateDockerfileTest(unittest.TestCase):
    """Unit tests for the update Dockerfile function."""

    def test_no_changes(self, mock_get: Mock, mock_glob: Mock, mock_info: Mock, mock_warning: Mock):
        """Test no changes."""
        mock_get.return_value = docker_hub_response()
        mock_dockerfile = mock_path(f"FROM node:28.1@{DIGEST}")
        mock_glob.return_value = [mock_dockerfile]
        assert_success(update_dockerfiles())
        mock_dockerfile.write_text.assert_not_called()
        assert_path_logged(mock_info, mock_dockerfile.relative_to())
        mock_warning.assert_not_called()

    def test_changes(self, mock_get: Mock, mock_glob: Mock, mock_info: Mock, mock_warning: Mock):
        """Test changes."""
        mock_get.return_value = docker_hub_response(docker_tag("3.15", DIGEST2))
        mock_dockerfile = mock_path(f"FROM python:3.14@{DIGEST1}\n")
        mock_glob.return_value = [mock_dockerfile]
        assert_success(update_dockerfiles())
        mock_dockerfile.write_text.assert_called_with(f"FROM python:3.15@{DIGEST2}\n")
        assert_path_logged(mock_info, mock_dockerfile.relative_to())
        assert_new_version_logged(mock_warning, "python", "3.15")

    def test_pin_unpinned_base_image(self, mock_get: Mock, mock_glob: Mock, mock_info: Mock, mock_warning: Mock):
        """Test that a base image referenced by tag only is pinned with the latest tag and digest."""
        mock_get.return_value = docker_hub_response(docker_tag("1.22", DIGEST2))
        mock_dockerfile = mock_path("FROM golang:1.21\n")
        mock_glob.return_value = [mock_dockerfile]
        assert_success(update_dockerfiles())
        mock_dockerfile.write_text.assert_called_with(f"FROM golang:1.22@{DIGEST2}\n")
        assert_path_logged(mock_info, mock_dockerfile.relative_to())
        assert_new_version_logged(mock_warning, "golang", "1.22")

    def test_stage_alias_is_preserved_when_pinning(
        self, mock_get: Mock, mock_glob: Mock, mock_info: Mock, mock_warning: Mock
    ):
        """Test that a multi-stage `FROM image:tag AS name` alias is kept intact when the image is pinned."""
        mock_get.return_value = docker_hub_response(docker_tag("3.4", DIGEST2))
        mock_dockerfile = mock_path("FROM ruby:3.3 AS build\n")
        mock_glob.return_value = [mock_dockerfile]
        assert_success(update_dockerfiles())
        mock_dockerfile.write_text.assert_called_with(f"FROM ruby:3.4@{DIGEST2} AS build\n")
        assert_path_logged(mock_info, mock_dockerfile.relative_to())
        assert_new_version_logged(mock_warning, "ruby", "3.4")
