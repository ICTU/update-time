"""npmjs."""

import re
from functools import cache
from typing import TYPE_CHECKING

from update_time.domain.dependency import Release, Yank
from update_time.io.fetch import fetch
from update_time.io.log import get_logger
from update_time.primitives.timestamp import parse_timestamp
from update_time.sources.github import changes_from_release, github_owner_and_repository

if TYPE_CHECKING:
    from datetime import datetime

_LOG = get_logger("npmjs")

# The npm registry API's base URL.
_REGISTRY = "https://registry.npmjs.org"

# The npm registry's `time` map carries two bookkeeping entries alongside the per-version publish times.
_TIME_BOOKKEEPING_KEYS = frozenset({"created", "modified"})


_GITHUB_URL = "https://github.com/"
# Matches `github:` in npm's `github:owner/repo` host shorthand.
_HOST_SHORTHAND_RE = re.compile(r"^github:")
# Matches npm's bare `owner/repo` shorthand in full, e.g. `bower/bower`. A git URL and git's scp-like
# `git@github.com:owner/repo` never match, since `@` and `:` are outside the character class.
_BARE_SHORTHAND_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def _expanded_shorthand(url: str) -> str:
    """Return npm's `github:owner/repo` and bare `owner/repo` shorthands as GitHub URLs, leaving others unchanged."""
    if _BARE_SHORTHAND_RE.match(url):
        return f"{_GITHUB_URL}{url}"
    return _HOST_SHORTHAND_RE.sub(_GITHUB_URL, url)


def _repository_url(metadata: dict) -> str:
    """Return the repository URL from npm package metadata, tolerating a missing field or one of npm's shorthands.

    npm's `repository` may be an object (`{"url": ...}`), a string holding a git URL or one of npm's shorthands, or
    absent. A shorthand is expanded to a GitHub URL; a missing or unreadable field yields an empty string.
    """
    repository = metadata.get("repository", "")
    url = repository.get("url", "") if isinstance(repository, dict) else repository
    return _expanded_shorthand(url) if isinstance(url, str) else ""


@cache
def get_changes(package: str, version: str) -> str:
    """Return the changelog for the package and version, or empty string if it can't be fetched or found."""
    response = fetch(f"{_REGISTRY}/{package}/{version}", _LOG)
    if response is None:
        return ""
    owner, repository = github_owner_and_repository(_repository_url(response.json()))
    return changes_from_release(owner, repository, package, version)


@cache
def _package_metadata(package: str) -> dict:
    """Get the npm registry's package document, or an empty dict if it can't be fetched.

    Shared by `get_publication_datetime` and `newest_release` so both read the same `time` map in one
    (cached) request.
    """
    response = fetch(f"{_REGISTRY}/{package}", _LOG)
    return response.json() if response is not None else {}


@cache
def get_publication_datetime(package: str, version: str) -> datetime | None:
    """Return the datetime the version was published, or None when the registry doesn't date it.

    The registry dates the versions it lists, so one it does not list — a version published moments ago, or one it
    no longer serves — has no date to read.
    """
    return parse_timestamp(_package_metadata(package).get("time", {}).get(version))


def deprecation(package: str, version: str) -> Yank:
    """Return the version's deprecation state as a yank (npm's counterpart to a PyPI yank).

    Read from the registry document's per-version metadata, where a deprecated version carries a `deprecated`
    message; an undeprecated version, or one the registry doesn't list, carries none.
    """
    deprecated = _package_metadata(package).get("versions", {}).get(version, {}).get("deprecated")
    return Yank(yanked=bool(deprecated), reason=deprecated if isinstance(deprecated, str) else "")


def newest_release(package: str) -> Release | None:
    """Return the version the package published most recently with its date, or None when the registry dates none.

    The release is read from the registry document's `time` map, which dates every version the package published.
    """
    times = _package_metadata(package).get("time", {})
    return Release.newest(
        Release(version=version, published=published)
        for version, time in times.items()
        if version not in _TIME_BOOKKEEPING_KEYS and (published := parse_timestamp(time)) is not None
    )
