"""Maven Central, the repository Update-time reads a Maven artefact's versions and poms from.

The repository lists an artefact's directory with every version it holds and the date it published each of them,
so one request fetches them all. Maven's own `maven-metadata.xml` lists versions without publication dates. Beside
each version sits the pom it was published with, which may name the repository the project's source lives in.
"""

import re
from datetime import UTC, datetime
from functools import cache
from http import HTTPStatus
from typing import TYPE_CHECKING

from update_time.domain.archival import archival_reporting
from update_time.domain.cooldown import within_cooldown
from update_time.domain.dependency import Archival, Project, Release
from update_time.io.fetch import fetch
from update_time.io.log import get_logger
from update_time.manifests import pom_xml as pom_xml_format
from update_time.sources.github import archival as github_archival
from update_time.sources.github import github_owner_and_repository

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyName, VersionString

_LOG = get_logger("maven central")

_MAVEN_CENTRAL = "https://repo1.maven.org/maven2"

# A row of the listing: a version's directory and its date, in GMT. Matching the date by its shape ensures undated
# rows, such as the link to the parent directory, are skipped.
_VERSION_ROW = re.compile(
    r'<a href="(?P<version>[\w.+-]+)/"[^>]*>[^<]*</a>\s+(?P<published>\d{4}-\d{2}-\d{2} \d{2}:\d{2})'
)
_PUBLISHED_FORMAT = "%Y-%m-%d %H:%M"


@archival_reporting
def project(artefact: DependencyName, *, check_archival: bool) -> Project:
    """Return the artefact's newest release on the repository, with the archival GitHub declares for its source."""
    newest = _newest_release(artefact)
    return Project(newest=newest, archival=_archival(artefact, newest) if check_archival else Archival())


def _archival(artefact: DependencyName, newest: Release | None) -> Archival:
    """Return what GitHub declares about the repository the artefact's pom names.

    The pom read is the one beside the newest release, since archival is a fact about the project. A project that
    moved to GitHub names the repository in its later poms alone. Update-time can read a pom only for an artefact it
    found a dated version of.
    """
    if newest is None:
        return Archival()
    owner, repository = _scm_repository(artefact, newest.version)
    if not repository:
        return Archival()
    return github_archival(owner, repository, check_archival=True)


# What Update-time reads for a pom that does not name a repository on GitHub.
_NO_REPOSITORY = ("", "")


def _scm_repository(artefact: DependencyName, version: VersionString) -> tuple[str, str]:
    """Return the owner and repository the version's pom names in its `<scm>`, empty where it names none on GitHub."""
    document = _pom(artefact, version)
    if document is None:
        return _NO_REPOSITORY
    scm_urls = pom_xml_format.scm_urls(document)
    if scm_urls is None:
        _LOG.invalid_pom(_pom_url(artefact, version))
        return _NO_REPOSITORY
    for scm_url in scm_urls:
        owner, repository = github_owner_and_repository(scm_url)
        if owner and repository:
            return owner, repository
    return _NO_REPOSITORY


def _pom_url(artefact: DependencyName, version: VersionString) -> str:
    """Return the URL of the pom the repository serves beside the artefact's version.

    The repository names a pom after the artefact, which its directory is named after too.
    """
    artefact_url = _artefact_url(artefact)
    artifact_id = artefact_url.rpartition("/")[2]
    return f"{artefact_url}/{version}/{artifact_id}-{version}.pom"


@cache
def _pom(artefact: DependencyName, version: VersionString) -> bytes | None:
    """Return the pom the repository serves beside the artefact's version, or None where fetching it failed."""
    response = fetch(_pom_url(artefact, version), _LOG)
    return None if response is None else response.content


def _newest_release(artefact: DependencyName) -> Release | None:
    """Return the version the repository published most recently with its date, or None where it dates none."""
    return Release.newest(
        Release(match["version"], published)
        for match in _VERSION_ROW.finditer(_listing(artefact))
        if (published := _published(match["published"])) is not None
    )


def versions_within_cooldown(artefact: DependencyName, cooldown_days: int) -> tuple[str, ...]:
    """Return the artefact's versions the repository published inside the cooldown window."""
    return tuple(
        match["version"]
        for match in _VERSION_ROW.finditer(_listing(artefact))
        if within_cooldown(_published(match["published"]), cooldown_days)
    )


def _artefact_url(artefact: DependencyName) -> str:
    """Return the URL of the artefact's own directory in the repository.

    Maven names an artefact `groupId:artifactId`, and serves it under the group's dots spelled as directories.
    """
    group_id, _, artifact_id = artefact.partition(":")
    return f"{_MAVEN_CENTRAL}/{group_id.replace('.', '/')}/{artifact_id}"


@cache
def _listing(artefact: DependencyName) -> str:
    """Return the directory listing the repository serves for the artefact, or nothing where it serves none.

    A project can depend on an artefact the repository does not serve, so a missing listing is reported at debug. Any
    other error warns, since the cooldown then held nothing back for an artefact the repository does have.
    """
    response = fetch(f"{_artefact_url(artefact)}/", _LOG, require_ok=False)
    if response is None:
        return ""
    if response.status_code == HTTPStatus.NOT_FOUND:
        _LOG.unserved_listing(response)
        return ""
    if not response.ok:
        _LOG.response(response)
        return ""
    return response.text


def _published(published: str) -> datetime | None:
    """Return the date the listing holds for a version, or None where that date does not parse.

    The repository dates a row in GMT without naming the zone. The pattern matches the date by its shape, so a
    month or a day outside the calendar reaches this format.
    """
    try:
        return datetime.strptime(published, _PUBLISHED_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
