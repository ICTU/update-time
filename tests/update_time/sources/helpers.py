"""Test helpers shared by the tests of the sources and of the updaters asking them: what each source answers."""

import contextlib
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from packaging.version import Version

from tests.helpers import mock_response, requested_urls
from tests.update_time.fixtures import COMMIT_SHA, STALE_DATE
from tests.update_time.manifests.helpers import pom_parent

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence
    from datetime import datetime

    from update_time.sources.github import GitHubRepository

# The GitHub REST API's per-repository base URL.
_GITHUB_API = "https://api.github.com/repos"
# The host these tests serve a repository's files at, one URL per file name.
_RAW = "https://raw"
# The host these tests serve a repository's trees at, one URL per directory name.
_TREE = "https://tree"
# The host of a file's page in these tests, one page per file name.
_PAGE = "https://page"


def respond_per_url(mock_get: Mock, responses: Mapping[Any, Mock]) -> None:
    """Point the mock Session.get at the response each URL maps to.

    A URL the mapping doesn't cover raises a KeyError, so a request the test did not expect fails loudly rather
    than being answered.
    """
    mock_get.side_effect = lambda url, **_kwargs: responses[url]


def repository_url(repository: GitHubRepository | str) -> str:
    """Return the URL GitHub serves the repository itself at, such as whether it is archived."""
    return f"{_GITHUB_API}/{repository}"


def releases_url(repository: str) -> str:
    """Return the URL GitHub serves the repository's releases at."""
    return f"{repository_url(repository)}/releases?per_page=100"


def contents_url(repository: GitHubRepository | str, directory: str = "") -> str:
    """Return the URL GitHub serves the listing of the repository's directory at, its root by default."""
    return f"{repository_url(repository)}/contents/{directory}"


def file_url(name: str) -> str:
    """Return the URL a repository serves the file of that name at."""
    return f"{_RAW}/{name}"


def page_url(name: str) -> str:
    """Return the URL of the page a repository shows the file of that name on."""
    return f"{_PAGE}/{name}"


def git_url(name: str) -> str:
    """Return the URL a contents listing gives as the endpoint of an entry: its tree for a directory."""
    return f"{_TREE}/{name}"


def tree_url(directory: str) -> str:
    """Return the URL GitHub serves the directory's recursive tree listing at."""
    return f"{git_url(directory)}?recursive=1"


def contents_entry(name: str, *, is_file: bool = True) -> dict[str, str | None]:
    """Return a GitHub contents listing entry for the name, one with no file to fetch when it is a directory."""
    return {
        "name": name,
        "download_url": file_url(name) if is_file else None,
        "html_url": page_url(name),
        "git_url": git_url(name),
    }


def markdown_changelog(version: str) -> str:
    """Return the text of a Markdown changelog file holding a section for the version."""
    return f"Changelog\n## {version}\n- Fixed foo\n"


def markdown_changes(version: str) -> str:
    """Return what the changelog parser extracts from `markdown_changelog` for the version."""
    return f"## {version}\n- Fixed foo"


def contents_json(*names: str) -> list[dict[str, str | None]]:
    """Return a GitHub contents listing holding a file per name, each served at its own URL."""
    return [contents_entry(name) for name in names]


def github_release_json(tag_name: str, published_at: datetime | None = None, **extra: object) -> dict[str, object]:
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


def github_tag_json(name: str, sha: str = COMMIT_SHA) -> dict[str, object]:
    """Return a GitHub tags API result for the tag, carrying the tagged commit's SHA."""
    return {"name": name, "commit": {"sha": sha}}


# The reason Update-time reports for a GitHub request refused because the rate limit is exceeded.
GITHUB_RATE_LIMITED_REASON = "HTTP 403, API rate limit exceeded"


def github_rate_limited() -> Mock:
    """Return the response GitHub refuses a request with once the rate limit is exceeded."""
    return mock_response({"message": "API rate limit exceeded"}, ok=False, status_code=403)


def github_not_found() -> Mock:
    """Return the 404 GitHub answers about a repository that does not exist, and a commit listing of an unknown ref."""
    return mock_response({"message": "Not Found"}, ok=False, status_code=404)


