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
from update_time.sources.maven_central import (
    _newest_release,
    _published,
    get_changes,
    project,
    versions_within_cooldown,
)

from tests.helpers import mock_response, patch_environ, patch_get
from tests.mutation import Mutation, kills
from tests.update_time.helpers import (
    LoggingTestCase,
    github_release_json,
    maven_central_dated_row,
    maven_central_listing,
    maven_central_pom,
    maven_central_row,
    maven_central_version_row,
    patch_maven_central,
)
from tests.update_time.sources.helpers import (
    contents_json,
    contents_url,
    file_url,
    markdown_changelog,
    markdown_changes,
    releases_url,
    requested_urls,
    respond_per_url,
)


def _guava_pom_url(artifact: str) -> str:
    """Return the URL of the pom Maven Central serves for the artifact in guava's group, at guava's version."""
    return f"{_GUAVA_GROUP_URL}/{artifact}/{_GUAVA_VERSION}/{artifact}-{_GUAVA_VERSION}.pom"


_GUAVA = "com.google.guava:guava"
_GUAVA_GROUP_URL = "https://repo1.maven.org/maven2/com/google/guava"
_GUAVA_LISTING = f"{_GUAVA_GROUP_URL}/guava/"
_GUAVA_VERSION = "33.7.1-jre"
_GUAVA_POM = _guava_pom_url("guava")
_GUAVA_REPOSITORY = "https://api.github.com/repos/google/guava"
_GUAVA_SCM = "scm:git:https://github.com/google/guava.git"
_GUAVA_PARENT = f"com.google.guava:guava-parent:{_GUAVA_VERSION}"
_GUAVA_PARENT_POM = _guava_pom_url("guava-parent")
# An artefact whose repository is named otherwise than its artifact.
_NETTY_ALL = "io.netty:netty-all"
_NETTY_VERSION = "4.1.138.Final"
_NETTY_SCM = "scm:git:https://github.com/netty/netty.git"


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


def _listing_of(version: str) -> str:
    """Return a listing that holds the version alone, dated yesterday."""
    return maven_central_listing(maven_central_version_row(version, _days_ago(1)))


# The listing the repository serves for guava where the tests care about the pom beside a version rather than the
# dates of the versions themselves.
_DATED_LISTING = _listing_of(_GUAVA_VERSION)


