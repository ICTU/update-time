"""Shared test assertions."""


def assert_success(result: int) -> None:
    """Assert that an updater returned the success exit code (0)."""
    if result != 0:
        message = f"Expected the updater to succeed (exit code 0), but got {result}"
        raise AssertionError(message)
