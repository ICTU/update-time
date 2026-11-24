"""What GitHub reports about a repository: versions, the commits tags and branches point at, changes, and archival."""

import os
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cache, cached_property, partial, total_ordering
from http import HTTPStatus
from typing import TYPE_CHECKING, Final, NotRequired, TypedDict
from urllib.parse import urlencode, urlparse

from packaging.version import Version

from update_time.domain.changelog import get_version_changes_from_changelog
from update_time.domain.cooldown import CooldownWalk, first_eligible, within_cooldown
from update_time.domain.dependency import (
    NO_CHANGES,
    Archival,
    ArchivedSubject,
    Changes,
    DependencyName,
    DependencyVersion,
    PinnedDependency,
    Project,
    ProjectGetter,
    Release,
    Unserved,
    VersionString,
    every_project,
    is_valid,
)
from update_time.formats import markdown
from update_time.formats.markdown import EXTENSION as MARKDOWN_EXTENSION
from update_time.formats.markdown import is_file
from update_time.formats.restructured_text import EXTENSION as RST_EXTENSION
from update_time.io.fetch import Response, fetch, json_body, look_up, next_page_url
from update_time.io.log import get_logger
from update_time.primitives.lookup import DeferredLookup, LookedUp
from update_time.primitives.timestamp import parse_timestamp

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from update_time.domain.bound import VersionBound
    from update_time.io.log import Logger
    from update_time.primitives.lookup import Lookup

_LOG: Final[Logger] = get_logger("github")

# The environment variable holding the token that authenticates the requests, which raises the rate limit.
TOKEN_VARIABLE: Final[str] = "GITHUB_TOKEN"  # noqa: S105 # nosec: B105

# The GitHub commits endpoint fetches a commit by a tag, a branch, or a commit SHA, which this type names.
type _GitRef = str

# The GitHub REST API's per-repository base URL: the repository's own endpoint, which the listings hang below.
_GITHUB_API: Final[str] = "https://api.github.com/repos"
# GitHub's maximum page size, for the releases and tags endpoints. Only the first page of each is fetched, so at
# most this many of the most recent releases and tags are considered.
_PER_PAGE: Final[int] = 100
# How many pages of a branch's commits are listed at most, and why the branch is left as it is when its newest commit
# older than the cooldown lies deeper.
_MAX_COMMIT_LISTING_PAGES: Final[int] = 5
_BEYOND_THE_COMMITS_EXAMINED: Final[str] = (
    "the branch's newest commit older than the cooldown is not among the "
    f"{_MAX_COMMIT_LISTING_PAGES * _PER_PAGE} newest commits examined"
)
# GitHub's commits endpoint answers with this status when a ref does not name a commit in the repository.
_NO_COMMIT_BY_THAT_NAME: Final[HTTPStatus] = HTTPStatus.UNPROCESSABLE_ENTITY
# The statuses GitHub's compare endpoint gives a commit that descends from the base, or that diverged from it.
_MOVED_ON: Final[frozenset[str]] = frozenset({"ahead", "diverged"})
# The host serving a repository's files as raw content.
_RAW_GITHUB: Final[str] = "https://raw.githubusercontent.com"
# The host showing a repository's pages to a person.
GITHUB: Final[str] = "https://github.com"
# The names a repository gives the changelog file, and the extensions it carries, compared in lower case.
_CHANGELOG_FILE_NAMES: Final[frozenset[str]] = frozenset({"changes", "changelog", "history", "news", "releases"})
_CHANGELOG_FILE_EXTENSIONS: Final[frozenset[str]] = frozenset({"", MARKDOWN_EXTENSION, RST_EXTENSION, ".txt"})
# The names a repository gives the directory it keeps its documentation in, compared in lower case.
_DOCUMENTATION_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset({"doc", "docs"})


class _TaggedCommit(TypedDict):
    """The commit a tag points to, in a GitHub tags endpoint result."""

    sha: str


class _TreeEntryJSON(TypedDict):
    """An entry in a GitHub repository's tree listing."""

    # Relative to the tree that was listed, so a path below `doc` reads `source/changes.rst`.
    path: str
    type: str  # `blob` for a file, `tree` for a directory


class _Committer(TypedDict):
    """The committer of the git commit in a GitHub commits endpoint result."""

    date: NotRequired[str]


class _GitCommit(TypedDict):
    """The git commit inside a GitHub commits endpoint result."""

    committer: _Committer | None


class _ParentJSON(TypedDict):
    """A parent of a commit, in a GitHub commits endpoint result."""

    sha: str