def _serve_guava_with_parent(mock_get: Mock, parent_pom: str, responses: dict[str, Mock] | None = None) -> None:
    """Point the mock requests.get at guava's listing, a guava pom naming guava's parent, and the parent's pom."""
    served = {
        _GUAVA_LISTING: mock_response(text=_DATED_LISTING),
        _GUAVA_POM: mock_response(content=maven_central_pom(parent=_GUAVA_PARENT).encode()),
        _GUAVA_PARENT_POM: mock_response(content=parent_pom.encode()),
    }
    respond_per_url(mock_get, served | (responses or {}))


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

    @kills(
        Mutation(
            maven_central._pom_repository,
            "    named = _named_repository(pom)\n",
            "    named = _named_repository(pom)\n"
            "    if eager := pom_xml_format.parent(pom):\n"
            "        _pom(eager.name, eager.version)\n",
            "every artefact whose pom names its repository costs a request for its parent pom as well",
        )
    )
    def test_a_pom_naming_a_github_repository_leaves_its_parent_pom_unread(self):
        """Test that an artefact whose pom names a GitHub repository is checked without a request for its parent pom."""
        served = maven_central_pom(_GUAVA_SCM, parent=_GUAVA_PARENT)
        with patch_maven_central(_DATED_LISTING, served, archived=True) as mock_get:
            self.assert_archived(mock_get, project(_GUAVA, check_archival=True).archival)

    @kills(
        Mutation(
            maven_central._parent_pom,
            "parent = pom_xml_format.parent(pom)",
            "parent = None",
            "an artefact whose pom leaves its repository to the parent pom goes unchecked for archival",
        )
    )
    def test_a_pom_naming_no_repository_takes_the_one_its_parent_pom_names(self):
        """Test that an artefact whose own pom does not name a GitHub repository is checked against its parent's."""
        with patch("requests.get") as mock_get:
            archived = {_GUAVA_REPOSITORY: mock_response({"archived": True})}
            _serve_guava_with_parent(mock_get, maven_central_pom(_GUAVA_SCM), archived)
            archival = project(_GUAVA, check_archival=True).archival
        self.assertEqual(archival, Archival(archived=True, subject=ArchivedSubject.REPOSITORY))
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING, _GUAVA_POM, _GUAVA_PARENT_POM, _GUAVA_REPOSITORY])

    @kills(
        Mutation(
            maven_central._pom_repository,
            "_parent_pom(pom))",
            "_parent_pom(_parent_pom(pom) or pom))",
            "an artefact's grandparent pom is read too, which may name a generic parent project's repository",
            raises=f"KeyError: '{_guava_pom_url('guava-grandparent')}'",
        )
    )
    def test_a_parent_pom_naming_no_repository_leaves_its_own_parent_unread(self):
        """Test that a parent pom that does not name a GitHub repository leaves GitHub and its own parent unasked."""
        parent_pom = maven_central_pom(parent=f"com.google.guava:guava-grandparent:{_GUAVA_VERSION}")
        with patch("requests.get") as mock_get:
            _serve_guava_with_parent(mock_get, parent_pom)
            archival = project(_GUAVA, check_archival=True).archival
        self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING, _GUAVA_POM, _GUAVA_PARENT_POM)

    @kills(
        Mutation(
            pom_xml_format.fully_resolved,
            "part and _is_resolved(part)",
            "_is_resolved(part)",
            "a parent pom is fetched at a URL with a coordinate left out, which Maven Central serves nothing at",
        ),
        Mutation(
            pom_xml_format.parent,
            "    return pinned if fully_resolved(pinned) else None",
            "    return pinned",
            "a parent pom is fetched at a URL that spells out a property, which Maven Central serves nothing at",
        ),
    )
    def test_a_parent_missing_a_coordinate_or_naming_one_by_property_is_passed_over(self):
        """Test that a `<parent>` without a group, artifact, or version of its own leaves its pom and GitHub unasked."""
        cases = {
            "no groupId": f":guava-parent:{_GUAVA_VERSION}",
            "no artifactId": f"com.google.guava::{_GUAVA_VERSION}",
            "no version": "com.google.guava:guava-parent:",
            "groupId as a property": f"${{project.groupId}}:guava-parent:{_GUAVA_VERSION}",
            "version as a property": "com.google.guava:guava-parent:${revision}",
        }
        for case, parent in cases.items():
            with self.subTest(case=case):
                self.clear_caches()
                served = maven_central_pom(parent=parent)
                with patch_maven_central(_DATED_LISTING, served, archived=True) as mock_get:
                    archival = project(_GUAVA, check_archival=True).archival
                self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING, _GUAVA_POM)

    @kills(
        Mutation(
            maven_central._parent_pom,
            "    if pom_xml_format.scm_urls(pom):\n        return None\n",
            "",
            "an artefact whose pom names its repository on another host is given its generic parent's repository",
        )
    )
    def test_a_pom_naming_a_repository_on_another_host(self):
        """Test that an artefact whose pom names a repository outside GitHub leaves its parent and GitHub unasked."""
        elsewhere = "scm:git:https://gitbox.apache.org/repos/asf/commons-lang.git"
        served = maven_central_pom(elsewhere, parent=_GUAVA_PARENT)
        with patch_maven_central(_DATED_LISTING, served, archived=True) as mock_get:
            archival = project(_GUAVA, check_archival=True).archival
        self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING, _GUAVA_POM)

    def test_a_pom_naming_no_source_repository(self):
        """Test that a pom naming a repository in neither `<scm>` nor its `<url>` leaves GitHub unasked."""
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
            maven_central._pom,
            "_LOG.invalid_pom(url)",
            "pass",
            "an artefact pom whose XML does not parse goes unreported",
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
            "@cache\ndef _pom(",
            "def _pom(",
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

    @kills(
        Mutation(
            maven_central._parent_pom,
            "_pom(parent.name",
            "_pom.__wrapped__(parent.name",
            "every artefact sharing a parent costs a request for the parent's pom of its own",
        )
    )
    def test_artefacts_sharing_a_parent_are_asked_for_its_pom_once_per_run(self):
        """Test that two artefacts whose poms name the same parent cost one request for the parent's pom."""
        with patch_maven_central(_DATED_LISTING, maven_central_pom(parent=_GUAVA_PARENT)) as mock_get:
            project(_GUAVA, check_archival=True)
            project("com.google.guava:guava-testlib", check_archival=True)
        self.assertEqual(requested_urls(mock_get).count(_GUAVA_PARENT_POM), 1)

    def test_a_run_that_checks_nothing_for_archival(self):
        """Test that a run checking no dependency for archival reads no pom, so the listing is all it asks for."""
        with patch_maven_central(_DATED_LISTING, maven_central_pom(_GUAVA_SCM), archived=True) as mock_get:
            archival = project(_GUAVA, check_archival=False).archival
        self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING)


