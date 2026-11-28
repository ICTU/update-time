"""What GitHub answers: its releases, tags, commits, comparisons, repositories, and the files they serve.

It also holds what the GitHub source makes of those answers for an updater: the version it resolves, and the project.
"""

import unittest
from typing import TYPE_CHECKING, Final, cast
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

from update_time.domain.dependency import NO_CHANGES, Changes, DependencyVersion, Project, ProjectGetter, every_project

from tests.http_mocks import json_response, mock_response, patching_get, requested_urls
from tests.update_time.fixtures import COMMIT_SHA, COMMIT_SHA2, FRESH_DATE, STALE_DATE

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from update_time.formats.json import JSON
    from update_time.sources.github import GitHubRepository


# The GitHub REST API's per-repository base URL.
_GITHUB_API: Final[str] = "https://api.github.com/repos"
# The host these tests serve a repository's files at, one URL per file name.
_RAW: Final[str] = "https://raw"
# The host these tests serve a repository's trees at, one URL per directory name.
_TREE: Final[str] = "https://tree"
# The host of a file's page in these tests, one page per file name.
_PAGE: Final[str] = "https://page"


def releases_url(repository: str) -> str:
    """Return the URL GitHub serves the repository's releases at."""
    return f"{repository_url(repository)}/releases?per_page=100"


def repository_url(repository: GitHubRepository | str) -> str:
    """Return the URL GitHub serves the repository itself at, such as whether it is archived."""
    return f"{_GITHUB_API}/{repository}"


def contents_url(repository: GitHubRepository | str, directory: str = "") -> str:
    """Return the URL GitHub serves the listing of the repository's directory at, its root by default."""
    return f"{repository_url(repository)}/contents/{directory}"


def tree_url(directory: str) -> str:
    """Return the URL GitHub serves the directory's recursive tree listing at."""
    return f"{git_url(directory)}?recursive=1"


def git_url(name: str) -> str:
    """Return the URL a contents listing gives as the endpoint of an entry: its tree for a directory."""
    return f"{_TREE}/{name}"


def markdown_changelog(version: str) -> str:
    """Return the text of a Markdown changelog file holding a section for the version."""
    return f"Changelog\n## {version}\n- Fixed foo\n"


def markdown_changes(version: str) -> str:
    """Return what the changelog parser extracts from `markdown_changelog` for the version."""
    return f"## {version}\n- Fixed foo"


def contents_json(*names: str) -> list[dict[str, str | None]]:
    """Return a GitHub contents listing holding a file per name, each served at its own URL."""
    return [contents_entry(name) for name in names]


def contents_entry(name: str, *, is_file: bool = True) -> dict[str, str | None]:
    """Return a GitHub contents listing entry for the name, one with no file to fetch when it is a directory."""
    return {
        "name": name,
        "download_url": file_url(name) if is_file else None,
        "html_url": page_url(name),
        "git_url": git_url(name),
    }


def file_url(name: str) -> str:
    """Return the URL a repository serves the file of that name at."""
    return f"{_RAW}/{name}"


def page_url(name: str) -> str:
    """Return the URL of the page a repository shows the file of that name on."""
    return f"{_PAGE}/{name}"


def github_release_json(tag_name: str, published_at: datetime | None = None, **extra: JSON) -> dict[str, JSON]:
    """Return a GitHub release API result for the tag, eligible and with its tag not moved, unless overridden."""
    published = published_at.isoformat() if published_at else None
    return {
        "draft": False,
        "prerelease": False,
        "tag_name": tag_name,
        "body": None,
        "html_url": f"https://github.com/releases/tag/{tag_name}",
        "published_at": published,
        "created_at": (published_at or STALE_DATE).isoformat(),
        **extra,
    }


def github_tag_json(name: str, sha: str = COMMIT_SHA) -> dict[str, JSON]:
    """Return a GitHub tags API result for the tag, carrying the tagged commit's SHA."""
    return {"name": name, "commit": {"sha": sha}}


# The reason Update-time reports for a GitHub request refused because the rate limit is exceeded.
GITHUB_RATE_LIMITED_REASON: Final[str] = "HTTP 403, API rate limit exceeded"


def github_rate_limited() -> Mock:
    """Return the response GitHub refuses a request with once the rate limit is exceeded."""
    return mock_response({"message": "API rate limit exceeded"}, ok=False, status_code=403)


@patching_get
def patch_github(  # noqa: PLR0913 — what each endpoint answers
    releases: list | None = None,
    tags: list | None = None,
    commit: Mapping | Mock | Exception | None = None,
    *,
    listed_commits: list | Mock | None = None,
    comparison_status: str | Mock = "ahead",
    archived: bool = False,
    tag_ref: Mock | Exception | None = None,
) -> Mock:
    """Patch Session.get to serve the GitHub releases, tags, commits, compare, and repository endpoints.

    An endpoint given as None answers non-OK. The commits endpoint serves the same commit for every ref.
    """

    def answer(url: str) -> Mapping | list | Mock | Exception | None:
        """Return what the endpoint the URL names answers: JSON, a whole response, or an exception to raise."""
        if urlparse(url).path.endswith("/commits"):
            listing = listed_commits if listed_commits is not None else [commit] if isinstance(commit, dict) else commit
            return _listing_page(listing, url) if isinstance(listing, list) else listing
        if (endpoint := github_endpoint(url)) == "git":
            return tag_ref if tag_ref is not None else _tag_ref(url, tags or [])
        if endpoint == "tags" and tags is not None:
            return _listing_page(tags, url)
        compared = {"status": comparison_status} if isinstance(comparison_status, str) else comparison_status
        answers = {"commits": commit, "releases": releases, "compare": compared, "": {"archived": archived}}
        return answers.get(endpoint, tags)

    def serve(url: str, **_kwargs: object) -> Mock:
        json = answer(url)
        if isinstance(json, Exception):
            raise json
        if isinstance(json, Mock):
            return json
        return json_response(json, url)

    return Mock(side_effect=serve)


