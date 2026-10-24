"""Test helpers the updater tests share: the files they update, their sources, and the checks they switch off."""

from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import ANY, Mock, patch
from urllib.parse import parse_qs, urlparse

from update_time.domain.dependency import DependencyVersion, Project
from update_time.domain.vulnerability import NO_RISK_LEVEL, VULNERABILITY_LEVEL

from tests.helpers import mock_response, patch_environ
from tests.update_time.fixtures import COMMIT_SHA, COMMIT_SHA2, FRESH_DATE
from tests.update_time.helpers import vulnerability
from tests.update_time.sources.helpers import (
    PYPI_OLD_UPLOAD,
    github_commits_json,
    github_requests,
    osv_advisory,
    osv_api,
    pypi_index,
)

if TYPE_CHECKING:
    from unittest.mock import _patch

    from update_time.domain.vulnerability import Vulnerability
    from update_time.sources.pypi import ReleaseMetadata

# Reusable class decorator that mocks the Docker Hub auth token request made by sources.docker_hub.api_headers
# when DOCKER_HUB_USERNAME/DOCKER_HUB_TOKEN are set, so the image updater tests never make a real network call.
mock_docker_hub_auth = patch("requests.Session.post", Mock(return_value=mock_response({"access_token": "token"})))  # nosec


# The default project for the versions a test builds without one.
_NO_PROJECT = Project()

# The upload times of the PyPI release metadata the uv updater tests answer with, with a changelog URL and without one.
UPLOAD_TIME_WITH_CHANGELOG = "2026-05-30T12:07:03.123456Z"
UPLOAD_TIME_WITHOUT_CHANGELOG = "2026-05-30T12:08:53.123321Z"


def pypi_metadata_without_changelog() -> ReleaseMetadata:
    """Create PyPI release metadata without project URLs, so the test needs no changelog response mocked."""
    return {
        "info": {"description": "Package", "project_urls": {}},
        "urls": [{"upload_time_iso_8601": UPLOAD_TIME_WITHOUT_CHANGELOG}],
    }


def github_version(version: str, project: Project = _NO_PROJECT) -> DependencyVersion:
    """Return the version the GitHub source resolves to, pinned to the new commit and tagged with a `v` prefix."""
    return DependencyVersion(version=version, sha=COMMIT_SHA2, tag_name=f"v{version}", project=project)


def dated_pypi_index(*versions: str, upload_time: str = PYPI_OLD_UPLOAD, archived: bool | str = False) -> Mock:
    """Return a mock Index API response listing the versions, the newest dated by one distribution file."""
    return pypi_index(*versions, files=[{"upload-time": upload_time}], archived=archived)


def osv_vulnerability(advisory: str, summary: str, level: str) -> tuple[dict[str, object], Vulnerability]:
    """Return an OSV advisory record and the vulnerability Update-time reads it as.

    Returned as a pair because a test that needs one needs the other: the record is what the mocked API serves, and
    the vulnerability is what the warning is asserted to carry. `level` is spelled the lower-case way Update-time
    reads it, since OSV states it upper-case.
    """
    return osv_advisory(advisory, summary, level.upper()), vulnerability(advisory, summary, level)


# The OSV endpoint that answers which of a batch of pins are affected. A file's references are asked there together.
OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"


def osv_queries(*pins: tuple[str, str], ecosystem: str = "PyPI") -> dict[str, object]:
    """Return the payload OSV's batch endpoint is asked with for the pins, each a package name and its version."""
    queries = [{"package": {"name": name, "ecosystem": ecosystem}, "version": version} for name, version in pins]
    return {"queries": queries}


def assert_osv_asked_about(mock_post: Mock, *pins: tuple[str, str], ecosystem: str = "PyPI") -> None:
    """Assert that OSV was asked about these pins alone, in the one batch request a file costs."""
    mock_post.assert_called_once_with(OSV_BATCH_URL, timeout=ANY, json=osv_queries(*pins, ecosystem=ecosystem))


def osv(*advisories: dict[str, object]) -> _patch:
    """Return a patch answering OSV with the advisories affecting a version, and with none when given none."""
    return patch("requests.Session.post", osv_api(*advisories))


def unreachable_osv() -> _patch:
    """Return a patch failing every OSV request, as an OSV a run cannot reach does."""
    status = HTTPStatus.SERVICE_UNAVAILABLE
    unreachable = mock_response(ok=False, status_code=status, reason=status.phrase, url="https://api.osv.dev")
    return patch("requests.Session.post", Mock(return_value=unreachable))


# Reusable class decorator that answers OSV with no advisories, for update tests that focus on the update flow.
# Without it their pins are looked up at OSV for real, since nothing else in those tests patches `Session.post`.
no_vulnerabilities = osv()

# Reusable decorator that switches the vulnerability check off, for update tests that focus on the update flow and
# would otherwise trigger the vulnerability pass's own OSV request.
vulnerability_check_disabled = patch_environ({VULNERABILITY_LEVEL.name: NO_RISK_LEVEL})


# Reusable decorator for the tests that patch the GitHub source's getter: the commit a reference's own tag points at
# carries that tag alone, so the reference keeps its version.
no_other_version_at_the_tag = patch(
    "update_time.references.github.version_at_tag", new=lambda _dependency, version: version
)


def pyproject_per_line(*specs: str, marker: str) -> str:
    """Return a minimal valid pyproject.toml declaring each of the given dependencies on a line of its own.

    `marker` goes in an `# update-time:` comment on the first spec's line, so it steers that declaration and leaves
    the ones below it alone.
    """
    lines = [f'    "{spec}",' for spec in specs]
    lines[0] += f"  # update-time: {marker}"
    dependencies = "".join(f"{line}\n" for line in lines)
    return f"[project]\ndependencies = [\n{dependencies}]\n"


def github_unknown_ref(ref: str) -> Mock:
    """Return the response GitHub's commits endpoint gives for a ref that does not name a commit in the repository."""
    return mock_response({"message": f"No commit found for SHA: {ref}"}, ok=False, status_code=422)


def github_fresh_line(length: int, parent: str) -> list[dict[str, object]]:
    """Return a line of fresh commits, newest first, each the first parent of the one listed before it."""
    shas = [f"{index:040x}" for index in range(length)]
    return [
        github_commits_json(sha, date=FRESH_DATE, parents=[first_parent])
        for sha, first_parent in zip(shas, [*shas[1:], parent], strict=True)
    ]


def full_commit_sha(short_sha: str) -> str:
    """Return a full commit SHA that the short one abbreviates, as GitHub resolves it."""
    return short_sha.ljust(len(COMMIT_SHA), "0")


def github_commit_listing_queries() -> list[dict[str, list[str]]]:
    """Return the query of each request that listed commits."""
    urls = [urlparse(url) for url in github_requests("commits")]
    return [parse_qs(url.query) for url in urls if url.path.endswith("/commits")]


def github_refs_whose_commits_were_listed() -> list[str]:
    """Return the refs whose commits were listed."""
    return [ref for query in github_commit_listing_queries() for ref in query["sha"]]