def github_to_raw(url: str) -> str:
    """Convert GitHub URLs to URLs that return raw content."""
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.netloc == "github.com":
        raw_path = parsed.path.replace("/blob/", "/")
        return f"{_RAW_GITHUB}{raw_path}"
    return url


# Matches `git@github.com:` in `git@github.com:owner/repo.git`, capturing the user and host.
_SCP_LIKE_RE: Final[re.Pattern[str]] = re.compile(r"^([^/@]+@[^/:]+):")
# GitHub serves its sponsorship pages under this path, which it reserves, so no owner can go by this name.
_GITHUB_SPONSORS_PATH: Final[str] = "sponsors"


def github_repository(url: str) -> GitHubRepository | None:
    """Return the GitHub repository a URL names, or None where it names none.

    Accepts `git+https`, `git+ssh`, and `.git` URLs, and git's scp-like `git@github.com:owner/repo` form. A
    `github.com/sponsors/…` URL names a sponsorship page rather than a repository, so it parses as none.
    """
    parsed = urlparse(_SCP_LIKE_RE.sub(r"ssh://\1/", url.removeprefix("git+")))
    if parsed.hostname == "github.com":
        path_parts = parsed.path.lstrip("/").split("/")
        if len(path_parts) > 1 and path_parts[0] != _GITHUB_SPONSORS_PATH:
            owner, name = path_parts[0], path_parts[1].removesuffix(".git")
            if owner and name:
                return GitHubRepository(owner, name)
    return None


@dataclass(frozen=True)
class GitHubRepository:
    """A repository on GitHub, named by its owner and its name."""

    owner: str
    name: str

    def __str__(self) -> str:
        """Return the repository as GitHub spells it: `owner/name`."""
        return f"{self.owner}/{self.name}"


def pinned_ref(dependency: DependencyName, ref: _GitRef) -> LookedUp[DependencyVersion]:
    """Return the commit the ref points at, named by the highest version tag at it, or else by the ref."""
    repository = repository_of(dependency)
    found = _get_commit(repository, ref)
    if (commit := found.value) is None:
        return found.failure()
    return LookedUp(_named_commit(repository, ref, commit))


def repository_of(dependency: DependencyName) -> GitHubRepository:
    """Return the repository the dependency names, dropping any path below the repository.

    `actions/checkout/sub-action` names an action in a subdirectory of the `actions/checkout` repository.
    """
    owner, name, *_path = dependency.split("/")
    return GitHubRepository(owner, name)


def _get_commit(repository: GitHubRepository, ref: _GitRef) -> LookedUp[_CommitJSON]:
    """Fetch the commit a tag, branch, or commit SHA names."""
    found = _look_up_github(f"{_GITHUB_API}/{repository}/commits/{ref}", (_NO_COMMIT_BY_THAT_NAME,))
    return _github_json(found, body=_CommitJSON)


@cache
def _look_up_github(url: str, statuses_meaning_absent: tuple[int, ...] = ()) -> LookedUp[Response]:
    """Look up a GitHub API URL once per run, authenticated where a token is set.

    Omitting the authorization header or the cache spends rate limit rather than failing, which nothing but an
    exhausted run catches, so every API request goes through here.
    """
    headers = {"Authorization": f"Bearer {github_token}"} if (github_token := os.environ.get(TOKEN_VARIABLE)) else {}
    report = _LOG.github_token_rate_limit_used_up if "Authorization" in headers else _LOG.github_rate_limit_used_up
    on_response = partial(_report_used_up_rate_limit, report=report)
    return look_up(url, _LOG, statuses_meaning_absent=statuses_meaning_absent, on_response=on_response, headers=headers)


def _report_used_up_rate_limit(response: Response, *, report: Callable[[int, datetime], None]) -> None:
    """Report when the response says the rate limit of the GitHub API is used up."""
    if response.headers.get("X-RateLimit-Remaining") != "0":
        return
    try:
        limit = int(response.headers["X-RateLimit-Limit"])
        reset = datetime.fromtimestamp(int(response.headers["X-RateLimit-Reset"]), UTC)
    except (KeyError, ValueError) as error:
        _LOG.unreadable_github_rate_limit(error)
        return
    _log_used_up_rate_limit(report, limit, reset)


@cache
def _log_used_up_rate_limit(report: Callable[[int, datetime], None], limit: int, reset: datetime) -> None:
    """Report that GitHub's API rate limit is used up until the reset, once per rate limit window in each process."""
    report(limit, reset)


