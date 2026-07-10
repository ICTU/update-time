"""Unit tests for the jsDelivr source."""

from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, Mock, patch

from update_time.domain.cooldown import COOLDOWN_DAYS
from update_time.io.log import Logger
from update_time.sources.jsdelivr import get_latest_version

from tests.update_time.fixtures import HASH1, HASH2
from tests.update_time.helpers import (
    LoggingTestCase,
    jsdelivr_versions,
    mock_response,
    npm_registry,
    staleness_disabled,
)

# The file referenced in the jsDelivr URL, and a flat package listing as returned by the API with ?structure=flat.
FILENAME = "/dist/clipboard.min.js"
FLAT_FILES = {"default": FILENAME, "files": [{"name": FILENAME, "hash": HASH2}]}

# npm publication dates, relative to now so the cooldown decision is independent of the wall clock.
ELIGIBLE = (datetime.now(UTC) - timedelta(days=COOLDOWN_DAYS + 1)).isoformat()  # comfortably past the cooldown
FRESH = (datetime.now(UTC) - timedelta(days=1)).isoformat()  # still within the cooldown


@patch("requests.get")
class GetLatestVersionTest(LoggingTestCase):
    """Unit tests for the get latest jsdelivr version function.

    `get_latest_version` makes its requests in a fixed order, so each test's `mock_get.side_effect` mirrors it:
    first the jsDelivr package API (the version list), then — newest-first — one npm registry call per candidate to
    read its publication date, stopping at the first eligible one, then the jsDelivr flat-files API for the chosen
    version's integrity hash. A test supplies only as many responses as reaching its expected version requires.
    """

    def test_unchanged_when_current_is_newest(self, mock_get: Mock):
        """Test that no newer version keeps the current version and fetches no integrity hash."""
        mock_get.side_effect = [jsdelivr_versions("1.0", "0.9"), npm_registry({"1.0": ELIGIBLE})]
        latest_version = get_latest_version("clipboard", "1.0", FILENAME)
        self.assertEqual("1.0", latest_version.version)
        self.assertEqual("", latest_version.sha)

    def test_newer_version_bumps_and_fetches_hash(self, mock_get: Mock):
        """Test that an eligible newer version is adopted together with its integrity hash."""
        mock_get.side_effect = [
            jsdelivr_versions("1.1", "1.0"),
            npm_registry({"1.1": ELIGIBLE}),
            mock_response(FLAT_FILES),
        ]
        latest_version = get_latest_version("clipboard", "1.0", FILENAME)
        self.assertEqual("1.1", latest_version.version)
        self.assertEqual(f"sha256-{HASH2}", latest_version.sha)

    def test_fresh_version_held_back(self, mock_get: Mock):
        """Test that a version within the cooldown is skipped and the newest eligible older version is chosen."""
        mock_get.side_effect = [
            jsdelivr_versions("2.0", "1.5", "1.0"),
            npm_registry({"2.0": FRESH, "1.5": ELIGIBLE}),  # one registry doc: 2.0 too fresh, 1.5 eligible
            mock_response(FLAT_FILES),
        ]
        latest_version = get_latest_version("clipboard", "1.0", FILENAME)
        self.assertEqual("1.5", latest_version.version)
        self.assertEqual(f"sha256-{HASH2}", latest_version.sha)

    def test_all_newer_versions_within_cooldown(self, mock_get: Mock):
        """Test that the current version is kept when every newer version is still within the cooldown."""
        mock_get.side_effect = [jsdelivr_versions("2.0", "1.0"), npm_registry({"2.0": FRESH})]
        latest_version = get_latest_version("clipboard", "1.0", FILENAME)
        self.assertEqual("1.0", latest_version.version)
        self.assertEqual("", latest_version.sha)

    def test_prerelease_ignored(self, mock_get: Mock):
        """Test that a newer pre-release is not adopted (and no publication date is looked up for it)."""
        mock_get.side_effect = [jsdelivr_versions("2.0.0-rc.1", "1.0"), npm_registry({"1.0": ELIGIBLE})]
        self.assertEqual("1.0", get_latest_version("clipboard", "1.0", FILENAME).version)

    def test_version_without_publication_date_skipped(self, mock_get: Mock):
        """Test that a version the npm registry has no release date for yet is treated as too fresh and skipped."""
        mock_get.side_effect = [jsdelivr_versions("1.1", "1.0"), npm_registry({})]
        self.assertEqual("1.0", get_latest_version("clipboard", "1.0", FILENAME).version)

    def test_unparsable_current_version_left_alone(self, mock_get: Mock):
        """Test that an unparsable current version (e.g. a trailing dot) is left unchanged, querying nothing."""
        self.assertEqual("1.", get_latest_version("clipboard", "1.", FILENAME).version)
        mock_get.assert_not_called()

    def test_package_api_unreachable_keeps_current(self, mock_get: Mock):
        """Test that an unreachable jsDelivr package API leaves the version unchanged instead of crashing."""
        mock_get.side_effect = [mock_response(ok=False), npm_registry({"1.0": ELIGIBLE})]
        self.assertEqual("1.0", get_latest_version("clipboard", "1.0", FILENAME).version)

    def test_integrity_fetch_failure_keeps_current(self, mock_get: Mock):
        """Test that a failed integrity-hash fetch leaves the version unchanged, so the hash can't fall out of sync."""
        mock_get.side_effect = [
            jsdelivr_versions("1.1", "1.0"),
            npm_registry({"1.1": ELIGIBLE}),
            mock_response(ok=False),  # the integrity-hash request fails
        ]
        latest_version = get_latest_version("clipboard", "1.0", FILENAME)
        self.assertEqual("1.0", latest_version.version)
        self.assertEqual("", latest_version.sha)

    def test_hashes_referenced_file_not_package_default(self, mock_get: Mock):
        """Test that the referenced file's hash is used, even when the package default isn't a listed file."""
        flat = {
            "default": "/es5/node-main.min.js",  # not in files (jsDelivr's default isn't always listed)
            "files": [{"name": "/es5/tex-mml-chtml.js", "hash": HASH2}, {"name": "/es5/other.js", "hash": HASH1}],
        }
        mock_get.side_effect = [
            jsdelivr_versions("3.3.0", "3.2.2"),
            npm_registry({"3.3.0": ELIGIBLE}),
            mock_response(flat),
        ]
        latest_version = get_latest_version("mathjax", "3.2.2", "/es5/tex-mml-chtml.js")
        self.assertEqual("3.3.0", latest_version.version)
        self.assertEqual(f"sha256-{HASH2}", latest_version.sha)  # from the referenced file, not the unlisted default

    def test_missing_referenced_file_hash_left_unchanged(self, mock_get: Mock):
        """Test that a version whose referenced file has no listed hash is left unchanged, and the reason is logged."""
        flat = {"default": FILENAME, "files": [{"name": "/other.js", "hash": HASH1}]}  # FILENAME is absent
        mock_get.side_effect = [jsdelivr_versions("1.1", "1.0"), npm_registry({"1.1": ELIGIBLE}), mock_response(flat)]
        latest_version = get_latest_version("clipboard", "1.0", FILENAME)
        self.assertEqual("1.0", latest_version.version)
        self.assertEqual("", latest_version.sha)
        message = Logger._MESSAGE_NO_INTEGRITY_HASH
        self.mock_warning.assert_called_once_with(message, "clipboard", "1.1", FILENAME, stacklevel=ANY)

    def test_newest_published_attached(self, mock_get: Mock):
        """Test that the package's newest npm publication date is attached for the staleness check."""
        old = (datetime.now(UTC) - timedelta(days=512)).isoformat()
        mock_get.side_effect = [jsdelivr_versions("1.0", "0.9"), npm_registry({"1.0": old})]
        self.assertEqual(datetime.fromisoformat(old), get_latest_version("clipboard", "1.0", FILENAME).newest_published)

    def test_newest_published_skipped_when_disabled(self, mock_get: Mock):
        """Test that the extra npm request is not made (and no date attached) when the staleness check is disabled.

        Only the jsDelivr version list is provided; a second (npm) request would raise, proving it isn't made.
        """
        mock_get.side_effect = [jsdelivr_versions("1.0", "0.9")]
        with staleness_disabled:
            latest_version = get_latest_version("clipboard", "1.0", FILENAME)
        self.assertIsNone(latest_version.newest_published)
