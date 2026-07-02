"""npmjs."""

from datetime import datetime
from functools import cache

from update_time.io.fetch import fetch
from update_time.io.log import get_logger
from update_time.sources.github import changes_from_release, github_owner_and_repository

LOG = get_logger("npmjs")


@cache
def get_changes(package: str, version: str) -> str:
    """Return the changelog for the package and version, or empty string if it can't be fetched."""
    response = fetch(f"https://registry.npmjs.org/{package}/{version}", LOG)
    if response is None:
        return ""
    owner, repository = github_owner_and_repository(response.json()["repository"]["url"])
    return changes_from_release(owner, repository, package, version)


@cache
def get_publication_datetime(package: str, version: str) -> datetime | None:
    """Return the datetime that the version of the package was published, or None if it can't be fetched."""
    response = fetch(f"https://registry.npmjs.org/{package}", LOG)
    if response is None:
        return None
    return datetime.fromisoformat(response.json()["time"][version])
