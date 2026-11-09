"""Test helpers the updater tests share: the files they update, their sources, the checks they skip, and uv lock."""

import subprocess  # nosec
from http import HTTPStatus
from itertools import repeat
from typing import TYPE_CHECKING, ClassVar, NoReturn
from unittest.mock import ANY, DEFAULT, Mock, patch
from urllib.parse import parse_qs, urlparse

from update_time.domain.dependency import NO_CHANGES, Changes, DependencyVersion, Project

from tests.helpers import FakeFiles, mock_response
from tests.update_time.fixtures import COMMIT_SHA, COMMIT_SHA2, FRESH_DATE
from tests.update_time.helpers import SCAN_ROOT, ExecutablesOnPathTestCase, vulnerability
from tests.update_time.sources.helpers import (
    PYPI_OLD_UPLOAD,
    github_commits_json,
    github_requests,
    osv_advisory,
    osv_api,
    pypi_index,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path
    from unittest.mock import _patch

    from update_time.domain.vulnerability import Vulnerability
    from update_time.primitives.command import Command
    from update_time.sources.pypi import ReleaseMetadata

# Reusable class decorator that mocks the Docker Hub auth token request made by sources.docker_hub.api_headers
# when DOCKER_HUB_USERNAME/DOCKER_HUB_TOKEN are set, so the image updater tests never make a real network call.
mock_docker_hub_auth = patch("requests.Session.post", Mock(return_value=mock_response({"access_token": "token"})))  # nosec


# The pyproject.toml at the root of the scan.
PYPROJECT_TOML = SCAN_ROOT / "pyproject.toml"

# The default project for the versions a test builds without one.
_NO_PROJECT = Project()

# The upload times of the PyPI release metadata the uv updater tests answer with, with a changelog URL and without one.
UPLOAD_TIME_WITH_CHANGELOG = "2026-05-30T12:07:03.123456Z"
UPLOAD_TIME_WITHOUT_CHANGELOG = "2026-05-30T12:08:53.123321Z"


def _executed_commands(run: Mock) -> list[Command]:
    """Return the command of each call the mock of `subprocess.run` received, in the order of the calls."""
    return [call.args[0] for call in run.call_args_list]


def _pypi_metadata_without_changelog() -> ReleaseMetadata:
    """Create PyPI release metadata without project URLs, so the test needs no changelog response mocked."""
    return {
        "info": {"description": "Package", "project_urls": {}},
        "urls": [{"upload_time_iso_8601": UPLOAD_TIME_WITHOUT_CHANGELOG}],
    }


def pypi_answer(url: str, *_arguments: object, **_options: object) -> Mock:
    """Answer a request to PyPI: the Index API lists an undated 1.0, and the JSON API does not name a changelog."""
    return pypi_index("1.0") if "/simple/" in url else mock_response(_pypi_metadata_without_changelog())


def github_version(version: str, project: Project = _NO_PROJECT, changes: Changes = NO_CHANGES) -> DependencyVersion:
    """Return the version the GitHub source resolves to, pinned to the new commit and tagged with a `v` prefix."""
    return DependencyVersion(version=version, changes=changes, sha=COMMIT_SHA2, tag_name=f"v{version}", project=project)


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


# The lock after that `UvLockTestCase.files_after_update` takes for a uv lock that fails.
LOCK_FAILS = None

# What uv lock writes to stderr where the pins contradict each other.
NO_SOLUTION = "error: No solution found when resolving dependencies"


def locked_package(package: str, version: str) -> str:
    """Return the entry of a lock that locks the package at the version."""
    return f'[[package]]\nname = "{package}"\nversion = "{version}"\n'


def uv_locks(run: Mock) -> list[Command]:
    """Return the uv lock commands the mock of `subprocess.run` received, in the order of the calls."""
    return [command for command in _executed_commands(run) if command[:2] == ("uv", "lock")]


def _failing_uv_lock(*_arguments: object, **_options: object) -> NoReturn:
    """Fail `uv lock` with the error uv gives for pins that contradict each other."""
    raise subprocess.CalledProcessError(cmd="", returncode=1, output="", stderr=NO_SOLUTION)


class UvLockTestCase(ExecutablesOnPathTestCase):
    """Base for the tests that run an updater delegating to uv, with uv lock faked.

    A subclass names the file the scan discovers, the lock uv writes for it, and the updater. `locks` holds each faked
    uv lock of the last update: its command, and the files as they were when it ran.
    """

    manifest: ClassVar[Path]
    lock_file: ClassVar[Path]
    locks: list[tuple[Command, dict[Path, str]]]

    def update(self) -> None:
        """Run the updater."""
        raise NotImplementedError

    def files_after_update(  # noqa: PLR0913 — the patched mocks travel with the fixtures they are given
        self,
        run: Mock,
        get: Mock,
        glob: Mock,
        files: Mapping[Path, str],
        lock_after: str | tuple[str | None, ...] | None,
        *,
        discovered: Sequence[Path] = (),
        lock_as_written: str | None = None,
    ) -> FakeFiles:
        """Update the files that the scan discovers, the manifest where the test names none, and return the files.

        The uv lock of the files as written writes the lock as written. Where the test gives none, it leaves the lock
        as it is, or absent where there was none. Each uv lock after it writes the lock after, or
        fails for `LOCK_FAILS`. A tuple gives the lock after of each of those in turn. uv writes past the write errors
        the files hold, since it is a process of its own. PyPI does not name a changelog for a version.
        """
        fake_files = files if isinstance(files, FakeFiles) else FakeFiles(files)
        locks_after = iter(lock_after) if isinstance(lock_after, tuple) else repeat(lock_after)
        self.locks = []

        def uv_lock(command: Command, *_arguments: object, **_options: object) -> object:
            as_written = not any("--upgrade" in locked for locked, _files in self.locks) and "--upgrade" not in command
            self.locks.append((command, dict(fake_files)))
            if as_written:
                if lock_as_written is not None:
                    fake_files[self.lock_file] = lock_as_written
                return DEFAULT
            if (lock := next(locks_after)) is None:
                _failing_uv_lock()
            fake_files[self.lock_file] = lock
            return DEFAULT

        run.side_effect = uv_lock
        run.return_value = Mock(stdout="", stderr="")
        get.return_value = mock_response(_pypi_metadata_without_changelog())
        glob.return_value = list(discovered) or [self.manifest]
        with fake_files.patched():
            self.update()
        return fake_files

    def updated_with_failing_lock(self, run: Mock, glob: Mock, contents: str) -> FakeFiles:
        """Update the manifest holding the contents, with every uv lock failing, and return the files."""
        files = FakeFiles({self.manifest: contents})
        run.side_effect = _failing_uv_lock
        glob.return_value = [self.manifest]
        with files.patched():
            self.update()
        return files

    def text_locked_with_upgrade(self) -> str:
        """Return the text the manifest held when the last update ran `uv lock --upgrade`."""
        return next(files[self.manifest] for command, files in self.locks if "--upgrade" in command)
