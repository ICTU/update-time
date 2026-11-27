"""What the npm registry and jsDelivr answer about npm packages."""

from typing import TYPE_CHECKING

from tests.http_mocks import mock_response

if TYPE_CHECKING:
    from unittest.mock import Mock


def jsdelivr_versions(*version_strings: str) -> Mock:
    """Return a mock jsDelivr package API response listing the given versions (newest first)."""
    return mock_response({"versions": [{"version": version} for version in version_strings]})


def npm_registry(published: dict[str, str], deprecated: dict[str, str] | None = None) -> Mock:
    """Return a mock npm registry response mapping versions to publish times and optional deprecation messages."""
    versions = {version: {"deprecated": reason} for version, reason in (deprecated or {}).items()}
    return mock_response({"time": published, "versions": versions})
