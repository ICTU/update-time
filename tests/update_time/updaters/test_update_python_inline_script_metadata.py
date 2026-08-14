"""Unit tests for the inline-script-metadata updater (discovery and orchestration of the uv package manager)."""

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import ANY, Mock, patch

from update_time.primitives.location import Location
from update_time.updaters.update_python_inline_script_metadata import update_python_inline_script_metadatas

from tests.helpers import mock_path, mock_response, patch_pathlib_path
from tests.update_time.helpers import (
    LoggingTestCase,
)

if TYPE_CHECKING:
    from update_time.primitives.command import Command
    from update_time.sources.pypi import Release


def _script(*specs: str, requires_python: str = ">=3.11") -> str:
    """Return a minimal .py file with a PEP 723 `# /// script` block pinning the given dependencies."""
    dependencies = "".join(f'#     "{spec}",\n' for spec in specs)
    return (
        "# /// script\n"
        f'# requires-python = "{requires_python}"\n'
        "# dependencies = [\n"
        f"{dependencies}"
        "# ]\n"
        "# ///\n"
        'print("hi")\n'
    )


def _discovered_script(glob: Mock, spec: str) -> Mock:
    """Return the single mock .py file the scan discovers, its inline block pinning the given dependency."""
    script_file = mock_path(_script(spec), parent=Path("/"))
    glob.return_value = [script_file]
    return script_file