@cache
def _github_json[Body](found: LookedUp[Response], *, body: type[Body]) -> LookedUp[Body]:
    """Return the JSON body of the response a GitHub lookup found, read once per lookup, so a bad body is logged once.

    `_look_up_github` caches each lookup, so the callers of one URL pass the same lookup.
    """
    return json_body(found, _LOG, body=body)


class _CommitJSON(TypedDict):
    """A commit from the GitHub commits endpoint."""

    sha: str
    commit: _GitCommit
    parents: list[_ParentJSON]  # The first is the commit the branch was on when the commit was made or merged


def _named_commit(repository: GitHubRepository, ref: _GitRef, commit: _CommitJSON) -> DependencyVersion:
    """Return the commit with its committer date, named by the highest version tag at it, or else by the ref."""
    sha, committed = commit["sha"], _committer_date(commit)
    if (tagged := _highest_version_at(repository, sha)) is None:
        return DependencyVersion(ref, sha=sha, publication=committed)
    return DependencyVersion(tagged.version_string, sha=sha, publication=committed, tag_name=tagged.tag_name)


def _committer_date(commit: _CommitJSON) -> LookedUp[datetime]:
    """Return the commit's committer date."""
    committer = commit["commit"]["committer"]
    if not committer or (committed := parse_timestamp(committer.get("date"))) is None:
        return LookedUp(None, "the commit has no committer date")
    return LookedUp(committed)


def _highest_version_at(repository: GitHubRepository, sha: str) -> _TaggedVersion | None:
    """Return the highest of the commit's version tags that could be an update, or None."""
    tagged = _tagged_versions(repository) or ()
    return max([version for version in tagged if version.sha == sha and version.is_candidate], default=None)


def _tagged_versions(repository: GitHubRepository) -> list[_TaggedVersion] | None:
    """Return the repository's versions: its tags with their releases, plus the releases whose tag wasn't listed.

    A tag without a release takes the publication date of the first release of the same commit. Both endpoints return
    their first page only, so a release can fall outside the tags listed. None means neither endpoint answered.
    """
    releases = _list_releases(repository)
    tags = _list_tags(repository)
    if releases is None and tags is None:
        return None
    releases_by_tag = {release["tag_name"]: release for release in releases or ()}
    listed_tags = {tag["name"] for tag in tags or ()}
    tagged_versions = [_TaggedVersion.from_tag(repository, tag, releases_by_tag.get(tag["name"])) for tag in tags or ()]
    release_dates_by_sha: dict[str, datetime] = {}
    for published_at, sha in sorted(
        (version.published_at, version.sha) for version in tagged_versions if version.published_at
    ):
        release_dates_by_sha.setdefault(sha, published_at)
    tagged_versions = [
        version if version.has_release else replace(version, published_at=release_dates_by_sha.get(version.sha))
        for version in tagged_versions
    ]
    tagged_versions.extend(
        _TaggedVersion.from_release(repository, release)
        for tag_name, release in releases_by_tag.items()
        if tag_name not in listed_tags
    )
    return tagged_versions


def _list_releases(repository: GitHubRepository) -> tuple[_ReleaseJSON, ...] | None:
    """Fetch the GitHub releases for a repository, or None when they couldn't be fetched."""
    return _list(repository, f"releases?per_page={_PER_PAGE}", item=_ReleaseJSON)


def _list[Item](
    repository: GitHubRepository, path: str, statuses_meaning_absent: tuple[int, ...] = (), *, item: type[Item]
) -> tuple[Item, ...] | None:
    """Fetch a listing of items under the repository's API path: empty when it lists nothing, None when it failed.

    The contents endpoint answers a path naming a file with that file's object, which lists nothing. A type checker
    does not take a union as a body type, so the body type leaves that object out.
    """
    del item
    found = _look_up_github(f"{_GITHUB_API}/{repository}/{path}", statuses_meaning_absent)
    if (listing := _github_json(found, body=list[Item]).value) is None:
        return None
    return tuple(listing) if isinstance(listing, list) else ()


class _ReleaseJSON(TypedDict):
    """A release from the GitHub releases endpoint."""

    tag_name: str
    body: NotRequired[str | None]  # Optional in GitHub's schema, and null for a release published without notes
    html_url: str  # The release's page on GitHub
    draft: bool
    prerelease: bool
    published_at: str | None  # None for a draft, which hasn't been published
    created_at: str  # The date of the commit the release's tag points at