class GetChangesTest(LoggingTestCase):
    """Unit tests for reading what a version of an artefact changed."""

    @staticmethod
    def serve_releases_and_changelog(
        mock_get: Mock, releases: list[dict[str, object]], heading: str = _GUAVA_VERSION
    ) -> None:
        """Point the mock requests.get at guava's listing and pom, and at a repository holding the releases.

        The repository's root holds a changelog file with a section headed `heading`. Unlike `patch_maven_central`,
        which answers any URL it does not know with the listing, this fails a request for a URL it does not serve.
        """
        respond_per_url(
            mock_get,
            {
                _GUAVA_LISTING: mock_response(text=_DATED_LISTING),
                _GUAVA_POM: mock_response(content=maven_central_pom(_GUAVA_SCM).encode()),
                releases_url("google/guava"): mock_response(releases),
                contents_url("google/guava"): mock_response(contents_json("CHANGELOG.md")),
                file_url("CHANGELOG.md"): mock_response(text=markdown_changelog(heading)),
            },
        )

    def assert_changes_from_releases(self, cases: dict[str, tuple[str, str]]) -> None:
        """Assert that each case's version has the changes of the release the repository published under its tag."""
        for case, (version, tag) in cases.items():
            with self.subTest(case=case):
                self.clear_caches()
                releases = [github_release_json(tag, body=f"Changes in {version}")]
                with patch_maven_central(_listing_of(version), maven_central_pom(_GUAVA_SCM), releases=releases):
                    self.assertEqual(get_changes(_GUAVA, version), f"Changes in {version}")

    @kills(
        Mutation(
            maven_central.get_changes,
            "repository, version)\n",
            "repository, _newest_release(artefact).version)\n",
            "the newest release's changes are reported for a version the run left behind it",
        ),
    )
    def test_the_changes_are_those_of_the_release_matching_the_version(self):
        """Test that the changes are the body of the release for the version the run left the dependency on."""
        releases = [
            github_release_json("v33.6.0-jre", body="Changes in 33.6.0"),
            github_release_json(f"v{_GUAVA_VERSION}", body="Changes in 33.7.1"),
        ]
        with patch_maven_central(_DATED_LISTING, maven_central_pom(_GUAVA_SCM), releases=releases):
            self.assertEqual(get_changes(_GUAVA, "33.6.0-jre"), "Changes in 33.6.0")

    @kills(
        Mutation(
            maven_central.get_changes,
            "_repository(artefact)",
            "_pom_repository(artefact, version)",
            "the repository is read from the pom beside the version, which may predate the project's move to GitHub",
        )
    )
    def test_the_repository_is_read_from_the_newest_releases_pom(self):
        """Test that the pom naming the repository is the newest release's, whatever version the changes are for."""
        with patch_maven_central(_DATED_LISTING, maven_central_pom(_GUAVA_SCM)) as mock_get:
            get_changes(_GUAVA, "33.6.0-jre")
        self.assertIn(_GUAVA_POM, requested_urls(mock_get))

    @kills(
        Mutation(
            maven_central._release_tags,
            "(artifact_id,",
            "(artefact,",
            "a release tagged by the artifact's name goes unmatched, since the tag names never carry the group",
        ),
        Mutation(
            maven_central._release_tags,
            "spelling, repository)",
            "spelling)",
            "a release tagged by the repository's name rather than the artifact's goes unmatched",
        ),
        Mutation(
            maven_central._release_tags,
            "(artifact_id, spelling, repository)",
            "(repository, spelling, artifact_id)",
            "a repository tagging one version by both names has the repository's release reported for the artifact",
        ),
        Mutation(
            maven_central._release_tags,
            "*release_tags(artifact_id, spelling, repository),",
            "*release_tags(artifact_id, spelling),\n            *release_tags(repository, spelling),",
            "a repository tagging one version by its name and by the version alone has the wrong release reported",
        ),
        Mutation(
            maven_central._spellings,
            '(version, _QUALIFIER.sub("", version))',
            '(_QUALIFIER.sub("", version), version)',
            "a repository tagging one version with and without its qualifier has the wrong release reported for it",
        ),
    )
    def test_the_more_specific_of_two_tags_for_a_version_takes_precedence(self):
        """Test that the release under the more specific of two tags for the same version holds its changes."""
        cases = {
            "artifact name over repository name": (f"netty-{_NETTY_VERSION}", f"netty-all-{_NETTY_VERSION}"),
            "repository name over version alone": (f"v{_NETTY_VERSION}", f"netty-{_NETTY_VERSION}"),
            "qualifier over no qualifier": ("v4.1.138", f"v{_NETTY_VERSION}"),
        }
        for case, (less_specific, more_specific) in cases.items():
            with self.subTest(case=case):
                self.clear_caches()
                releases = [
                    github_release_json(less_specific, body="Less specific"),
                    github_release_json(more_specific, body="More specific"),
                ]
                with patch_maven_central(_listing_of(_NETTY_VERSION), maven_central_pom(_NETTY_SCM), releases=releases):
                    self.assertEqual(get_changes(_NETTY_ALL, _NETTY_VERSION), "More specific")

    @kills(
        Mutation(
            maven_central._release_tags,
            "in _spellings(version)",
            "in (version,)",
            "a release tagged without the qualifier Maven appends to the version goes unmatched",
        )
    )
    def test_a_release_tagged_with_the_version_without_its_qualifier_matches(self):
        """Test that a release tagged with the version less the qualifier Maven appends holds the version's changes."""
        cases = {"dash qualifier": ("33.7.1-jre", "v33.7.1"), "dot qualifier": ("7.4.10.Final", "7.4.10")}
        self.assert_changes_from_releases(cases)

    @kills(
        Mutation(
            maven_central._release_tags,
            "in _VERSION_PREFIXES",
            "in ()",
            "a release tagged with a prefix other than `v` goes unmatched",
        ),
        Mutation(
            maven_central._release_tags,
            'f"{prefix}{spelling}"',
            'f"{prefix}{version}"',
            "a release tagged with a prefix other than `v` and without the version's qualifier goes unmatched",
        ),
    )
    def test_a_release_tagged_with_a_prefix_other_than_v_matches(self):
        """Test that a release tagged with the version behind `r`, `version-`, or `REL` holds the version's changes."""
        cases = {
            "r": ("6.1.3", "r6.1.3"),
            "version-": ("2.5.250", "version-2.5.250"),
            "REL": ("42.7.13", "REL42.7.13"),
            "prefix without the qualifier": ("6.1.3-jre", "r6.1.3"),
        }
        self.assert_changes_from_releases(cases)

    @kills(
        Mutation(
            maven_central.get_changes,
            " or _changes_from_changelog_file(\n        owner, repository, version\n    )",
            "",
            "the changes of a version its repository tagged without publishing a release go unreported",
        ),
        Mutation(
            maven_central._changes_from_changelog_file,
            "in _spellings(version)",
            "in (version,)",
            "a changelog heading the version without the qualifier Maven appends has its changes go unreported",
        ),
    )
    def test_the_changes_come_from_a_changelog_file_where_no_release_matches(self):
        """Test that the changes come from the changelog file when the repository did not release the version."""
        for heading in (_GUAVA_VERSION, "33.7.1"):
            with self.subTest(heading=heading), patch("requests.get") as mock_get:
                self.clear_caches()
                self.serve_releases_and_changelog(mock_get, [github_release_json("v33.6.0-jre")], heading)
                self.assertEqual(get_changes(_GUAVA, _GUAVA_VERSION), markdown_changes(heading))

    @kills(
        Mutation(
            maven_central.get_changes,
            "return changes_from_tagged_release(",
            "return changes_from_changelog_file(owner, repository, version) or changes_from_tagged_release(",
            "the changelog file wins over the release the repository published for the version",
        ),
        Mutation(
            maven_central.get_changes,
            "    tags =",
            "    changelog = changes_from_changelog_file(owner, repository, version)\n    tags =",
            "every dependency Maven moved costs a changelog file request, whether or not a release matched",
        ),
    )
    def test_a_matching_release_wins_over_the_changelog_file(self):
        """Test that the changes come from the release matching the version, without reading the changelog file."""
        releases = [github_release_json(f"v{_GUAVA_VERSION}", body="Changes in 33.7.1")]
        with patch("requests.get") as mock_get:
            self.serve_releases_and_changelog(mock_get, releases)
            self.assertEqual(get_changes(_GUAVA, _GUAVA_VERSION), "Changes in 33.7.1")
        self.assertNotIn(contents_url("google/guava"), requested_urls(mock_get))

    @kills(
        Mutation(
            pom_xml_format.source_urls,
            "    return urls if project_url is None else [*urls, project_url.text]",
            "    return urls",
            "a pom naming its repository in the project's `<url>` alone is read as naming none",
        ),
        Mutation(
            pom_xml_format.source_urls,
            "    urls = scm_urls(project)\n",
            '    urls = scm_urls(project)\n    if project.child("scm") is None:\n        return urls\n',
            "Update-time reads the project's `<url>` only when the pom declares an `<scm>` element",
        ),
        Mutation(
            pom_xml_format.source_urls,
            "[*urls, project_url.text]",
            "[project_url.text, *urls]",
            "the project's `<url>` wins over `<scm>`, so a module's changes are read from its umbrella project",
        ),
    )
    def test_the_changes_come_from_the_first_repository_on_github_the_pom_names(self):
        """Test that `<scm>` names the repository the changes come from, and the project's `<url>` where it fails to."""
        umbrella = "https://github.com/google/guava-umbrella"
        cases = {
            "no scm": (maven_central_pom(project_url=umbrella), "google/guava-umbrella"),
            "scm on another host": (
                maven_central_pom("scm:git:https://gitbox.example.org/guava.git", project_url=umbrella),
                "google/guava-umbrella",
            ),
            "scm and url on github": (maven_central_pom(_GUAVA_SCM, project_url=umbrella), "google/guava"),
        }
        releases = [github_release_json(f"v{_GUAVA_VERSION}", body="Changes in 33.7.1")]
        for case, (pom, repository) in cases.items():
            with self.subTest(case=case):
                self.clear_caches()
                with patch_maven_central(_DATED_LISTING, pom, releases=releases) as mock_get:
                    self.assertEqual(get_changes(_GUAVA, _GUAVA_VERSION), "Changes in 33.7.1")
                self.assertIn(releases_url(repository), requested_urls(mock_get))

    @kills(
        Mutation(
            maven_central.get_changes,
            "_repository(artefact)",
            "_named_repository(_pom(artefact, _newest_release(artefact).version))",
            "an artefact whose pom leaves its repository to the parent pom has its changes go unreported",
        )
    )
    def test_the_changes_come_from_the_repository_the_parent_pom_names(self):
        """Test that an artefact whose own pom does not name a GitHub repository has its parent's release notes."""
        releases = [github_release_json(f"v{_GUAVA_VERSION}", body="Changes in 33.7.1")]
        with patch("requests.get") as mock_get:
            _serve_guava_with_parent(
                mock_get, maven_central_pom(_GUAVA_SCM), {releases_url("google/guava"): mock_response(releases)}
            )
            self.assertEqual(get_changes(_GUAVA, _GUAVA_VERSION), "Changes in 33.7.1")


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
