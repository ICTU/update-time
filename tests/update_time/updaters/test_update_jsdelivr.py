"""Unit tests for the jsdelivr CDN URLs update script."""

from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, Mock, patch

from update_time.domain.cooldown import COOLDOWN
from update_time.io.log import Logger
from update_time.updaters.update_jsdelivr import update_jsdelivrs

from tests.update_time.fixtures import HASH1, HASH2
from tests.update_time.helpers import LoggingTestCase, jsdelivr_versions, mock_path, mock_response, npm_registry

# The flat package listing as returned by the jsDelivr API with ?structure=flat, referencing the file below.
FILENAME = "/dist/clipboard.min.js"
FLAT_FILES = {"default": FILENAME, "files": [{"name": FILENAME, "hash": HASH2}]}

# An npm publication date comfortably past the cooldown, relative to now so the decision doesn't depend on the clock.
ELIGIBLE = (datetime.now(UTC) - timedelta(days=COOLDOWN.default + 1)).isoformat()


# The lines a jsDelivr reference is declared with, formatted as Ruff would format them: the URL, and below it the
# attribute dictionary, either declaring the Subresource Integrity hash or, unpinned, declaring none.
URL = '"https://cdn.jsdelivr.net/npm/clipboard@2.0.11/dist/clipboard.min.js",'
INTEGRITY = f'{{"integrity": "sha256-{HASH1}", "crossorigin": "anonymous"}},'
NO_INTEGRITY = '{"crossorigin": "anonymous"},'


def entry(*lines: str) -> str:
    """Return an `html_js_files` entry declaring the given lines as a tuple, as Ruff would format it."""
    return "(\n" + "".join(f"        {line}\n" for line in lines) + "    ),"


def conf(*entries: str) -> str:
    """Return the relevant part of a Sphinx config declaring the given entries, plus an unrelated one."""
    return "html_js_files = [\n" + "".join(f"    {declared}\n" for declared in entries) + '    "copy_button.js",\n]\n'


