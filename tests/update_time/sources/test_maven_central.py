"""Unit tests for the Maven Central source."""

import time
import unittest
from datetime import UTC, datetime
from unittest.mock import Mock, patch

import requests

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import Archival, ArchivedSubject, Project, Release
from update_time.io.log import Logger
from update_time.manifests import pom_xml as pom_xml_format
from update_time.sources import maven_central
from update_time.sources.maven_central import _newest_release, _published, get_changes, project, versions_held_back

from tests.helpers import mock_response, patch_environ, patch_get
from tests.mutation import Mutation, kills
from tests.update_time.helpers import (
    GITBOX_SCM,
    GUAVA,
    GUAVA_SCM,
    LoggingTestCase,
    days_ago,
    github_release_json,
    maven_central_dated_row,
    maven_central_listing,
    maven_central_listing_url,
    maven_central_pom,
    maven_central_pom_url,
    maven_central_row,
    maven_central_version_row,
    message_of,
    patch_maven_central,
    properties_element,
)
from tests.update_time.sources.helpers import (
    contents_json,
    contents_url,
    file_url,
    markdown_changelog,
    markdown_changes,
    releases_url,
    repository_url,
    requested_urls,
    respond_per_url,
)


def _guava_pom_url(artifact: str) -> str:
    """Return the URL of the pom Maven Central serves for the artifact in guava's group, at guava's version."""
    return maven_central_pom_url(f"com.google.guava:{artifact}", _GUAVA_VERSION)


_GUAVA_LISTING = maven_central_listing_url(GUAVA)
_GUAVA_VERSION = "33.7.1-jre"
_GUAVA_POM = _guava_pom_url("guava")
_GUAVA_REPOSITORY = repository_url("google/guava")
# A module's `<url>` may name this umbrella project rather than the module's own repository.
_GUAVA_UMBRELLA = "https://github.com/google/guava-umbrella"
_GUAVA_PARENT = f"com.google.guava:guava-parent:{_GUAVA_VERSION}"
_GUAVA_PARENT_POM = _guava_pom_url("guava-parent")
# An artefact whose repository is named otherwise than its artifact.
_NETTY_ALL = "io.netty:netty-all"
_NETTY_VERSION = "4.1.138.Final"
_NETTY_SCM = "scm:git:https://github.com/netty/netty.git"


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
    return maven_central_listing(maven_central_version_row(version, days_ago(1)))


# The listing the repository serves for guava where the tests care about the pom beside a version rather than the
# dates of the versions themselves.
_DATED_LISTING = _listing_of(_GUAVA_VERSION)


def _serve_guava_with_parent(mock_get: Mock, parent_pom: str, responses: dict[str, Mock] | None = None) -> None:
    """Point the mock Session.get at guava's listing, a guava pom naming guava's parent, and the parent's pom."""
    served = {
        _GUAVA_LISTING: mock_response(text=_DATED_LISTING),
        _GUAVA_POM: mock_response(content=maven_central_pom(parent=_GUAVA_PARENT).encode()),
        _GUAVA_PARENT_POM: mock_response(content=parent_pom.encode()),
    }
    respond_per_url(mock_get, served | (responses or {}))


