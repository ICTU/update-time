"""npmjs."""

from datetime import datetime
from functools import cache

import requests

from update_time.sources.github import changes_from_release, github_owner_and_repository


@cache
def get_changes(package: str, version: str) -> str:
    """Return the changelog for the package and version."""
    response = requests.get(f"https://registry.npmjs.org/{package}/{version}", timeout=10)
    response.raise_for_status()
    owner, repository = github_owner_and_repository(response.json()["repository"]["url"])
    return changes_from_release(owner, repository, package, version)


@cache
def get_publication_datetime(package: str, version: str) -> datetime:
    """Return the datetime that the version of the package was published."""
    response = requests.get(f"https://registry.npmjs.org/{package}", timeout=10)
    response.raise_for_status()
    json = response.json()
    return datetime.fromisoformat(json["time"][version])
