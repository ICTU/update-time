"""Unit tests for the Node engine update script."""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.io.log import Logger
from update_time.manifests import package_json
from update_time.markers.directive import Reason
from update_time.primitives.location import Location
from update_time.references import rewrite
from update_time.updaters import update_node_engine
from update_time.updaters.update_node_engine import update_node_engines

from tests.helpers import mock_path, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import DIGEST
from tests.update_time.helpers import LoggingTestCase, docker_hub_version, docker_tag, message_of
from tests.update_time.registry import RegistryRequestsMixin, mock_docker_registry
from tests.update_time.updaters.helpers import mock_docker_hub_auth

if TYPE_CHECKING:
    from unittest.mock import Mock


# The package.json a test is given unless it needs another: an engine to update, and nothing else.
_PACKAGE_JSON = '{"engines": {"node": "18" }}\n'


# Another section declares this node version. It is older than the latest release, so a misread entry gets updated.
_OTHER_NODE_VERSION = "16.20.2"


def _package_json(node: str = "18", *, section: str = "", directives: str = "", field: object = None) -> str:
    """Return a package.json declaring the Node engine, with the section and the marker the test asks for.

    `section` declares a `node` version of its own above the engine, and `directives` steers the engine from the
    file's `update-time` field. `field` sets that field's whole value instead, for a file whose field the marker
    language cannot read. The engine sits on line 3, and on line 6 where a section is declared above it.
    """
    other = {section: {"node": _OTHER_NODE_VERSION}} if section else {}
    marker: dict[str, object] = {"update-time": {"engines": {"node": directives}}} if directives else {}
    if field is not None:
        marker = {"update-time": field}
    contents = json.dumps({**other, "engines": {"node": node}, **marker}, indent=2)
    return f"{contents}\n"


def _compact_package_json(node: str = "18") -> str:
    """Return a package.json declaring a node version and the Node engine on one line."""
    return f'{{"volta": {{"node": "{_OTHER_NODE_VERSION}"}}, "engines": {{"node": "{node}"}}}}\n'


