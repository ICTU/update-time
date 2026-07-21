"""Unit tests for the Node engine update script."""

from pathlib import Path
from unittest.mock import ANY, Mock, patch

from update_time.io.log import Logger
from update_time.updaters.update_node_engine import update_node_engines

from tests.update_time.fixtures import DIGEST
from tests.update_time.helpers import (
    LoggingTestCase,
    docker_tag,
    mock_docker_hub_auth,
    mock_path,
    patch_pathlib_path,
)
from tests.update_time.registry import mock_docker_registry


@patch_pathlib_path("rglob", cwd=Path("/"))
class UpdateNodeEnginesTest(LoggingTestCase):
    """Unit tests for the update Node engines function."""

    def create_package_json(self, contents: str = '{"engines": {"node": "18" }}') -> Mock:
        """Create a mock package.json file."""
        return mock_path(contents, parent=Path("/"))

    @patch_pathlib_path(exists=True, read_text="FROM node:18")
    def test_unchanged(self, mock_glob: Mock):
        """Test that the package.json is not written if there is no new Node version."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        update_node_engines()
        mock_package_json.write_text.assert_not_called()
        self.assert_path_logged(mock_package_json)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM node:19")
    def test_update(self, mock_glob: Mock):
        """Test that the package.json is updated if there is a new Node version."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        update_node_engines()
        mock_package_json.write_text.assert_called_once_with('{"engines": {"node": "19" }}\n')
        self.assert_path_logged(mock_package_json)
        self.assert_new_version_logged(mock_package_json, "node", "19")
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM node:lts AS base")
    def test_non_numeric_node_base_image(self, mock_glob: Mock):
        """Test that a non-numeric Node base image tag (e.g. node:lts) is skipped with a warning, not an error."""
        mock_package_json = self.create_package_json()
        mock_glob.return_value = [mock_package_json]
        update_node_engines()
        self.mock_warning.assert_called_once_with(
            Logger._MESSAGE_NON_NUMERIC_NODE_BASE_IMAGE_TAG, "lts", Path("/Dockerfile"), stacklevel=ANY
        )
        mock_package_json.write_text.assert_not_called()
        self.assert_no_path_logged()
        self.assert_no_new_version_logged()

    def test_no_node_engine(self, mock_glob: Mock):
        """Test that the package.json is skipped if it has no Node engine."""
        mock_package_json = self.create_package_json("{}")
        mock_glob.return_value = [mock_package_json]
        update_node_engines()
        mock_package_json.write_text.assert_not_called()
        self.assert_no_path_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def assert_falls_back_to_latest_node(self, mock_glob: Mock) -> None:
        """Assert the engine is updated to the latest Node release on Docker Hub, with no local version to derive."""
        mock_package_json = self.create_package_json()  # Its engine is node 18.
        mock_glob.return_value = [mock_package_json]
        registry = Mock(side_effect=mock_docker_registry(docker_tag("20", DIGEST)))
        with mock_docker_hub_auth, patch("requests.get", registry), patch("requests.head", registry):
            update_node_engines()
        mock_package_json.write_text.assert_called_once_with('{"engines": {"node": "20" }}\n')
        self.assert_path_logged(mock_package_json)
        self.assert_new_version_logged(mock_package_json, "node", "20")
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=False)
    def test_no_dockerfile(self, mock_glob: Mock):
        """Test that the engine falls back to the latest Node release when there is no Dockerfile to derive it from."""
        self.assert_falls_back_to_latest_node(mock_glob)

    @patch_pathlib_path(exists=True, read_text="FROM python:3.14")
    def test_no_node_base_image(self, mock_glob: Mock):
        """Test that the engine falls back to the latest Node release when no Dockerfile has a Node base image."""
        self.assert_falls_back_to_latest_node(mock_glob)

    @patch_pathlib_path(exists=False)
    def test_fallback_dockerfile(self, mock_glob: Mock):
        """Test that a Node base image elsewhere in the repo is used when the package.json has no local Dockerfile."""
        mock_package_json = self.create_package_json()
        fallback_dockerfile = mock_path("FROM node:20")

        def rglob(pattern: str, **_kwargs: object) -> list[Mock]:
            # The package.json glob finds the manifest; the Dockerfile globs find only the fallback elsewhere in the
            # repo. The local Dockerfile next to the package.json doesn't exist here, so find_node_dockerfile skips it.
            return [mock_package_json] if pattern == "package.json" else [fallback_dockerfile]

        mock_glob.side_effect = rglob
        update_node_engines()
        mock_package_json.write_text.assert_called_once_with('{"engines": {"node": "20" }}\n')
        self.assert_path_logged(mock_package_json)
        self.assert_new_version_logged(mock_package_json, "node", "20")
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=False)
    def test_numeric_dockerfile_preferred_over_non_numeric(self, mock_glob: Mock):
        """Test that a Dockerfile with a numeric Node tag wins over one with a non-numeric tag (e.g. node:lts)."""
        mock_package_json = self.create_package_json()
        non_numeric_dockerfile = mock_path("FROM node:lts")
        numeric_dockerfile = mock_path("FROM node:20")

        def rglob(pattern: str, **_kwargs: object) -> list[Mock]:
            # The non-numeric Dockerfile is listed first, so a naive "first Node base image" match would pick node:lts
            # and warn; the numeric-tag preference must skip past it to the syncable node:20.
            return [mock_package_json] if pattern == "package.json" else [non_numeric_dockerfile, numeric_dockerfile]

        mock_glob.side_effect = rglob
        update_node_engines()
        mock_package_json.write_text.assert_called_once_with('{"engines": {"node": "20" }}\n')
        self.assert_new_version_logged(mock_package_json, "node", "20")
        self.assert_no_warnings_logged()  # node:lts is passed over, so it is never warned about.