class ProjectTest(LoggingTestCase):
    """Unit tests for reading whether the repository behind an artefact is archived."""

    def assert_archived(self, mock_get: Mock, archival: Archival, *urls: str) -> None:
        """Assert that the run reported the archival, having asked for guava's listing, its pom, and the given URLs."""
        self.assertEqual(archival, Archival(archived=True, subject=ArchivedSubject.REPOSITORY))
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING, _GUAVA_POM, *urls])

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
        """Test that an artefact whose pom names a repository GitHub declares archived is reported as archived.

        The pom names a parent, which stays unread.
        """
        cases = {
            "url": ("url", "https://github.com/google/guava"),
            "connection": ("connection", GUAVA_SCM),
            "developerConnection": ("developerConnection", "scm:git:git@github.com:google/guava.git"),
        }
        for case, (tag, scm) in cases.items():
            with self.subTest(case=case):
                served = maven_central_pom(scm, tag, parent=_GUAVA_PARENT)
                with patch_maven_central(_DATED_LISTING, served, archived=True) as mock_get:
                    self.assert_archived(mock_get, project(GUAVA, check_archival=True).archival, _GUAVA_REPOSITORY)

    def test_a_pom_naming_its_repository_through_its_coordinates(self):
        """Test that a pom naming its repository through its own coordinates is checked with them filled in."""
        cases = {
            "project.artifactId in scm": (
                maven_central_pom("https://github.com/google/${project.artifactId}"),
                "guava",
            ),
            "groupId in scm": (maven_central_pom("https://github.com/google/${groupId}"), "com.google.guava"),
            "project.artifactId in url": (
                maven_central_pom(project_url="https://github.com/google/${project.artifactId}"),
                "guava",
            ),
        }
        for case, (served, name) in cases.items():
            with self.subTest(case=case), patch_maven_central(_DATED_LISTING, served, archived=True) as mock_get:
                archival = project(GUAVA, check_archival=True).archival
                self.assert_archived(mock_get, archival, repository_url(f"google/{name}"))

    def test_a_pom_naming_its_repository_through_its_own_properties(self):
        """Test that a pom naming its repository through a property it declares is checked with that filled in."""
        scm = "https://github.com/${github.org}/${project.artifactId}"
        served = maven_central_pom(scm, properties=properties_element({"github.org": "google"}))
        with patch_maven_central(_DATED_LISTING, served, archived=True) as mock_get:
            self.assert_archived(mock_get, project(GUAVA, check_archival=True).archival, _GUAVA_REPOSITORY)

    def test_a_url_is_passed_over_where_its_owner_or_repository_names_a_property(self):
        """Test that a URL is passed over where its owner or repository names a property, not where its path does."""
        umbrella = repository_url("google/guava-umbrella")
        cases = {
            "owner": ("https://github.com/${github.owner}/guava", umbrella),
            "repository": ("https://github.com/google/${repo.name}", umbrella),
            "path below the repository": ("https://github.com/google/guava/tree/${project.scm.tag}", _GUAVA_REPOSITORY),
        }
        for case, (scm, repository) in cases.items():
            with self.subTest(case=case):
                served = maven_central_pom(scm, project_url=_GUAVA_UMBRELLA)
                with patch_maven_central(_DATED_LISTING, served, archived=True) as mock_get:
                    self.assert_archived(mock_get, project(GUAVA, check_archival=True).archival, repository)

    @kills(
        Mutation(
            maven_central._parent_pom,
            "parent = pom_xml_format.parent(pom)",
            "parent = None",
            "an artefact whose pom leaves its repository to the parent pom goes unchecked for archival",
        )
    )
    def test_a_pom_naming_no_repository_takes_the_one_its_parent_pom_names(self):
        """Test that an artefact whose own pom does not name a GitHub repository is checked against its parent's.

        A parent pom naming the repository through `${project.artifactId}` names the artefact's own artifact.
        """
        cases = {"literal": GUAVA_SCM, "artifactId": "https://github.com/google/${project.artifactId}"}
        for case, scm in cases.items():
            with self.subTest(case=case), patch("requests.Session.get") as mock_get:
                archived = {_GUAVA_REPOSITORY: mock_response({"archived": True})}
                _serve_guava_with_parent(mock_get, maven_central_pom(scm), archived)
                archival = project(GUAVA, check_archival=True).archival
                self.assert_archived(mock_get, archival, _GUAVA_PARENT_POM, _GUAVA_REPOSITORY)

    @kills(
        Mutation(
            maven_central._pom_repository,
            "parent_pom = _parent_pom(pom)\n",
            "parent_pom = _parent_pom(_parent_pom(pom).value or pom)\n",
            "an artefact's grandparent pom is read too, which may name a generic parent project's repository",
            raises=f"KeyError: '{_guava_pom_url('guava-grandparent')}'",
        )
    )
    def test_a_parent_pom_naming_no_repository_leaves_its_own_parent_unread(self):
        """Test that a parent pom that does not name a GitHub repository leaves GitHub and its own parent unasked."""
        parent_pom = maven_central_pom(parent=f"com.google.guava:guava-grandparent:{_GUAVA_VERSION}")
        with patch("requests.Session.get") as mock_get:
            _serve_guava_with_parent(mock_get, parent_pom)
            archival = project(GUAVA, check_archival=True).archival
        self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING, _GUAVA_POM, _GUAVA_PARENT_POM)

    @kills(
        Mutation(
            pom_xml_format.fully_resolved,
            "part and is_resolved(part)",
            "is_resolved(part)",
            "a parent pom is fetched at a URL with a coordinate left out, which Maven Central serves nothing at",
        ),
        Mutation(
            pom_xml_format.parent,
            "return pinned if fully_resolved(pinned) else None",
            "return pinned",
            "a parent pom is fetched at a URL that spells out a property, which Maven Central serves nothing at",
        ),
    )
    def test_a_pom_naming_no_repository_and_no_complete_parent_leaves_github_unasked(self):
        """Test that GitHub stays unasked for a pom lacking both a repository and a complete parent."""
        cases = {
            "no parent": "",
            "no groupId": f":guava-parent:{_GUAVA_VERSION}",
            "no artifactId": f"com.google.guava::{_GUAVA_VERSION}",
            "no version": "com.google.guava:guava-parent:",
            "groupId as a property": f"${{project.groupId}}:guava-parent:{_GUAVA_VERSION}",
            "version as a property": "com.google.guava:guava-parent:${revision}",
        }
        for case, parent in cases.items():
            with self.subTest(case=case):
                served = maven_central_pom(parent=parent)
                with patch_maven_central(_DATED_LISTING, served, archived=True) as mock_get:
                    archival = project(GUAVA, check_archival=True).archival
                self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING, _GUAVA_POM)

    @kills(
        Mutation(
            maven_central._parent_pom,
            "    if pom_xml_format.scm_urls(pom):\n        return LookedUp(None, absent=True)\n",
            "",
            "an artefact whose pom names its repository on another host is given its generic parent's repository",
        )
    )
    def test_a_pom_naming_a_repository_on_another_host(self):
        """Test that an artefact whose pom names a repository outside GitHub leaves its parent and GitHub unasked."""
        elsewhere = GITBOX_SCM
        served = maven_central_pom(elsewhere, parent=_GUAVA_PARENT)
        with patch_maven_central(_DATED_LISTING, served, archived=True) as mock_get:
            archival = project(GUAVA, check_archival=True).archival
        self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING, _GUAVA_POM)

    def test_an_artefact_the_repository_dates_no_version_for(self):
        """Test that an artefact whose listing dates no version has no pom to read, so nothing follows the listing."""
        undated = maven_central_listing()
        with patch_maven_central(undated, maven_central_pom(GUAVA_SCM), archived=True) as mock_get:
            reported = project(GUAVA, check_archival=True)
        self.assertEqual(reported, Project())
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING])

    def test_a_pom_the_repository_does_not_serve(self):
        """Test that a pom the repository withholds is warned about, and leaves GitHub unasked about the artefact."""
        with patch_maven_central(_DATED_LISTING, None, archived=True) as mock_get:
            archival = project(GUAVA, check_archival=True).archival
        self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING, _GUAVA_POM)
        self.assert_could_not_fetch_logged(_GUAVA_POM, status=404, reason="Not Found")

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
            archival = project(GUAVA, check_archival=True).archival
        self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING, _GUAVA_POM)
        self.assert_logged(message_of(Logger.invalid_pom), url=_GUAVA_POM)

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
        with patch_maven_central(_DATED_LISTING, maven_central_pom(GUAVA_SCM), archived=True) as mock_get:
            first = project(GUAVA, check_archival=True).archival
            second = project(GUAVA, check_archival=True).archival
        # Asserting what each answered, so the one request says the pom was shared rather than never read.
        self.assert_archived(mock_get, first, _GUAVA_REPOSITORY)
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
            project(GUAVA, check_archival=True)
            project("com.google.guava:guava-testlib", check_archival=True)
        self.assertEqual(requested_urls(mock_get).count(_GUAVA_PARENT_POM), 1)

    def test_a_run_that_checks_nothing_for_archival(self):
        """Test that a run checking no dependency for archival reads no pom, so the listing is all it asks for."""
        with patch_maven_central(_DATED_LISTING, maven_central_pom(GUAVA_SCM), archived=True) as mock_get:
            archival = project(GUAVA, check_archival=False).archival
        self.assert_github_unasked(mock_get, archival, _GUAVA_LISTING)