def _list_tags(repository: GitHubRepository) -> tuple[_TagJSON, ...] | None:
    """Fetch the GitHub tags for a repository, or None when they couldn't be fetched."""
    return _list(repository, f"tags?per_page={_PER_PAGE}", item=_TagJSON)


class _TagJSON(TypedDict):
    """A tag from the GitHub tags endpoint."""

    name: str
    commit: _TaggedCommit


@total_ordering
@dataclass(  # simplint: ignore[dataclass-slots] # its cached properties need the instance's __dict__
    frozen=True, kw_only=True
)
class _TaggedVersion:
    """A version of a GitHub repository, known from its tag, its release, or both."""

    repository: GitHubRepository
    tag_name: str
    body: Changes = NO_CHANGES
    draft: bool = False
    prerelease: bool = False
    published_at: datetime | None = None
    sha: str = ""  # The tagged commit's SHA, when the version came from the tags endpoint (which lists it)
    has_release: bool = True  # False for a version that was tagged but not published as a GitHub release

    @classmethod
    def from_tag(cls, repository: GitHubRepository, tag: _TagJSON, release: _ReleaseJSON | None) -> _TaggedVersion:
        """Create a TaggedVersion from a GitHub tags endpoint result, enriched with the tag's release when there is one.

        A tag without a release takes its pre-release flag from its version, such as `v4.0.0-alpha.8`.
        """
        sha = tag["commit"]["sha"]
        if release is not None:
            return replace(cls.from_release(repository, release), sha=sha)
        tag_name = tag["name"]
        prerelease = is_valid(tag_name) and Version(tag_name).is_prerelease
        return cls(repository=repository, tag_name=tag_name, prerelease=prerelease, sha=sha, has_release=False)

    @classmethod
    def from_release(cls, repository: GitHubRepository, release: _ReleaseJSON) -> _TaggedVersion:
        """Create a TaggedVersion from a GitHub releases endpoint result.

        GitHub renders a release body as Markdown, whatever the repository's own changelog file is written in. A release
        whose tag was moved onto a commit made after its publication takes the commit's date.
        """
        published_at = parse_timestamp(release["published_at"])
        if published_at is not None:
            published_at = max(published_at, datetime.fromisoformat(release["created_at"]))
        return cls(
            repository=repository,
            tag_name=release["tag_name"],
            body=Changes(release.get("body") or "", markdown=True, url=release["html_url"]),
            draft=release["draft"],
            prerelease=release["prerelease"],
            published_at=published_at,
        )

    def __lt__(self, other: _TaggedVersion) -> bool:
        """Order by version, and equal versions by how precisely they are spelled."""
        return (self.version, len(self.version.release)) < (other.version, len(other.version.release))

    @property
    def version(self) -> Version:
        """Return the tag's version. Version accepts (and normalizes away) the common `v` prefix."""
        return Version(self.tag_name)

    @property
    def version_string(self) -> VersionString:
        """Return the tag's version without its `v` prefix, leaving a tag that names no version as it is."""
        return str(self.version) if self.has_valid_version else self.tag_name

    @property
    def has_valid_version(self) -> bool:
        """Return whether the tag is a valid version."""
        return is_valid(self.tag_name)

    @property
    def is_candidate(self) -> bool:
        """Return whether this version could be an update: a valid, non-draft, non-prerelease version."""
        return not self.draft and not self.prerelease and self.has_valid_version

    @cached_property
    def commit_sha(self) -> str | None:
        """Return the commit SHA for this version's tag — as listed by the tags endpoint, or fetched — or None."""
        if self.sha:
            return self.sha
        dependency = self.dependency
        found = _get_commit(self.repository, self.tag_name)
        if (commit := found.value) is None:
            _LOG.no_commit_sha(
                dependency, self.tag_name, found.reason, f"{GITHUB}/{dependency}/releases/tag/{self.tag_name}"
            )
            return None
        return commit["sha"]

    @property
    def dependency(self) -> str:
        """Return the dependency the version belongs to, named after its repository."""
        return str(self.repository)

    @cached_property
    def publication(self) -> Lookup[datetime]:
        """Look up the publication date, or else, for a tag without a release, the tagged commit's committer date.

        A tag can be created long after its commit, so the committer date can overstate the version's age.
        """
        if self.has_release or self.published_at is not None:
            return LookedUp(self.published_at)
        return DeferredLookup(partial(_commit_date, self.repository, self.commit_ref))

    @property
    def commit_ref(self) -> _GitRef:
        """Return the ref the commits endpoint knows the tagged commit by: its listed SHA, or else the tag name."""
        return self.sha or self.tag_name


