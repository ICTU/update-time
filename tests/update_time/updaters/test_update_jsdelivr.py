"""Unit tests for the jsdelivr CDN URLs update script."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from update_time.domain.cooldown import COOLDOWN_DAYS
from update_time.updaters.update_jsdelivr import update_jsdelivr, update_jsdelivrs

from tests.update_time.assertions import assert_success
from tests.update_time.fixtures import HASH1, HASH2
from tests.update_time.helpers import LoggingTestCase, mock_path, mock_response

# The flat package listing as returned by the jsDelivr API with ?structure=flat, referencing the file below.
FILENAME = "/dist/clipboard.min.js"
FLAT_FILES = {"default": FILENAME, "files": [{"name": FILENAME, "hash": HASH2}]}

# An npm publication date comfortably past the cooldown, relative to now so the decision doesn't depend on the clock.
ELIGIBLE = (datetime.now(UTC) - timedelta(days=COOLDOWN_DAYS + 1)).isoformat()


def jsdelivr_versions(*version_strings: str) -> Mock:
    """Return a mock jsDelivr package API response listing the given versions (newest first)."""
    return mock_response({"versions": [{"version": version} for version in version_strings]})


def npm_registry(published: dict[str, str]) -> Mock:
    """Return a mock npm registry response mapping versions to their publication dates."""
    return mock_response({"time": published})


# The relevant part of the Sphinx config, formatted as Ruff would format it.
CONF = (
    "html_js_files = [\n"
    "    (\n"
    '        "https://cdn.jsdelivr.net/npm/clipboard@2.0.11/dist/clipboard.min.js",\n'
    f'        {{"integrity": "sha256-{HASH1}", "crossorigin": "anonymous"}},\n'
    "    ),\n"
    '    "copy_button.js",\n'
    "]\n"
)


@patch("requests.get")
class UpdateJsdelivrTest(LoggingTestCase):
    """Unit tests for rewriting the version and integrity hash in the Sphinx config."""

    def test_new_version_and_hash(self, mock_get: Mock):
        """Test that both the version and the integrity hash are updated on a bump."""
        mock_get.side_effect = [
            jsdelivr_versions("2.0.12", "2.0.11"),
            npm_registry({"2.0.12": ELIGIBLE}),
            mock_response(FLAT_FILES),
        ]
        conf_py = Path.cwd() / "docs" / "conf.py"
        new_content = update_jsdelivr(CONF, conf_py)
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", new_content)
        self.assertIn(f'"integrity": "sha256-{HASH2}"', new_content)
        self.assertNotIn("2.0.11", new_content)
        self.assertNotIn(HASH1, new_content)
        self.assert_new_version_logged(conf_py, "clipboard", ANY, "No changelog available!")
        self.assert_no_warnings_logged()

    def test_version_lookalike_between_url_and_hash_is_left_untouched(self, mock_get: Mock):
        """Test that a version substring appearing between the URL and its integrity hash isn't rewritten.

        The match spans (with re.DOTALL) from the URL to the integrity line, so a naive replace would also bump a
        version-lookalike in that span; only the captured version pin should change.
        """
        conf = (
            "html_js_files = [\n"
            "    (\n"
            '        "https://cdn.jsdelivr.net/npm/clipboard@2.0.11/dist/clipboard.min.js",\n'
            "        # do not remove 2.0.11 note\n"
            f'        {{"integrity": "sha256-{HASH1}", "crossorigin": "anonymous"}},\n'
            "    ),\n"
            "]\n"
        )
        mock_get.side_effect = [
            jsdelivr_versions("2.0.12", "2.0.11"),
            npm_registry({"2.0.12": ELIGIBLE}),
            mock_response(FLAT_FILES),
        ]
        new_content = update_jsdelivr(conf, Path.cwd() / "docs" / "conf.py")
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", new_content)  # the pin is bumped
        self.assertIn("# do not remove 2.0.11 note", new_content)  # the lookalike is preserved
        self.assertIn(f'"integrity": "sha256-{HASH2}"', new_content)
        self.assertNotIn(HASH1, new_content)

    def test_unchanged(self, mock_get: Mock):
        """Test that the content is unchanged if there is no new version."""
        mock_get.side_effect = [jsdelivr_versions("2.0.11")]
        self.assertEqual(CONF, update_jsdelivr(CONF, Path.cwd() / "docs" / "conf.py"))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


@patch("pathlib.Path.rglob")
@patch("requests.get")
class UpdateJsdelivrsTest(LoggingTestCase):
    """Unit tests for discovering and updating the Sphinx config files under docs/."""

    def test_changes(self, mock_get: Mock, mock_glob: Mock):
        """Test that a discovered Sphinx config is updated when a new eligible version is available."""
        mock_get.side_effect = [
            jsdelivr_versions("2.0.12", "2.0.11"),
            npm_registry({"2.0.12": ELIGIBLE}),
            mock_response(FLAT_FILES),
        ]
        mock_conf = mock_path(CONF)
        mock_glob.return_value = [mock_conf]
        assert_success(update_jsdelivrs())
        written = mock_conf.write_text.call_args.args[0]
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", written)
        self.assertIn(f'"integrity": "sha256-{HASH2}"', written)
        self.assert_path_logged(mock_conf)
        self.assert_new_version_logged(mock_conf, "clipboard", ANY, ANY)
        self.assert_no_warnings_logged()

    def test_no_changes(self, mock_get: Mock, mock_glob: Mock):
        """Test that a discovered Sphinx config is not rewritten when there is no new version."""
        mock_get.side_effect = [jsdelivr_versions("2.0.11")]
        mock_conf = mock_path(CONF)
        mock_glob.return_value = [mock_conf]
        assert_success(update_jsdelivrs())
        mock_conf.write_text.assert_not_called()
        self.assert_path_logged(mock_conf)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()
