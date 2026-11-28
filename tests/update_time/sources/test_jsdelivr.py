"""Unit tests for the jsDelivr source."""

from typing import TYPE_CHECKING, Final

from mutation_time.mutation import kills
from update_time.domain.bound import NO_BOUND
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion, PinnedDependency, Unserved, Yank
from update_time.io.log import Logger
from update_time.sources.jsdelivr import version_getter

from tests.http_mocks import mock_response, patch_get, unparsable_response
from tests.update_time.base_test_cases import LoggingTestCase
from tests.update_time.domain_objects import days_ago
from tests.update_time.fixtures import FRESH_DATE, HASH, HASH1, HASH2, PAST_COOLDOWN_DATE
from tests.update_time.io.log_messages import message_of

from .npm_answers import jsdelivr_versions, npm_registry

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from unittest.mock import Mock

# The file referenced in the jsDelivr URL, and a flat package listing as returned by the API with ?structure=flat.
_FILENAME: Final[str] = "/dist/clipboard.min.js"
_FLAT_FILES: Final[dict[str, Sequence[Collection[str]]]] = {
    "default": _FILENAME,
    "files": [{"name": _FILENAME, "hash": HASH}],
}


# npm publication dates, relative to now so the cooldown decision is independent of the wall clock.
_ELIGIBLE: Final[str] = PAST_COOLDOWN_DATE.isoformat()
_FRESH: Final[str] = FRESH_DATE.isoformat()


