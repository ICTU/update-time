"""Unit tests for the pyproject.toml updater (discovery and orchestration of the uv package manager)."""

import subprocess  # nosec
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import ANY, Mock, patch

from update_time.file_formats import pyproject_toml
from update_time.file_formats.dependency_file import PyprojectToml
from update_time.io.log import Logger
from update_time.markers.marker import Marker, Scope, Threshold
from update_time.package_managers import uv
from update_time.primitives.location import Location
from update_time.updaters.update_pyproject_toml import update_pyproject_tomls

from tests.helpers import mock_path, mock_response, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import BARE_IGNORE, CHANGELOG
from tests.update_time.helpers import (
    LoggingTestCase,
    github_commits_json,
    github_release_json,
    pyproject,
)

if TYPE_CHECKING:
    from update_time.sources.pypi import ReleaseMetadata


# What the log renders for the new version `updated_pyproject_toml` has uv and PyPI report.
_NEW_VERSION = "1.1, published: 2026-05-30 12:08"


def _discovered_pyproject_toml(glob: Mock, spec: str) -> Mock:
    """Return the single mock pyproject.toml the scan discovers, pinning the given dependency."""
    pyproject_toml = mock_path(pyproject(spec), parent=Path("/"))
    glob.return_value = [pyproject_toml]
    return pyproject_toml


