"""Unit tests for the jsDelivr source."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from update_time.domain.bound import NO_BOUND
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion, Yank
from update_time.io.log import Logger
from update_time.sources.jsdelivr import version_getter

from tests.helpers import mock_response
from tests.update_time.fixtures import HASH1, HASH2
from tests.update_time.helpers import LoggingTestCase, jsdelivr_versions, npm_registry, staleness_disabled

# The file referenced in the jsDelivr URL, and a flat package listing as returned by the API with ?structure=flat.
_FILENAME = "/dist/clipboard.min.js"
_FLAT_FILES = {"default": _FILENAME, "files": [{"name": _FILENAME, "hash": HASH2}]}


def _get_latest_version(
    dependency: str, current_version: str, filename: str, cooldown_days: int = COOLDOWN.default
) -> DependencyVersion:
    """Return the version the source resolves for the file the URL references, unbounded."""
    return version_getter(filename)(dependency, current_version, NO_BOUND, cooldown_days)


# npm publication dates, relative to now so the cooldown decision is independent of the wall clock.
_ELIGIBLE = (datetime.now(UTC) - timedelta(days=COOLDOWN.default + 1)).isoformat()  # comfortably past the cooldown
_FRESH = (datetime.now(UTC) - timedelta(days=1)).isoformat()  # still within the cooldown


@patch("requests.get")
class GetLatestVersionTest(LoggingTestCase):
    """Unit tests for the get latest jsdelivr version function.

    The source makes its requests in a fixed order, which each test's `mock_get.side_effect` mirrors: the jsDelivr
    package API for the version list, then one npm registry call whose `time` map dates every candidate, then the
    jsDelivr flat-files API for the chosen version's hash. A test supplies only the responses its version needs.
    """

    def test_unchanged_when_current_is_newest(self, mock_get: Mock):
        """Test that no newer version keeps the current version and fetches no integrity hash."""
        mock_get.side_effect = [jsdelivr_versions("1.0", "0.9"), npm_registry({"1.0": _ELIGIBLE})]
        latest_version = _get_latest_version("clipboard", "1.0", _FILENAME)
        self.assertEqual(latest_version.version, "1.0")
        self.assertEqual(latest_version.sha, "")

    def test_newer_version_bumps_and_fetches_hash(self, mock_get: Mock):
        """Test that an eligible newer version is adopted together with its integrity hash."""
        mock_get.side_effect = [
            jsdelivr_versions("1.1", "1.0"),
            npm_registry({"1.1": _ELIGIBLE}),
            mock_response(_FLAT_FILES),
        ]
        latest_version = _get_latest_version("clipboard", "1.0", _FILENAME)
        self.assertEqual(latest_version.version, "1.1")
        self.assertEqual(latest_version.sha, f"sha256-{HASH2}")

    def test_fresh_version_held_back(self, mock_get: Mock):
        """Test that a version within the cooldown is skipped and the newest eligible older version is chosen."""
        mock_get.side_effect = [
            jsdelivr_versions("2.0", "1.5", "1.0"),
            npm_registry({"2.0": _FRESH, "1.5": _ELIGIBLE}),  # one registry doc: 2.0 too fresh, 1.5 eligible
            mock_response(_FLAT_FILES),
        ]
        latest_version = _get_latest_version("clipboard", "1.0", _FILENAME)
        self.assertEqual(latest_version.version, "1.5")
        self.assertEqual(latest_version.sha, f"sha256-{HASH2}")

    def test_all_newer_versions_within_cooldown(self, mock_get: Mock):
        """Test that the current version is kept when every newer version is still within the cooldown."""
        mock_get.side_effect = [jsdelivr_versions("2.0", "1.0"), npm_registry({"2.0": _FRESH})]
        latest_version = _get_latest_version("clipboard", "1.0", _FILENAME)
        self.assertEqual(latest_version.version, "1.0")
        self.assertEqual(latest_version.sha, "")

    def test_cooldown_decides_eligibility(self, mock_get: Mock):
        """Test that a version is held back or adopted according to the cooldown the getter is passed."""
        published = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        for cooldown_days, expected in ((30, "1.0"), (5, "1.1")):
            with self.subTest(cooldown_days=cooldown_days):
                mock_get.side_effect = [
                    jsdelivr_versions("1.1", "1.0"),
                    npm_registry({"1.1": published}),
                    mock_response(_FLAT_FILES),
                ]
                dependency = f"clipboard{cooldown_days}"  # A fresh name per case, as the npm fetches are cached.
                latest = _get_latest_version(dependency, "1.0", _FILENAME, cooldown_days)
                self.assertEqual(latest.version, expected)

    def test_deprecated_version_skipped(self, mock_get: Mock):
        """Test that a newer version that is deprecated on npm is not adopted as an update."""
        mock_get.side_effect = [
            jsdelivr_versions("1.1", "1.0"),
            npm_registry({"1.1": _ELIGIBLE}, deprecated={"1.1": "use 2.0 instead"}),
        ]
        self.assertEqual(_get_latest_version("clipboard", "1.0", _FILENAME).version, "1.0")

    def test_deprecated_current_version_attached(self, mock_get: Mock):
        """Test that when the pin stays put on a deprecated version, its deprecation is attached as a yank."""
        mock_get.side_effect = [
            jsdelivr_versions("1.0", "0.9"),
            npm_registry({"1.0": _ELIGIBLE}, deprecated={"1.0": "use 2.0 instead"}),
        ]
        latest = _get_latest_version("clipboard", "1.0", _FILENAME)
        self.assertEqual(latest.version, "1.0")
        self.assertEqual(latest.yank, Yank(yanked=True, reason="use 2.0 instead"))

    def test_prerelease_ignored(self, mock_get: Mock):
        """Test that a newer pre-release is not adopted (and no publication date is looked up for it)."""
        mock_get.side_effect = [jsdelivr_versions("2.0.0-rc.1", "1.0"), npm_registry({"1.0": _ELIGIBLE})]
        self.assertEqual(_get_latest_version("clipboard", "1.0", _FILENAME).version, "1.0")

    def test_version_without_publication_date_skipped(self, mock_get: Mock):
        """Test that a version the npm registry has no release date for yet is treated as too fresh and skipped."""
        mock_get.side_effect = [jsdelivr_versions("1.1", "1.0"), npm_registry({})]
        self.assertEqual(_get_latest_version("clipboard", "1.0", _FILENAME).version, "1.0")

    def test_unparsable_current_version_left_alone(self, mock_get: Mock):
        """Test that an unparsable current version (e.g. a trailing dot) is left unchanged, querying nothing."""
        self.assertEqual(_get_latest_version("clipboard", "1.", _FILENAME).version, "1.")
        mock_get.assert_not_called()

    def test_package_api_unreachable_keeps_current(self, mock_get: Mock):
        """Test that an unreachable jsDelivr package API leaves the version unchanged instead of crashing."""
        mock_get.side_effect = [mock_response(ok=False), npm_registry({"1.0": _ELIGIBLE})]
        self.assertEqual(_get_latest_version("clipboard", "1.0", _FILENAME).version, "1.0")

    def test_integrity_fetch_failure_keeps_current(self, mock_get: Mock):
        """Test that a failed integrity-hash fetch leaves the version unchanged, so the hash can't fall out of sync."""
        mock_get.side_effect = [
            jsdelivr_versions("1.1", "1.0"),
            npm_registry({"1.1": _ELIGIBLE}),
            mock_response(ok=False),  # the integrity-hash request fails
        ]
        latest_version = _get_latest_version("clipboard", "1.0", _FILENAME)
        self.assertEqual(latest_version.version, "1.0")
        self.assertEqual(latest_version.sha, "")

    def test_hashes_referenced_file_not_package_default(self, mock_get: Mock):
        """Test that the referenced file's hash is used, even when the package default isn't a listed file."""
        flat = {
            "default": "/es5/node-main.min.js",  # not in files (jsDelivr's default isn't always listed)
            "files": [{"name": "/es5/tex-mml-chtml.js", "hash": HASH2}, {"name": "/es5/other.js", "hash": HASH1}],
        }
        mock_get.side_effect = [
            jsdelivr_versions("3.3.0", "3.2.2"),
            npm_registry({"3.3.0": _ELIGIBLE}),
            mock_response(flat),
        ]
        latest_version = _get_latest_version("mathjax", "3.2.2", "/es5/tex-mml-chtml.js")
        self.assertEqual(latest_version.version, "3.3.0")
        self.assertEqual(latest_version.sha, f"sha256-{HASH2}")  # from the referenced file, not the unlisted default

    def test_missing_referenced_file_hash_left_unchanged(self, mock_get: Mock):
        """Test that a version whose referenced file has no listed hash is left unchanged, and the reason is logged."""
        flat = {"default": _FILENAME, "files": [{"name": "/other.js", "hash": HASH1}]}  # _FILENAME is absent
        mock_get.side_effect = [jsdelivr_versions("1.1", "1.0"), npm_registry({"1.1": _ELIGIBLE}), mock_response(flat)]
        latest_version = _get_latest_version("clipboard", "1.0", _FILENAME)
        self.assertEqual(latest_version.version, "1.0")
        self.assertEqual(latest_version.sha, "")
        message = Logger._MESSAGE_NO_INTEGRITY_HASH
        self.assert_logged(message, dependency="clipboard", version="1.1", filename=_FILENAME)

    def test_newest_published_attached(self, mock_get: Mock):
        """Test that the package's newest npm publication date is attached for the staleness check."""
        old = (datetime.now(UTC) - timedelta(days=512)).isoformat()
        mock_get.side_effect = [jsdelivr_versions("1.0", "0.9"), npm_registry({"1.0": old})]
        self.assertEqual(
            datetime.fromisoformat(old), _get_latest_version("clipboard", "1.0", _FILENAME).newest_published
        )

    def test_newest_published_attached_when_globally_disabled(self, mock_get: Mock):
        """Test that the date is attached even with the global check disabled, so a marker can set its own threshold."""
        old = (datetime.now(UTC) - timedelta(days=512)).isoformat()
        mock_get.side_effect = [jsdelivr_versions("1.0", "0.9"), npm_registry({"1.0": old})]
        with staleness_disabled:
            latest_version = _get_latest_version("clipboard", "1.0", _FILENAME)
        self.assertEqual(datetime.fromisoformat(old), latest_version.newest_published)