@patch_get()
class GetLatestVersionTest(LoggingTestCase):
    """Unit tests for the get latest jsdelivr version function.

    The source makes its requests in a fixed order, which each test's `mock_get.side_effect` mirrors.
    """

    def test_which_answers_keep_the_current_version(self, mock_get: Mock):
        """Test which answers keep the current version, without an integrity hash."""
        cases = {
            "no newer version": [jsdelivr_versions("1.0", "0.9"), npm_registry({"1.0": _ELIGIBLE})],
            "every newer version inside the cooldown": [jsdelivr_versions("2.0", "1.0"), npm_registry({"2.0": _FRESH})],
            "a deprecated newer version": [
                jsdelivr_versions("1.1", "1.0"),
                npm_registry({"1.1": _ELIGIBLE}, deprecated={"1.1": "use 2.0 instead"}),
            ],
            "a newer pre-release": [jsdelivr_versions("2.0.0-rc.1", "1.0"), npm_registry({"1.0": _ELIGIBLE})],
            "a newer version without a publication date": [jsdelivr_versions("1.1", "1.0"), npm_registry({})],
            "an unreachable package API": [mock_response(ok=False), npm_registry({"1.0": _ELIGIBLE})],
            "a failing integrity hash request": [
                jsdelivr_versions("1.1", "1.0"),
                npm_registry({"1.1": _ELIGIBLE}),
                mock_response(ok=False),
            ],
        }
        for case, answers in cases.items():
            with self.subTest(case=case):
                mock_get.side_effect = answers
                latest_version = _get_latest_version("clipboard", "1.0", _FILENAME)
                self.assertEqual((latest_version.version, latest_version.sha), ("1.0", ""))

    def test_newer_version_bumps_and_fetches_hash(self, mock_get: Mock):
        """Test that an eligible newer version is adopted together with its integrity hash."""
        mock_get.side_effect = [
            jsdelivr_versions("1.1", "1.0"),
            npm_registry({"1.1": _ELIGIBLE}),
            mock_response(_FLAT_FILES),
        ]
        latest_version = _get_latest_version("clipboard", "1.0", _FILENAME)
        self.assertEqual(latest_version.version, "1.1")
        self.assertEqual(latest_version.sha, f"sha256-{HASH}")

    def test_fresh_version_held_back(self, mock_get: Mock):
        """Test that a version within the cooldown is skipped and the newest eligible older version is chosen."""
        mock_get.side_effect = [
            jsdelivr_versions("2.0", "1.5", "1.0"),
            npm_registry({"2.0": _FRESH, "1.5": _ELIGIBLE}),  # one registry doc: 2.0 too fresh, 1.5 eligible
            mock_response(_FLAT_FILES),
        ]
        latest_version = _get_latest_version("clipboard", "1.0", _FILENAME)
        self.assertEqual(latest_version.version, "1.5")
        self.assertEqual(latest_version.sha, f"sha256-{HASH}")

    def test_cooldown_decides_eligibility(self, mock_get: Mock):
        """Test that a version is held back or adopted according to the cooldown the getter is passed."""
        published = days_ago(10).isoformat()
        for cooldown_days, expected in ((30, "1.0"), (5, "1.1")):
            with self.subTest(cooldown_days=cooldown_days):
                mock_get.side_effect = [
                    jsdelivr_versions("1.1", "1.0"),
                    npm_registry({"1.1": published}),
                    mock_response(_FLAT_FILES),
                ]
                dependency = f"clipboard{cooldown_days}"  # A fresh name per case, as the npm fetches are cached.
                self.assertEqual(_get_latest_version(dependency, "1.0", _FILENAME, cooldown_days).version, expected)

    def test_deprecated_current_version_attached(self, mock_get: Mock):
        """Test that when the pin stays put on a deprecated version, its deprecation is attached as a yank."""
        mock_get.side_effect = [
            jsdelivr_versions("1.0", "0.9"),
            npm_registry({"1.0": _ELIGIBLE}, deprecated={"1.0": "use 2.0 instead"}),
        ]
        latest = _get_latest_version("clipboard", "1.0", _FILENAME)
        self.assertEqual(latest.version, "1.0")
        self.assertEqual(latest.yank, Yank(yanked=True, reason="use 2.0 instead"))

    @kills.delete(
        version_getter,
        ", unserved=Unserved.VERSION",
        "a URL whose version is not PEP 440 is looked up at the npm registry and checked for staleness",
    )
    def test_unparsable_current_version_left_alone(self, mock_get: Mock):
        """Test that an unparsable current version, such as `1.`, is left unchanged and unserved, querying nothing."""
        latest = _get_latest_version("clipboard", "1.", _FILENAME)
        self.assertEqual(latest.version, "1.")
        self.assertIs(latest.unserved, Unserved.VERSION)
        mock_get.assert_not_called()

    def test_package_api_answer_that_is_not_json_keeps_current(self, mock_get: Mock):
        """Test that a jsDelivr package API answer that is not JSON is warned about, leaving the version unchanged."""
        url = "https://data.jsdelivr.com/v1/packages/npm/clipboard"
        mock_get.side_effect = [unparsable_response(ok=True, url=url), npm_registry({"1.0": _ELIGIBLE})]
        self.assertEqual(_get_latest_version("clipboard", "1.0", _FILENAME).version, "1.0")
        self.assert_invalid_json_logged(url)

    def test_integrity_listing_that_is_not_json_keeps_current(self, mock_get: Mock):
        """Test that a jsDelivr file listing that is not JSON is warned about, leaving the version unchanged."""
        url = "https://data.jsdelivr.com/v1/packages/npm/clipboard@1.1?structure=flat"
        mock_get.side_effect = [
            jsdelivr_versions("1.1", "1.0"),
            npm_registry({"1.1": _ELIGIBLE}),
            unparsable_response(ok=True, url=url),
        ]
        latest_version = _get_latest_version("clipboard", "1.0", _FILENAME)
        self.assertEqual(latest_version.version, "1.0")
        self.assertEqual(latest_version.sha, "")
        self.assert_invalid_json_logged(url, among_others=True)

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
        flat = {"default": _FILENAME, "files": [{"name": "/other.js", "hash": HASH}]}  # _FILENAME is absent
        mock_get.side_effect = [jsdelivr_versions("1.1", "1.0"), npm_registry({"1.1": _ELIGIBLE}), mock_response(flat)]
        latest_version = _get_latest_version("clipboard", "1.0", _FILENAME)
        self.assertEqual(latest_version.version, "1.0")
        self.assertEqual(latest_version.sha, "")
        message = message_of(Logger.no_integrity_hash)
        self.assert_logged(message, dependency="clipboard", version="1.1", filename=_FILENAME)


def _get_latest_version(
    dependency: str, current_version: str, filename: str, cooldown_days: int = COOLDOWN.default
) -> DependencyVersion:
    """Return the version the source resolves for the file the URL references, unbounded."""
    return version_getter(filename)(PinnedDependency(dependency, current_version), NO_BOUND, cooldown_days)