class LacksAGitHubRepositoryTest(LoggingTestCase):
    """Unit tests for telling whether an artefact's poms name a repository on GitHub."""

    @kills(
        Mutation(
            maven_central._pom_repository,
            "if pom is None:\n        return LookedUp(None)",
            "if pom is None:\n        return LookedUp(None, absent=True)",
            "a pom that fails to fetch reads as lacking a repository, so its `archived` scope is reported as redundant",
        ),
        Mutation(
            maven_central._pom_repository,
            "LookedUp(None, absent=parent_pom.absent)",
            "LookedUp(None, absent=True)",
            "a parent pom that fails to fetch reads as lacking a repository, so the `archived` scope is reported too",
        ),
        Mutation(
            maven_central._repository,
            "return LookedUp(None) if newest is None else",
            "return LookedUp(None, absent=True) if newest is None else",
            "a listing that fails to fetch reads as lacking a repository, so the `archived` scope is reported too",
        ),
        Mutation(
            maven_central._parent_pom,
            "return LookedUp(None, absent=True) if parent is None else",
            "return LookedUp(None) if parent is None else",
            "a pom naming neither a repository nor a parent reads as unknown, so its `archived` scope goes unreported",
        ),
    )
    def test_an_artefact_lacks_a_github_repository_only_where_its_poms_say_so(self):
        """Test that an artefact lacks a GitHub repository where its poms say so, not where they could not be read."""
        outside_github = maven_central_pom(GITBOX_SCM)
        failed = mock_response(ok=False, status_code=503, reason="Service Unavailable", text="")
        cases = {
            "a pom naming a repository outside GitHub": (
                {_GUAVA_POM: mock_response(content=outside_github.encode())},
                True,
            ),
            "a parent pom lacking a repository": ({}, True),
            "a pom lacking both a repository and a parent": (
                {_GUAVA_POM: mock_response(content=maven_central_pom().encode())},
                True,
            ),
            "a listing that fails to fetch": ({_GUAVA_LISTING: failed}, False),
            "a pom that fails to fetch": ({_GUAVA_POM: failed}, False),
            "a parent pom that fails to fetch": ({_GUAVA_PARENT_POM: failed}, False),
        }
        for case, (responses, lacks) in cases.items():
            with self.subTest(case=case), patch("requests.Session.get") as mock_get:
                _serve_guava_with_parent(mock_get, maven_central_pom(), responses)
                self.assertEqual(maven_central.lacks_a_github_repository(GUAVA), lacks)