def _listing_page(commits: list, url: str) -> Mock:
    """Return the page of the listing that the URL asks for, linking to the next page while the listing goes on."""
    parsed = urlparse(url)
    query = {name: values[0] for name, values in parse_qs(parsed.query).items()}
    size, page = int(query["per_page"]), int(query.get("page", 1))
    start = (page - 1) * size
    next_url = urlunparse(parsed._replace(query=urlencode({**query, "page": page + 1})))
    links = {"next": {"url": next_url, "rel": "next"}} if start + size < len(commits) else {}
    return mock_response(commits[start : start + size], ok=True, status_code=200, url=url, links=links)


def github_endpoint(url: str) -> str:
    """Return the endpoint the URL names below the repository, empty for the repository's own URL."""
    _owner, _, below_owner = urlparse(url).path.removeprefix("/repos/").partition("/")
    _repository, _, endpoint = below_owner.partition("/")
    return endpoint.partition("/")[0]


def _tag_ref(url: str, tags: list) -> Mapping | Mock:
    """Return the ref of the tag the URL looks up, or the 404 GitHub answers for a name the tags do not carry."""
    name = url.partition("/git/ref/tags/")[2]
    return {"ref": f"refs/tags/{name}"} if any(tag["name"] == name for tag in tags) else github_not_found()


def github_not_found() -> Mock:
    """Return the 404 GitHub answers about a repository that does not exist, and a commit listing of an unknown ref."""
    return mock_response({"message": "Not Found"}, ok=False, status_code=404)


def github_version(version: str, changes: Changes = NO_CHANGES) -> DependencyVersion:
    """Return the version the GitHub source resolves to, pinned to the new commit and tagged with a `v` prefix."""
    return DependencyVersion(version=version, changes=changes, sha=COMMIT_SHA2, tag_name=f"v{version}")


def github_unknown_ref(ref: str) -> Mock:
    """Return the response GitHub's commits endpoint gives for a ref that does not name a commit in the repository."""
    return mock_response({"message": f"No commit found for SHA: {ref}"}, ok=False, status_code=422)


def github_fresh_line(length: int, parent: str) -> list[dict[str, JSON]]:
    """Return a line of fresh commits, newest first, each the first parent of the one listed before it."""
    shas = [f"{index:040x}" for index in range(length)]
    return [
        github_commits_json(sha, date=FRESH_DATE, parents=[first_parent])
        for sha, first_parent in zip(shas, [*shas[1:], parent], strict=True)
    ]


def github_commits_json(
    sha: str = COMMIT_SHA, date: datetime | None = None, parents: Sequence[str] = ()
) -> dict[str, JSON]:
    """Return a GitHub commits API result carrying a commit SHA, its parents, and, when given, its committer date.

    GitHub reports the commit and its committer whether or not the committer has a date, so a dateless commit carries
    both, with the date left out.
    """
    committer = {"date": date.isoformat()} if date else {"name": "The committer"}
    return {"sha": sha, "commit": {"committer": committer}, "parents": [{"sha": parent} for parent in parents]}


def full_commit_sha(short_sha: str) -> str:
    """Return a full commit SHA that the short one abbreviates, as GitHub resolves it."""
    return short_sha.ljust(len(COMMIT_SHA), "0")


def github_refs_whose_commits_were_listed() -> list[str]:
    """Return the refs whose commits were listed."""
    return [ref for query in github_commit_listing_queries() for ref in query["sha"]]


def github_commit_listing_queries() -> list[dict[str, list[str]]]:
    """Return the query of each request that listed commits."""
    urls = [urlparse(url) for url in github_requests("commits")]
    return [parse_qs(url.query) for url in urls if url.path.endswith("/commits")]


def github_requests(endpoint: str) -> list[str]:
    """Return the URLs requested from the GitHub API endpoint."""
    urls = requested_urls(cast("Mock", requests.Session.get))
    return [url for url in urls if github_endpoint(url) == endpoint]


class GitHubProjectMixin(unittest.TestCase):
    """Mix in to patch the project getter a GitHub reference resolves through, exposed as `self.get_project`."""

    def setUp(self) -> None:
        """Patch the project getter with one that asks `self.get_project`, for the duration of the test."""
        super().setUp()
        self.get_project = Mock(return_value=Project())
        patcher = patch(
            "update_time.references.github.project", ProjectGetter(self.get_project, declares_archival=every_project)
        )
        self.addCleanup(patcher.stop)
        patcher.start()