def github_commits_json(
    sha: str = COMMIT_SHA, date: datetime | None = None, parents: Sequence[str] = ()
) -> dict[str, object]:
    """Return a GitHub commits API result carrying a commit SHA, its parents, and, when given, its committer date.

    GitHub reports the commit and its committer whether or not the committer has a date, so a dateless commit carries
    both, with the date left out.
    """
    committer = {"date": date.isoformat()} if date else {"name": "The committer"}
    return {"sha": sha, "commit": {"committer": committer}, "parents": [{"sha": parent} for parent in parents]}


def _listing_page(commits: list, url: str) -> Mock:
    """Return the page of the listing that the URL asks for, linking to the next page while the listing goes on.

    A response among the commits answers for the page holding it, so a page after the first can fail.
    """
    parsed = urlparse(url)
    query = {name: values[0] for name, values in parse_qs(parsed.query).items()}
    size, page = int(query["per_page"]), int(query.get("page", 1))
    start = (page - 1) * size
    if failed := [response for response in commits[start : start + size] if isinstance(response, Mock)]:
        return failed[0]
    next_url = urlunparse(parsed._replace(query=urlencode({**query, "page": page + 1})))
    links = {"next": {"url": next_url, "rel": "next"}} if start + size < len(commits) else {}
    return mock_response(commits[start : start + size], ok=True, status_code=200, url=url, links=links)


def _github_endpoint(url: str) -> str:
    """Return the endpoint the URL names below the repository, empty for the repository's own URL."""
    _owner, _, below_owner = urlparse(url).path.removeprefix("/repos/").partition("/")
    _repository, _, endpoint = below_owner.partition("/")
    return endpoint.partition("/")[0]


def _tag_ref(url: str, tags: list) -> Mapping | Mock:
    """Return the ref of the tag the URL looks up, or the 404 GitHub answers for a name the tags do not carry."""
    name = url.partition("/git/ref/tags/")[2]
    return {"ref": f"refs/tags/{name}"} if any(tag["name"] == name for tag in tags) else github_not_found()


def _github_api(  # noqa: PLR0913 — what each endpoint answers
    releases: list | None = None,
    tags: list | None = None,
    commit: Mapping | Mock | Exception | None = None,
    *,
    listed_commits: list | Mock | None = None,
    comparison_status: str | Mock = "ahead",
    archived: bool = False,
    tag_ref: Mock | Exception | None = None,
) -> Mock:
    """Return a Session.get mock serving the GitHub releases, tags, commits, compare, and repository endpoints.

    Each request is answered by the endpoint it names. An endpoint given as None answers non-OK, so a test can make it
    unreachable. The commits endpoint serves the same commit for every ref. A listing of commits serves the listed
    commits a page at a time, or else that commit alone. The compare endpoint answers every comparison of two commits
    with the comparison status. Pass a Mock as the commit, the listed commits, or the comparison status to serve a
    whole response rather than JSON, such as a non-OK one with an error body, or an exception as the commit to fail the
    request itself. A Mock among the listed commits answers for the page holding it. The tags are listed a page at a
    time too, and the lookup of a single tag finds any of them, unless a Mock or an exception given as the tag ref
    answers it.
    """

    def answer(url: str) -> Mapping | list | Mock | Exception | None:
        """Return what the endpoint the URL names answers: JSON, a whole response, or an exception to raise."""
        if urlparse(url).path.endswith("/commits"):
            listing = listed_commits if listed_commits is not None else [commit] if isinstance(commit, dict) else commit
            return _listing_page(listing, url) if isinstance(listing, list) else listing
        if (endpoint := _github_endpoint(url)) == "git":
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
        return mock_response(json, ok=json is not None, status_code=200 if json is not None else 404, url=url)

    return Mock(side_effect=serve)


def _patching_get[**P](api: Callable[P, Mock]) -> Callable[P, contextlib._GeneratorContextManager[Mock]]:
    """Return a patch of Session.get with the mock the API builds.

    A test decorated with the patch gets a fresh mock on every run, so each run sees only its own requests.
    """

    @contextlib.contextmanager
    def patched(*args: P.args, **kwargs: P.kwargs) -> Generator[Mock]:
        with patch("requests.Session.get", api(*args, **kwargs)) as mock_get:
            yield mock_get

    return patched


# Patch Session.get to serve the GitHub API endpoints from the given values (see `_github_api`).
patch_github = _patching_get(_github_api)


def github_requests(endpoint: str) -> list[str]:
    """Return the URLs requested from the GitHub API endpoint."""
    urls = requested_urls(cast("Mock", requests.Session.get))
    return [url for url in urls if _github_endpoint(url) == endpoint]