class GetChangesTest(LoggingTestCase):
    """Unit tests for reading what a version of an artefact changed."""

    def test_an_artefact_without_a_github_repository_has_no_changes(self):
        """Test that an artefact whose pom does not name a GitHub repository lacks changes, and GitHub goes unasked."""
        with patch_maven_central(_DATED_LISTING, maven_central_pom()) as mock_get:
            self.assertEqual(get_changes(GUAVA, _GUAVA_VERSION), "")
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING, _GUAVA_POM])

    @staticmethod
    def serve_releases_and_changelog(
        mock_get: Mock, releases: list[dict[str, object]], heading: str = _GUAVA_VERSION
    ) -> None:
        """Point the mock Session.get at guava's listing and pom, and at a repository holding the releases.

        The repository's root holds a changelog file with a section headed `heading`. Unlike `patch_maven_central`,
        which answers any URL it does not know with the listing, this fails a request for a URL it does not serve.
        """
        respond_per_url(
            mock_get,
            {
                _GUAVA_LISTING: mock_response(text=_DATED_LISTING),
                _GUAVA_POM: mock_response(content=maven_central_pom(GUAVA_SCM).encode()),
                releases_url("google/guava"): mock_response(releases),
                contents_url("google/guava"): mock_response(contents_json("CHANGELOG.md")),
                file_url("CHANGELOG.md"): mock_response(text=markdown_changelog(heading)),
            },
        )

    def assert_changes_from_releases(self, cases: dict[str, tuple[str, str]]) -> None:
        """Assert that each case's version has the changes of the release the repository published under its tag."""
        for case, (version, tag) in cases.items():
            with self.subTest(case=case):
                releases = [github_release_json(tag, body=f"Changes in {version}")]
                with patch_maven_central(_listing_of(version), maven_central_pom(GUAVA_SCM), releases=releases):
                    self.assertEqual(get_changes(GUAVA, version), f"Changes in {version}")

    @kills(
        Mutation(
            maven_central.get_changes,
            "_release_tags(artefact, repository, version)",
            "_release_tags(artefact, repository, _newest_release(artefact).version)",
            "the newest release's changes are reported for a version the run left behind it",
        ),
    )
    def test_the_changes_are_those_of_the_release_matching_the_version(self):
        """Test that the changes are the body of the release for the version the run left the dependency on."""
        releases = [
            github_release_json("v33.6.0-jre", body="Changes in 33.6.0"),
            github_release_json(f"v{_GUAVA_VERSION}", body="Changes in 33.7.1"),
        ]
        with patch_maven_central(_DATED_LISTING, maven_central_pom(GUAVA_SCM), releases=releases):
            self.assertEqual(get_changes(GUAVA, "33.6.0-jre"), "Changes in 33.6.0")

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
        with patch_maven_central(_DATED_LISTING, maven_central_pom(GUAVA_SCM)) as mock_get:
            get_changes(GUAVA, "33.6.0-jre")
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
            "spelling, repository.name)",
            "spelling)",
            "a release tagged by the repository's name rather than the artifact's goes unmatched",
        ),
        Mutation(
            maven_central._release_tags,
            "(artifact_id, spelling, repository.name)",
            "(repository.name, spelling, artifact_id)",
            "a repository tagging one version by both names has the repository's release reported for the artifact",
        ),
        Mutation(
            maven_central._release_tags,
            "*release_tags(artifact_id, spelling, repository.name),",
            "*release_tags(artifact_id, spelling),\n            *release_tags(repository.name, spelling),",
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
            " or _changes_from_changelog_file(repository, version)",
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
            with self.subTest(heading=heading), patch("requests.Session.get") as mock_get:
                self.serve_releases_and_changelog(mock_get, [github_release_json("v33.6.0-jre")], heading)
                self.assertEqual(get_changes(GUAVA, _GUAVA_VERSION), markdown_changes(heading))

    @kills(
        Mutation(
            maven_central.get_changes,
            "return changes_from_tagged_release(",
            "return changes_from_changelog_file(repository, version) or changes_from_tagged_release(",
            "the changelog file wins over the release the repository published for the version",
        ),
        Mutation(
            maven_central.get_changes,
            "tags =",
            "changelog = changes_from_changelog_file(repository, version)\n    tags =",
            "every dependency Maven moved costs a changelog file request, whether or not a release matched",
        ),
    )
    def test_a_matching_release_wins_over_the_changelog_file(self):
        """Test that the changes come from the release matching the version, without reading the changelog file."""
        releases = [github_release_json(f"v{_GUAVA_VERSION}", body="Changes in 33.7.1")]
        with patch("requests.Session.get") as mock_get:
            self.serve_releases_and_changelog(mock_get, releases)
            self.assertEqual(get_changes(GUAVA, _GUAVA_VERSION), "Changes in 33.7.1")
        self.assertNotIn(contents_url("google/guava"), requested_urls(mock_get))

    @kills(
        Mutation(
            pom_xml_format.source_urls,
            "named = urls if project_url is None else [*urls, project_url.text]",
            "named = urls",
            "a pom naming its repository in the project's `<url>` alone is read as naming none",
        ),
        Mutation(
            pom_xml_format.source_urls,
            "urls = scm_urls(project)\n",
            'urls = scm_urls(project)\n    if project.child("scm") is None:\n        return urls\n',
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
        cases = {
            "no scm": (maven_central_pom(project_url=_GUAVA_UMBRELLA), "google/guava-umbrella"),
            "scm on another host": (
                maven_central_pom("scm:git:https://gitbox.example.org/guava.git", project_url=_GUAVA_UMBRELLA),
                "google/guava-umbrella",
            ),
            "scm and url on github": (maven_central_pom(GUAVA_SCM, project_url=_GUAVA_UMBRELLA), "google/guava"),
        }
        releases = [github_release_json(f"v{_GUAVA_VERSION}", body="Changes in 33.7.1")]
        for case, (pom, repository) in cases.items():
            with self.subTest(case=case):
                with patch_maven_central(_DATED_LISTING, pom, releases=releases) as mock_get:
                    self.assertEqual(get_changes(GUAVA, _GUAVA_VERSION), "Changes in 33.7.1")
                self.assertIn(releases_url(repository), requested_urls(mock_get))

    @kills(
        Mutation(
            maven_central.get_changes,
            "_repository(artefact)",
            "_named_repository(_pom(artefact, _newest_release(artefact).version), artefact)",
            "an artefact whose pom leaves its repository to the parent pom has its changes go unreported",
        )
    )
    def test_the_changes_come_from_the_repository_the_parent_pom_names(self):
        """Test that an artefact whose own pom does not name a GitHub repository has its parent's release notes."""
        releases = [github_release_json(f"v{_GUAVA_VERSION}", body="Changes in 33.7.1")]
        with patch("requests.Session.get") as mock_get:
            _serve_guava_with_parent(
                mock_get, maven_central_pom(GUAVA_SCM), {releases_url("google/guava"): mock_response(releases)}
            )
            self.assertEqual(get_changes(GUAVA, _GUAVA_VERSION), "Changes in 33.7.1")


class VersionsHeldBackTest(LoggingTestCase):
    """Unit tests for reading the versions the cooldown holds back."""

    def test_only_a_version_published_inside_the_window_is_held_back(self):
        """Test that the version dated inside the cooldown is held back, and the one dated before it is not."""
        past_cooldown = maven_central_version_row("33.7.0-jre", days_ago(COOLDOWN.default + 1))
        fresh = maven_central_version_row("33.7.1-jre", days_ago(1))
        with patch_get(text=maven_central_listing(past_cooldown, fresh)):
            self.assertEqual(versions_held_back(GUAVA, COOLDOWN.default), ("33.7.1-jre",))

    @kills(
        Mutation(
            maven_central._listing,
            "    if response.status_code == HTTPStatus.NOT_FOUND:\n        _LOG.unserved_listing(response)\n"
            "        return LookedUp(None, absent=True)\n",
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
        with patch("requests.Session.get", mock_get):
            self.assertEqual(versions_held_back(GUAVA, COOLDOWN.default), ())
        # Asserting the request was made, so holding nothing back says something about the answer rather than
        # about a request that was never sent.
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING])
        self.assert_logged(message_of(Logger.unserved_listing), url=_GUAVA_LISTING, status=404, reason="Not Found")

    @kills(
        Mutation(
            maven_central._listing,
            "    if not response.ok:\n        _LOG.response(response)\n        return LookedUp(None)\n",
            "",
            "the repository fails without a word, so the cooldown is lost for an artefact it does serve",
        )
    )
    def test_an_error_other_than_a_missing_listing_is_warned_about(self):
        """Test that a repository answering with a server error warns, and that nothing is held back."""
        answer = mock_response(ok=False, status_code=503, reason="Service Unavailable", url=_GUAVA_LISTING, text="")
        with patch("requests.Session.get", Mock(return_value=answer)):
            self.assertEqual(versions_held_back(GUAVA, COOLDOWN.default), ())
        self.assert_could_not_fetch_logged(_GUAVA_LISTING, status=503, reason="Service Unavailable")

    @kills(
        Mutation(
            maven_central._listing,
            "    if response is None:\n        return LookedUp(None)\n",
            "",
            "a listing whose request fails ends the run with a traceback",
            raises="AttributeError: 'NoneType' object has no attribute 'status_code'",
        )
    )
    def test_a_listing_request_that_times_out_holds_no_version_back(self):
        """Test that a listing request that times out is reported as a timeout, and that nothing is held back."""
        with patch("requests.Session.get", Mock(side_effect=requests.exceptions.Timeout)):
            self.assertEqual(versions_held_back(GUAVA, COOLDOWN.default), ())
        self.assert_logged(message_of(Logger.timeout), url=_GUAVA_LISTING)

    @kills(
        Mutation(
            maven_central._published,
            "try:\n        return datetime.strptime(published, _PUBLISHED_FORMAT).replace(tzinfo=UTC)\n"
            "    except ValueError:\n        return None\n",
            "return datetime.strptime(published, _PUBLISHED_FORMAT).replace(tzinfo=UTC)\n",
            "one row the repository dated outside the calendar ends the run over every pom",
            raises="ValueError: time data '2026-13-45 99:99' does not match format '%Y-%m-%d %H:%M'",
        ),
        Mutation(
            maven_central._held_back,
            "return True",
            "return False",
            "the versions plugin adopts a version dated outside the calendar, with the cooldown unchecked",
        ),
        Mutation(
            maven_central._held_back,
            '_LOG.unreadable_publication_date(artefact, row["version"], row["published"])',
            "pass",
            "a version dated outside the calendar is held back without a word",
        ),
    )
    def test_a_row_dated_in_a_shape_the_format_cannot_read_is_held_back_and_logged(self):
        """Test that a version whose date does not parse is held back and logged, and the rows beside it are read."""
        unreadable = maven_central_dated_row("33.7.2-jre/", "2026-13-45 99:99", "-")
        fresh = maven_central_version_row("33.7.1-jre", days_ago(1))
        with patch_get(text=maven_central_listing(unreadable, fresh)):
            self.assertEqual(versions_held_back(GUAVA, COOLDOWN.default), ("33.7.2-jre", "33.7.1-jre"))
        self.assert_logged(
            message_of(Logger.unreadable_publication_date),
            dependency=GUAVA,
            version="33.7.2-jre",
            date="2026-13-45 99:99",
        )

    @kills(
        Mutation(
            maven_central,
            '/"[^>]*>',
            '/?"[^>]*>',
            "a file beside the versions reads as a version, so the metadata files are held back as releases",
        )
    )
    def test_a_metadata_file_is_not_held_back(self):
        """Test that a metadata file the listing dates inside the cooldown is not among the versions held back."""
        published = days_ago(1)
        listing = maven_central_listing(maven_central_version_row("33.7.1-jre", published), *_metadata_rows(published))
        with patch_get(text=listing):
            held_back = versions_held_back(GUAVA, COOLDOWN.default)
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
            return_value=mock_response(text=maven_central_listing(maven_central_version_row("33.7.1-jre", days_ago(1))))
        )
        with patch("requests.Session.get", mock_get):
            first = versions_held_back(GUAVA, COOLDOWN.default)
            second = versions_held_back(GUAVA, COOLDOWN.default)
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING])
        self.assertEqual(first, ("33.7.1-jre",))
        self.assertEqual(second, ("33.7.1-jre",))