@mock_docker_hub_auth
@patch_pathlib_path("rglob", cwd=Path("/"))
class UpdateNodeEnginesTest(RegistryRequestsMixin, LoggingTestCase):
    """Unit tests for the update Node engines function."""

    def update_engine(
        self, mock_glob: Mock, contents: str = _PACKAGE_JSON, *, latest: tuple[str, ...] = ("19",)
    ) -> Mock:
        """Update the Node engine of a package.json holding these contents, and return the mock file."""
        self.requests.side_effect = mock_docker_registry(*(docker_tag(tag, DIGEST) for tag in latest))
        mock_package_json = mock_path(contents, parent=Path("/"))
        mock_glob.return_value = [mock_package_json]
        update_node_engines()
        return mock_package_json

    def test_unchanged(self, mock_glob: Mock):
        """Test that the package.json is not written if there is no new Node version."""
        mock_package_json = self.update_engine(mock_glob, latest=("18",))
        mock_package_json.write_text.assert_not_called()
        self.assert_path_logged(mock_package_json)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_update(self, mock_glob: Mock):
        """Test that the package.json is updated if there is a new Node version."""
        mock_package_json = self.update_engine(mock_glob)
        mock_package_json.write_text.assert_called_once_with('{"engines": {"node": "19" }}\n')
        self.assert_path_logged(mock_package_json)
        self.assert_new_version_logged("node", docker_hub_version("19"), Location(mock_package_json, 1))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            rewrite._reference_match,
            """if not reference_marker.reference_location.is_on_the_same_line_as(line.location):
        return None
    return pattern.search(line.text, reference_marker.reference_location.column)""",
            "return pattern.search(line.text, reference_marker.reference_location.column)",
            "the pattern reads every line it matches, rather than the line the file names",
        )
    )
    def test_a_node_version_outside_the_engines_section_is_left_as_it_is(self, mock_glob: Mock):
        """Test that a node version another section declares is not taken for the engine."""
        mock_package_json = self.update_engine(mock_glob, _package_json(section="volta"))
        mock_package_json.write_text.assert_called_once_with(_package_json("19", section="volta"))
        self.assert_new_version_logged("node", docker_hub_version("19"), Location(mock_package_json, 6))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            rewrite._reference_match,
            "return pattern.search(line.text, reference_marker.reference_location.column)",
            "return pattern.search(line.text)",
            "the named line is read from its start, so the entry declared before the engine is taken for it",
        )
    )
    def test_the_engine_is_updated_where_another_node_entry_shares_its_line(self, mock_glob: Mock):
        """Test that the engine is the entry updated where another section declares a node version on its line."""
        mock_package_json = self.update_engine(mock_glob, _compact_package_json())
        mock_package_json.write_text.assert_called_once_with(_compact_package_json("19"))
        self.assert_new_version_logged("node", docker_hub_version("19"), Location(mock_package_json, 1))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_node_engine._has_node_engine,
            "return isinstance(engines, dict) and _NODE in engines",
            "return engines is not None and _NODE in engines",
            "a section that is a string or a list is read as one naming the engine, so the file is opened for it",
        )
    )
    def test_no_node_engine(self, mock_glob: Mock):
        """Test that a package.json whose `engines` section declares no Node version is skipped."""
        for case, contents in (
            ("no engines section", "{}"),
            ("an engines section that is a string", '{"engines": "node >=18"}'),
            ("an engines section that is a list", '{"engines": ["node"]}'),
        ):
            with self.subTest(case=case):
                mock_package_json = self.update_engine(mock_glob, contents)
                mock_package_json.read_text.assert_called_once()
                mock_package_json.write_text.assert_not_called()
                self.assert_no_debug_logged()
                self.assert_no_new_version_logged()
                self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_node_engine._update_node_engine,
            """if engine_marker.reference_location.line_number is None:
        _LOG.no_entry(_NODE, package_json.path)
        return
    update_file(""",
            "update_file(",
            "the run passes over an engine it cannot find the entry of, in silence",
        )
    )
    def test_an_engine_whose_entry_cannot_be_found(self, mock_glob: Mock):
        """Test that an engine the file declares but whose entry cannot be found is warned about, not passed over."""
        # The section is declared twice: the parse reads the engine off the second, the entry off the first.
        contents = '{"engines": {"npm": ">=10"}, "engines": {"node": "18"}}\n'
        mock_package_json = self.update_engine(mock_glob, contents)
        mock_package_json.write_text.assert_not_called()
        self.assert_logged(message_of(Logger.no_entry), dependency="node", location=Location(mock_package_json))
        self.assert_no_new_version_logged()

    def test_marker_in_the_update_time_field_holds_the_engine_back(self, mock_glob: Mock):
        """Test that an `ignore` directive in the `update-time` field holds back the engine's update, and it alone."""
        mock_package_json = self.update_engine(mock_glob, _package_json(section="volta", directives="ignore"))
        mock_package_json.write_text.assert_not_called()
        self.assert_ignored_logged("node", Location(mock_package_json, 6), "ignore")
        self.assertEqual(len(self.records_of(message_of(Logger.ignored))), 1)  # The volta pin is steered by no marker.
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_the_engine_is_updated_when_the_marker_field_is_declared_above_it(self, mock_glob: Mock):
        """Test that the `engines` the `update-time` field mirrors is not taken for the engine it steers."""
        marked = {"update-time": {"engines": {"node": "allow[update<20]"}}, "engines": {"node": "18"}}
        mock_package_json = self.update_engine(mock_glob, f"{json.dumps(marked, indent=2)}\n")
        updated = json.dumps({**marked, "engines": {"node": "19"}}, indent=2)
        mock_package_json.write_text.assert_called_once_with(f"{updated}\n")
        self.assert_new_version_logged("node", docker_hub_version("19"), Location(mock_package_json, 8))
        self.assert_no_warnings_logged()

    def test_bound_in_the_update_time_field_admits_a_version_it_covers(self, mock_glob: Mock):
        """Test that a bounded engine adopts the newest version the bound admits, rather than the newest release."""
        mock_package_json = self.update_engine(
            mock_glob, _package_json(directives="allow[update<20]"), latest=("19", "20")
        )
        mock_package_json.write_text.assert_called_once_with(_package_json("19", directives="allow[update<20]"))
        self.assert_new_version_logged("node", docker_hub_version("19"), Location(mock_package_json, 3))
        self.assert_no_warnings_logged()

    def test_the_yanked_vulnerable_and_archived_scopes_are_reported_as_redundant(self, mock_glob: Mock):
        """Test that a `yanked`, `vulnerable`, or `archived` marker on the engine is reported as redundant."""
        for directives, reason in (
            ("ignore[yanked]", Reason.NO_YANK_CONCEPT),
            ("ignore[vulnerable]", Reason.NO_VULNERABILITY_REPORTS),
            ("ignore[archived]", Reason.NO_ARCHIVAL_SIGNAL),
        ):
            with self.subTest(directives=directives):
                mock_package_json = self.update_engine(mock_glob, _package_json(directives=directives))
                mock_package_json.write_text.assert_called_once_with(_package_json("19", directives=directives))
                self.assert_redundant_directive_logged(reason, "node", Location(mock_package_json, 3), directives)
                self.assert_new_version_logged("node", docker_hub_version("19"), Location(mock_package_json, 3))

    def test_the_cooldown_and_stale_scopes_are_not_reported_as_redundant(self, mock_glob: Mock):
        """Test that a `cooldown` or `stale` marker on the engine is not reported as redundant."""
        for directives in ("ignore[cooldown<30]", "ignore[stale<1095]"):
            with self.subTest(directives=directives):
                mock_package_json = self.update_engine(mock_glob, _package_json(directives=directives))
                mock_package_json.write_text.assert_called_once_with(_package_json("19", directives=directives))
                self.assert_no_warnings_logged()

    def test_invalid_item_in_the_update_time_field_leaves_the_engine_unchanged(self, mock_glob: Mock):
        """Test that an item Update-time cannot read is warned about and holds the engine's update back."""
        mock_package_json = self.update_engine(mock_glob, _package_json(directives="ignore[updaet]"))
        mock_package_json.write_text.assert_not_called()
        self.assert_invalid_bracket_item_logged("node", Location(mock_package_json, 3), "updaet")
        self.assert_no_new_version_logged()

    @kills(
        Mutation(
            package_json._marker,
            "return parse_directives(directives) if isinstance(directives, str) else _unreadable_field(section, name)",
            "return parse_directives(directives)",
            "a value that is not the directive list it should be reaches the parser",
            raises="TypeError: expected string or bytes-like object, got 'list'",
        ),
    )
    def test_a_field_the_marker_language_cannot_read_leaves_the_engine_unchanged(self, mock_glob: Mock):
        """Test that an `update-time` field of the wrong shape is warned about, and the engine left as it is."""
        fields: dict[str, object] = {
            "a field that is not an object": "ignore",
            "a section that is not an object": {"engines": "ignore"},
            "directives that are not a string": {"engines": {"node": ["ignore"]}},
        }
        for case, field in fields.items():
            with self.subTest(case=case):
                mock_package_json = self.update_engine(mock_glob, _package_json(field=field))
                mock_package_json.write_text.assert_not_called()
                self.assert_invalid_bracket_item_logged(
                    "node", Location(mock_package_json, 3), "update-time.engines.node"
                )
                self.assert_no_new_version_logged()
