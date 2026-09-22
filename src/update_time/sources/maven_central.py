"""Maven Central, the repository Update-time reads the publication date of a Maven artefact's versions from.

The repository lists an artefact's directory with every version it holds and the date it published each of them,
so one request fetches them all. Maven's own `maven-metadata.xml` lists versions without publication dates.
"""

import re
from datetime import UTC, datetime
from functools import cache
from http import HTTPStatus
from typing import TYPE_CHECKING

from update_time.domain.cooldown import within_cooldown
from update_time.domain.dependency import Release
from update_time.io.fetch import fetch
from update_time.io.log import get_logger

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyName

_LOG = get_logger("maven central")

_MAVEN_CENTRAL = "https://repo1.maven.org/maven2"

# A row of the listing: a version's directory and its date, in GMT. Matching the date by its shape ensures undated
# rows, such as the link to the parent directory, are skipped.
_VERSION_ROW = re.compile(
    r'<a href="(?P<version>[\w.+-]+)/"[^>]*>[^<]*</a>\s+(?P<published>\d{4}-\d{2}-\d{2} \d{2}:\d{2})'
)
_PUBLISHED_FORMAT = "%Y-%m-%d %H:%M"


def newest_release(artefact: DependencyName) -> Release | None:
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


@cache
def _listing(artefact: DependencyName) -> str:
    """Return the directory listing the repository serves for the artefact, or nothing where it serves none.

    Maven names an artefact `groupId:artifactId`, and serves it under the group's dots spelled as directories. A
    project can depend on an artefact the repository does not serve, so a missing listing is reported at debug. Any
    other error warns, since the cooldown then held nothing back for an artefact the repository does have.
    """
    group_id, _, artifact_id = artefact.partition(":")
    response = fetch(f"{_MAVEN_CENTRAL}/{group_id.replace('.', '/')}/{artifact_id}/", _LOG, require_ok=False)
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