class ServesTest(LoggingTestCase):
    """Unit tests for telling whether the repository serves an artefact."""

    @kills(
        Mutation(
            maven_central._listing,
            "_LOG.response(response)\n        return LookedUp(None)",
            "_LOG.response(response)\n        return LookedUp(None, absent=True)",
            "a server error reads as an artefact the repository lacks, so its scopes are reported as redundant",
        ),
        Mutation(
            maven_central._listing,
            "if response is None:\n        return LookedUp(None)",
            "if response is None:\n        return LookedUp(None, absent=True)",
            "a request that fails reads as an artefact the repository lacks, so its scopes are reported as redundant",
        ),
    )
    def test_an_artefact_is_unserved_only_where_the_repository_lacks_its_listing(self):
        """Test that a missing listing reads as an unserved artefact, and a failed request as a served one."""
        missing = mock_response(ok=False, status_code=404, reason="Not Found", url=_GUAVA_LISTING, text="")
        failed = mock_response(ok=False, status_code=503, reason="Service Unavailable", url=_GUAVA_LISTING, text="")
        cases = {
            "a missing listing": (Mock(return_value=missing), False),
            "a server error": (Mock(return_value=failed), True),
            "a timeout": (Mock(side_effect=requests.exceptions.Timeout), True),
        }
        for case, (mock_get, served) in cases.items():
            with self.subTest(case=case), patch("requests.Session.get", mock_get):
                self.assertEqual(maven_central.serves(GUAVA), served)
                self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING])