def _commit_date(repository: GitHubRepository, ref: _GitRef) -> LookedUp[datetime]:
    """Return the committer date of the commit the ref points to."""
    found = _get_commit(repository, ref)
    if (commit := found.value) is None:
        return LookedUp(None, found.reason)
    return _committer_date(commit)


def newest_commit_past_cooldown(
    dependency: DependencyName, ref: _GitRef, cooldown_days: int
) -> LookedUp[DependencyVersion]:
    """Return the newest commit on the ref's own line older than the cooldown, named by its version tag or by the ref.

    The ref's own line runs from its head through the first parent of each commit. So it leaves out the commits of a
    merged branch, however their committers dated them.
    """
    repository = repository_of(dependency)
    # Without a cooldown the head is the newest commit, so the listing needs to hold the head alone.
    query = urlencode({"sha": ref, "per_page": _PER_PAGE if cooldown_days > 0 else 1})
    url = f"{_GITHUB_API}/{repository}/commits?{query}"
    commits: list[_CommitJSON] = []
    for _page in range(_MAX_COMMIT_LISTING_PAGES):
        # GitHub answers 404 for a ref, or a repository, that does not exist.
        found = _look_up_github(url, (HTTPStatus.NOT_FOUND,))
        if (response := found.value) is None:
            return found.failure()
        if (page := _github_json(found, body=list[_CommitJSON])).value is None:
            return LookedUp(None, page.reason)
        commits.extend(page.value)
        if (newest := _newest_on_the_line(commits, cooldown_days)) is not None:
            return LookedUp(_named_commit(repository, ref, newest))
        if (next_url := next_page_url(response)) is None:
            return LookedUp(None)
        url = next_url
    return LookedUp(None, _BEYOND_THE_COMMITS_EXAMINED)


def _newest_on_the_line(commits: list[_CommitJSON], cooldown_days: int) -> _CommitJSON | None:
    """Return the newest commit older than the cooldown on the line from the listing's head, or None if none is listed.

    Without a cooldown the head is the newest commit, whatever date its committer gave it.
    """
    listed = {commit["sha"]: commit for commit in commits}
    commit = commits[0] if commits else None
    while commit is not None and within_cooldown(_committer_date(commit).value, cooldown_days):
        parents = commit["parents"]
        commit = listed.get(parents[0]["sha"]) if parents else None
    return commit


def moved_on(dependency: DependencyName, pinned: str, commit: str) -> LookedUp[bool]:
    """Return whether a branch moved on from the pinned commit to the commit, or why GitHub can't compare them.

    A branch moves on by adding commits to its history, or by a force-push that rewrites it.
    """
    repository = repository_of(dependency)
    # A comparison lists the commits between the two, which the answer needs none of.
    found = _github_json(
        _look_up_github(f"{_GITHUB_API}/{repository}/compare/{pinned}...{commit}?per_page=1"), body=_ComparisonJSON
    )
    if (comparison := found.value) is None:
        return LookedUp(None, found.reason)
    return LookedUp(comparison["status"] in _MOVED_ON)


class _ComparisonJSON(TypedDict):
    """A comparison of two commits from the GitHub compare endpoint."""

    status: str  # How the head relates to the base: `ahead`, `behind`, `diverged`, or `identical`


def full_sha(dependency: DependencyName, ref: _GitRef) -> str | None:
    """Return the full SHA of the commit the ref names, or None when the commit can't be fetched."""
    commit = _get_commit(repository_of(dependency), ref).value
    return None if commit is None else commit["sha"]


def is_tag(dependency: DependencyName, ref: _GitRef) -> bool | None:
    """Return whether the repository has a tag of the ref's name, or None when GitHub refuses to say.

    The listing holds the first page of tags alone, so a ref a full page leaves out is looked up by name.
    """
    repository = repository_of(dependency)
    if (tags := _list_tags(repository)) is None:
        return None
    if any(tag["name"] == ref for tag in tags):
        return True
    if len(tags) < _PER_PAGE:  # A page with room to spare lists every tag
        return False
    return _looked_up_tag(repository, ref)


def _looked_up_tag(repository: GitHubRepository, ref: _GitRef) -> bool | None:
    """Return whether GitHub finds a tag of the ref's name once per run, or None when it refuses to say."""
    found = _look_up_github(f"{_GITHUB_API}/{repository}/git/ref/tags/{ref}", (HTTPStatus.NOT_FOUND,))
    if found.absent:
        return False
    return None if found.value is None else True


