"""Unit tests for the Maven Central source."""

import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import requests

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import Release
from update_time.io.log import Logger
from update_time.sources import maven_central
from update_time.sources.maven_central import _published, newest_release, versions_within_cooldown

from tests.helpers import mock_response, patch_environ, patch_get
from tests.mutation import Mutation, kills
from tests.update_time.helpers import LoggingTestCase
from tests.update_time.sources.helpers import requested_urls

_GUAVA = "com.google.guava:guava"
_GUAVA_LISTING = "https://repo1.maven.org/maven2/com/google/guava/guava/"


def _days_ago(days: int) -> datetime:
    """Return the instant the given number of days ago."""
    return datetime.now(UTC) - timedelta(days=days)


def _row(name: str, published: datetime, size: str) -> str:
    """Return a row of the repository's directory listing, padded out to the date column.

    The repository dates the row in GMT without naming the zone, and closes it with the entry's size.
    """
    return _dated_row(name, f"{published:%Y-%m-%d %H:%M}", size)


def _dated_row(name: str, published: str, size: str) -> str:
    """Return a row of the listing, dated exactly as given rather than by an instant."""
    link = f'<a href="{name}" title="{name}">{name}</a>'
    padding = " " * max(1, 50 - len(name))
    return f"{link}{padding}{published}   {size}\n"


def _version_row(version: str, published: datetime) -> str:
    """Return the row for a version, which the repository serves as a directory, sized with a dash."""
    return _row(f"{version}/", published, "-")


# The metadata files a listing closes with, each with the bytes the repository sizes it at: the metadata itself,
# and the checksums over it, whose lengths are those of an MD5 and a SHA-1 written out in hexadecimal.
_METADATA_FILES = {"maven-metadata.xml": "5927", "maven-metadata.xml.md5": "32", "maven-metadata.xml.sha1": "40"}


def _metadata_rows(published: datetime) -> tuple[str, ...]:
    """Return the rows for the metadata files a listing closes with.

    The repository rewrites them whenever the artefact publishes, so a listing dates them at its newest version.
    """
    return tuple(_row(name, published, size) for name, size in _METADATA_FILES.items())


def _listing(*rows: str) -> str:
    """Return the directory listing the repository serves for an artefact, holding the given rows.

    The listing opens with a link to the parent directory.
    """
    return f'<html><body><pre>\n<a href="../">../</a>\n{"".join(rows)}</pre></body></html>'


class VersionsWithinCooldownTest(LoggingTestCase):
    """Unit tests for reading the versions the repository published inside the cooldown window."""

    def test_only_a_version_published_inside_the_window_is_held_back(self):
        """Test that the version dated inside the window is held back, and the one dated before it is not."""
        settled = _version_row("33.7.0-jre", _days_ago(COOLDOWN.default + 1))
        fresh = _version_row("33.7.1-jre", _days_ago(1))
        with patch_get(text=_listing(settled, fresh)):
            self.assertEqual(versions_within_cooldown(_GUAVA, COOLDOWN.default), ("33.7.1-jre",))

    @kills(
        Mutation(
            maven_central,
            "    if response.status_code == HTTPStatus.NOT_FOUND:\n        _LOG.unserved_listing(response)\n"
            '        return ""\n',
            "",
            "the repository warns about an artefact it does not serve, as though the request had failed",
        )
    )
    def test_an_unserved_listing_holds_no_version_back(self):
        """Test that an unserved listing is reported at debug, and that the run holds nothing back."""
        # The repository serves this error page beside the status. A run that read it as a listing would find nothing.
        error_page = "<html><head><title>404 Not Found</title></head><body>404 Not Found</body></html>"
        answer = mock_response(ok=False, status_code=404, reason="Not Found", url=_GUAVA_LISTING, text=error_page)
        mock_get = Mock(return_value=answer)
        with patch("requests.get", mock_get):
            self.assertEqual(versions_within_cooldown(_GUAVA, COOLDOWN.default), ())
        # Asserting the request was made, so holding nothing back says something about the answer rather than
        # about a request that was never sent.
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING])
        self.assert_logged(Logger._MESSAGE_UNSERVED_LISTING, url=_GUAVA_LISTING, status=404, reason="Not Found")

    @kills(
        Mutation(
            maven_central,
            '    if not response.ok:\n        _LOG.response(response)\n        return ""\n',
            "",
            "the repository fails without a word, so the cooldown is lost for an artefact it does serve",
        )
    )
    def test_an_error_other_than_a_missing_listing_is_warned_about(self):
        """Test that a repository answering with a server error warns, and that nothing is held back.

        The cooldown then held nothing back for an artefact the repository does have, which is worth knowing.
        """
        answer = mock_response(ok=False, status_code=503, reason="Service Unavailable", url=_GUAVA_LISTING, text="")
        with patch("requests.get", Mock(return_value=answer)):
            self.assertEqual(versions_within_cooldown(_GUAVA, COOLDOWN.default), ())
        self.assert_could_not_fetch_logged(url=_GUAVA_LISTING, status=503, reason="Service Unavailable")

    @kills(
        Mutation(
            maven_central,
            '    if response is None:\n        return ""\n',
            "",
            "a listing whose request fails ends the run with a traceback",
            raises="AttributeError: 'NoneType' object has no attribute 'status_code'",
        )
    )
    def test_a_listing_request_that_times_out_holds_no_version_back(self):
        """Test that a listing request that times out is reported as a timeout, and that nothing is held back."""
        with patch("requests.get", Mock(side_effect=requests.exceptions.Timeout)):
            self.assertEqual(versions_within_cooldown(_GUAVA, COOLDOWN.default), ())
        self.assert_logged(Logger._MESSAGE_TIMEOUT, url=_GUAVA_LISTING)

    @kills(
        Mutation(
            maven_central,
            "    try:\n        return datetime.strptime(published, _PUBLISHED_FORMAT).replace(tzinfo=UTC)\n"
            "    except ValueError:\n        return None\n",
            "    return datetime.strptime(published, _PUBLISHED_FORMAT).replace(tzinfo=UTC)\n",
            "one row the repository dated outside the calendar ends the run over every pom",
            raises="ValueError: time data '2026-13-45 99:99' does not match format '%Y-%m-%d %H:%M'",
        )
    )
    def test_a_row_dated_in_a_shape_the_format_cannot_read_is_passed_over(self):
        """Test that a row whose date does not parse holds nothing back, and the rows beside it are still read."""
        unreadable = _dated_row("33.7.2-jre/", "2026-13-45 99:99", "-")
        fresh = _version_row("33.7.1-jre", _days_ago(1))
        with patch_get(text=_listing(unreadable, fresh)):
            self.assertEqual(versions_within_cooldown(_GUAVA, COOLDOWN.default), ("33.7.1-jre",))

    @kills(
        Mutation(
            maven_central,
            '/"[^>]*>',
            '/?"[^>]*>',
            "a file beside the versions reads as a version, so the metadata files are held back as releases",
        )
    )
    def test_a_metadata_file_is_not_held_back(self):
        """Test that a metadata file the listing dates inside the window is not among the versions held back."""
        published = _days_ago(1)
        listing = _listing(_version_row("33.7.1-jre", published), *_metadata_rows(published))
        with patch_get(text=listing):
            held_back = versions_within_cooldown(_GUAVA, COOLDOWN.default)
        self.assertEqual(held_back, ("33.7.1-jre",))

    @kills(
        Mutation(
            maven_central,
            "@cache\n",
            "",
            "every pom declaring an artefact costs a request of its own",
        )
    )
    def test_an_artefact_is_asked_about_once_per_run(self):
        """Test that asking about an artefact twice costs one request."""
        mock_get = Mock(return_value=mock_response(text=_listing(_version_row("33.7.1-jre", _days_ago(1)))))
        with patch("requests.get", mock_get):
            first = versions_within_cooldown(_GUAVA, COOLDOWN.default)
            second = versions_within_cooldown(_GUAVA, COOLDOWN.default)
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING])
        self.assertEqual(first, ("33.7.1-jre",))
        self.assertEqual(second, ("33.7.1-jre",))