# Persisting the cooldown into config is exercised by the uv package manager's tests; stub it out here so these tests
# focus on the discovery/version-update flow (and don't try to write config to the mock pyproject.toml files). The
# checks over the settled pins are stubbed out for the same reason: each makes registry requests of its own, and
# what they report is `test_uv_pins.py`'s to pin, while being handed the files found is `CheckedPinsTest`'s below.
@patch("update_time.updaters.update_pyproject_toml.warn_about_pins", Mock())
@patch("update_time.package_managers.uv.configure_cooldown", Mock())
@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("requests.get")
@patch("subprocess.run")
class UpdatePyprojectTomlsTest(LoggingTestCase):
    """Unit tests for the update pyproject.tomls function."""

    @staticmethod
    def pypi_metadata(
        changelog_url: str = "https://changelog",
        repository: str = "https://github.com/repo/package_with_github_releases",
    ) -> ReleaseMetadata:
        """Create PyPI release metadata fixture."""
        project_urls = {"Homepage": "https://home", "repository": repository}
        if changelog_url:
            project_urls["Changelog"] = changelog_url
        return {
            "info": {"description": "Package description", "project_urls": project_urls},
            "urls": [{"upload_time_iso_8601": "2026-05-30T12:07:03.123456Z"}],
        }

    @staticmethod
    def pypi_metadata_without_changelog() -> ReleaseMetadata:
        """Create PyPI release metadata without project URLs, so the test needs no changelog response mocked."""
        return {
            "info": {"description": "Package", "project_urls": {}},
            "urls": [{"upload_time_iso_8601": "2026-05-30T12:08:53.123321Z"}],
        }

    def create_pyproject_toml(self, contents: str) -> Mock:
        """Create a mock pyproject.toml file."""
        return mock_path(contents, parent=Path("/"))

    def mock_update_on_stdout(self, package: str, latest: str = "") -> Mock:
        """Mock stdout with optional package update."""
        update = f" (latest: {latest})" if latest else ""
        return Mock(stdout=f"| {package}{update}\n")

    def updated_pyproject_toml(  # noqa: PLR0913 — the patched mocks travel with the fixtures they are given
        self, run: Mock, get: Mock, glob: Mock, contents: str, *, package: str = "package", latest: str = "v1.1"
    ) -> Mock:
        """Update a mock pyproject.toml holding the contents, the only file the scan discovers, and return that mock.

        uv reports `latest` as the latest version of `package`, or no update at all when `latest` is empty. PyPI
        answers with metadata naming no changelog, so the log renders an available new version as `_NEW_VERSION`.
        """
        run.return_value = self.mock_update_on_stdout(package, latest)
        get.return_value = mock_response(self.pypi_metadata_without_changelog())
        pyproject_toml = self.create_pyproject_toml(contents)
        glob.return_value = [pyproject_toml]
        update_pyproject_tomls()
        return pyproject_toml

    def updated_marked_pin(
        self, run: Mock, get: Mock, glob: Mock, marker: str, latest: str = "v1.1"
    ) -> tuple[Mock, Location]:
        """Update a pyproject.toml whose only pin carries the marker, and return the file and the pin's location."""
        contents = pyproject("package==1.0", marker=marker)
        pyproject_toml = self.updated_pyproject_toml(run, get, glob, contents, latest=latest)
        return pyproject_toml, Location(pyproject_toml, 2)

    def assert_no_cli_cooldown(self, run: Mock) -> None:
        """Assert uv tree/lock carry no `--exclude-newer` flag (the cooldown lives in config), nor `--frozen`."""
        commands = [call.args[0] for call in run.call_args_list]
        uv_tree = next(command for command in commands if command[:2] == ("uv", "tree"))
        uv_lock = next(command for command in commands if command[:2] == ("uv", "lock"))
        self.assertNotIn("--frozen", uv_tree)  # --frozen would make uv tree --outdated ignore the cooldown
        for command in (uv_tree, uv_lock):
            with self.subTest(command=command[:2]):
                self.assertNotIn("--exclude-newer", command)

    def test_update(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml, with Update-time's cooldown passed to uv."""
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, pyproject("package==1.0"))
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged("package", _NEW_VERSION, Location(mock_pyproject_toml, 2))
        self.assert_no_cli_cooldown(run)
        self.assert_no_warnings_logged()

    def test_update_of_a_pin_the_file_spells_its_own_way(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pin uv names differently is found at its line and keeps the spelling the file gave it."""
        for spelling, reported in (("Jinja2", "jinja2"), ("typing_extensions", "typing-extensions")):
            with self.subTest(spelling=spelling):
                contents = pyproject(f"{spelling}==1.0")
                mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, contents, package=reported)
                mock_pyproject_toml.write_text.assert_called_with(pyproject(f"{spelling}==1.1"))
                self.assert_new_version_logged(reported, _NEW_VERSION, Location(mock_pyproject_toml, 2))

    def test_update_of_a_name_pinned_twice(self, run: Mock, get: Mock, glob: Mock):
        """Test that a name pinned in two arrays has its new version logged at each line pinning it."""
        contents = '[project]\ndependencies = ["package==1.0"]\n[dependency-groups]\ndev = ["package==1.0"]\n'
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, contents)
        self.assert_new_version_logged_among_others("package", _NEW_VERSION, Location(mock_pyproject_toml, 2), ANY)
        self.assert_new_version_logged_among_others("package", _NEW_VERSION, Location(mock_pyproject_toml, 4), ANY)

    def test_update_of_a_dependency_without_an_exact_pin(self, run: Mock, get: Mock, glob: Mock):
        """Test that a dependency uv reports outdated that the file doesn't `==`-pin is logged at its own line."""
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, pyproject("package>=1.0"))
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_new_version_logged("package", _NEW_VERSION, Location(mock_pyproject_toml, 2))
        self.assert_no_warnings_logged()

    def test_update_of_a_dependency_the_file_declares_nowhere(self, run: Mock, get: Mock, glob: Mock):
        """Test that a package uv reports outdated that the file declares nowhere is logged at the file."""
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, pyproject("package>=1.0"), package="other")
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_new_version_logged("other", _NEW_VERSION, Location(mock_pyproject_toml))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            uv,
            "_with_reported_markers(pyproject_toml_format.declared_dependencies(file), log)",
            "_with_reported_markers("
            "pyproject_toml_format.declared_dependencies(file) if lines_with_updates else [], log)",
            "only a file with an available update reports its markers, so a marker on a pin nothing updates is "
            "reported nowhere and reads as unrecognised",
        )
    )
    def test_a_marker_on_a_pin_uv_reports_no_update_for_is_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that a marker is reported as recognised and as holding the update back, with no update available."""
        mock_pyproject_toml, location = self.updated_marked_pin(run, get, glob, "ignore", latest="")
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_recognised_marker_logged("package", location, BARE_IGNORE)
        self.assert_ignored_logged("package", location, "ignore", among_others=True)

    @kills(
        Mutation(
            pyproject_toml,
            "        return not (self.pins_a_version and self.marker.ignores(Scope.UPDATE))",
            "        return not self.marker.ignores(Scope.UPDATE)",
            "an `ignore[update]` holds back a dependency that pins no version, whose version uv resolves whatever "
            "the marker says, so the new version reaches nobody",
        )
    )
    def test_a_new_version_is_reported_for_a_dependency_that_pins_no_version(self, run: Mock, get: Mock, glob: Mock):
        """Test that an `ignore[update]` on a dependency that pins no version leaves its new version reported.

        The marker freezes no pin there, and uv resolves the version whatever it says, so the report is all the
        reader gets.
        """
        contents = pyproject("package>=1.0", marker="ignore[update]")
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, contents)
        self.assert_new_version_logged("package", _NEW_VERSION, Location(mock_pyproject_toml, 2))

    @kills(
        Mutation(
            uv,
            "        lambda: log.ignored(*reported) if declaration.pins_a_version else None,",
            "        lambda: log.ignored(*reported),",
            "a dependency that pins no version reads as one whose update was held back, though the marker froze no "
            "pin and uv resolved the version anyway",
        )
    )
    def test_no_update_is_reported_as_held_back_for_a_dependency_that_pins_no_version(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that an `ignore[update]` on a dependency that pins no version is not reported as holding it back."""
        contents = pyproject("package>=1.0", marker="ignore[update]")
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, contents)
        marker = Marker(ignored_scopes=Scope.UPDATE)
        self.assert_recognised_marker_logged("package", Location(mock_pyproject_toml, 2), marker)
        self.assert_none_logged(Logger._MESSAGE_IGNORED, "update held back by a marker")

    @kills(
        Mutation(
            uv,
            "[declaration for declaration in declared if declaration.updatable]",
            "declared",
            "a marker holding the update back is not obeyed, so the pin it freezes is rewritten anyway",
        ),
        Mutation(
            uv,
            "if declared else [Location(file.path)]",
            "or [Location(file.path)]",
            "a package whose every declaration is held back has its new version reported at the file, and costs a "
            "request for the changelog of a version nothing adopts",
        ),
    )
    def test_a_marker_holds_the_update_back(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pin whose marker holds its update back keeps its version, and is reported as held back."""
        for directive, marker in {"ignore": BARE_IGNORE, "ignore[update]": Marker(ignored_scopes=Scope.UPDATE)}.items():
            with self.subTest(directive=directive):
                mock_pyproject_toml, location = self.updated_marked_pin(run, get, glob, directive)
                mock_pyproject_toml.write_text.assert_not_called()
                get.assert_not_called()  # a pin nothing updates costs no request for its changelog
                self.assert_no_new_version_logged()
                self.assert_recognised_marker_logged("package", location, marker)
                self.assert_ignored_logged("package", location, directive, among_others=True)

    @kills(
        Mutation(
            uv,
            "if declaration.updatable",
            "if all(other.updatable for other in declared)",
            "a marker freezes the name rather than the declaration, so another declaration of that name is frozen too",
        )
    )
    def test_a_marker_freezes_one_declaration_of_a_name_while_the_other_updates(self, run: Mock, get: Mock, glob: Mock):
        """Test that a name declared twice is frozen on the line its marker steers, and updated on the other."""
        frozen = '[project]\ndependencies = ["package==1.0"]  # update-time: ignore\n[dependency-groups]\n'
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, f'{frozen}dev = ["package==1.0"]\n')
        mock_pyproject_toml.write_text.assert_called_once_with(f'{frozen}dev = ["package==1.1"]\n')
        self.assert_new_version_logged("package", _NEW_VERSION, Location(mock_pyproject_toml, 4))
        self.assert_ignored_logged("package", Location(mock_pyproject_toml, 2), "ignore", among_others=True)

    @kills(
        Mutation(
            uv,
            "replace(declaration, marker=_reported_marker(declaration, log))",
            "(_reported_marker(declaration, log), declaration)[1]",
            "the marker a report settles on is discarded, so an unreadable item warns and the pin it may have been "
            "meant to freeze is rewritten anyway",
        )
    )
    def test_an_invalid_bracket_item_leaves_the_pin_unchanged(self, run: Mock, get: Mock, glob: Mock):
        """Test that an item the marker language cannot read warns and freezes the pin, reporting no marker."""
        for directive, item in {"ignore[stlae]": "stlae", "ignore[update": "[update"}.items():
            with self.subTest(directive=directive):
                mock_pyproject_toml, location = self.updated_marked_pin(run, get, glob, directive)
                mock_pyproject_toml.write_text.assert_not_called()
                get.assert_not_called()
                self.assert_no_new_version_logged()
                self.assert_invalid_bracket_item_logged("package", location, item)
                # The item is reported as invalid, so the marker is neither echoed back nor obeyed as a hold-back.
                self.assert_none_logged(Logger._MESSAGE_RECOGNISED_MARKER, "recognised marker")
                self.assert_none_logged(Logger._MESSAGE_IGNORED, "update held back by a marker")

    @kills(
        Mutation(
            uv,
            "if declaration.updatable",
            "if declaration.updatable and not any(one.marker.invalid_item for one in declared)",
            "an unreadable item freezes every declaration of its name rather than the one carrying it",
        )
    )
    def test_an_invalid_item_freezes_one_declaration_of_a_name_while_the_other_updates(
        self, run: Mock, get: Mock, glob: Mock
    ):
        """Test that a name declared twice is frozen on the line whose item cannot be read, and updated on the other."""
        frozen = '[project]\ndependencies = ["package==1.0"]  # update-time: ignore[stlae]\n[dependency-groups]\n'
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, f'{frozen}dev = ["package==1.0"]\n')
        mock_pyproject_toml.write_text.assert_called_once_with(f'{frozen}dev = ["package==1.1"]\n')
        self.assert_new_version_logged("package", _NEW_VERSION, Location(mock_pyproject_toml, 4))
        self.assert_invalid_bracket_item_logged("package", Location(mock_pyproject_toml, 2), "stlae")

    @kills(
        Mutation(
            uv,
            "log.report_inverted_items(declaration, declaration.marker)",
            "log.report_inverted_items(declaration, type(declaration.marker)())",
            "the declaration's own marker never reaches the warning, so an item comparing the wrong way is left "
            "unreported and reads as understood",
        ),
        Mutation(
            uv,
            "[replace(declaration, marker=_reported_marker(declaration, log)) for declaration in declarations]",
            "[replace(declaration, marker=_reported_marker(declaration, log)) "
            "if declaration.current_version else declaration for declaration in declarations]",
            "a dependency that pins no exact version has its marker reported nowhere, so what that marker gets wrong "
            "reaches nobody",
        ),
    )
    def test_an_inverted_comparison_is_reported(self, run: Mock, get: Mock, glob: Mock):
        """Test that each item running the wrong way round is reported, and the dependency updates as usual."""
        messages = {
            "stale>=90": Logger._MESSAGE_INVERTED_STALE_ITEM,
            "cooldown>=30": Logger._MESSAGE_INVERTED_COOLDOWN_ITEM,
            "vulnerable>=high": Logger._MESSAGE_INVERTED_VULNERABLE_ITEM,
        }
        for spec in ("package==1.0", "package>=1.0"):
            for item, message in messages.items():
                with self.subTest(spec=spec, item=item):
                    mock_pyproject_toml = self.updated_pyproject_toml(
                        run, get, glob, pyproject(spec, marker=f"ignore[{item}]")
                    )
                    location = Location(mock_pyproject_toml, 2)
                    self.assert_logged(message, item=item, dependency="package", location=location)
                    self.assert_new_version_logged("package", _NEW_VERSION, location)

    @kills(
        Mutation(
            uv,
            "return acted_on",
            "return acted_on.frozen if any(threshold.inverted_item for threshold in "
            "(declaration.marker.stale, declaration.marker.cooldown, declaration.marker.vulnerable)) else acted_on",
            "an item comparing the wrong way round freezes the pin, as an item that cannot be read at all does",
        )
    )
    def test_a_pin_with_inverted_comparisons_still_updates(self, run: Mock, get: Mock, glob: Mock):
        """Test that items comparing the wrong way round leave the pin updated and its marker recognised."""
        items = "stale>=90, cooldown>=30, vulnerable>=high"
        mock_pyproject_toml, location = self.updated_marked_pin(run, get, glob, f"ignore[{items}]")
        mock_pyproject_toml.write_text.assert_called_once_with(pyproject("package==1.1", marker=f"ignore[{items}]"))
        self.assert_new_version_logged("package", _NEW_VERSION, location)
        inverted = Marker(
            stale=Threshold(inverted_item="stale>=90"),
            cooldown=Threshold(inverted_item="cooldown>=30"),
            vulnerable=Threshold(inverted_item="vulnerable>=high"),
        )
        self.assert_recognised_marker_logged("package", location, inverted)

    def test_update_with_changelog(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml with changelog."""
        run.return_value = self.mock_update_on_stdout("package_with_changelog", "v1.1")
        get.side_effect = [
            mock_response(self.pypi_metadata()),
            Mock(headers={"Content-Type": "text"}, text=CHANGELOG),
        ]
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_with_changelog==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_with_changelog==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            "package_with_changelog",
            "1.1, published: 2026-05-30 12:07",
            Location(mock_pyproject_toml, 2),
            CHANGELOG,
        )
        self.assert_no_warnings_logged()

    def test_update_with_html_changelog(self, run: Mock, get: Mock, glob: Mock):
        """Test that updating a pyproject.toml with only a HTML changelog ignores the changelog."""
        run.return_value = self.mock_update_on_stdout("package_with_html_changelog", "v1.1")
        get.side_effect = [
            mock_response(self.pypi_metadata()),
            Mock(text=CHANGELOG, headers={"Content-Type": "text/html"}),
            mock_response([github_release_json("v1.1")]),
            mock_response([]),
        ]
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_with_html_changelog==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_with_html_changelog==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            "package_with_html_changelog", "1.1, published: 2026-05-30 12:07", Location(mock_pyproject_toml, 2)
        )
        self.assert_no_warnings_logged()

    def test_update_with_github_url(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml with GitHub releases."""
        run.return_value = self.mock_update_on_stdout("package_with_github_releases", "v1.1")
        get.side_effect = [
            mock_response(self.pypi_metadata(changelog_url="")),
            mock_response([github_release_json("v1.1", body=CHANGELOG)]),
            mock_response(github_commits_json()),
        ]
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_with_github_releases==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_with_github_releases==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            "package_with_github_releases",
            "1.1, published: 2026-05-30 12:07",
            Location(mock_pyproject_toml, 2),
            CHANGELOG,
        )
        self.assert_no_warnings_logged()

    def test_update_without_github_url(self, run: Mock, get: Mock, glob: Mock):
        """Test updating a pyproject.toml without a GitHub URL."""
        run.return_value = self.mock_update_on_stdout("package_without_github_releases", "v1.1")
        get.return_value = mock_response(self.pypi_metadata(changelog_url="", repository="https://gitlab.com/org/repo"))
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package_without_github_releases==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        mock_pyproject_toml.write_text.assert_called_with(pyproject("package_without_github_releases==1.1"))
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_new_version_logged(
            "package_without_github_releases",
            "1.1, published: 2026-05-30 12:07",
            Location(mock_pyproject_toml, 2),
        )
        self.assert_no_warnings_logged()

    def test_unchanged(self, run: Mock, get: Mock, glob: Mock):
        """Test that the pyproject.toml is not written if there are no changes."""
        mock_pyproject_toml = self.updated_pyproject_toml(run, get, glob, pyproject("package==1.0"), latest="")
        mock_pyproject_toml.write_text.assert_not_called()
        get.assert_not_called()
        self.assert_path_logged(mock_pyproject_toml.parent / "uv.lock")
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_uv_lock_skipped_when_uv_tree_fails(self, run: Mock, get: Mock, glob: Mock):
        """Test that a failed uv tree (e.g. offline) skips the futile uv lock, logging only uv tree's failure."""
        run.side_effect = subprocess.CalledProcessError(cmd="", returncode=2, output="", stderr="error: offline")
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        commands = [call.args[0][:2] for call in run.call_args_list]
        self.assertIn(("uv", "tree"), commands)
        self.assertNotIn(("uv", "lock"), commands)  # uv lock is skipped because uv tree failed
        mock_pyproject_toml.write_text.assert_not_called()
        get.assert_not_called()
        self.assert_command_stderr_logged(stderr="error: offline")  # only uv tree's stderr is surfaced

    def test_skip_non_uv_tool_section(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pyproject.toml with a non-uv tool section (e.g. [tool.poetry]) is skipped without running uv."""
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0") + '\n[tool.poetry]\nname = "x"\n')
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        run.assert_not_called()
        get.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_unsupported_package_manager_logged(mock_pyproject_toml, "poetry", "uv")
        self.assert_no_new_version_logged()

    @patch_pathlib_path(exists=True)
    def test_skip_non_uv_lockfile(self, run: Mock, get: Mock, glob: Mock):
        """Test that a pyproject.toml with a non-uv lockfile (e.g. poetry.lock) is skipped without running uv."""
        mock_pyproject_toml = self.create_pyproject_toml(pyproject("package==1.0"))
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        run.assert_not_called()
        get.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_unsupported_package_manager_logged(mock_pyproject_toml, "poetry", "uv")
        self.assert_no_new_version_logged()

    def test_skip_invalid_pyproject_toml(self, run: Mock, get: Mock, glob: Mock):
        """Test that an unparsable pyproject.toml is skipped with a warning, without running uv or crashing."""
        mock_pyproject_toml = self.create_pyproject_toml('[project]\ndependencies = ["package==1.0"\n')  # missing ]
        glob.return_value = [mock_pyproject_toml]
        update_pyproject_tomls()
        run.assert_not_called()
        get.assert_not_called()
        mock_pyproject_toml.write_text.assert_not_called()
        self.assert_invalid_pyproject_toml_logged(mock_pyproject_toml)
        self.assert_no_new_version_logged()


@patch("update_time.package_managers.uv.configure_cooldown", Mock())
@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("requests.get")
@patch("subprocess.run")
class CheckedPinsTest(LoggingTestCase):
    """Unit test for handing the discovered manifests to the checks uv-delegated updaters share."""

    @patch("update_time.updaters.update_pyproject_toml.warn_about_pins")
    def test_the_discovered_manifests_are_checked(self, warn: Mock, run: Mock, get: Mock, glob: Mock):
        """Test that the checks are handed every manifest the scan found, whether uv updated it or not."""
        run.return_value = Mock(stdout="| django\n")
        pyproject_toml = _discovered_pyproject_toml(glob, "django==3.2.0")
        update_pyproject_tomls()
        get.assert_not_called()
        warn.assert_called_once_with([PyprojectToml(pyproject_toml)], ANY)