def maven_central_dated_row(name: str, published: str, size: str) -> str:
    """Return a row of an artefact's directory listing, dated exactly as given rather than by an instant."""
    link = f'<a href="{name}" title="{name}">{name}</a>'
    padding = " " * max(1, 50 - len(name))
    return f"{link}{padding}{published}   {size}\n"


def maven_central_row(name: str, published: datetime, size: str) -> str:
    """Return a row of the listing, padded out to the date column.

    The repository dates the row in GMT without naming the zone, and closes it with the entry's size.
    """
    return maven_central_dated_row(name, f"{published:%Y-%m-%d %H:%M}", size)


def maven_central_version_row(version: str, published: datetime) -> str:
    """Return the row for a version, which the repository serves as a directory, sized with a dash."""
    return maven_central_row(f"{version}/", published, "-")


# The base URL Maven Central serves its artefacts under.
_MAVEN_CENTRAL = "https://repo1.maven.org/maven2"


def maven_central_listing_url(artefact: str) -> str:
    """Return the URL Maven Central serves the artefact's directory listing at."""
    group_id, artifact_id = artefact.split(":")
    return f"{_MAVEN_CENTRAL}/{group_id.replace('.', '/')}/{artifact_id}/"


def maven_central_pom_url(artefact: str, version: str) -> str:
    """Return the URL Maven Central serves the pom beside the artefact's version at."""
    artifact_id = artefact.split(":")[1]
    return f"{maven_central_listing_url(artefact)}{version}/{artifact_id}-{version}.pom"


def maven_central_listing(*rows: str) -> str:
    """Return the directory listing the repository serves for an artefact, holding the given rows.

    The listing opens with a link to the parent directory.
    """
    return f'<html><body><pre>\n<a href="../">../</a>\n{"".join(rows)}</pre></body></html>'


def maven_central_pom(
    scm: str = "", tag: str = "url", project_url: str = "", parent: str = "", properties: str = ""
) -> str:
    """Return the pom beside a version, naming the source repository in the given child of its `<scm>` element.

    An scm given as nothing is a pom that does not declare an `<scm>` element at all, as guava's own pom does not.
    A project URL is the project's own `<url>` element, where guava's pom names its repository instead. A parent,
    given as `groupId:artifactId:version`, is the pom's `<parent>` element.
    """
    url = f"  <url>{project_url}</url>\n" if project_url else ""
    declared = f"  <scm>\n    <{tag}>{scm}</{tag}>\n  </scm>\n" if scm else ""
    elements = f"{pom_parent(parent)}{properties}{url}{declared}"
    return f'<project xmlns="http://maven.apache.org/POM/4.0.0">\n{elements}</project>\n'


# The source repositories poms name: guava's own on GitHub, and one outside GitHub, where commons-lang3's lives.
GUAVA_SCM = "scm:git:https://github.com/google/guava.git"


GITBOX_SCM = "scm:git:https://gitbox.apache.org/repos/asf/commons-lang.git"


def _maven_central_api(listing: str, pom: str | None, *, archived: bool = False, releases: list | None = None) -> Mock:
    """Return a Session.get mock serving an artefact's listing, a version's pom, and the GitHub repository it names.

    GitHub serves the given releases for every repository, and answers the releases request non-OK where none are given.

    Each request is answered by the URL it names, so a test needs no expectation about the order they are made in.
    A pom given as None is one the repository does not serve.
    """
    github = _github_api(releases, archived=archived)

    def serve(url: str, **kwargs: object) -> Mock:
        if urlparse(url).hostname == "api.github.com":
            return github(url, **kwargs)
        if not url.endswith(".pom"):
            return mock_response(text=listing, url=url)
        if pom is None:
            return mock_response(ok=False, status_code=404, reason="Not Found", url=url, text="")
        return mock_response(text=pom, content=pom.encode(), url=url)

    return Mock(side_effect=serve)


# Patch Session.get to serve a Maven artefact's listing and pom (see `_maven_central_api`).
patch_maven_central = _patching_get(_maven_central_api)


def jsdelivr_versions(*version_strings: str) -> Mock:
    """Return a mock jsDelivr package API response listing the given versions (newest first)."""
    return mock_response({"versions": [{"version": version} for version in version_strings]})


