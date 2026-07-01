"""Unit tests for the jsdelivr CDN URLs update script."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from update_time.domain.cooldown import COOLDOWN_DAYS
from update_time.updaters.update_jsdelivr import get_latest_version, update_jsdelivr, update_jsdelivrs

from tests.update_time.assertions import assert_success
from tests.update_time.fixtures import HASH1, HASH2
from tests.update_time.helpers import LoggingTestCase, mock_path, mock_response

# A flat package listing as returned by the jsDelivr API with the ?structure=flat query parameter.
FLAT_FILES = {"default": "/dist/clipboard.min.js", "files": [{"name": "/dist/clipboard.min.js", "hash": HASH2}]}

# npm publication dates, relative to now so the cooldown decision is independent of the wall clock.
ELIGIBLE = (datetime.now(UTC) - timedelta(days=COOLDOWN_DAYS + 1)).isoformat()  # comfortably past the cooldown
FRESH = (datetime.now(UTC) - timedelta(days=1)).isoformat()  # still within the cooldown

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


def jsdelivr_versions(*version_strings: str) -> Mock:
    """Return a mock jsDelivr package API response listing the given versions (newest first)."""
    return mock_response({"versions": [{"version": version} for version in version_strings]})


def npm_registry(published: dict[str, str]) -> Mock:
    """Return a mock npm registry response mapping versions to their publication dates."""
    return mock_response({"time": published})


@patch("requests.get")
class GetLatestVersionTest(LoggingTestCase):
    """Unit tests for the get latest jsdelivr version function."""

    def test_unchanged_when_current_is_newest(self, mock_get: Mock):
        """Test that no newer version keeps the current version and fetches no integrity hash."""
        mock_get.side_effect = [jsdelivr_versions("1.0", "0.9")]
        latest_version = get_latest_version("clipboard", "1.0")
        self.assertEqual("1.0", latest_version.version)
        self.assertEqual("", latest_version.sha)

    def test_newer_version_bumps_and_fetches_hash(self, mock_get: Mock):
        """Test that an eligible newer version is adopted together with its integrity hash."""
        mock_get.side_effect = [
            jsdelivr_versions("1.1", "1.0"),
            npm_registry({"1.1": ELIGIBLE}),
            mock_response(FLAT_FILES),
        ]
        latest_version = get_latest_version("clipboard", "1.0")
        self.assertEqual("1.1", latest_version.version)
        self.assertEqual(f"sha256-{HASH2}", latest_version.sha)

    def test_fresh_version_held_back(self, mock_get: Mock):
        """Test that a version within the cooldown is skipped and the newest eligible older version is chosen."""
        mock_get.side_effect = [
            jsdelivr_versions("2.0", "1.5", "1.0"),
            npm_registry({"2.0": FRESH}),  # too fresh, skipped
            npm_registry({"1.5": ELIGIBLE}),  # eligible, chosen
            mock_response(FLAT_FILES),
        ]
        latest_version = get_latest_version("clipboard", "1.0")
        self.assertEqual("1.5", latest_version.version)
        self.assertEqual(f"sha256-{HASH2}", latest_version.sha)

    def test_all_newer_versions_within_cooldown(self, mock_get: Mock):
        """Test that the current version is kept when every newer version is still within the cooldown."""
        mock_get.side_effect = [jsdelivr_versions("2.0", "1.0"), npm_registry({"2.0": FRESH})]
        latest_version = get_latest_version("clipboard", "1.0")
        self.assertEqual("1.0", latest_version.version)
        self.assertEqual("", latest_version.sha)

    def test_prerelease_ignored(self, mock_get: Mock):
        """Test that a newer pre-release is not adopted (and no publication date is looked up for it)."""
        mock_get.side_effect = [jsdelivr_versions("2.0.0-rc.1", "1.0")]
        self.assertEqual("1.0", get_latest_version("clipboard", "1.0").version)

    def test_version_without_publication_date_skipped(self, mock_get: Mock):
        """Test that a version the npm registry has no release date for yet is treated as too fresh and skipped."""
        mock_get.side_effect = [jsdelivr_versions("1.1", "1.0"), npm_registry({})]
        self.assertEqual("1.0", get_latest_version("clipboard", "1.0").version)

    def test_unparsable_current_version_left_alone(self, mock_get: Mock):
        """Test that an unparsable current version (e.g. a trailing dot) is left unchanged, querying nothing."""
        self.assertEqual("1.", get_latest_version("clipboard", "1.").version)
        mock_get.assert_not_called()


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
