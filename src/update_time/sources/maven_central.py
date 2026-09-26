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
from update_time.domain.dependency import NO_CHANGES, Archival, Changes, Project, Release
from update_time.formats import xml
from update_time.io.fetch import fetch
from update_time.io.log import get_logger
from update_time.manifests import pom_xml as pom_xml_format
from update_time.sources.github import archival as github_archival
from update_time.sources.github import (
    changes_from_changelog_file,
    changes_from_tagged_release,
    github_owner_and_repository,
    release_tags,
)

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.formats.xml import XmlElement

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
    return Project(newest=newest, archival=_archival(artefact) if check_archival else Archival())


def _archival(artefact: DependencyName) -> Archival:
    """Return what GitHub declares about the artefact's repository."""
    owner, repository = _repository(artefact)
    if not repository:
        return Archival()
    return github_archival(owner, repository, check_archival=True)


def get_changes(artefact: DependencyName, version: VersionString) -> Changes:
    """Return the version's changes, from the release notes the artefact's GitHub repository published for it.

    The changes come from the repository's changelog file when it did not publish a release for the version.
    """
    owner, repository = _repository(artefact)
    tags = _release_tags(artefact, repository, version)
    return changes_from_tagged_release(owner, repository, tags) or _changes_from_changelog_file(
        owner, repository, version
    )


# The qualifier Maven appends to a version, such as `-jre` or `.Final`, which a repository may leave out of its tags.
_QUALIFIER = re.compile(r"[.-][A-Za-z].*$")
# A Maven project may prefix a version's tag with one of these instead of a `v`, as JUnit tags `r6.1.3`.
_VERSION_PREFIXES = ("r", "version-", "REL")


def _spellings(version: VersionString) -> tuple[str, str]:
    """Return the spellings a repository may give the version: as Maven spells it, and without its qualifier."""
    return (version, _QUALIFIER.sub("", version))


def _release_tags(artefact: DependencyName, repository: str, version: VersionString) -> list[str]:
    """Return the tags the artefact's repository may release the version under, the version as Maven spells it first.

    A repository may tag a release by the artifact's name, without its group, or by its own name.
    """
    _group_id, artifact_id = pom_xml_format.coordinates(artefact)
    tags = (
        tag
        for spelling in _spellings(version)
        for tag in [
            *release_tags(artifact_id, spelling, repository),
            *(f"{prefix}{spelling}" for prefix in _VERSION_PREFIXES),
        ]
    )
    return list(dict.fromkeys(tags))


def _changes_from_changelog_file(owner: str, repository: str, version: VersionString) -> Changes:
    """Return the version's changes from the repository's changelog file, the version as Maven spells it first."""
    for spelling in _spellings(version):
        if changes := changes_from_changelog_file(owner, repository, spelling):
            return changes
    return NO_CHANGES


# What Update-time reads for an artefact where it does not find a repository on GitHub.
_NO_REPOSITORY = ("", "")


def _repository(artefact: DependencyName) -> tuple[str, str]:
    """Return the owner and repository of the artefact's project, read from the pom beside its newest release.

    Where the project's source lives is a fact about the project rather than about a version. A project that moved to
    GitHub names the repository in its later poms alone. Update-time can read a pom only for an artefact it found a
    dated version of.
    """
    newest = _newest_release(artefact)
    return _NO_REPOSITORY if newest is None else _pom_repository(artefact, newest.version)


def _pom_repository(artefact: DependencyName, version: VersionString) -> tuple[str, str]:
    """Return the owner and repository the version's pom names, or else the one its parent pom names."""
    pom = _pom(artefact, version)
    if pom is None:
        return _NO_REPOSITORY
    named = _named_repository(pom)
    if named != _NO_REPOSITORY:
        return named
    return _named_repository(_parent_pom(pom))


def _parent_pom(pom: XmlElement) -> XmlElement | None:
    """Return the parent pom to read the repository from, or None where the pom's `<scm>` names a URL of its own."""
    if pom_xml_format.scm_urls(pom):
        return None
    parent = pom_xml_format.parent(pom)
    return None if parent is None else _pom(parent.name, parent.version)


def _named_repository(pom: XmlElement | None) -> tuple[str, str]:
    """Return the owner and repository the pom names, empty where it names none on GitHub."""
    if pom is None:
        return _NO_REPOSITORY
    for url in pom_xml_format.source_urls(pom):
        owner, repository = github_owner_and_repository(url)
        if owner and repository:
            return owner, repository
    return _NO_REPOSITORY


def _pom_url(artefact: DependencyName, version: VersionString) -> str:
    """Return the URL of the pom the repository serves beside the artefact's version, named after the artifact."""
    _group_id, artifact_id = pom_xml_format.coordinates(artefact)
    return f"{_artefact_url(artefact)}/{version}/{artifact_id}-{version}.pom"


@cache
def _pom(artefact: DependencyName, version: VersionString) -> XmlElement | None:
    """Return the pom served beside the artefact's version, or None where it is unserved or unparsable."""
    url = _pom_url(artefact, version)
    response = fetch(url, _LOG)
    if response is None:
        return None
    pom = xml.parse(response.content)
    if pom is None:
        _LOG.invalid_pom(url)
    return pom


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
    group_id, artifact_id = pom_xml_format.coordinates(artefact)
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