def version_at_tag(dependency: DependencyName, version: VersionString) -> VersionString:
    """Return the highest version tagging the commit the version's own tag points at, or the version itself."""
    repository = repository_of(dependency)
    own_tag = _own_tag(_tagged_versions(repository) or (), Version(version))
    if own_tag is None or (highest := _highest_version_at(repository, own_tag.sha)) is None:
        return version
    return highest.version_string


def _own_tag(tagged_versions: Iterable[_TaggedVersion], version: Version) -> _TaggedVersion | None:
    """Return the listed tag spelled as precisely as the version: `v4` for version `4`, whatever `v4.0.0` tags."""
    return next(
        (
            tagged
            for tagged in tagged_versions
            if tagged.sha
            and tagged.has_valid_version
            and tagged.version == version
            and len(tagged.version.release) == len(version.release)
        ),
        None,
    )


def commit_date(dependency: DependencyName, ref: _GitRef) -> LookedUp[datetime]:
    """Return the committer date of the commit the ref points to, in the repository the dependency names."""
    return _commit_date(repository_of(dependency), ref)


@cache
def get_latest_version(pinned: PinnedDependency, version_bound: VersionBound, cooldown_days: int) -> DependencyVersion:
    """Return the latest eligible version of the GitHub repository, or the current version unchanged.

    Where the repository does not tag a release to update to, the current version comes back marked as unserved.

    The current version is a candidate too, so a reference to a version tag is pinned to its commit without an update.
    Neither the cooldown nor a bound holds the current version back, since the reference uses it already.
    """
    action, current_version = pinned.name, pinned.version
    tagged_versions = _tagged_versions(repository_of(action))
    if tagged_versions is None:  # Couldn't reach GitHub; the fetches already logged a warning.
        return DependencyVersion(current_version)
    valid_versions = [version for version in tagged_versions if version.is_candidate]
    if not valid_versions:
        return DependencyVersion(current_version, unserved=Unserved.RELEASES)
    current = Version(current_version)
    own_tag = _own_tag(tagged_versions, current)
    candidates = [
        version
        for version in valid_versions
        if version.version >= current
        and (version.version == current or version_bound.keeps(version.version, current_version))
        and not _tags_another_commit(version, own_tag)
    ]
    changes_by_version = {version.version: version.body for version in valid_versions if version.body}

    return first_eligible(
        candidates,
        lambda version, walk: _eligible_version(version, walk, changes_by_version, current_version),
        lambda version: version.version == current,
        current_version,
        cooldown_days,
    )


def _tags_another_commit(tagged_version: _TaggedVersion, own_tag: _TaggedVersion | None) -> bool:
    """Return whether the version equals the own tag's but tags another commit, as `v4.0.0` can beside `v4`."""
    return own_tag is not None and tagged_version.version == own_tag.version and tagged_version.sha != own_tag.sha


def _eligible_version(
    tagged_version: _TaggedVersion,
    walk: CooldownWalk,
    changes_by_version: Mapping[Version, Changes],
    current_version: VersionString,
) -> DependencyVersion | None:
    """Return the candidate with the changes of its version, or None when it is not eligible.

    Eligible means older than the cooldown, which does not hold back the current version, and with a commit SHA to
    pin to. A cooldown that applies holds back a candidate that cannot be dated, and every one between it and the
    current version. A `v4.3.0` tag without a release takes the changes of a `v4.3` release, since the two name an
    equal version.
    """
    is_current = tagged_version.version == Version(current_version)
    dependency, tag_name = tagged_version.dependency, tagged_version.tag_name
    report_undated = partial(_LOG.undated_commit, dependency, tag_name, current_version)
    version = str(tagged_version.version)
    if not is_current and not walk.past(version, tagged_version.publication, report_undated):
        return None
    if (sha := tagged_version.commit_sha) is None:
        return None
    changes = changes_by_version.get(tagged_version.version, NO_CHANGES)
    return DependencyVersion(version, changes, sha, tagged_version.publication, tag_name=tagged_version.tag_name)


@partial(ProjectGetter, declares_archival=every_project)
def project(dependency: DependencyName) -> Project:
    """Return what GitHub reports about the repository the dependency names: its newest release, and its archival."""
    repository = repository_of(dependency)
    return Project(newest=_newest_release(repository), archival=archival(repository))


