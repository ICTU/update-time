"""Unit tests for the checks the updaters that delegate to uv run over the dependencies their files declare."""

from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock, patch

from update_time.domain.dependency import Yank
from update_time.file_formats.dependency_file import InlineScript, PyprojectToml
from update_time.io.log import get_logger
from update_time.package_managers import uv as uv_module
from update_time.primitives.location import Location
from update_time.updaters.uv_pins import warn_about_pins

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time.helpers import (
    PYPI_OLD_UPLOAD,
    LoggingTestCase,
    pypi_index,
    pypi_release,
    pyproject,
    staleness_disabled,
    yanked_file,
)
from tests.update_time.updaters.helpers import (
    DJANGO_ADVISORY,
    DJANGO_VULNERABILITY,
    PYPI_RECENT_UPLOAD,
    dated_pypi_index,
    no_vulnerabilities,
    osv,
    vulnerability_check_disabled,
)

_LOG = get_logger("uv pins")

# The yank pass asks PyPI for every pin, whichever of the checks a test is about, so the tests that are about
# another check answer it with an index that lists no version and no distribution file to read a yank from.
_no_yanks = patch("requests.get", Mock(return_value=pypi_index()))


class DependencyTomlFileTestCase(LoggingTestCase):
    """Base for the tests of the checks both uv-delegated updaters share.

    The dependencies are read from the file rather than from uv, so no check needs a package manager to run: each
    reads whatever the file declares by the time it is called.
    """

    def dependency_toml_file(self, *specs: str) -> PyprojectToml:
        """Return a mock file declaring the specs as its dependencies, in the form both file kinds declare them."""
        return PyprojectToml(mock_path(pyproject(*specs), parent=Path("/")))


