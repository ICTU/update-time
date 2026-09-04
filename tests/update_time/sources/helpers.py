"""Test helpers shared by the tests of the sources."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from unittest.mock import Mock

# The GitHub REST API's per-repository base URL.
_GITHUB_API = "https://api.github.com/repos"
# The host these tests serve a repository's files at, one URL per file name.
_RAW = "https://raw"
# The host these tests serve a repository's trees at, one URL per directory name.
_TREE = "https://tree"


def requested_urls(mock_get: Mock) -> list[str]:
    """Return the URLs the mock requests.get was asked for, in the order they were asked for."""
    return [call.args[0] for call in mock_get.call_args_list]


def respond_per_url(mock_get: Mock, responses: Mapping[Any, Mock]) -> None:
    """Point the mock requests.get at the response each URL maps to.

    A URL the mapping doesn't cover raises a KeyError, so a request the test did not expect fails loudly rather
    than being answered.
    """
    mock_get.side_effect = lambda url, **_kwargs: responses[url]


def releases_url(repository: str) -> str:
    """Return the URL GitHub serves the repository's releases at."""
    return f"{_GITHUB_API}/{repository}/releases?per_page=100"


def contents_url(repository: str, directory: str = "") -> str:
    """Return the URL GitHub serves the listing of the repository's directory at, its root by default."""
    return f"{_GITHUB_API}/{repository}/contents/{directory}"


def file_url(name: str) -> str:
    """Return the URL a repository serves the file of that name at."""
    return f"{_RAW}/{name}"


def git_url(name: str) -> str:
    """Return the URL a contents listing gives as the endpoint of an entry: its tree for a directory."""
    return f"{_TREE}/{name}"


def tree_url(directory: str) -> str:
    """Return the URL GitHub serves the directory's recursive tree listing at."""
    return f"{git_url(directory)}?recursive=1"


def contents_entry(name: str, *, is_file: bool = True) -> dict[str, str | None]:
    """Return a GitHub contents listing entry for the name, one with no file to fetch when it is a directory."""
    return {"name": name, "download_url": file_url(name) if is_file else None, "git_url": git_url(name)}


def markdown_changelog(version: str) -> str:
    """Return the text of a Markdown changelog file holding a section for the version."""
    return f"Changelog\n## {version}\n- Fixed foo\n"


def markdown_changes(version: str) -> str:
    """Return what the changelog parser extracts from `markdown_changelog` for the version."""
    return f"## {version}\n- Fixed foo"


def contents_json(*names: str) -> list[dict[str, str | None]]:
    """Return a GitHub contents listing holding a file per name, each served at its own URL."""
    return [contents_entry(name) for name in names]