# The checks over the settled pins are stubbed out so these tests focus on the discovery/version-update flow: each
# check makes registry requests of its own, and what they report is `test_uv_pins.py`'s to pin, while being handed
# the scripts found is `CheckedInlineScriptPinsTest`'s below.
@patch("update_time.updaters.update_python_inline_script_metadata.warn_about_pins", Mock())
@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("requests.get")
@patch("subprocess.run")
class UpdatePythonInlineScriptMetadatasTest(LoggingTestCase):
    """Unit tests for the update python-inline-script-metadatas function."""

    changelog: ClassVar = "Changelog"

    @staticmethod
    def pypi_metadata() -> Release:
        """Create PyPI release metadata fixture carrying a changelog URL."""
        return {
            "info": {
                "description": "Package description",
                "project_urls": {"Homepage": "https://home", "Changelog": "https://changelog"},
            },
            "urls": [{"upload_time_iso_8601": "2026-05-30T12:07:03.123456Z"}],
        }

    def create_script(self, contents: str) -> Mock:
        """Create a mock .py file with inline script metadata."""
        return mock_path(contents, parent=Path("/"))

    def mock_update_on_stdout(self, package: str, latest: str = "") -> Mock:
        """Mock `uv tree --script` stdout for a top-level dependency, with an optional available update.

        A script's direct dependencies carry no tree prefix (each is its own tree root), so the line is just
        `package v<current>[ (latest: v<latest>)]` — the exact shape the updater parses.
        """
        update = f" (latest: {latest})" if latest else ""
        return Mock(stdout=f"{package} v1.0{update}\n")

    def uv_commands(self, run: Mock) -> list[Command]:
        """Return the argv of every command run() invoked."""
        return [call.args[0] for call in run.call_args_list]

    def test_update(self, run: Mock, get: Mock, glob: Mock):
        """Test bumping an exact pin in an inline `# /// script` block."""
        run.return_value = self.mock_update_on_stdout("package", "v1.1")
        get.return_value = mock_response(
            {"info": {"description": "Package"}, "urls": [{"upload_time_iso_8601": "2026-05-30T12:08:53.123321Z"}]}
        )
        mock_script = self.create_script(_script("package==1.0"))
        glob.return_value = [mock_script]
        update_python_inline_script_metadatas()
        mock_script.write_text.assert_called_with(_script("package==1.1"))
        self.assert_new_version_logged("package", "1.1, published: 2026-05-30 12:08", Location(mock_script, 4))
        self.assert_no_warnings_logged()

    def test_update_with_changelog(self, run: Mock, get: Mock, glob: Mock):
        """Test bumping an exact pin whose new version has a changelog, which is logged."""
        run.return_value = self.mock_update_on_stdout("package_with_changelog", "v1.1")
        get.side_effect = [
            mock_response(self.pypi_metadata()),
            Mock(headers={"Content-Type": "text"}, text=self.changelog),
        ]
        mock_script = self.create_script(_script("package_with_changelog==1.0"))
        glob.return_value = [mock_script]
        update_python_inline_script_metadatas()
        mock_script.write_text.assert_called_with(_script("package_with_changelog==1.1"))
        self.assert_new_version_logged(
            "package_with_changelog", "1.1, published: 2026-05-30 12:07", Location(mock_script, 4), self.changelog
        )
        self.assert_no_warnings_logged()

    def test_uv_command_targets_the_script_with_the_cooldown(self, run: Mock, get: Mock, glob: Mock):
        """Test that uv is run against the script with `--depth=0` and the cooldown as an `--exclude-newer` cutoff."""
        run.return_value = self.mock_update_on_stdout("package")
        glob.return_value = [self.create_script(_script("package==1.0"))]
        update_python_inline_script_metadatas()
        get.assert_not_called()  # nothing outdated, so no changelog is fetched
        command = self.uv_commands(run)[0]
        self.assertEqual(command[:3], ("uv", "tree", "--script"))
        self.assertIn("--depth=0", command)
        self.assertTrue(command[command.index("--exclude-newer") + 1])  # a non-empty cutoff follows the flag
        self.assertNotIn(("uv", "lock"), [command[:2] for command in self.uv_commands(run)])  # scripts have no lockfile

    def test_only_outdated_pin_is_rewritten(self, run: Mock, get: Mock, glob: Mock):
        """Test that only the dependency uv reports as outdated is rewritten; the rest of the block is preserved."""
        run.return_value = self.mock_update_on_stdout("package", "v1.1")
        get.return_value = mock_response({"info": {"description": "Package"}, "urls": []})
        mock_script = self.create_script(_script("package==1.0", "other==2.0", requires_python=">=3.14"))
        glob.return_value = [mock_script]
        update_python_inline_script_metadatas()
        # `other` is not reported outdated, and `requires-python` and the comment prefixes/trailing commas are kept.
        mock_script.write_text.assert_called_with(_script("package==1.1", "other==2.0", requires_python=">=3.14"))

    def test_unchanged(self, run: Mock, get: Mock, glob: Mock):
        """Test that the script is not written when uv reports nothing outdated."""
        run.return_value = self.mock_update_on_stdout("package")
        mock_script = self.create_script(_script("package==1.0"))
        glob.return_value = [mock_script]
        update_python_inline_script_metadatas()
        mock_script.write_text.assert_not_called()
        get.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_skip_py_file_without_script_block(self, run: Mock, get: Mock, glob: Mock):
        """Test that a .py file without a `# /// script` block is left untouched and does not invoke uv."""
        mock_script = self.create_script("import os\n\nprint(os.getcwd())\n")
        glob.return_value = [mock_script]
        update_python_inline_script_metadatas()
        run.assert_not_called()
        get.assert_not_called()
        mock_script.write_text.assert_not_called()
        self.assert_no_new_version_logged()


@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("requests.get")
@patch("subprocess.run")
class CheckedInlineScriptPinsTest(LoggingTestCase):
    """Unit test for handing the discovered scripts to the checks uv-delegated updaters share."""

    @patch("update_time.updaters.update_python_inline_script_metadata.warn_about_pins")
    def test_the_discovered_scripts_are_checked(self, warn: Mock, run: Mock, get: Mock, glob: Mock):
        """Test that the checks are handed every script the scan found, whether uv updated it or not."""
        run.return_value = Mock(stdout="django v3.2.0\n")
        script_file = _discovered_script(glob, "django==3.2.0")
        update_python_inline_script_metadatas()
        get.assert_not_called()
        warn.assert_called_once_with([script_file], ANY)