@no_vulnerabilities
@patch("requests.get")
class StaleDependencyTest(DependencyTomlFileTestCase):
    """Unit tests for the staleness check, which reads the newest release PyPI lists for each dependency."""

    def test_stale_pin_warned(self, get: Mock):
        """Test that a pin whose newest release is old is warned about, located at the line the pin sits on."""
        get.return_value = dated_pypi_index("1.0")
        file = self.dependency_toml_file("package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_stale_dependency_logged("package", "1.0", Location(file.path, 2))

    def test_stale_dependency_without_an_exact_pin_warned(self, get: Mock):
        """Test that a dependency declared without an exact pin is warned about, at the line declaring it."""
        get.return_value = dated_pypi_index("1.0")
        file = self.dependency_toml_file("package>=1.0")
        warn_about_pins([file], _LOG)
        self.assert_stale_dependency_logged("package", "1.0", Location(file.path, 2))

    def test_a_script_whose_block_does_not_parse_leaves_the_other_files_checked(self, get: Mock):
        """Test that a script whose metadata block is not valid TOML leaves the files after it checked."""
        get.return_value = dated_pypi_index("1.0")
        malformed = InlineScript(mock_path("# /// script\n# dependencies = [\n# ///\n", parent=Path("/")))
        file = self.dependency_toml_file("package>=1.0")
        warn_about_pins([malformed, file], _LOG)
        self.assert_stale_dependency_logged("package", "1.0", Location(file.path, 2))

    def test_dependency_without_an_exact_pin_the_index_lists_no_release_for(self, get: Mock):
        """Test that a dependency whose package the index lists no release for is not warned about."""
        get.return_value = pypi_index()
        warn_about_pins([self.dependency_toml_file("package>=1.0")], _LOG)
        self.assert_no_warnings_logged()

    def test_recent_pin_not_warned(self, get: Mock):
        """Test that a pin whose newest release is recent is not warned about as stale."""
        get.return_value = dated_pypi_index("1.0", upload_time=PYPI_RECENT_UPLOAD)
        warn_about_pins([self.dependency_toml_file("package==1.0")], _LOG)
        self.assert_no_warnings_logged()


@no_vulnerabilities
@patch("requests.get")
class YankedPinTest(DependencyTomlFileTestCase):
    """Unit tests for the yank check, which reads the yank state PyPI reports for the version each pin is left on."""

    reason: ClassVar = "broke Python 3.10"

    @classmethod
    def yanked_simple_api(cls, version: str, *newer: str) -> Mock:
        """Mock the PyPI Index API response listing the version, whose distribution file the maintainer yanked."""
        return pypi_index(version, *newer, files=[yanked_file(f"package-{version}.tar.gz", reason=cls.reason)])

    def test_yanked_pin_warned(self, get: Mock):
        """Test that a pin left on a yanked release is warned about, located at the line the pin sits on."""
        get.return_value = self.yanked_simple_api("1.0")
        file = self.dependency_toml_file("package==1.0")
        warn_about_pins([file], _LOG)
        yank = Yank(yanked=True, reason=self.reason)
        self.assert_yanked_dependency_logged("package", "1.0", Location(file.path, 2), yank)

    def test_yanked_pin_warned_although_a_newer_release_exists(self, get: Mock):
        """Test that a pin left on a yanked release is warned about although PyPI has a newer release to move to."""
        get.side_effect = [self.yanked_simple_api("1.0", "2.0"), pypi_release(PYPI_OLD_UPLOAD)]
        file = self.dependency_toml_file("package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_yanked_dependency_logged("package", "1.0", Location(file.path, 2))

    def test_makes_no_pypi_request_of_its_own(self, get: Mock):
        """Test that the yank check reads the index the staleness check fetched, so a pin costs one request."""
        get.return_value = self.yanked_simple_api("1.0")
        file = self.dependency_toml_file("package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_yanked_dependency_logged("package", "1.0", Location(file.path, 2))
        self.assertEqual(get.call_count, 1)

    def test_unparsable_version_leaves_the_other_pins_checked(self, get: Mock):
        """Test that a declaration whose version does not parse leaves the pins after it in the file checked.

        One spec packaging rejects outright, the other names a range no version parses from.
        """
        get.return_value = self.yanked_simple_api("1.0")
        file = self.dependency_toml_file("broken==nightly", "wild==1.0.*", "package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_yanked_dependency_logged("package", "1.0", Location(file.path, 2))

    @staleness_disabled
    def test_yanked_pin_warned_with_the_staleness_check_off(self, get: Mock):
        """Test that `--stale-after 0` leaves the yank check running, so the pin is still warned about."""
        get.return_value = self.yanked_simple_api("1.0")
        file = self.dependency_toml_file("package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_yanked_dependency_logged("package", "1.0", Location(file.path, 2))


@_no_yanks
@staleness_disabled
class VulnerablePinTest(DependencyTomlFileTestCase):
    """Unit tests for the vulnerability check, whose OSV pass looks each pin up."""

    def test_vulnerable_pin_warned(self):
        """Test that a pin OSV reports an advisory for is warned about, located at the line the pin sits on."""
        file = self.dependency_toml_file("django==3.2.0")
        with osv(DJANGO_ADVISORY):
            warn_about_pins([file], _LOG)
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(file.path, 2))

    def test_a_name_pinned_twice(self):
        """Test that every pin of a name is looked up, so one pin never hides another's vulnerability."""
        file = PyprojectToml(
            mock_path(
                '[project]\ndependencies = ["django==3.2.0"]\n[dependency-groups]\ndev = ["django==4.2.0"]\n',
                parent=Path("/"),
            )
        )
        with osv(DJANGO_ADVISORY):
            warn_about_pins([file], _LOG)
        vulnerable = DJANGO_VULNERABILITY
        self.assert_vulnerable_dependency_logged(
            "django", "3.2.0", vulnerable, Location(file.path, 2), among_others=True
        )
        self.assert_vulnerable_dependency_logged(
            "django", "4.2.0", vulnerable, Location(file.path, 4), among_others=True
        )

    @vulnerability_check_disabled
    def test_disabled_makes_no_osv_request(self):
        """Test that `--vulnerability-level none` skips the check, so OSV is not asked at all."""
        with osv(DJANGO_ADVISORY) as mock_post:
            warn_about_pins([self.dependency_toml_file("django==3.2.0")], _LOG)
        mock_post.assert_not_called()
        self.assert_no_warnings_logged()


@no_vulnerabilities
@patch("requests.get")
class ArchivedDependencyTest(DependencyTomlFileTestCase):
    """Unit tests for the archival check, which reads the project status PyPI publishes for each dependency."""

    def test_archived_dependency_without_an_exact_pin_warned(self, get: Mock):
        """Test that a dependency declared without an exact pin is warned about, at the line declaring it."""
        get.return_value = dated_pypi_index("1.0", upload_time=PYPI_RECENT_UPLOAD, archived=True)
        file = self.dependency_toml_file("package>=1.0")
        warn_about_pins([file], _LOG)
        self.assert_archived_dependency_logged("package", Location(file.path, 2))

    def test_stale_and_archived_dependency_warned_about_on_both_counts(self, get: Mock):
        """Test that a project whose newest release is old and that PyPI declares archived gets both warnings."""
        get.return_value = dated_pypi_index("1.0", archived=True)
        file = self.dependency_toml_file("package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_stale_dependency_logged("package", "1.0", Location(file.path, 2), among_others=True)
        self.assert_archived_dependency_logged("package", Location(file.path, 2), among_others=True)

    @kills(
        Mutation(
            uv_module,
            "@archival_reporting\ndef pypi_projects(",
            "def pypi_projects(",
            "the resolver reports no archival, so a switched-off staleness check skips the file it reads",
        ),
    )
    @staleness_disabled
    def test_staleness_disabled_still_warns_about_an_archived_project(self, get: Mock):
        """Test that an archived project is warned about when the staleness check is switched off."""
        get.return_value = dated_pypi_index("1.0", archived=True)
        file = self.dependency_toml_file("package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_archived_dependency_logged("package", Location(file.path, 2))

    def test_archived_pin_warned_at_the_cost_of_one_request(self, get: Mock):
        """Test that a pin on an archived project is warned about, reading the index the staleness check fetched."""
        get.return_value = dated_pypi_index("1.0", upload_time=PYPI_RECENT_UPLOAD, archived=True)
        file = self.dependency_toml_file("package==1.0")
        warn_about_pins([file], _LOG)
        self.assert_archived_dependency_logged("package", Location(file.path, 2))
        self.assertEqual(get.call_count, 1)

    @kills(
        Mutation(
            uv_module,
            "        yield ResolvedReference.from_reference(declaration, release=release)",
            "        if release.project.newest is not None:\n"
            "            yield ResolvedReference.from_reference(declaration, release=release)",
            "a declaration whose package the index lists no release for is dropped, archival and all",
        )
    )
    def test_archived_project_the_index_lists_no_release_for_warned(self, get: Mock):
        """Test that an archived project is warned about although the index lists no release for it."""
        get.return_value = pypi_index(archived=True)
        file = self.dependency_toml_file("package>=1.0")
        warn_about_pins([file], _LOG)
        self.assert_archived_dependency_logged("package", Location(file.path, 2))


@patch("requests.get")
class UvSourcedDependencyTest(DependencyTomlFileTestCase):
    """Unit tests for the dependencies uv resolves from a source of its own, which PyPI serves no release for."""

    @kills(
        Mutation(
            uv_module,
            "    for declaration in pypi_served_dependencies(file):",
            "    for declaration in pyproject_toml_format.declared_dependencies(file):",
            "the project checks read every declaration, so PyPI is asked about one it serves no release for",
        )
    )
    def test_a_pin_with_a_uv_source_is_asked_about_nowhere(self, get: Mock):
        """Test that a pin uv resolves from a source of its own is looked up at neither PyPI nor OSV."""
        contents = (
            '[project]\ndependencies = ["local==1.0", "django==3.2.0"]\n'
            '[tool.uv.sources]\nlocal = {path = "../local"}\n'
        )
        file = PyprojectToml(mock_path(contents, parent=Path("/")))
        get.return_value = dated_pypi_index("3.2.0", upload_time=PYPI_RECENT_UPLOAD)
        with osv(DJANGO_ADVISORY) as mock_post:
            warn_about_pins([file], _LOG)
        self.assertEqual([call.args[0] for call in get.call_args_list], ["https://pypi.org/simple/django/"])
        query = {"package": {"name": "django", "ecosystem": "PyPI"}, "version": "3.2.0"}
        # The first request batches every pin the check looks up; the ones after it follow from what it answered.
        self.assertEqual(mock_post.call_args_list[0].kwargs["json"], {"queries": [query]})
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(file.path, 2))