def _newest_release(repository: GitHubRepository) -> Release | None:
    """Return the repository's most recently published version with its date, or None if it has none or is unknown.

    Every release counts, pre-releases and backports included, and so does a tag that runs ahead of them. Only that tag
    is dated, since dating a tag costs a commits request. Where the releases or the tags can't be fetched, the newest
    release is unknown, since the listing that failed may hold a newer one.
    """
    if (releases := _list_releases(repository)) is None or _list_tags(repository) is None:
        return None
    versions = [_TaggedVersion.from_release(repository, release) for release in releases]
    if (tag := _newest_tag_beyond_releases(repository)) is not None:
        versions.append(_TaggedVersion.from_tag(repository, tag, release=None))
    return Release.newest(
        Release(version=version.version_string, published=published)
        for version in versions
        if (published := version.publication.value) is not None
    )


def _newest_tag_beyond_releases(repository: GitHubRepository) -> _TagJSON | None:
    """Return the highest-versioned tag when it runs ahead of every dated release, or None.

    The highest tag may be a pre-release, and so may the dated releases the tag is measured against.
    """
    versioned_tags = [(Version(tag["name"]), tag) for tag in _list_tags(repository) or () if is_valid(tag["name"])]
    if not versioned_tags:
        return None
    # The key limits the comparison to the version: at equal versions (a moving `v5` tag next to `v5.0.0`) comparing
    # the whole pair would fail on the tag dicts.
    newest_version, newest_tag = max(versioned_tags, key=lambda versioned_tag: versioned_tag[0])
    release_versions = [
        Version(release["tag_name"])
        for release in _list_releases(repository) or ()
        if release["published_at"] and is_valid(release["tag_name"])
    ]
    if release_versions and newest_version <= max(release_versions):
        return None
    return newest_tag


def archival(repository: GitHubRepository) -> Archival | None:
    """Return what GitHub declares about the repository: whether it is archived, or None where it can't be read."""
    found = _look_up_github(f"{_GITHUB_API}/{repository}")
    if (metadata := _github_json(found, body=_GitHubRepositoryJSON).value) is None:
        return None
    if not isinstance(archived := metadata.get("archived"), bool):
        _LOG.unreadable_archival(str(repository))
        return None
    return Archival(archived=archived, subject=ArchivedSubject.REPOSITORY)


class _GitHubRepositoryJSON(TypedDict):
    """What the GitHub repository endpoint declares about a repository."""

    archived: NotRequired[bool]


def changes_from_release(repository: GitHubRepository | None, package: str, version: str) -> Changes:
    """Return the body of the GitHub release matching the package and version."""
    return changes_from_tagged_release(repository, release_tags(package, version))


def changes_from_tagged_release(repository: GitHubRepository | None, tags: list[str]) -> Changes:
    """Return the body of the GitHub release carrying the first of the tags."""
    if repository is None:
        return NO_CHANGES
    release = _get_release(repository, tags)
    return release.body if release else NO_CHANGES


def _get_release(repository: GitHubRepository, tags: list[str]) -> _TaggedVersion | None:
    """Get the release carrying the first of the tags that the repository released under."""
    releases_by_tag = {release["tag_name"]: release for release in (_list_releases(repository) or ())}
    for tag in tags:
        if tag in releases_by_tag:
            return _TaggedVersion.from_release(repository, releases_by_tag[tag])
    return None


def release_tags(package: str, version: str, *aliases: str) -> list[str]:
    """Return the tags a repository may release the package's version under, in order of preference.

    The first four repeat for each name `_package_names` returns, and then for each alias:
    1. `<name>-v<version>` (monorepo, e.g. `puppeteer-core-v25.0.4`).
    2. `<name>-<version>` (monorepo without the `v`, e.g. `selenium-4.47.0`).
    3. `<name>@<version>` (monorepo joining the two with an `@`, e.g. `astro@7.1.4`).
    4. `<name>/<version>` (monorepo joining the two with a slash, e.g. `pyproject-fmt/2.28.2`).
    5. `v<version>` (e.g. `v25.0.4`).
    6. `<version>` (e.g. `25.0.4`).
    """
    names = [*_package_names(package), *aliases]
    return [*[f"{name}{joiner}{version}" for name in names for joiner in ("-v", "-", "@", "/")], f"v{version}", version]


def _package_names(package: str) -> list[str]:
    """Return the names a repository may tag the package's releases under, the package's own spelling first.

    A monorepo tags a scoped npm package's releases by the name without the scope, so `@vitejs/plugin-react` gets both
    `@vitejs/plugin-react` and `plugin-react`.
    """
    unscoped = package.rpartition("/")[2]
    return [package] if unscoped == package else [package, unscoped]


