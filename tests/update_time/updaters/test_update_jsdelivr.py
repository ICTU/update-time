"""Unit tests for the jsdelivr CDN URLs update script."""

from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, Mock, patch

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.version import Yank
from update_time.io.log import Logger
from update_time.primitives.location import Location
from update_time.updaters.update_jsdelivr import update_jsdelivrs

from tests.helpers import mock_path, mock_response
from tests.update_time.fixtures import HASH1, HASH2
from tests.update_time.helpers import (
    LoggingTestCase,
    jsdelivr_versions,
    no_vulnerabilities,
    npm_registry,
    osv,
    osv_vulnerability,
)

_FILENAME = "/dist/clipboard.min.js"


def _served(*hashes: str) -> Mock:
    """Return the flat package listing the jsDelivr API returns with ?structure=flat, serving the file's hash.

    Passing no hash models a listing that doesn't mention the referenced file at all, so no hash can be resolved.
    """
    return mock_response({"default": _FILENAME, "files": [{"name": _FILENAME, "hash": served} for served in hashes]})


# An npm publication date comfortably past the cooldown, relative to now so the decision doesn't depend on the clock.
_ELIGIBLE = (datetime.now(UTC) - timedelta(days=COOLDOWN.default + 1)).isoformat()

# The deprecation npm reports for clipboard 2.0.11: the reason the registry states, and the yank it becomes.
_DEPRECATION_REASON = "use 3.0 instead"
_DEPRECATED = Yank(yanked=True, reason=_DEPRECATION_REASON)

# An advisory affecting the URL's version, for the tests that need OSV to report one, and what it is read as.
_ADVISORY, _VULNERABILITY = osv_vulnerability("GHSA-1111-1111-1111", "Cross-site scripting in clipboard", "moderate")


# The lines a jsDelivr reference is declared with, formatted as Ruff would format them: the URL, and below it the
# attribute dictionary, either declaring the Subresource Integrity hash or, unpinned, declaring none.
_URL = '"https://cdn.jsdelivr.net/npm/clipboard@2.0.11/dist/clipboard.min.js",'
_INTEGRITY = f'{{"integrity": "sha256-{HASH1}", "crossorigin": "anonymous"}},'
_NO_INTEGRITY = '{"crossorigin": "anonymous"},'


def _entry(*lines: str) -> str:
    """Return an `html_js_files` entry declaring the given lines as a tuple, as Ruff would format it."""
    return "(\n" + "".join(f"        {line}\n" for line in lines) + "    ),"


def _conf(*entries: str) -> str:
    """Return the relevant part of a Sphinx config declaring the given entries, plus an unrelated one."""
    return "html_js_files = [\n" + "".join(f"    {declared}\n" for declared in entries) + '    "copy_button.js",\n]\n'


_CONF = _conf(_entry(_URL, _INTEGRITY))


