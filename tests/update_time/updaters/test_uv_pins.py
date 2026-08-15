"""Unit tests for the checks the updaters that delegate to uv run over the dependencies their files declare."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock, patch

from update_time.domain.dependency import Yank
from update_time.io.log import get_logger
from update_time.primitives.location import Location
from update_time.updaters.uv_pins import warn_about_pins

from tests.helpers import mock_path
from tests.update_time.helpers import (
    DJANGO_ADVISORY,
    DJANGO_VULNERABILITY,
    PYPI_OLD_UPLOAD,
    LoggingTestCase,
    no_vulnerabilities,
    osv,
    pypi_index,
    pypi_release,
    pyproject,
    staleness_disabled,
    vulnerability_check_disabled,
    yanked_file,
)

_LOG = get_logger("uv pins")

# The yank pass asks PyPI for every pin, whichever of the checks a test is about, so the tests that are about
# another check answer it with an index that lists no version and no distribution file to read a yank from.
_no_yanks = patch("requests.get", Mock(return_value=pypi_index()))


class DependencyFileTestCase(LoggingTestCase):
    """Base for the tests of the checks both uv-delegated updaters share.

    The dependencies are read from the file rather than from uv, so no check needs a package manager to run: each
    reads whatever the file declares by the time it is called.
    """

    def dependency_file(self, *specs: str) -> Mock:
        """Return a mock file declaring the specs as its dependencies, in the form both file kinds declare them."""
        return mock_path(pyproject(*specs), parent=Path("/"))


@no_vulnerabilities
@patch("requests.get")
class StaleDependencyTest(DependencyFileTestCase):
    """Unit tests for the staleness check, whose PyPI pass reads the newest release of every dependency declared."""

    @staticmethod
    def simple_api(version: str, upload_time: str) -> Mock:
        """Mock the PyPI Index API response listing one version with a distribution-file upload time."""
        return pypi_index(version, files=[{"upload-time": upload_time}])

    def test_stale_pin_warned(self, get: Mock):
        """Test that a pin whose newest release is old is warned about, located at the line the pin sits on."""
        get.return_value = self.simple_api("1.0", (datetime.now(UTC) - timedelta(days=512)).isoformat())
        file = self.dependency_file("package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_stale_dependency_logged("package", "1.0", Location(file, 2))

    def test_stale_dependency_without_an_exact_pin_warned(self, get: Mock):
        """Test that a dependency declared without an exact pin is warned about, at the line declaring it."""
        get.return_value = self.simple_api("1.0", (datetime.now(UTC) - timedelta(days=512)).isoformat())
        file = self.dependency_file("package>=1.0")
        warn_about_pins([file], _LOG)
        self.assert_stale_dependency_logged("package", "1.0", Location(file, 2))

    def test_dependency_without_an_exact_pin_the_index_lists_no_release_for(self, get: Mock):
        """Test that a dependency whose package the index lists no release for is not warned about."""
        get.return_value = pypi_index()
        warn_about_pins([self.dependency_file("package>=1.0")], _LOG)
        self.assert_no_warnings_logged()

    def test_recent_pin_not_warned(self, get: Mock):
        """Test that a pin whose newest release is recent is not warned about as stale."""
        get.return_value = self.simple_api("1.0", datetime.now(UTC).isoformat())
        warn_about_pins([self.dependency_file("package==1.0")], _LOG)
        self.assert_no_warnings_logged()


@no_vulnerabilities
@patch("requests.get")
class YankedPinTest(DependencyFileTestCase):
    """Unit tests for the yank check, whose PyPI pass reads the yank state of the version each pin is left on."""

    reason: ClassVar = "broke Python 3.10"

    @classmethod
    def yanked_simple_api(cls, version: str, *newer: str) -> Mock:
        """Mock the PyPI Index API response listing the version, whose distribution file the maintainer yanked."""
        return pypi_index(version, *newer, files=[yanked_file(f"package-{version}.tar.gz", reason=cls.reason)])

    def test_yanked_pin_warned(self, get: Mock):
        """Test that a pin left on a yanked release is warned about, located at the line the pin sits on."""
        get.return_value = self.yanked_simple_api("1.0")
        file = self.dependency_file("package==1.0")
        warn_about_pins([file], _LOG)
        yank = Yank(yanked=True, reason=self.reason)
        self.assert_yanked_dependency_logged("package", "1.0", Location(file, 2), yank)

    def test_yanked_pin_warned_although_a_newer_release_exists(self, get: Mock):
        """Test that a pin left on a yanked release is warned about although PyPI has a newer release to move to."""
        get.side_effect = [self.yanked_simple_api("1.0", "2.0"), pypi_release(PYPI_OLD_UPLOAD)]
        file = self.dependency_file("package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_yanked_dependency_logged("package", "1.0", Location(file, 2))

    def test_makes_no_pypi_request_of_its_own(self, get: Mock):
        """Test that the yank check reads the index the staleness check fetched, so a pin costs one request."""
        get.return_value = self.yanked_simple_api("1.0")
        file = self.dependency_file("package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_yanked_dependency_logged("package", "1.0", Location(file, 2))
        self.assertEqual(get.call_count, 1)

    def test_unparsable_version_leaves_the_other_pins_checked(self, get: Mock):
        """Test that a pin whose version does not parse leaves the pins after it in the file checked."""
        get.return_value = self.yanked_simple_api("1.0")
        file = self.dependency_file("broken==nightly", "package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_yanked_dependency_logged("package", "1.0", Location(file, 2))

    @staleness_disabled
    def test_yanked_pin_warned_with_the_staleness_check_off(self, get: Mock):
        """Test that `--stale-after 0` leaves the yank check running, so the pin is still warned about."""
        get.return_value = self.yanked_simple_api("1.0")
        file = self.dependency_file("package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_yanked_dependency_logged("package", "1.0", Location(file, 2))


@_no_yanks
@staleness_disabled
class VulnerablePinTest(DependencyFileTestCase):
    """Unit tests for the vulnerability check, whose OSV pass looks each pin up."""

    def test_vulnerable_pin_warned(self):
        """Test that a pin OSV reports an advisory for is warned about, located at the line the pin sits on."""
        file = self.dependency_file("django==3.2.0")
        with osv(DJANGO_ADVISORY):
            warn_about_pins([file], _LOG)
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(file, 2))

    def test_a_name_pinned_twice(self):
        """Test that every pin of a name is looked up, so one pin never hides another's vulnerability."""
        file = mock_path(
            '[project]\ndependencies = ["django==3.2.0"]\n[dependency-groups]\ndev = ["django==4.2.0"]\n',
            parent=Path("/"),
        )
        with osv(DJANGO_ADVISORY):
            warn_about_pins([file], _LOG)
        for version, line in (("3.2.0", 2), ("4.2.0", 4)):
            with self.subTest(version=version):
                self.assert_vulnerable_dependency_logged(
                    "django", version, DJANGO_VULNERABILITY, Location(file, line), among_others=True
                )

    @vulnerability_check_disabled
    def test_disabled_makes_no_osv_request(self):
        """Test that `--vulnerability-level none` skips the check, so OSV is not asked at all."""
        with osv(DJANGO_ADVISORY) as mock_post:
            warn_about_pins([self.dependency_file("django==3.2.0")], _LOG)
        mock_post.assert_not_called()
        self.assert_no_warnings_logged()
