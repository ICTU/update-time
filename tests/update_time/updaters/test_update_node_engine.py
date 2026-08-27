"""Unit tests for the Node engine update script."""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain import base_image
from update_time.domain.bound import Verb
from update_time.domain.directive import Reason
from update_time.domain.marker import Marker
from update_time.file_formats import package_json
from update_time.io.log import Logger
from update_time.primitives.location import Location
from update_time.references import resolve, rewrite
from update_time.updaters import update_node_engine
from update_time.updaters.update_node_engine import update_node_engines

from tests.helpers import mock_path, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import DIGEST
from tests.update_time.helpers import LoggingTestCase, bound, docker_tag, mock_docker_hub_auth
from tests.update_time.registry import RegistryRequestsMixin, mock_docker_registry

if TYPE_CHECKING:
    from unittest.mock import Mock


# The package.json a test is given unless it needs another: an engine to update, and nothing else.
_PACKAGE_JSON = '{"engines": {"node": "18" }}\n'


def _marked_package_json(directives: str, node: str = "18") -> str:
    """Return a package.json whose `update-time` field carries the directives steering its Node engine."""
    contents = json.dumps({"engines": {"node": node}, "update-time": {"engines": {"node": directives}}}, indent=2)
    return f"{contents}\n"


@mock_docker_hub_auth
@patch_pathlib_path("rglob", cwd=Path("/"))
class UpdateNodeEnginesTest(RegistryRequestsMixin, LoggingTestCase):
    """Unit tests for the update Node engines function."""

    def create_package_json(self, contents: str = _PACKAGE_JSON) -> Mock:
        """Create a mock package.json file."""
        return mock_path(contents, parent=Path("/"))

    def update_engine(self, mock_glob: Mock, contents: str = _PACKAGE_JSON) -> Mock:
        """Update the Node engine of a package.json holding these contents, and return the mock file."""
        mock_package_json = self.create_package_json(contents)
        mock_glob.return_value = [mock_package_json]
        update_node_engines()
        return mock_package_json

    @patch_pathlib_path(exists=True, read_text="FROM node:18")
    def test_unchanged(self, mock_glob: Mock):
        """Test that the package.json is not written if there is no new Node version."""
        mock_package_json = self.update_engine(mock_glob)
        mock_package_json.write_text.assert_not_called()
        self.assert_path_logged(mock_package_json)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM node:19")
    def test_update(self, mock_glob: Mock):
        """Test that the package.json is updated if there is a new Node version."""
        mock_package_json = self.update_engine(mock_glob)
        mock_package_json.write_text.assert_called_once_with('{"engines": {"node": "19" }}\n')
        self.assert_path_logged(mock_package_json)
        self.assert_new_version_logged("node", "19", Location(mock_package_json, 1))
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM node:lts AS base")
    def test_non_numeric_node_base_image(self, mock_glob: Mock):
        """Test that a non-numeric Node base image tag (e.g. node:lts) is skipped with a warning, not an error."""
        mock_package_json = self.update_engine(mock_glob)
        self.assert_logged(
            Logger._MESSAGE_NON_NUMERIC_NODE_BASE_IMAGE_TAG, tag="lts", location=Location(Path("/Dockerfile"))
        )
        mock_package_json.write_text.assert_not_called()
        self.assert_no_path_logged()
        self.assert_no_new_version_logged()

    @kills(
        Mutation(
            update_node_engine,
            '    return version if is_valid(version) else ""',
            "    return version",
            "a tag's digits are adopted as a version even when they do not form one, ending the run",
            raises="packaging.version.InvalidVersion: Invalid version: '22.'",
        )
    )
    @patch_pathlib_path(exists=True, read_text="FROM node:22.x-alpine")
    def test_unreadable_node_base_image_version(self, mock_glob: Mock):
        """Test that a base image tag whose version cannot be read is warned about, not adopted."""
        mock_package_json = self.update_engine(mock_glob)
        self.assert_logged(
            Logger._MESSAGE_NON_NUMERIC_NODE_BASE_IMAGE_TAG,
            tag="22.x-alpine",
            location=Location(Path("/Dockerfile")),
        )
        mock_package_json.write_text.assert_not_called()
        self.assert_no_new_version_logged()

    def test_no_node_engine(self, mock_glob: Mock):
        """Test that the package.json is skipped if it has no Node engine."""
        mock_package_json = self.update_engine(mock_glob, "{}")
        mock_package_json.write_text.assert_not_called()
        self.assert_no_path_logged()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def assert_falls_back_to_latest_node(self, mock_glob: Mock) -> None:
        """Assert the engine is updated to the latest Node release on Docker Hub, with no local version to derive."""
        self.requests.side_effect = mock_docker_registry(docker_tag("20", DIGEST))
        mock_package_json = self.update_engine(mock_glob)  # Its engine is node 18.
        mock_package_json.write_text.assert_called_once_with('{"engines": {"node": "20" }}\n')
        self.assert_path_logged(mock_package_json)
        self.assert_new_version_logged("node", "20", Location(mock_package_json, 1))
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
        self.assert_new_version_logged("node", "20", Location(mock_package_json, 1))
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
        self.assert_new_version_logged("node", "20", Location(mock_package_json, 1))
        self.assert_no_warnings_logged()  # node:lts is passed over, so it is never warned about.

    @kills(
        Mutation(
            rewrite,
            "marker = parse_marker(line) if marker is None else marker",
            "marker = parse_marker(line)",
            "the marker the file names goes unread, leaving only the marker its lines could carry",
        )
    )
    @patch_pathlib_path(exists=True, read_text="FROM node:19")
    def test_marker_in_the_update_time_field_holds_the_engine_back(self, mock_glob: Mock):
        """Test that an `ignore` directive in the `update-time` field holds the engine's update back."""
        mock_package_json = self.update_engine(mock_glob, _marked_package_json("ignore"))
        mock_package_json.write_text.assert_not_called()
        self.assert_ignored_logged("node", Location(mock_package_json, 3), "ignore")
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_node_engine,
            "logger=_LOG, marker=marker)",
            "logger=_LOG)",
            "a marker is read but never handed to the gate, where the engine follows Docker Hub",
        )
    )
    @patch_pathlib_path(exists=False)
    def test_marker_holds_the_engine_back_when_it_follows_docker_hub(self, mock_glob: Mock):
        """Test that a field marker holds the engine back when no Dockerfile derives its version."""
        self.requests.side_effect = mock_docker_registry(docker_tag("20", DIGEST))
        mock_package_json = self.update_engine(mock_glob, _marked_package_json("ignore"))
        mock_package_json.write_text.assert_not_called()
        self.assert_ignored_logged("node", Location(mock_package_json, 3), "ignore")
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM node:20")
    def test_bound_in_the_update_time_field_blocks_a_base_image_jump(self, mock_glob: Mock):
        """Test that a bound in the `update-time` field keeps a base image version it excludes from being adopted."""
        mock_package_json = self.update_engine(mock_glob, _marked_package_json("allow[update<20]"))
        mock_package_json.write_text.assert_not_called()
        self.assert_logged_among_others(
            Logger._MESSAGE_RECOGNISED_MARKER,
            directives=Marker(version_bound=bound(Verb.ALLOW, "update<20"), raw="allow[update<20]"),
            dependency="node",
            location=Location(mock_package_json, 3),
        )
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM node:19")
    def test_bound_in_the_update_time_field_admits_a_version_it_covers(self, mock_glob: Mock):
        """Test that a bounded engine still adopts a base image version the bound admits."""
        mock_package_json = self.update_engine(mock_glob, _marked_package_json("allow[update<20]"))
        mock_package_json.write_text.assert_called_once_with(_marked_package_json("allow[update<20]", "19"))
        self.assert_new_version_logged("node", "19", Location(mock_package_json, 3))
        self.assert_no_warnings_logged()

    @patch_pathlib_path(exists=True, read_text="FROM node:19")
    def test_scopes_the_engine_never_gets_are_reported_as_redundant(self, mock_glob: Mock):
        """Test that a scope whose check the engine never gets holds nothing back, and the engine updates."""
        for directives, reason in (
            ("ignore[yanked]", Reason.NO_YANK_CONCEPT),
            ("ignore[vulnerable]", Reason.NO_VULNERABILITY_REPORTS),
            ("ignore[cooldown<30]", Reason.NO_COOLDOWN_DATES),
            ("ignore[stale<90]", Reason.NO_STALENESS_DATES),
        ):
            with self.subTest(directives=directives):
                self.start_new_run()
                mock_package_json = self.update_engine(mock_glob, _marked_package_json(directives))
                mock_package_json.write_text.assert_called_once_with(_marked_package_json(directives, "19"))
                self.assert_redundant_directive_logged(reason, "node", Location(mock_package_json, 3), directives)
                self.assert_new_version_logged("node", "19", Location(mock_package_json, 3))

    @patch_pathlib_path(exists=True, read_text="FROM node:19")
    def test_invalid_item_in_the_update_time_field_leaves_the_engine_unchanged(self, mock_glob: Mock):
        """Test that an item Update-time cannot read is warned about and holds the engine's update back."""
        mock_package_json = self.update_engine(mock_glob, _marked_package_json("ignore[updaet]"))
        mock_package_json.write_text.assert_not_called()
        self.assert_logged(
            Logger._MESSAGE_INVALID_BRACKET_ITEM,
            bracket_item="updaet",
            dependency="node",
            location=Location(mock_package_json, 3),
        )
        self.assert_no_new_version_logged()

    @kills(
        Mutation(
            package_json,
            """    if not isinstance(field, dict):
        return _unreadable_field(section, name)""",
            """    if not isinstance(field, dict):
        return Marker()""",
            "a field that is not the object it should be reads as naming no marker",
        ),
        Mutation(
            package_json,
            """    if not isinstance(references, dict):
        return _unreadable_field(section, name)""",
            """    if not isinstance(references, dict):
        return Marker()""",
            "a section that is not the object it should be reads as naming no marker",
        ),
        Mutation(
            package_json,
            "    return parse_directives(directives) if isinstance(directives, str)"
            " else _unreadable_field(section, name)",
            "    return parse_directives(directives)",
            "a value that is not the directive list it should be reaches the parser",
            raises="TypeError: expected string or bytes-like object, got 'list'",
        ),
    )
    @patch_pathlib_path(exists=True, read_text="FROM node:19")
    def test_a_field_the_marker_language_cannot_read_leaves_the_engine_unchanged(self, mock_glob: Mock):
        """Test that an `update-time` field of the wrong shape is warned about, and the engine left as it is."""
        fields: dict[str, dict[str, object]] = {
            "a field that is not an object": {"update-time": "ignore"},
            "a section that is not an object": {"update-time": {"engines": "ignore"}},
            "directives that are not a string": {"update-time": {"engines": {"node": ["ignore"]}}},
        }
        for case, field in fields.items():
            with self.subTest(case=case):
                self.start_new_run()
                contents = json.dumps({"engines": {"node": "18"}, **field})
                mock_package_json = self.update_engine(mock_glob, f"{contents}\n")
                mock_package_json.write_text.assert_not_called()
                self.assert_logged(
                    Logger._MESSAGE_INVALID_BRACKET_ITEM,
                    bracket_item="update-time.engines.node",
                    dependency="node",
                    location=Location(mock_package_json, 1),
                )
                self.assert_no_new_version_logged()

    @kills(
        Mutation(
            base_image,
            "        adoptable = allow_downgrade or (",
            "        adoptable = (",
            "an engine ahead of its base image keeps its version, as a `.python-version` entry does",
        )
    )
    @patch_pathlib_path(exists=True, read_text="FROM node:18")
    def test_an_engine_ahead_of_the_base_image_follows_it_down(self, mock_glob: Mock):
        """Test that an engine ahead of its base image is brought back to it, unlike a `.python-version` entry."""
        mock_package_json = self.update_engine(mock_glob, '{"engines": {"node": "20" }}\n')
        mock_package_json.write_text.assert_called_once_with(_PACKAGE_JSON)
        self.assert_new_version_logged("node", "18", Location(mock_package_json, 1))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            resolve,
            "    if not downgrades(get_new_version, dependency):\n"
            "        log.warn_if_redundant_bound(reference, marker)",
            "    log.warn_if_redundant_bound(reference, marker)",
            "a bound is judged by sampling the versions above the current one, for a reference that can move below it",
        )
    )
    @patch_pathlib_path(exists=True, read_text="FROM node:20")
    def test_a_bound_that_blocks_a_downgrade_is_not_reported_as_redundant(self, mock_glob: Mock):
        """Test that a bound blocking a base image below the engine holds something back, so it is not reported."""
        mock_package_json = self.update_engine(mock_glob, _marked_package_json("allow[update>=21]", "22.1"))
        mock_package_json.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()