@no_vulnerabilities
@patch("pathlib.Path.rglob")
@patch("requests.get")
class UpdateJsdelivrsTest(LoggingTestCase):
    """Unit tests for updating the version and integrity hash in the Sphinx configs found under docs/."""

    def update(self, content: str, mock_glob: Mock) -> Mock:
        """Update the jsDelivr URLs in a Sphinx config with the given content, and return its mocked path."""
        mock_conf = mock_path(content)
        mock_glob.return_value = [mock_conf]
        update_jsdelivrs()
        return mock_conf

    @staticmethod
    def offer_versions(
        mock_get: Mock,
        *versions: str,
        published: str = _ELIGIBLE,
        deprecated: dict[str, str] | None = None,
        served_hash: str = HASH2,
    ) -> None:
        """Let the registries offer the versions, newest first, each published on the given date.

        The three responses are the fixed order the source asks for them in: the jsDelivr version list, the npm
        registry document carrying each version's publication date and deprecation, and the flat file listing
        carrying `served_hash` as the integrity hash of the referenced file. A run that stays on its current version
        reaches the last one too, to compare the hash the config declares against the one jsDelivr serves, so such a
        test passes the hash its config declares to leave the two agreeing.
        """
        mock_get.side_effect = [
            jsdelivr_versions(*versions),
            npm_registry(dict.fromkeys(versions, published), deprecated),
            _served(served_hash),
        ]

    @staticmethod
    def written(mock_conf: Mock) -> str:
        """Return the content the updater wrote back to the Sphinx config."""
        return mock_conf.write_text.call_args.args[0]

    def test_new_version_and_hash_reported_at_the_url_line(self, mock_get: Mock, mock_glob: Mock):
        """Test that the version and the integrity hash are updated on a bump, and reported at the URL's line."""
        self.offer_versions(mock_get, "2.0.12", "2.0.11")
        mock_conf = self.update(_CONF, mock_glob)
        new_content = self.written(mock_conf)
        self.assert_path_logged(mock_conf)
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", new_content)
        self.assertIn(f'"integrity": "sha256-{HASH2}"', new_content)
        self.assertNotIn("2.0.11", new_content)
        self.assertNotIn(HASH1, new_content)
        self.assert_new_version_logged("clipboard", ANY, Location(mock_conf, 3), Logger._NO_CHANGELOG)
        self.assert_no_warnings_logged()

    def test_cooldown_marker_is_not_reported_as_redundant(self, mock_get: Mock, mock_glob: Mock):
        """Test that a `cooldown` marker on a URL holds something back, since npm dates its versions."""
        self.offer_versions(mock_get, "2.0.11", served_hash=HASH1)
        marked_url = f"{_URL}  # update-time: ignore[cooldown<30]"
        mock_conf = self.update(_conf(_entry(marked_url, _INTEGRITY)), mock_glob)
        mock_conf.write_text.assert_not_called()
        self.assert_no_warnings_logged()

    def test_line_between_the_url_and_its_hash_is_left_untouched(self, mock_get: Mock, mock_glob: Mock):
        """Test that the hash still follows the URL across an intervening line, which is itself left alone.

        The line in between holds a version-lookalike, which is neither taken for the pin nor rewritten with it.
        """
        self.offer_versions(mock_get, "2.0.12", "2.0.11")
        mock_conf = self.update(_conf(_entry(_URL, "# do not remove 2.0.11 note", _INTEGRITY)), mock_glob)
        new_content = self.written(mock_conf)
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", new_content)  # the pin is bumped
        self.assertIn("# do not remove 2.0.11 note", new_content)  # the lookalike is preserved
        self.assertIn(f'"integrity": "sha256-{HASH2}"', new_content)
        self.assertNotIn(HASH1, new_content)
        self.assert_new_version_logged("clipboard", ANY, Location(mock_conf, 3), Logger._NO_CHANGELOG)

    def test_unchanged(self, mock_get: Mock, mock_glob: Mock):
        """Test that the config is not rewritten if there is no new version."""
        self.offer_versions(mock_get, "2.0.11", served_hash=HASH1)
        mock_conf = self.update(_CONF, mock_glob)
        mock_conf.write_text.assert_not_called()
        self.assert_path_logged(mock_conf)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_hash_mismatch_warned_not_rewritten(self, mock_get: Mock, mock_glob: Mock):
        """Test that a declared hash disagreeing with the one jsDelivr serves is warned about, not quietly rewritten.

        The config declares HASH1 for the version it sits on, while jsDelivr serves HASH2 for that same version.
        """
        self.offer_versions(mock_get, "2.0.11")
        mock_conf = self.update(_CONF, mock_glob)
        mock_conf.write_text.assert_not_called()
        self.assert_hash_mismatch_logged(
            "clipboard", "2.0.11", f"sha256-{HASH1}", f"sha256-{HASH2}", Location(mock_conf, 3)
        )

    def test_unresolvable_hash_is_not_a_mismatch(self, mock_get: Mock, mock_glob: Mock):
        """Test that a file jsDelivr doesn't list leaves nothing to compare, so no mismatch is reported."""
        mock_get.side_effect = [jsdelivr_versions("2.0.11"), npm_registry({"2.0.11": _ELIGIBLE}), _served()]
        mock_conf = self.update(_CONF, mock_glob)
        mock_conf.write_text.assert_not_called()
        self.assert_no_warnings_logged()

    def test_url_without_an_integrity_hash_is_pinned(self, mock_get: Mock, mock_glob: Mock):
        """Test that a URL whose attribute dictionary carries no integrity hash gains one, logged as a pin."""
        self.offer_versions(mock_get, "2.0.11")
        mock_conf = self.update(_conf(_entry(_URL, _NO_INTEGRITY)), mock_glob)
        self.assertIn(f'{{"integrity": "sha256-{HASH2}", "crossorigin": "anonymous"}},', self.written(mock_conf))
        self.assert_pinned_logged("clipboard", "2.0.11", f"sha256-{HASH2}", Location(mock_conf, 3))
        self.assert_no_warnings_logged()

    def test_out_of_date_url_without_an_integrity_hash_is_bumped_and_pinned(self, mock_get: Mock, mock_glob: Mock):
        """Test that a URL that is both out of date and unpinned is bumped and gains its hash, reported once."""
        self.offer_versions(mock_get, "2.0.12", "2.0.11")
        mock_conf = self.update(_conf(_entry(_URL, _NO_INTEGRITY)), mock_glob)
        new_content = self.written(mock_conf)
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", new_content)
        self.assertIn(f'{{"integrity": "sha256-{HASH2}", "crossorigin": "anonymous"}},', new_content)
        self.assert_new_version_logged("clipboard", ANY, Location(mock_conf, 3), Logger._NO_CHANGELOG)
        self.assert_no_warnings_logged()

    def test_bare_url_string_cannot_be_pinned(self, mock_get: Mock, mock_glob: Mock):
        """Test that a URL declared as a bare string, without an attribute dictionary, is reported as unpinnable."""
        self.offer_versions(mock_get, "2.0.11")
        mock_conf = self.update(_conf(_URL), mock_glob)
        mock_conf.write_text.assert_not_called()
        self.assert_cannot_pin_logged("clipboard", Location(mock_conf, 2))
        self.assert_no_warnings_logged()

    def test_bare_url_followed_by_another_url_is_still_reported(self, mock_get: Mock, mock_glob: Mock):
        """Test that a bare URL string is reported even when another jsDelivr URL follows it."""
        mock_get.side_effect = [
            jsdelivr_versions("2.0.11"),
            npm_registry({"2.0.11": _ELIGIBLE}),
            jsdelivr_versions("2.0.11"),  # the second URL's version list; the npm registry document is cached
            _served(HASH1),  # the second URL stays on its version, so its declared hash is compared against this one
        ]
        mock_conf = self.update(_conf(_URL, _entry(_URL, _INTEGRITY)), mock_glob)
        mock_conf.write_text.assert_not_called()
        self.assert_cannot_pin_logged("clipboard", Location(mock_conf, 2))
        self.assert_no_warnings_logged()

    def test_bare_url_does_not_pin_the_entry_below_it(self, mock_get: Mock, mock_glob: Mock):
        """Test that a bare URL's hash is not written into the next entry's dictionary when that entry is held back."""
        self.offer_versions(mock_get, "2.0.11")
        held_back = _entry(f"{_URL}  # update-time: ignore", _NO_INTEGRITY)
        mock_conf = self.update(_conf(_URL, held_back), mock_glob)
        mock_conf.write_text.assert_not_called()
        self.assert_cannot_pin_logged("clipboard", Location(mock_conf, 2))
        self.assert_ignored_logged("clipboard", Location(mock_conf, 4))

    def test_stale_dependency_warned(self, mock_get: Mock, mock_glob: Mock):
        """Test that a jsDelivr package whose newest release is old is warned about, without rewriting the URL."""
        old = (datetime.now(UTC) - timedelta(days=512)).isoformat()
        self.offer_versions(mock_get, "2.0.11", published=old, served_hash=HASH1)
        mock_conf = self.update(_CONF, mock_glob)
        mock_conf.write_text.assert_not_called()  # no newer version, so no rewrite
        self.assert_stale_dependency_logged("clipboard", "2.0.11", Location(mock_conf, 3))

    def test_deprecated_dependency_warned(self, mock_get: Mock, mock_glob: Mock):
        """Test that a jsDelivr URL left on a deprecated version is warned about, without rewriting the URL."""
        self.offer_versions(mock_get, "2.0.11", deprecated={"2.0.11": _DEPRECATION_REASON}, served_hash=HASH1)
        mock_conf = self.update(_CONF, mock_glob)
        mock_conf.write_text.assert_not_called()  # no newer version, so no rewrite
        self.assert_yanked_dependency_logged("clipboard", "2.0.11", Location(mock_conf, 3), _DEPRECATED)

    def test_vulnerable_dependency_warned(self, mock_get: Mock, mock_glob: Mock):
        """Test that a URL left on a version OSV reports an advisory for is warned about, without rewriting the URL."""
        self.offer_versions(mock_get, "2.0.11", served_hash=HASH1)
        with osv(_ADVISORY):
            mock_conf = self.update(_CONF, mock_glob)
        mock_conf.write_text.assert_not_called()  # no newer version, so no rewrite
        self.assert_vulnerable_dependency_logged("clipboard", "2.0.11", _VULNERABILITY, Location(mock_conf, 3))

    def test_marker_holds_the_pin_back_as_well_as_the_update(self, mock_get: Mock, mock_glob: Mock):
        """Test that an `# update-time: ignore` marker leaves an unpinned URL unpinned, looking nothing up."""
        mock_conf = self.update(_conf(_entry(f"{_URL}  # update-time: ignore", _NO_INTEGRITY)), mock_glob)
        mock_conf.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_ignored_logged("clipboard", Location(mock_conf, 3))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_marker_above_the_url_holds_it_back(self, mock_get: Mock, mock_glob: Mock):
        """Test that an `# update-time: ignore` comment above a jsDelivr URL holds it back, looking up no version."""
        mock_conf = self.update(_conf(_entry("# update-time: ignore", _URL, _INTEGRITY)), mock_glob)
        mock_conf.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_ignored_logged("clipboard", Location(mock_conf, 4))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_inline_marker_holds_the_url_back(self, mock_get: Mock, mock_glob: Mock):
        """Test that an `# update-time: ignore` comment on the URL's own line holds it back too."""
        mock_conf = self.update(_conf(_entry(f"{_URL}  # update-time: ignore", _INTEGRITY)), mock_glob)
        mock_conf.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_ignored_logged("clipboard", Location(mock_conf, 3))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_bound_limits_the_update(self, mock_get: Mock, mock_glob: Mock):
        """Test that a version bound on the URL is honoured, so the URL advances only as far as the bound allows."""
        self.offer_versions(mock_get, "3.0.0", "2.0.12", "2.0.11")
        mock_conf = self.update(_conf(_entry(f"{_URL}  # update-time: allow[update<3]", _INTEGRITY)), mock_glob)
        new_content = self.written(mock_conf)
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", new_content)
        self.assertNotIn("3.0.0", new_content)
        self.assert_new_version_logged("clipboard", ANY, Location(mock_conf, 3), Logger._NO_CHANGELOG)
        self.assert_no_warnings_logged()

    def test_ignore_update_marker_holds_the_update_back_but_still_warns(self, mock_get: Mock, mock_glob: Mock):
        """Test that `ignore[update]` leaves the URL on its version but still warns that the version was deprecated."""
        self.offer_versions(mock_get, "2.0.12", "2.0.11", deprecated={"2.0.11": _DEPRECATION_REASON})
        mock_conf = self.update(_conf(_entry(f"{_URL}  # update-time: ignore[update]", _INTEGRITY)), mock_glob)
        mock_conf.write_text.assert_not_called()  # the newer version is held back
        location = Location(mock_conf, 3)
        self.assert_yanked_dependency_logged("clipboard", "2.0.11", location, _DEPRECATED)
        self.assert_ignored_logged("clipboard", location, "ignore[update]")

    def test_ignore_stale_marker_silences_the_warning(self, mock_get: Mock, mock_glob: Mock):
        """Test that an `ignore[stale]` marker on the URL's line holds the warning back, but not the update."""
        old = (datetime.now(UTC) - timedelta(days=512)).isoformat()
        self.offer_versions(mock_get, "2.0.12", "2.0.11", published=old)
        mock_conf = self.update(_conf(_entry(f"{_URL}  # update-time: ignore[stale]", _INTEGRITY)), mock_glob)
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", self.written(mock_conf))
        self.assert_no_warnings_logged()
        self.assert_ignored_staleness_logged("clipboard", Location(mock_conf, 3), "ignore[stale]")

    def test_ignore_yanked_marker_silences_the_warning(self, mock_get: Mock, mock_glob: Mock):
        """Test that an `ignore[yanked]` marker on the URL's line holds back the deprecation warning."""
        self.offer_versions(mock_get, "2.0.11", deprecated={"2.0.11": _DEPRECATION_REASON}, served_hash=HASH1)
        mock_conf = self.update(_conf(_entry(f"{_URL}  # update-time: ignore[yanked]", _INTEGRITY)), mock_glob)
        mock_conf.write_text.assert_not_called()
        self.assert_no_warnings_logged()
        self.assert_ignored_yank_logged("clipboard", Location(mock_conf, 3), "ignore[yanked]")

    def test_ignore_vulnerable_marker_silences_the_warning(self, mock_get: Mock, mock_glob: Mock):
        """Test that an `ignore[vulnerable]` marker holds back the vulnerability warning and nothing else."""
        self.offer_versions(mock_get, "2.0.12", "2.0.11")
        marked_url = f"{_URL}  # update-time: ignore[vulnerable]"
        with osv(_ADVISORY) as mock_post:
            mock_conf = self.update(_conf(_entry(marked_url, _INTEGRITY)), mock_glob)
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", self.written(mock_conf))
        mock_post.assert_called()
        self.assert_no_warnings_logged()
        self.assert_ignored_vulnerability_logged("clipboard", Location(mock_conf, 3), "ignore[vulnerable]")
