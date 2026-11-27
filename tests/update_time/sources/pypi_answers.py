"""What PyPI answers: a package's Index API listing and a version's release metadata."""

from typing import TYPE_CHECKING, Final
from unittest.mock import Mock

from packaging.version import Version

from tests.http_mocks import mock_response

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from unittest.mock import Mock

    from update_time.formats.json import JSON
    from update_time.sources.pypi import _ReleaseMetadata  # simplint: ignore[private-import-path] # annotates with it
PYPI_OLD_UPLOAD: Final[str] = "2020-01-01T00:00:00.000000Z"  # A distribution upload time far older than the cooldown.


def yanked_file(filename: str, *, reason: str | bool = True) -> dict[str, str | bool]:
    """Return a mock Index API distribution file entry marked as yanked (a reason string, or True for none)."""
    return {"filename": filename, "yanked": reason}


_NO_PROJECT_URLS: Final[dict[str, str]] = {}  # The default for a release fixture no test asks project URLs of.


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


# The upload times of the PyPI release metadata the uv updater tests answer with, with a changelog URL and without one.
UPLOAD_TIME_WITH_CHANGELOG: Final[str] = "2026-05-30T12:07:03.123456Z"
UPLOAD_TIME_WITHOUT_CHANGELOG: Final[str] = "2026-05-30T12:08:53.123321Z"


def pypi_answer(url: str, *_arguments: object, **_options: object) -> Mock:
    """Answer a request to PyPI: the Index API lists an undated 1.0, and the JSON API does not name a changelog."""
    return pypi_index("1.0") if "/simple/" in url else mock_response(pypi_metadata_without_changelog())


def pypi_index(
    *versions: str, files: Sequence[Mapping[str, str | bool]] | None = None, archived: bool | str = False
) -> Mock:
    """Return a mock PyPI Index API response listing the versions, distribution files, and project status."""
    body: dict[str, JSON] = {"versions": list(versions)}
    if files is not None:
        newest = str(max(Version(version) for version in versions))
        body["files"] = [{"filename": f"package-{newest}.tar.gz", **file} for file in files]
    if archived:
        body["project-status"] = {"status": "archived", "reason": (archived if isinstance(archived, str) else None)}
    return mock_response(body)


def pypi_metadata_without_changelog() -> _ReleaseMetadata:
    """Create PyPI release metadata without project URLs, so the test needs no changelog response mocked."""
    return {
        "info": {"description": "Package", "project_urls": {}},
        "urls": [{"upload_time_iso_8601": UPLOAD_TIME_WITHOUT_CHANGELOG}],
    }


def dated_pypi_index(*versions: str, upload_time: str = PYPI_OLD_UPLOAD, archived: bool | str = False) -> Mock:
    """Return a mock Index API response listing the versions, the newest dated by one distribution file."""
    return pypi_index(*versions, files=[{"upload-time": upload_time}], archived=archived)