def osv_advisory(
    advisory: str,
    summary: str,
    level: str = "",
    aliases: list[str] | None = None,
    vectors: dict[str, str] | None = None,
) -> dict[str, object]:
    """Return an OSV advisory record, carrying the risk level a GitHub-reviewed advisory reports when given.

    An advisory nobody reviewed for severity carries no `database_specific` section at all, which is the shape OSV
    returns for the PYSEC records among others; it states a CVSS vector at best, which `vectors` gives per CVSS
    version. `aliases` are the identifiers the other databases name the vulnerability by.
    """
    reviewed = {"database_specific": {"severity": level}} if level else {}
    named = {"aliases": aliases} if aliases else {}
    severities = [{"type": version, "score": vector} for version, vector in (vectors or {}).items()]
    scored = {"severity": severities} if severities else {}
    return {"id": advisory, "summary": summary, **reviewed, **named, **scored}


def osv_action_package(action: str) -> dict[str, str]:
    """Return the package OSV files the action's advisories under."""
    return {"name": action, "ecosystem": "GitHub Actions"}


def osv_action_entry(action: str, *events: dict[str, str], **fields: object) -> dict[str, object]:
    """Return an OSV `affected` entry naming the action, stating its affected versions as one range of the events.

    The fields add to the entry, such as the versions it lists, or replace its range with ranges of their own.
    """
    ranges = [{"type": "ECOSYSTEM", "events": list(events)}] if events else []
    return {"package": osv_action_package(action), "ranges": ranges, **fields}


def _osv_response(*advisories: dict[str, object]) -> Mock:
    """Return a mock OSV response listing the advisories that affect a version."""
    return mock_response({"vulns": list(advisories)})


def osv_api(*advisories: dict[str, object]) -> Mock:
    """Return a Session.post mock serving both OSV endpoints, reporting the advisories for every version queried.

    Routing by URL keeps tests independent of how many of each request the source makes: the batch endpoint answers
    which of the queried versions are affected, and the details endpoint answers with the advisories in full.
    """

    def serve(url: str, **kwargs: object) -> Mock:
        if not url.endswith("/querybatch"):
            return _osv_response(*advisories)
        queries = cast("dict[str, list]", kwargs["json"])["queries"]
        affected = {"vulns": [{"id": advisory["id"]} for advisory in advisories]} if advisories else {}
        return mock_response({"results": [affected for _query in queries]})

    return Mock(side_effect=serve)


def npm_registry(published: dict[str, str], deprecated: dict[str, str] | None = None) -> Mock:
    """Return a mock npm registry response mapping versions to publish times and optional deprecation messages."""
    versions = {version: {"deprecated": reason} for version, reason in (deprecated or {}).items()}
    return mock_response({"time": published, "versions": versions})


PYPI_OLD_UPLOAD = "2020-01-01T00:00:00.000000Z"  # A distribution upload time far older than the cooldown.


def pypi_index(
    *versions: str, files: Sequence[Mapping[str, str | bool]] | None = None, archived: bool | str = False
) -> Mock:
    """Return a mock PyPI Index API response listing the versions, distribution files, and project status."""
    body: dict[str, object] = {"versions": list(versions)}
    if files is not None:
        newest = str(max(Version(version) for version in versions))
        body["files"] = [{"filename": f"package-{newest}.tar.gz", **file} for file in files]
    if archived:
        reason = archived if isinstance(archived, str) else None
        body["project-status"] = {"status": "archived", "reason": reason}
    return mock_response(body)


def yanked_file(filename: str, *, reason: str | bool = True) -> dict[str, str | bool]:
    """Return a mock Index API distribution file entry marked as yanked (a reason string, or True for none)."""
    return {"filename": filename, "yanked": reason}


_NO_PROJECT_URLS: dict[str, str] = {}  # The default for a release fixture no test asks project URLs of.


def pypi_release(
    upload_time: str = PYPI_OLD_UPLOAD,
    *,
    yanked: bool = False,
    description: str = "",
    project_urls: dict[str, str] | None = _NO_PROJECT_URLS,
) -> Mock:
    """Return a mock PyPI per-version metadata response with the given upload time, yank state, and description.

    An empty `upload_time` models a release with no distribution files (an empty `urls` list). Passing None for the
    project URLs models the null PyPI answers for a package that declares none.
    """
    urls = [{"upload_time_iso_8601": upload_time}] if upload_time else []
    info = {"description": description, "yanked": yanked, "project_urls": project_urls}
    return mock_response({"info": info, "urls": urls})