CONF = conf(entry(URL, INTEGRITY))


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
        mock_get: Mock, *versions: str, published: str = ELIGIBLE, deprecated: dict[str, str] | None = None
    ) -> None:
        """Let the registries offer the versions, newest first, each published on the given date.

        The three responses are the fixed order the source asks for them in: the jsDelivr version list, the npm
        registry document carrying each version's publication date and deprecation, and the flat file listing
        carrying the integrity hash. A run that stays on its current version never reaches the last one.
        """
        mock_get.side_effect = [
            jsdelivr_versions(*versions),
            npm_registry(dict.fromkeys(versions, published), deprecated),
            mock_response(FLAT_FILES),
        ]

    @staticmethod
    def written(mock_conf: Mock) -> str:
        """Return the content the updater wrote back to the Sphinx config."""
        return mock_conf.write_text.call_args.args[0]

    def test_new_version_and_hash_reported_at_the_url_line(self, mock_get: Mock, mock_glob: Mock):
        """Test that the version and the integrity hash are updated on a bump, and reported at the URL's line."""
        self.offer_versions(mock_get, "2.0.12", "2.0.11")
        mock_conf = self.update(CONF, mock_glob)
        new_content = self.written(mock_conf)
        self.assert_path_logged(mock_conf)
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", new_content)
        self.assertIn(f'"integrity": "sha256-{HASH2}"', new_content)
        self.assertNotIn("2.0.11", new_content)
        self.assertNotIn(HASH1, new_content)
        self.assert_new_version_logged(mock_conf, "clipboard", ANY, Logger.NO_CHANGELOG, line=3)
        self.assert_no_warnings_logged()

    def test_line_between_the_url_and_its_hash_is_left_untouched(self, mock_get: Mock, mock_glob: Mock):
        """Test that the hash still follows the URL across an intervening line, which is itself left alone.

        The hash belongs to the URL above it however many lines separate the two, so a version-lookalike on a line in
        between is neither taken for the pin nor rewritten along with it.
        """
        self.offer_versions(mock_get, "2.0.12", "2.0.11")
        mock_conf = self.update(conf(entry(URL, "# do not remove 2.0.11 note", INTEGRITY)), mock_glob)
        new_content = self.written(mock_conf)
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", new_content)  # the pin is bumped
        self.assertIn("# do not remove 2.0.11 note", new_content)  # the lookalike is preserved
        self.assertIn(f'"integrity": "sha256-{HASH2}"', new_content)
        self.assertNotIn(HASH1, new_content)
        self.assert_new_version_logged(mock_conf, "clipboard", ANY, Logger.NO_CHANGELOG, line=3)

    def test_unchanged(self, mock_get: Mock, mock_glob: Mock):
        """Test that the config is not rewritten if there is no new version."""
        self.offer_versions(mock_get, "2.0.11")
        mock_conf = self.update(CONF, mock_glob)
        mock_conf.write_text.assert_not_called()
        self.assert_path_logged(mock_conf)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_url_without_an_integrity_hash_is_pinned(self, mock_get: Mock, mock_glob: Mock):
        """Test that a URL whose attribute dictionary carries no integrity hash gains one, logged as a pin."""
        self.offer_versions(mock_get, "2.0.11")
        mock_conf = self.update(conf(entry(URL, NO_INTEGRITY)), mock_glob)
        self.assertIn(f'{{"integrity": "sha256-{HASH2}", "crossorigin": "anonymous"}},', self.written(mock_conf))
        self.assert_pinned_logged(mock_conf, "clipboard", "2.0.11", f"sha256-{HASH2}", line=3)
        self.assert_no_warnings_logged()

    def test_out_of_date_url_without_an_integrity_hash_is_bumped_and_pinned(self, mock_get: Mock, mock_glob: Mock):
        """Test that a URL that is both out of date and unpinned is bumped and gains its hash, reported once.

        The version change is the headline, as it is for an unpinned image reference, so the pin is not reported on
        top of it.
        """
        self.offer_versions(mock_get, "2.0.12", "2.0.11")
        mock_conf = self.update(conf(entry(URL, NO_INTEGRITY)), mock_glob)
        new_content = self.written(mock_conf)
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", new_content)
        self.assertIn(f'{{"integrity": "sha256-{HASH2}", "crossorigin": "anonymous"}},', new_content)
        self.assert_new_version_logged(mock_conf, "clipboard", ANY, Logger.NO_CHANGELOG, line=3)
        self.assert_no_warnings_logged()

    def test_bare_url_string_cannot_be_pinned(self, mock_get: Mock, mock_glob: Mock):
        """Test that a URL declared as a bare string, without an attribute dictionary, is reported as unpinnable.

        Pinning it would mean restructuring the string into a `(url, {"integrity": ...})` tuple, which is more than
        rewriting a line, so the URL is left as it is and the reason is reported instead.
        """
        self.offer_versions(mock_get, "2.0.11")
        mock_conf = self.update(conf(URL), mock_glob)
        mock_conf.write_text.assert_not_called()
        self.assert_cannot_pin_logged(mock_conf, "clipboard", line=2)
        self.assert_no_warnings_logged()

    def test_bare_url_followed_by_another_url_is_still_reported(self, mock_get: Mock, mock_glob: Mock):
        """Test that a bare URL string is reported even when another jsDelivr URL follows it.

        Each URL becomes the one awaiting an attribute dictionary, so a bare URL has to be reported when the next
        URL arrives rather than only when the lines run out.
        """
        mock_get.side_effect = [
            jsdelivr_versions("2.0.11"),
            npm_registry({"2.0.11": ELIGIBLE}),
            jsdelivr_versions("2.0.11"),  # the second URL's version list; the npm registry document is cached
        ]
        mock_conf = self.update(conf(URL, entry(URL, INTEGRITY)), mock_glob)
        mock_conf.write_text.assert_not_called()
        self.assert_cannot_pin_logged(mock_conf, "clipboard", line=2)
        self.assert_no_warnings_logged()

    def test_bare_url_does_not_pin_the_entry_below_it(self, mock_get: Mock, mock_glob: Mock):
        """Test that a bare URL's hash is not written into the next entry's dictionary when that entry is held back.

        The bare URL is reported and forgotten, so the dictionary below it belongs to the held-back URL and is left
        alone rather than pinned to a hash resolved for a different reference.
        """
        self.offer_versions(mock_get, "2.0.11")
        held_back = entry(f"{URL}  # update-time: ignore", NO_INTEGRITY)
        mock_conf = self.update(conf(URL, held_back), mock_glob)
        mock_conf.write_text.assert_not_called()
        self.assert_cannot_pin_logged(mock_conf, "clipboard", line=2)
        self.assert_ignored_logged(mock_conf, "clipboard", line=4)

    def test_stale_dependency_warned(self, mock_get: Mock, mock_glob: Mock):
        """Test that a jsDelivr package whose newest release is old is warned about, without rewriting the URL."""
        old = (datetime.now(UTC) - timedelta(days=512)).isoformat()
        self.offer_versions(mock_get, "2.0.11", published=old)
        mock_conf = self.update(CONF, mock_glob)
        mock_conf.write_text.assert_not_called()  # no newer version, so no rewrite
        self.assert_stale_dependency_logged(mock_conf, "clipboard", "2.0.11", line=3)

    def test_deprecated_dependency_warned(self, mock_get: Mock, mock_glob: Mock):
        """Test that a jsDelivr URL left on a deprecated version is warned about, without rewriting the URL."""
        self.offer_versions(mock_get, "2.0.11", deprecated={"2.0.11": "use 3.0 instead"})
        mock_conf = self.update(CONF, mock_glob)
        mock_conf.write_text.assert_not_called()  # no newer version, so no rewrite
        self.assert_yanked_dependency_logged(mock_conf, "clipboard", "2.0.11", '"use 3.0 instead"', line=3)

    def test_marker_holds_the_pin_back_as_well_as_the_update(self, mock_get: Mock, mock_glob: Mock):
        """Test that an `# update-time: ignore` marker leaves an unpinned URL unpinned, looking nothing up.

        A URL whose dictionary declares no hash would otherwise have one looked up and inserted, so holding the
        update back is not enough: the marker has to hold the pin back too, before any source is queried.
        """
        mock_conf = self.update(conf(entry(f"{URL}  # update-time: ignore", NO_INTEGRITY)), mock_glob)
        mock_conf.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_ignored_logged(mock_conf, "clipboard", line=3)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_marker_above_the_url_holds_it_back(self, mock_get: Mock, mock_glob: Mock):
        """Test that an `# update-time: ignore` comment above a jsDelivr URL holds it back, looking up no version."""
        mock_conf = self.update(conf(entry("# update-time: ignore", URL, INTEGRITY)), mock_glob)
        mock_conf.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_ignored_logged(mock_conf, "clipboard", line=4)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_inline_marker_holds_the_url_back(self, mock_get: Mock, mock_glob: Mock):
        """Test that an `# update-time: ignore` comment on the URL's own line holds it back too."""
        mock_conf = self.update(conf(entry(f"{URL}  # update-time: ignore", INTEGRITY)), mock_glob)
        mock_conf.write_text.assert_not_called()
        mock_get.assert_not_called()
        self.assert_ignored_logged(mock_conf, "clipboard", line=3)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_bound_limits_the_update(self, mock_get: Mock, mock_glob: Mock):
        """Test that a version bound on the URL is honoured, so the URL advances only as far as the bound allows."""
        self.offer_versions(mock_get, "3.0.0", "2.0.12", "2.0.11")
        mock_conf = self.update(conf(entry(f"{URL}  # update-time: allow[update<3]", INTEGRITY)), mock_glob)
        new_content = self.written(mock_conf)
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", new_content)
        self.assertNotIn("3.0.0", new_content)
        self.assert_new_version_logged(mock_conf, "clipboard", ANY, Logger.NO_CHANGELOG, line=3)
        self.assert_no_warnings_logged()

    def test_ignore_update_marker_holds_the_update_back_but_still_warns(self, mock_get: Mock, mock_glob: Mock):
        """Test that `ignore[update]` leaves the URL on its version but still warns that the version was deprecated.

        The scope narrows the marker to the update, so a URL deliberately frozen on a deprecated version is still
        reported as one, rather than going quiet along with the update.
        """
        self.offer_versions(mock_get, "2.0.12", "2.0.11", deprecated={"2.0.11": "use 3.0 instead"})
        mock_conf = self.update(conf(entry(f"{URL}  # update-time: ignore[update]", INTEGRITY)), mock_glob)
        mock_conf.write_text.assert_not_called()  # the newer version is held back
        self.assert_yanked_dependency_logged(mock_conf, "clipboard", "2.0.11", '"use 3.0 instead"', line=3)
        self.assert_ignored_logged(mock_conf, "clipboard", "ignore[update]", line=3)

    def test_ignore_stale_marker_silences_the_warning(self, mock_get: Mock, mock_glob: Mock):
        """Test that an `ignore[stale]` marker on the URL's line holds the staleness warning back, but not the update.

        The hold-back is logged at debug level. The scope narrows the marker to the warning, so a package whose
        newest release is long in the past is silently updated to it rather than warned about.
        """
        old = (datetime.now(UTC) - timedelta(days=512)).isoformat()
        self.offer_versions(mock_get, "2.0.12", "2.0.11", published=old)
        mock_conf = self.update(conf(entry(f"{URL}  # update-time: ignore[stale]", INTEGRITY)), mock_glob)
        self.assertIn("clipboard@2.0.12/dist/clipboard.min.js", self.written(mock_conf))
        self.assert_no_warnings_logged()
        self.assert_ignored_staleness_logged(mock_conf, "clipboard", "ignore[stale]", line=3)

    def test_ignore_yanked_marker_silences_the_warning(self, mock_get: Mock, mock_glob: Mock):
        """Test that an `ignore[yanked]` marker on the URL's line holds back the deprecation warning.

        The hold-back is logged at debug level, and no warning survives, so npm reporting deprecations means the
        marker is not reported as redundant either.
        """
        self.offer_versions(mock_get, "2.0.11", deprecated={"2.0.11": "use 3.0 instead"})
        mock_conf = self.update(conf(entry(f"{URL}  # update-time: ignore[yanked]", INTEGRITY)), mock_glob)
        mock_conf.write_text.assert_not_called()
        self.assert_no_warnings_logged()
        self.assert_ignored_yank_logged(mock_conf, "clipboard", "ignore[yanked]", line=3)