class PublishedTest(unittest.TestCase):
    """Unit tests for reading the date the listing holds for a version."""

    @kills(
        Mutation(
            maven_central._published,
            "return datetime.strptime(published, _PUBLISHED_FORMAT).replace(tzinfo=UTC)\n",
            "return datetime.strptime(published, _PUBLISHED_FORMAT).astimezone()\n",
            "a date is read in the machine's own zone, so a version falls on the wrong side of the cooldown",
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
            self.assertEqual(_newest_release(GUAVA), Release("33.7.0-jre", newest))

    @kills(
        Mutation(
            maven_central._rows,
            "_listing(artefact)",
            "_listing.__wrapped__(artefact)",
            "each read fetches a listing of its own, so an artefact the cooldown read costs a second request",
        )
    )
    def test_the_cooldown_has_already_paid_for_the_listing(self):
        """Test that the newest release is free once the cooldown has read the same artefact's listing."""
        # The listing dates a row to the minute, so the expected release is dated to the minute too.
        dated = days_ago(1).replace(second=0, microsecond=0)
        mock_get = Mock(
            return_value=mock_response(text=maven_central_listing(maven_central_version_row("33.7.1-jre", dated)))
        )
        with patch("requests.Session.get", mock_get):
            held_back = versions_held_back(GUAVA, COOLDOWN.default)
            newest = _newest_release(GUAVA)
        self.assertEqual(requested_urls(mock_get), [_GUAVA_LISTING])
        # Asserting what each read, so the single request says the listing was shared rather than never read.
        self.assertEqual(held_back, ("33.7.1-jre",))
        self.assertEqual(newest, Release("33.7.1-jre", dated))