class PublishedTest(unittest.TestCase):
    """Unit tests for reading the date the listing holds for a version."""

    @kills(
        Mutation(
            maven_central,
            "    return datetime.strptime(published, _PUBLISHED_FORMAT).replace(tzinfo=UTC)\n",
            "    return datetime.strptime(published, _PUBLISHED_FORMAT).astimezone()\n",
            "a date is read in the machine's own zone, so a version falls on the wrong side of the window",
        )
    )
    @patch_environ({"TZ": "Asia/Tokyo"})
    def test_a_date_is_read_as_gmt(self):
        """Test that a date is read as GMT, whatever zone the machine runs in."""
        time.tzset()
        self.addCleanup(time.tzset)
        self.assertEqual(_published("2026-09-12 23:30"), datetime(2026, 9, 12, 23, 30, tzinfo=UTC))


class NewestReleaseTest(LoggingTestCase):
    """Unit tests for reading the release an artefact published most recently."""

    def test_the_newest_release_is_the_one_published_last(self):
        """Test that the release read is the one published most recently, rather than the one listed last."""
        newest = datetime(2026, 5, 1, 12, 30, tzinfo=UTC)
        rows = (_version_row("33.7.0-jre", newest), _version_row("33.8.0-jre", datetime(2020, 1, 2, 3, 4, tzinfo=UTC)))
        with patch_get(text=_listing(*rows)):
            self.assertEqual(newest_release(_GUAVA), Release("33.7.0-jre", newest))

    def test_a_listing_the_repository_does_not_serve_dates_no_release(self):
        """Test that an artefact whose listing the repository withholds leaves staleness nothing to measure."""
        answer = mock_response(ok=False, status_code=404, reason="Not Found", url=_GUAVA_LISTING, text="")
        mock_get = Mock(return_value=answer)
        with patch("requests.get", mock_get):
            self.assertIsNone(newest_release(_GUAVA))
        # Asserting the request was made, so the missing release says something about the answer rather than
        # about a request that was never sent.
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING])

    @kills(
        Mutation(
            maven_central.newest_release,
            "_listing(artefact)",
            "_listing.__wrapped__(artefact)",
            "staleness fetches a listing of its own, so an artefact the cooldown read costs a second request",
        )
    )
    def test_the_cooldown_has_already_paid_for_the_listing(self):
        """Test that the newest release is free once the cooldown has read the same artefact's listing."""
        # The listing dates a row to the minute, so the expected release is dated to the minute too.
        dated = _days_ago(1).replace(second=0, microsecond=0)
        mock_get = Mock(return_value=mock_response(text=_listing(_version_row("33.7.1-jre", dated))))
        with patch("requests.get", mock_get):
            held_back = versions_within_cooldown(_GUAVA, COOLDOWN.default)
            newest = newest_release(_GUAVA)
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING])
        # Asserting what each read, so the single request says the listing was shared rather than never read.
        self.assertEqual(held_back, ("33.7.1-jre",))
        self.assertEqual(newest, Release("33.7.1-jre", dated))
