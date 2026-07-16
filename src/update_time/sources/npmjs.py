"""npmjs."""

from datetime import datetime
from functools import cache

from update_time.domain.staleness import newest_datetime
from update_time.domain.version import DependencyVersion
from update_time.io.fetch import fetch
from update_time.io.log import get_logger
from update_time.sources.github import changes_from_release, github_owner_and_repository

LOG = get_logger("npmjs")

# The npm registry API's base URL.
REGISTRY = "https://registry.npmjs.org"

# The npm registry's `time` map carries two bookkeeping entries alongside the per-version publish times.
_TIME_BOOKKEEPING_KEYS = frozenset({"created", "modified"})


def _repository_url(metadata: dict) -> str:
    """Return the repository URL from npm package metadata, tolerating a missing field or the string shorthand.

    npm's `repository` may be an object (`{"url": ...}`), a string shorthand (`"github:org/repo"`, a git URL), or
    absent. Any resulting string is handed to `github_owner_and_repository`, which resolves the ones it recognizes.
    """
    repository = metadata.get("repository", "")
    if isinstance(repository, dict):
        return repository.get("url", "")
    return repository if isinstance(repository, str) else ""


@cache
def get_changes(package: str, version: str) -> str:
    """Return the changelog for the package and version, or empty string if it can't be fetched or found."""
    response = fetch(f"{REGISTRY}/{package}/{version}", LOG)
    if response is None:
        return ""
    owner, repository = github_owner_and_repository(_repository_url(response.json()))
    return changes_from_release(owner, repository, package, version)


@cache
def _package_metadata(package: str) -> dict:
    """Get the npm registry's package document, or an empty dict if it can't be fetched.

    Shared by `get_publication_datetime` and `newest_publication_date` so both read the same `time` map in one
    (cached) request.
    """
    response = fetch(f"{REGISTRY}/{package}", LOG)
    return response.json() if response is not None else {}


@cache
def get_publication_datetime(package: str, version: str) -> datetime | None:
    """Return the datetime that the version of the package was published, or None if it can't be fetched."""
    metadata = _package_metadata(package)
    return datetime.fromisoformat(metadata["time"][version]) if metadata else None


def newest_publication_date(package: str) -> datetime | None:
    """Return the package's most recent publication date across all versions, or None if it can't be fetched.

    Read from the npm registry's `time` map, ignoring its `created`/`modified` bookkeeping entries, so the date
    reflects the latest version actually published.
    """
    times = _package_metadata(package).get("time", {})
    return newest_datetime(time for key, time in times.items() if key not in _TIME_BOOKKEEPING_KEYS)


def newest_release(package: str) -> DependencyVersion | None:
    """Return the package's newest published release (its `latest` dist-tag) with its date, or None if unavailable.

    Feeds the package.json staleness check: unlike PyPI there is no update-target source call to reuse (npm/pnpm do
    the update themselves), so the newest release is read straight from the registry document. Returns None when the
    package has no `latest` dist-tag (e.g. it can't be fetched, or the dependency isn't an npm registry package).
    """
    latest = _package_metadata(package).get("dist-tags", {}).get("latest")
    if latest is None:
        return None
    return DependencyVersion(version=latest, newest_published=newest_publication_date(package))
