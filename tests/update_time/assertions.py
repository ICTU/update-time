"""Shared test assertions."""

from unittest.mock import ANY, Mock


def assert_success(result: int) -> None:
    """Assert that an updater returned the success exit code (0)."""
    if result != 0:
        message = f"Expected the updater to succeed (exit code 0), but got {result}"
        raise AssertionError(message)


def assert_new_version_logged(
    mock_warning: Mock, dependency: str, version: str, changes: str = "No changelog available!", *, once: bool = False
) -> None:
    """Assert that the availability of a new version was logged as a warning for the dependency."""
    assert_called = mock_warning.assert_called_once_with if once else mock_warning.assert_called_with
    assert_called("New version available for %s: %s\n%s", dependency, version, changes, stacklevel=ANY)


def assert_pinned_logged(mock_warning: Mock, dependency: str, version: str, sha: str) -> None:
    """Assert that pinning a previously unpinned reference to a digest was logged as a warning."""
    mock_warning.assert_called_with("Pinned %s to %s@%s", dependency, version, sha, stacklevel=ANY)


def assert_path_logged(mock_info: Mock, relative_path: object) -> None:
    """Assert that the file being updated was logged as info."""
    mock_info.assert_called_with("Checking if there are updates for %s", relative_path, stacklevel=ANY)