def changes_from_changelog_file(repository: GitHubRepository | None, version: str, directory: str = "") -> Changes:
    """Return the version's changes from a changelog file in the repository, or nothing when there is none.

    A monorepo keeps a package's changelog in the directory it builds that package from, or keeps one changelog for all
    its packages at the root. A monorepo that versions its packages apart may describe another package's version of
    the same number at the root, and those are then the changes returned. Some projects keep the changelog in a
    documentation directory, and leave a root file that only links to it.
    """
    if repository is None:
        return NO_CHANGES
    if directory and (changes := _changes_from_files(_list_contents(repository, directory) or (), version)):
        return changes
    root = _list_contents(repository) or ()
    return _changes_from_files(root, version) or _changes_from_documentation(repository, root, version)


def _changes_from_files(entries: tuple[_ContentJSON, ...], version: str) -> Changes:
    """Return the version's changes from a changelog file among the entries, or nothing when none holds them."""
    for entry in entries:
        url = entry["download_url"]
        if _is_changelog_file(entry["name"]) and url and (changes := _changes_from_changelog_url(url, version)):
            return Changes(changes, markdown=markdown.is_file(entry["name"]), url=entry["html_url"] or "")
    return NO_CHANGES


def _is_changelog_file(name: str) -> bool:
    """Return whether the name is one a repository gives its changelog file."""
    stem, dot, extension = name.lower().partition(".")
    return stem in _CHANGELOG_FILE_NAMES and dot + extension in _CHANGELOG_FILE_EXTENSIONS


def _changes_from_changelog_url(url: str, version: str) -> str:
    """Return the version's changes from the changelog file the URL serves, or nothing when there are none."""
    return get_version_changes_from_changelog(_changelog_file(url), version)


@cache
def _changelog_file(url: str) -> str:
    """Fetch the changelog file the URL serves once per run, or an empty string when it can't be fetched."""
    response = fetch(url, _LOG)
    return response.text if response is not None else ""


def _list_contents(repository: GitHubRepository, directory: str = "") -> tuple[_ContentJSON, ...] | None:
    """Fetch the entries in a directory of a repository, its root by default, or None when they can't be fetched.

    GitHub answers 404 for a directory the repository does not serve. That is unremarkable, so it goes unreported.
    """
    return _list(repository, f"contents/{directory}", (HTTPStatus.NOT_FOUND,) if directory else (), item=_ContentJSON)


class _ContentJSON(TypedDict):
    """An entry in a GitHub repository's contents listing."""

    name: str
    # Null for an entry that is no file to fetch, such as a directory: pip's root keeps its `news` fragments in one.
    download_url: str | None
    html_url: str | None  # The entry's page on GitHub, which GitHub's schema lets be null
    # The endpoint listing the entry: its tree for a directory, its blob for a file.
    git_url: str


def _changes_from_documentation(repository: GitHubRepository, root: tuple[_ContentJSON, ...], version: str) -> Changes:
    """Return the version's changes from a changelog file below a documentation directory the root names."""
    for entry in root:
        if entry["name"].lower() in _DOCUMENTATION_DIRECTORY_NAMES and (
            changes := _changes_from_tree(repository, entry, version)
        ):
            return changes
    return NO_CHANGES


def _changes_from_tree(repository: GitHubRepository, directory: _ContentJSON, version: str) -> Changes:
    """Return the version's changes from a changelog file below the directory, or nothing when none names them."""
    root_url = f"{_RAW_GITHUB}/{repository}/HEAD/{directory['name']}"
    page_root_url = f"{GITHUB}/{repository}/blob/HEAD/{directory['name']}"
    for path in _list_tree(directory["git_url"]):
        name = path.rpartition("/")[2]
        if _is_changelog_file(name) and (changes := _changes_from_changelog_url(f"{root_url}/{path}", version)):
            return Changes(changes, markdown=is_file(name), url=f"{page_root_url}/{path}")
    return NO_CHANGES


def _list_tree(git_url: str) -> tuple[str, ...]:
    """Fetch the paths of the files below the tree the URL names, or an empty tuple when they can't be fetched."""
    if (listing := _github_json(_look_up_github(f"{git_url}?recursive=1"), body=_TreeJSON).value) is None:
        return ()
    tree = listing.get("tree", [])
    return tuple(entry["path"] for entry in tree if entry["type"] == "blob")


class _TreeJSON(TypedDict):
    """A tree from the GitHub trees endpoint, listed recursively."""

    tree: NotRequired[list[_TreeEntryJSON]]
