"""Python Package Index."""

import re
from datetime import datetime
from functools import cache
from typing import TypedDict

import requests

from update_time.domain.changelog import get_version_changes_from_changelog
from update_time.io.log import get_logger
from update_time.sources.github import changes_from_release, github_owner_and_repository, github_to_raw

LOG = get_logger("pypi")

CHANGELOG_URL_KEYS = {"changes", "changelog", "release notes"}
REPOSITORY_URL_KEYS = {"repository", "source", "homepage"}


class Info(TypedDict):
    """PyPI release info."""

    description: str
    project_urls: dict[str, str]


class Release(TypedDict):
    """PyPI release metadata."""

    info: Info
    urls: list[dict[str, str]]


@cache
def release_metadata(package: str, version: str) -> Release:
    """Get the release metadata from PyPI."""
    response = requests.get(f"https://pypi.org/pypi/{package}/{version}/json", timeout=10)
    response.raise_for_status()
    return response.json()


def get_changes(package: str, version: str) -> str:
    """Return the changelog for the PyPI package and version.

    Since there's no standardized way that PyPI packages refer to a changelog, apply several heuristics to find it:
    - Check for changelog URLs in attributes typically used to refer to the changelog
    - Check for GitHub repository URLs in attributes typically used to refer to the source repository
      and use that to find GitHub releases
    - Check for a changelog in the package description
    - Check for a GitHub URL in the package description and use that to find GitHub releases
    """
    info = release_metadata(package, version)["info"]
    urls = info.get("project_urls", {})
    for url_key, url in urls.items():
        if url_key.lower() in CHANGELOG_URL_KEYS and (changelog := changelog_from_url(url, version)):
            return changelog
    for url_key, url in urls.items():
        if url_key.lower() in REPOSITORY_URL_KEYS and (
            changelog := changelog_from_github_releases(url, package, version)
        ):
            return changelog
    description = info["description"]
    return changelog_from_description(description, version) or changelog_from_github_url_in_description(
        description, package, version
    )


def get_publication_datetime(package: str, version: str) -> datetime:
    """Return the datetime that the version of the package was published."""
    urls = release_metadata(package, version)["urls"]
    upload_time = max(url["upload_time_iso_8601"] for url in urls)
    return datetime.fromisoformat(upload_time)


def changelog_from_url(url: str, version: str) -> str:
    """Get the changelog from the URL."""
    changelog_response = requests.get(github_to_raw(url), timeout=10)
    if not changelog_response.ok:
        LOG.response(changelog_response)
        return ""
    if changelog_response.headers["Content-Type"].startswith("text/html"):
        return ""
    return get_version_changes_from_changelog(changelog_response.text, version)


def changelog_from_description(description: str, version: str) -> str:
    """Get the changelog from the description."""
    return get_version_changes_from_changelog(description, version) if version in description else ""


def changelog_from_github_url_in_description(description: str, package: str, version: str) -> str:
    """Get the changelog from the description posted to PyPI."""
    github_url = r"https://github\.com/([\w.-]+)/([\w.-]+)"
    if not (match := re.search(github_url, description)):
        return ""
    return changelog_from_github_releases(match.group(), package, version)


def changelog_from_github_releases(url: str, package: str, version: str) -> str:
    """Get the changelog from the GitHub releases."""
    owner, repository = github_owner_and_repository(url)
    return changes_from_release(owner, repository, package, version)
