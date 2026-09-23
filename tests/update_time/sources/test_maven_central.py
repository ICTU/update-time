"""Unit tests for the Maven Central source."""

import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import requests

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import Archival, ArchivedSubject, Project, Release
from update_time.io.log import Logger
from update_time.manifests import pom_xml as pom_xml_format
from update_time.sources import maven_central
from update_time.sources.maven_central import _newest_release, _published, project, versions_within_cooldown

from tests.helpers import mock_response, patch_environ, patch_get
from tests.mutation import Mutation, kills
from tests.update_time.helpers import (
    LoggingTestCase,
    maven_central_dated_row,
    maven_central_listing,
    maven_central_pom,
    maven_central_row,
    maven_central_version_row,
    patch_maven_central,
)
from tests.update_time.sources.helpers import requested_urls

_GUAVA = "com.google.guava:guava"
_GUAVA_LISTING = "https://repo1.maven.org/maven2/com/google/guava/guava/"
_GUAVA_VERSION = "33.7.1-jre"
_GUAVA_POM = f"{_GUAVA_LISTING}{_GUAVA_VERSION}/guava-{_GUAVA_VERSION}.pom"
_GUAVA_REPOSITORY = "https://api.github.com/repos/google/guava"
_GUAVA_SCM = "scm:git:https://github.com/google/guava.git"


def _days_ago(days: int) -> datetime:
    """Return the instant the given number of days ago."""
    return datetime.now(UTC) - timedelta(days=days)


# The metadata files a listing closes with, each with the bytes the repository sizes it at: the metadata itself,
# and the checksums over it, whose lengths are those of an MD5 and a SHA-1 written out in hexadecimal.
_METADATA_FILES = {"maven-metadata.xml": "5927", "maven-metadata.xml.md5": "32", "maven-metadata.xml.sha1": "40"}


def _metadata_rows(published: datetime) -> tuple[str, ...]:
    """Return the rows for the metadata files a listing closes with.

    The repository rewrites them whenever the artefact publishes, so a listing dates them at its newest version.
    """
    return tuple(maven_central_row(name, published, size) for name, size in _METADATA_FILES.items())


# The listing the repository serves for guava where the tests care about the pom beside a version rather than the
# dates of the versions themselves: one version, dated yesterday.
_DATED_LISTING = maven_central_listing(maven_central_version_row(_GUAVA_VERSION, _days_ago(1)))


class ProjectTest(LoggingTestCase):
    """Unit tests for reading whether the repository behind an artefact is archived."""

    def assert_archived(self, mock_get: Mock, archival: Archival) -> None:
        """Assert that the run reported the archival, having asked GitHub about the repository the pom names."""
        self.assertEqual(archival, Archival(archived=True, subject=ArchivedSubject.REPOSITORY))
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING, _GUAVA_POM, _GUAVA_REPOSITORY])

    def assert_github_unasked(self, mock_get: Mock, archival: Archival, *urls: str) -> None:
        """Assert that the run reported no archival, having asked the given URLs and GitHub nothing.

        GitHub declares every repository archived in these tests, so asking it nothing is what leaves the artefact
        unarchived.
        """
        self.assertEqual(archival, Archival())
        self.assertEqual(requested_urls(mock_get), list(urls))

    @kills(
        Mutation(
            pom_xml_format,
            '("url", "connection", "developerConnection")',
            '("url",)',
            "a pom naming its repository in a connection alone is read as naming none, so its archival goes unread",
        )
    )
    def test_a_pom_naming_an_archived_repository(self):
        """Test that an artefact whose pom names a repository GitHub declares archived is reported as archived."""
        cases = {
            "url": ("url", "https://github.com/google/guava"),
            "connection": ("connection", _GUAVA_SCM),
            "developerConnection": ("developerConnection", "scm:git:git@github.com:google/guava.git"),
        }
        for case, (tag, scm) in cases.items():
            with self.subTest(case=case):
                self.clear_caches()
                served = maven_central_pom(scm, tag)
                with patch_maven_central(_DATED_LISTING, served, archived=True) as mock_get:
                    self.assert_archived(mock_get, project(_GUAVA, check_archival=True).archival)

    def test_a_pom_naming_a_repository_on_another_host(self):
        """Test that an artefact whose pom names a repository outside GitHub leaves GitHub unasked."""
        elsewhere = "scm:git:https://gitbox.apache.org/repos/asf/commons-lang.git"
        with patch_maven_central(_DATED_LISTING, maven_central_pom(elsewhere), archived=True) as mock_get:
            archival = project(_GUAVA, check_archival=True).archival
        self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING, _GUAVA_POM)

    def test_a_pom_naming_no_source_repository(self):
        """Test that an artefact whose pom declares no `<scm>` element leaves GitHub unasked."""
        with patch_maven_central(_DATED_LISTING, maven_central_pom(), archived=True) as mock_get:
            archival = project(_GUAVA, check_archival=True).archival
        self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING, _GUAVA_POM)

    def test_an_artefact_the_repository_dates_no_version_for(self):
        """Test that an artefact whose listing dates no version has no pom to read, so nothing follows the listing."""
        undated = maven_central_listing()
        with patch_maven_central(undated, maven_central_pom(_GUAVA_SCM), archived=True) as mock_get:
            reported = project(_GUAVA, check_archival=True)
        self.assertEqual(reported, Project())
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING])

    def test_a_pom_the_repository_does_not_serve(self):
        """Test that a pom the repository withholds is warned about, and leaves GitHub unasked about the artefact."""
        with patch_maven_central(_DATED_LISTING, None, archived=True) as mock_get:
            archival = project(_GUAVA, check_archival=True).archival
        self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING, _GUAVA_POM)
        self.assert_could_not_fetch_logged(url=_GUAVA_POM, status=404, reason="Not Found")

    @kills(
        Mutation(
            pom_xml_format.scm_urls,
            "    if project is None:\n        return None\n",
            "",
            "an artefact pom whose XML does not parse ends the run",
            raises="AttributeError: 'NoneType' object has no attribute 'child'",
        )
    )
    def test_a_pom_whose_xml_does_not_parse(self):
        """Test that an artefact whose pom does not parse is warned about, and leaves GitHub unasked."""
        with patch_maven_central(_DATED_LISTING, "<project><scm>", archived=True) as mock_get:
            archival = project(_GUAVA, check_archival=True).archival
        self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING, _GUAVA_POM)
        self.assert_logged(Logger._MESSAGE_INVALID_POM, url=_GUAVA_POM)

    @kills(
        Mutation(
            maven_central,
            "@cache\ndef _pom",
            "def _pom",
            "every pom declaring an artefact costs a pom request of its own",
        )
    )
    def test_an_artefact_is_asked_for_its_pom_once_per_run(self):
        """Test that two poms declaring the same artefact cost one pom request between them."""
        with patch_maven_central(_DATED_LISTING, maven_central_pom(_GUAVA_SCM), archived=True) as mock_get:
            first = project(_GUAVA, check_archival=True).archival
            second = project(_GUAVA, check_archival=True).archival
        # Asserting what each answered, so the one request says the pom was shared rather than never read.
        self.assert_archived(mock_get, first)
        self.assertEqual(second, first)

    def test_a_run_that_checks_nothing_for_archival(self):
        """Test that a run checking no dependency for archival reads no pom, so the listing is all it asks for."""
        with patch_maven_central(_DATED_LISTING, maven_central_pom(_GUAVA_SCM), archived=True) as mock_get:
            archival = project(_GUAVA, check_archival=False).archival
        self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING)


