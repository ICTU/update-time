"""Unit tests for the checks the updaters that delegate to uv run over the pins it settled on."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from update_time.io.log import get_logger
from update_time.primitives.location import Location
from update_time.updaters.uv_pins import warn_about_pins

from tests.helpers import mock_path, mock_response
from tests.update_time.helpers import (
    DJANGO_ADVISORY,
    DJANGO_VULNERABILITY,
    LoggingTestCase,
    no_vulnerabilities,
    osv,
    pyproject,
    staleness_disabled,
    vulnerability_check_disabled,
)

if TYPE_CHECKING:
    from unittest.mock import Mock

_LOG = get_logger("uv pins")


class PinnedFileTestCase(LoggingTestCase):
    """Base for the tests of the checks both uv-delegated updaters share.

    The pins are read from the file rather than from uv, so neither check needs a package manager to run: each reads
    whichever `==` pins the file holds by the time it is called. A pyproject.toml stands in for both file kinds,
    which declare their dependencies as the same quoted specs.
    """

    def pinned_file(self, spec: str) -> Mock:
        """Return a mock file whose dependencies pin the spec, in the form both file kinds declare them."""
        return mock_path(pyproject(spec), parent=Path("/"))


@no_vulnerabilities
@patch("requests.get")
class StalePinTest(PinnedFileTestCase):
    """Unit tests for the staleness half, whose PyPI pass resolves each pin's newest release."""

    @staticmethod
    def simple_api(version: str, upload_time: str) -> Mock:
        """Mock the PyPI Index API response listing one version with a distribution-file upload time."""
        return mock_response({"versions": [version], "files": [{"upload-time": upload_time}]})

    def test_stale_pin_warned(self, get: Mock):
        """Test that a pin whose newest release is old is warned about, located at the file without a line."""
        get.return_value = self.simple_api("1.0", (datetime.now(UTC) - timedelta(days=512)).isoformat())
        file = self.pinned_file("package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_stale_dependency_logged("package", "1.0", Location(file))

    def test_recent_pin_not_warned(self, get: Mock):
        """Test that a pin whose newest release is recent is not warned about as stale."""
        get.return_value = self.simple_api("1.0", datetime.now(UTC).isoformat())
        warn_about_pins([self.pinned_file("package==1.0")], _LOG)
        self.assert_no_warnings_logged()

    @staleness_disabled
    def test_disabled_makes_no_pypi_request(self, get: Mock):
        """Test that `--stale-after 0` skips the staleness check, so PyPI is not asked at all."""
        warn_about_pins([self.pinned_file("package==1.0")], _LOG)
        get.assert_not_called()
        self.assert_no_warnings_logged()


@staleness_disabled
class VulnerablePinTest(PinnedFileTestCase):
    """Unit tests for the vulnerability half, whose OSV pass looks each pin up."""

    def test_vulnerable_pin_warned(self):
        """Test that a pin OSV reports an advisory for is warned about, located at the file without a line."""
        file = self.pinned_file("django==3.2.0")
        with osv(DJANGO_ADVISORY):
            warn_about_pins([file], _LOG)
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(file))

    @vulnerability_check_disabled
    def test_disabled_makes_no_osv_request(self):
        """Test that `--warn-vulnerability-level none` skips the check, so OSV is not asked at all."""
        with osv(DJANGO_ADVISORY) as mock_post:
            warn_about_pins([self.pinned_file("django==3.2.0")], _LOG)
        mock_post.assert_not_called()
        self.assert_no_warnings_logged()