class VersionsWithinCooldownTest(LoggingTestCase):
    """Unit tests for reading the versions the repository published inside the cooldown window."""

    def test_only_a_version_published_inside_the_window_is_held_back(self):
        """Test that the version dated inside the window is held back, and the one dated before it is not."""
        settled = maven_central_version_row("33.7.0-jre", _days_ago(COOLDOWN.default + 1))
        fresh = maven_central_version_row("33.7.1-jre", _days_ago(1))
        with patch_get(text=maven_central_listing(settled, fresh)):
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
        unreadable = maven_central_dated_row("33.7.2-jre/", "2026-13-45 99:99", "-")
        fresh = maven_central_version_row("33.7.1-jre", _days_ago(1))
        with patch_get(text=maven_central_listing(unreadable, fresh)):
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
        listing = maven_central_listing(maven_central_version_row("33.7.1-jre", published), *_metadata_rows(published))
        with patch_get(text=listing):
            held_back = versions_within_cooldown(_GUAVA, COOLDOWN.default)
        self.assertEqual(held_back, ("33.7.1-jre",))

    @kills(
        Mutation(
            maven_central,
            "@cache\ndef _listing",
            "def _listing",
            "every pom declaring an artefact costs a request of its own",
        )
    )
    def test_an_artefact_is_asked_about_once_per_run(self):
        """Test that asking about an artefact twice costs one request."""
        mock_get = Mock(
            return_value=mock_response(
                text=maven_central_listing(maven_central_version_row("33.7.1-jre", _days_ago(1)))
            )
        )
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
        rows = (
            maven_central_version_row("33.7.0-jre", newest),
            maven_central_version_row("33.8.0-jre", datetime(2020, 1, 2, 3, 4, tzinfo=UTC)),
        )
        with patch_get(text=maven_central_listing(*rows)):
            self.assertEqual(_newest_release(_GUAVA), Release("33.7.0-jre", newest))

    def test_a_listing_the_repository_does_not_serve_dates_no_release(self):
        """Test that an artefact whose listing the repository withholds leaves staleness nothing to measure."""
        answer = mock_response(ok=False, status_code=404, reason="Not Found", url=_GUAVA_LISTING, text="")
        mock_get = Mock(return_value=answer)
        with patch("requests.get", mock_get):
            self.assertIsNone(_newest_release(_GUAVA))
        # Asserting the request was made, so the missing release says something about the answer rather than
        # about a request that was never sent.
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING])

    @kills(
        Mutation(
            _newest_release,
            "_listing(artefact)",
            "_listing.__wrapped__(artefact)",
            "staleness fetches a listing of its own, so an artefact the cooldown read costs a second request",
        )
    )
    def test_the_cooldown_has_already_paid_for_the_listing(self):
        """Test that the newest release is free once the cooldown has read the same artefact's listing."""
        # The listing dates a row to the minute, so the expected release is dated to the minute too.
        dated = _days_ago(1).replace(second=0, microsecond=0)
        mock_get = Mock(
            return_value=mock_response(text=maven_central_listing(maven_central_version_row("33.7.1-jre", dated)))
        )
        with patch("requests.get", mock_get):
            held_back = versions_within_cooldown(_GUAVA, COOLDOWN.default)
            newest = _newest_release(_GUAVA)
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING])
        # Asserting what each read, so the single request says the listing was shared rather than never read.
        self.assertEqual(held_back, ("33.7.1-jre",))
        self.assertEqual(newest, Release("33.7.1-jre", dated))
