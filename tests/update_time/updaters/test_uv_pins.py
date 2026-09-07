"""Unit tests for the checks the updaters that delegate to uv run over the dependencies their files declare."""

from pathlib import Path
from typing import ClassVar
from unittest.mock import ANY, Mock, patch

from update_time.domain.dependency import Yank
from update_time.domain.vulnerability import NO_RISK_LEVEL, VULNERABILITY_LEVEL
from update_time.file_formats import toml as toml_module
from update_time.file_formats.dependency_file import InlineScript, PyprojectToml
from update_time.io.log import get_logger
from update_time.markers import reference as marker_reference_module
from update_time.markers.directive import Reason
from update_time.package_managers import uv as uv_module
from update_time.primitives.location import Location
from update_time.references import delegated as delegated_module
from update_time.references import vulnerability as vulnerability_module
from update_time.updaters import uv_pins as uv_pins_module
from update_time.updaters.uv_pins import warn_about_pins

from tests.helpers import mock_path, patch_environ
from tests.mutation import Mutation, kills
from tests.update_time.helpers import (
    PYPI_OLD_UPLOAD,
    LoggingTestCase,
    archival_check_disabled,
    pypi_index,
    pypi_release,
    pyproject,
    pyproject_per_line,
    staleness_disabled,
    yanked_file,
)
from tests.update_time.updaters.fixtures import (
    ADVISORY,
    DJANGO_ADVISORY,
    DJANGO_VULNERABILITY,
    OTHER_DJANGO_ADVISORY,
    OTHER_DJANGO_VULNERABILITY,
    PYPI_RECENT_UPLOAD,
    VULNERABILITY,
)
from tests.update_time.updaters.helpers import (
    dated_pypi_index,
    days_ago,
    no_vulnerabilities,
    osv,
    osv_queries,
    vulnerability_check_disabled,
)

_LOG = get_logger("uv pins")

# The `[tool.uv] sources` table naming `local`, so uv resolves that dependency from a path and PyPI serves it not.
_UV_SOURCE = '[tool.uv.sources]\nlocal = {path = "../local"}\n'

# The yank pass asks PyPI for every pin, whichever of the checks a test is about, so the tests that are about
# another check answer it with an index that lists no version and no distribution file to read a yank from.
_no_yanks = patch("requests.get", Mock(return_value=pypi_index()))


class DependencyTomlFileTestCase(LoggingTestCase):
    """Base for the tests of the checks both uv-delegated updaters share.

    The dependencies are read from the file rather than from uv, so no check needs a package manager to run: each
    reads whatever the file declares by the time it is called.
    """

    def dependency_toml_file(self, *specs: str, marker: str = "") -> PyprojectToml:
        """Return a mock file declaring the specs as its dependencies, in the form both file kinds declare them.

        `marker` steers every dependency the file declares, since they share the array's line (see `pyproject`).
        """
        return self.file_holding(pyproject(*specs, marker=marker))

    def file_holding(self, contents: str) -> PyprojectToml:
        """Return a mock file holding the contents, whichever way they declare their dependencies."""
        return PyprojectToml(mock_path(contents, parent=Path("/")))


@no_vulnerabilities
@patch("requests.get")
class StaleDependencyTest(DependencyTomlFileTestCase):
    """Unit tests for the staleness check, which reads the newest release PyPI lists for each dependency."""

    def test_stale_dependency_warned(self, get: Mock):
        """Test that a dependency whose newest release is old is warned about, at the line declaring it."""
        get.return_value = dated_pypi_index("1.0")
        for spec in ("package==1.0", "package>=1.0"):
            with self.subTest(spec=spec):
                file = self.dependency_toml_file(spec)
                warn_about_pins([file], _LOG)
                self.assert_stale_dependency_logged("package", "1.0", Location(file.path, 2))

    @kills(
        Mutation(
            toml_module,
            "    except tomlkit.exceptions.TOMLKitError:",
            "    except tomlkit.exceptions.ParseError:",
            "TOML that tomlkit rejects with the base error aborts the run instead of leaving the file out",
            raises="tomlkit.exceptions.TOMLKitError: Redefinition of an existing table",
        )
    )
    def test_a_script_whose_block_does_not_parse_leaves_the_other_files_checked(self, get: Mock):
        """Test that a script whose metadata block is not valid TOML leaves the files after it checked."""
        get.return_value = dated_pypi_index("1.0")
        cases = {
            "unterminated array": "# dependencies = [\n",
            "redefined table": "# [tool]\n# uv.sources = {}\n#\n# [tool.uv]\n# x = 1\n",
        }
        for case, block in cases.items():
            with self.subTest(case=case):
                malformed = InlineScript(mock_path(f"# /// script\n{block}# ///\n", parent=Path("/")))
                file = self.dependency_toml_file("package>=1.0")
                warn_about_pins([malformed, file], _LOG)
                self.assert_stale_dependency_logged("package", "1.0", Location(file.path, 2))

    def test_dependency_without_an_exact_pin_the_index_lists_no_release_for(self, get: Mock):
        """Test that a dependency whose package the index lists no release for is not warned about."""
        get.return_value = pypi_index()
        warn_about_pins([self.dependency_toml_file("package>=1.0")], _LOG)
        self.assert_no_warnings_logged()

    def test_stale_pin_a_marker_silences_not_warned(self, get: Mock):
        """Test that an `ignore[stale]` marker silences the staleness warning, and is reported as holding it back."""
        get.return_value = dated_pypi_index("1.0")
        file = self.dependency_toml_file("package==1.0", marker="ignore[stale]")
        warn_about_pins([file], _LOG)
        self.assert_no_warnings_logged()
        self.assert_ignored_staleness_logged("package", Location(file.path, 2))

    @kills(
        Mutation(
            uv_module,
            "    for declaration in declarations:\n"
            "        release = DependencyVersion.unpinned("
            "project(declaration.dependency, check_archival=archival_is_checked()))",
            "    steered = list(declarations)\n"
            "    for declaration in [replace(one, marker=steered[0].marker) for one in steered]:\n"
            "        release = DependencyVersion.unpinned("
            "project(declaration.dependency, check_archival=archival_is_checked()))",
            "one declaration's marker steers every dependency the file declares, rather than the one carrying it",
        )
    )
    def test_a_marker_silences_the_declaration_it_steers_and_no_other(self, get: Mock):
        """Test that a marker on one declaration's line leaves the dependency declared below it warned about."""
        get.return_value = dated_pypi_index("1.0")
        file = self.file_holding(pyproject_per_line("package==1.0", "other==1.0", marker="ignore[stale]"))
        warn_about_pins([file], _LOG)
        self.assert_stale_dependency_logged("other", "1.0", Location(file.path, 4))
        self.assert_ignored_staleness_logged("package", Location(file.path, 3))

    def test_the_markers_threshold_is_used(self, get: Mock):
        """Test that a pin carrying a staleness threshold of its own is judged by that one, not by the run's.

        The newest release is 100 days old, which is stale against the marker's 90 and not against the run's 365,
        so the warning is given only when the marker's threshold is the one applied.
        """
        get.return_value = dated_pypi_index("1.0", upload_time=days_ago(100))
        file = self.dependency_toml_file("package==1.0", marker="ignore[stale<90]")
        warn_about_pins([file], _LOG)
        self.assert_stale_dependency_logged("package", "1.0", Location(file.path, 2))

    def test_an_inverted_item_sets_no_threshold(self, get: Mock):
        """Test that a pin whose `stale` item compares the wrong way round is judged by the run's threshold.

        The newest release is 100 days old, which is stale against the 90 the item names and not against the run's
        365, so the silence shows the item set no threshold of its own.
        """
        get.return_value = dated_pypi_index("1.0", upload_time=days_ago(100))
        file = self.dependency_toml_file("package==1.0", marker="ignore[stale>=90]")
        warn_about_pins([file], _LOG)
        self.assertEqual(get.call_count, 1)  # The release was looked up, so the silence is a judgement of its age.
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            delegated_module,
            "project_is_checked(projects, reference.dependency, staleness_threshold(reference.marker))",
            "project_is_checked(projects, reference.dependency, staleness_threshold(type(reference.marker)()))",
            "a reference's own threshold does not reach the gate, so a run with both checks off looks it up for none",
        )
    )
    @staleness_disabled
    @archival_check_disabled
    def test_the_markers_threshold_survives_the_checks_being_switched_off(self, get: Mock):
        """Test that the dependency with a threshold of its own is the only one looked up once both checks are off.

        The newest release is 100 days old, so it is stale against the marker's 90, which the `--stale-after 0` in
        force run-wide does not override. The dependency below it sets no threshold, so no check runs for it and
        PyPI is not asked about it at all.
        """
        get.return_value = dated_pypi_index("1.0", upload_time=days_ago(100))
        file = self.file_holding(pyproject_per_line("package>=1.0", "other>=1.0", marker="ignore[stale<90]"))
        warn_about_pins([file], _LOG)
        self.assert_stale_dependency_logged("package", "1.0", Location(file.path, 3))
        self.assertEqual(get.call_count, 1)

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

    def check_a_yanked_pin(self, get: Mock, marker: str = "") -> PyprojectToml:
        """Run the checks over a file declaring one pin, on a release its maintainer yanked, and return the file."""
        get.return_value = self.yanked_simple_api("1.0")
        file = self.dependency_toml_file("package==1.0", marker=marker)
        warn_about_pins([file], _LOG)
        return file

    def test_yanked_pin_warned(self, get: Mock):
        """Test that a pin left on a yanked release is warned about, located at the line the pin sits on."""
        file = self.check_a_yanked_pin(get)
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
        file = self.check_a_yanked_pin(get)
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

    def test_a_marker_silences_the_yank_warning(self, get: Mock):
        """Test that an `ignore[yanked]` silences the warning, and is reported as holding it back.

        A bare `ignore` holds every check PyPI answers back, so it reaches this one not (see
        `test_a_bare_ignore_asks_pypi_nothing`).
        """
        file = self.check_a_yanked_pin(get, "ignore[yanked]")
        self.assert_no_warnings_logged()
        self.assert_ignored_yank_logged("package", Location(file.path, 2), "ignore[yanked]")

    @kills(
        Mutation(
            uv_module,
            "SteeredResolvedReference.from_reference(pin, release=DependencyVersion(pin.current_version, yank=yank))",
            "SteeredResolvedReference.from_reference("
            "pin, release=DependencyVersion(pin.current_version, yank=yank), "
            "marker=pinned_versions(declarations)[0].marker)",
            "one pin's marker steers every pin the file declares, rather than the one carrying it",
        )
    )
    def test_a_marker_silences_the_pin_it_steers_and_no_other(self, get: Mock):
        """Test that a marker on one pin's line leaves the pin declared below it warned about."""
        get.return_value = self.yanked_simple_api("1.0")
        file = self.file_holding(pyproject_per_line("package==1.0", "other==1.0", marker="ignore[yanked]"))
        warn_about_pins([file], _LOG)
        self.assert_yanked_dependency_logged("other", "1.0", Location(file.path, 4))
        self.assert_ignored_yank_logged("package", Location(file.path, 3))

    def test_ignore_update_marker_still_warns_about_a_yanked_pin(self, get: Mock):
        """Test that a pin frozen by `ignore[update]` still reports the yank of the version it is left on."""
        file = self.check_a_yanked_pin(get, "ignore[update]")
        self.assert_yanked_dependency_logged("package", "1.0", Location(file.path, 2))

    @staleness_disabled
    def test_yanked_pin_warned_with_the_staleness_check_off(self, get: Mock):
        """Test that `--stale-after 0` leaves the yank check running, so the pin is still warned about."""
        file = self.check_a_yanked_pin(get)
        self.assert_yanked_dependency_logged("package", "1.0", Location(file.path, 2))


@_no_yanks
@staleness_disabled
class VulnerablePinTest(DependencyTomlFileTestCase):
    """Unit tests for the vulnerability check, whose OSV pass looks each pin up."""

    def check_pins(self, file: PyprojectToml, *advisories: dict[str, object]) -> Mock:
        """Run the checks over the file, with OSV answering the advisories, and return the mocked OSV endpoint."""
        with osv(*advisories) as mock_post:
            warn_about_pins([file], _LOG)
        return mock_post

    def test_vulnerable_pin_warned(self):
        """Test that a pin OSV reports an advisory for is warned about, located at the line the pin sits on."""
        file = self.dependency_toml_file("django==3.2.0")
        self.check_pins(file, DJANGO_ADVISORY)
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(file.path, 2))

    def test_a_name_pinned_twice(self):
        """Test that every pin of a name is looked up, so one pin never hides another's vulnerability."""
        file = PyprojectToml(
            mock_path(
                '[project]\ndependencies = ["django==3.2.0"]\n[dependency-groups]\ndev = ["django==4.2.0"]\n',
                parent=Path("/"),
            )
        )
        self.check_pins(file, DJANGO_ADVISORY)
        vulnerable = DJANGO_VULNERABILITY
        self.assert_vulnerable_dependency_logged(
            "django", "3.2.0", vulnerable, Location(file.path, 2), among_others=True
        )
        self.assert_vulnerable_dependency_logged(
            "django", "4.2.0", vulnerable, Location(file.path, 4), among_others=True
        )

    def test_a_marker_silences_the_vulnerability_warning(self):
        """Test that an `ignore[vulnerable]` marker silences the warning, and is reported as holding it back."""
        file = self.dependency_toml_file("django==3.2.0", marker="ignore[vulnerable]")
        self.check_pins(file, DJANGO_ADVISORY)
        self.assert_no_warnings_logged()
        self.assert_ignored_vulnerability_logged("django", Location(file.path, 2), "ignore[vulnerable]")

    def test_a_marker_naming_an_advisory_silences_that_one_alone(self):
        """Test that an `ignore[vulnerable=ID]` marker leaves the pin's other advisories warned about."""
        directive = f"ignore[vulnerable={DJANGO_VULNERABILITY.advisory}]"
        file = self.dependency_toml_file("django==3.2.0", marker=directive)
        self.check_pins(file, DJANGO_ADVISORY, OTHER_DJANGO_ADVISORY)
        self.assert_vulnerable_dependency_logged("django", "3.2.0", OTHER_DJANGO_VULNERABILITY, Location(file.path, 2))
        self.assert_ignored_vulnerability_logged("django", Location(file.path, 2), directive)

    @kills(
        Mutation(
            vulnerability_module,
            "_reference_to_check(pin, pin.marker, run_wide_level)",
            "_reference_to_check(pin, list(pinned_versions(references))[0].marker, run_wide_level)",
            "one pin's marker steers every pin in the batch, rather than the one carrying it",
        )
    )
    def test_a_marker_silences_its_own_pin_in_the_batch(self):
        """Test that a marker on one pin's line leaves the pin declared below it in the batch warned about."""
        directive = "ignore[vulnerable]"
        file = self.file_holding(pyproject_per_line("package==1.0", "other==1.0", marker=directive))
        self.check_pins(file, ADVISORY)
        self.assert_vulnerable_dependency_logged("other", "1.0", VULNERABILITY, Location(file.path, 4))
        self.assert_ignored_vulnerability_logged("package", Location(file.path, 3), directive)

    def test_the_markers_risk_level_is_used(self):
        """Test that a pin's own risk level decides what it is warned about, whatever level the run is set to."""
        for run_wide_level in (VULNERABILITY_LEVEL.default, NO_RISK_LEVEL):
            with self.subTest(run_wide_level=run_wide_level), patch_environ({VULNERABILITY_LEVEL.name: run_wide_level}):
                file = self.dependency_toml_file("django==3.2.0", marker="ignore[vulnerable<high]")
                self.check_pins(file, DJANGO_ADVISORY, OTHER_DJANGO_ADVISORY)
                self.assert_vulnerable_dependency_logged(
                    "django", "3.2.0", DJANGO_VULNERABILITY, Location(file.path, 2)
                )

    def test_an_inverted_item_sets_no_risk_level(self):
        """Test that a pin whose `vulnerable` item compares the wrong way round is judged by the run's level.

        The advisory is moderate, which the item's `high` would silence and the run's `low` does not, so the warning
        shows the item set no level of its own.
        """
        file = self.dependency_toml_file("django==3.2.0", marker="ignore[vulnerable>=high]")
        self.check_pins(file, OTHER_DJANGO_ADVISORY)
        self.assert_vulnerable_dependency_logged("django", "3.2.0", OTHER_DJANGO_VULNERABILITY, Location(file.path, 2))

    @vulnerability_check_disabled
    def test_a_pin_without_a_level_of_its_own_stays_out_of_the_batch(self):
        """Test that a run with the check switched off looks up the pin setting a level of its own, and no other."""
        file = self.file_holding(pyproject_per_line("django==3.2.0", "other==1.0", marker="ignore[vulnerable<high]"))
        mock_post = self.check_pins(file, DJANGO_ADVISORY, OTHER_DJANGO_ADVISORY)
        mock_post.assert_any_call(ANY, timeout=ANY, json=osv_queries(("django", "3.2.0")))

    def test_every_redundant_suppression_is_reported(self):
        """Test that each of a marker's three vulnerability suppressions is reported when it holds nothing back."""
        advisory = f"ignore[vulnerable={VULNERABILITY.advisory}]"
        file = self.dependency_toml_file(
            "package==1.0", marker=f"ignore[vulnerable] {advisory} ignore[vulnerable<high]"
        )
        self.check_pins(file)  # OSV answers with no vulnerability, so all three forms hold nothing back.
        location = Location(file.path, 2)
        self.assert_redundant_vulnerable_scope_logged(
            "package", "1.0", location, "ignore[vulnerable]", among_others=True
        )
        self.assert_redundant_vulnerable_advisory_logged("package", "1.0", location, advisory)
        self.assert_redundant_vulnerable_level_logged("package", "1.0", "high", location, "ignore[vulnerable<high]")

    def test_a_bare_ignore_makes_no_osv_request(self):
        """Test that a bare `ignore` keeps the pin out of the batch, so OSV is not asked about it at all."""
        file = self.dependency_toml_file("package==1.0", marker="ignore")
        mock_post = self.check_pins(file)
        mock_post.assert_not_called()
        self.assert_no_warnings_logged()

    @vulnerability_check_disabled
    def test_disabled_makes_no_osv_request(self):
        """Test that `--vulnerability-level none` skips the check, so OSV is not asked at all."""
        mock_post = self.check_pins(self.dependency_toml_file("django==3.2.0"), DJANGO_ADVISORY)
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

    def test_archived_dependency_a_marker_silences_not_warned(self, get: Mock):
        """Test that an `ignore[archived]` marker silences the archival warning, and is reported as holding it back."""
        get.return_value = dated_pypi_index("1.0", upload_time=PYPI_RECENT_UPLOAD, archived=True)
        file = self.dependency_toml_file("package==1.0", marker="ignore[archived]")
        warn_about_pins([file], _LOG)
        self.assert_no_warnings_logged()
        self.assert_ignored_archival_logged("package", Location(file.path, 2))

    @kills(
        Mutation(
            marker_reference_module,
            '            resolved.setdefault("marker", reference.marker)',
            '            resolved.setdefault("marker", reference.marker if reference.current_version '
            "else type(reference.marker)())",
            "a declaration that pins no version loses its marker, so the warnings it silences are reported anyway",
        )
    )
    def test_a_marker_silences_a_dependency_that_pins_no_version(self, get: Mock):
        """Test that both project scopes silence their warning for a dependency declared without an exact pin."""
        cases = {
            "ignore[stale]": dated_pypi_index("1.0"),
            "ignore[archived]": dated_pypi_index("1.0", upload_time=PYPI_RECENT_UPLOAD, archived=True),
        }
        for marker, index in cases.items():
            with self.subTest(marker=marker):
                self.clear_caches()  # each case reads its own index, where the cache would serve the case before it
                get.return_value = index
                warn_about_pins([self.dependency_toml_file("package>=1.0", marker=marker)], _LOG)
                self.assert_no_warnings_logged()

    @kills(
        Mutation(
            delegated_module,
            "    return not reference.marker.holds_back_source_checks",
            "    return True",
            "a marker holding back every check PyPI answers still costs a request for the dependency it steers",
        )
    )
    def test_a_bare_ignore_asks_pypi_nothing(self, get: Mock):
        """Test that a bare `ignore` holds back every check PyPI answers, so the dependency is looked up not.

        The dependency declared below it carries no marker, so the request it costs shows the pass ran over the file.
        """
        get.return_value = pypi_index("1.0")
        file = self.file_holding(pyproject_per_line("package==1.0", "other==1.0", marker="ignore"))
        warn_about_pins([file], _LOG)
        self.assertEqual([call.args[0] for call in get.call_args_list], ["https://pypi.org/simple/other/"])
        self.assert_no_warnings_logged()

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
            "        yield SteeredResolvedReference.from_reference(declaration, release=release)",
            "        if release.project.newest is not None:\n"
            "            yield SteeredResolvedReference.from_reference(declaration, release=release)",
            "a declaration whose package the index lists no release for is dropped, archival and all",
        )
    )
    def test_archived_project_the_index_lists_no_release_for_warned(self, get: Mock):
        """Test that an archived project is warned about although the index lists no release for it."""
        get.return_value = pypi_index(archived=True)
        file = self.dependency_toml_file("package>=1.0")
        warn_about_pins([file], _LOG)
        self.assert_archived_dependency_logged("package", Location(file.path, 2))


@no_vulnerabilities
@patch("requests.get")
class RedundantDirectiveTest(DependencyTomlFileTestCase):
    """Unit tests for the directives that hold nothing back for the dependency carrying them."""

    def check_dependencies(self, get: Mock, file: PyprojectToml) -> None:
        """Run the checks over the file, with PyPI listing an undated release, so nothing is warned about as stale."""
        get.return_value = pypi_index("1.0")
        warn_about_pins([file], _LOG)

    @kills(
        Mutation(
            delegated_module,
            "        if directive.without_a_version is not None and (written := as_written.directive_for",
            "        if directive.scope is Scope.YANKED and (written := as_written.directive_for",
            "only the yank scope is reported as needing a version, so a `vulnerable` scope on a dependency that "
            "pins none reads as holding its warning back",
        ),
    )
    def test_a_scope_needing_a_version_is_redundant_without_an_exact_pin(self, get: Mock):
        """Test that a scope whose check needs a version reports that the dependency pins none."""
        vulnerable = Reason.NO_VERSION_TO_CHECK_FOR_A_VULNERABILITY
        for directive, reason in {
            "ignore[yanked]": Reason.NO_VERSION_TO_CHECK_FOR_A_YANK,
            "ignore[vulnerable]": vulnerable,
            f"ignore[vulnerable={VULNERABILITY.advisory}]": vulnerable,
            "ignore[vulnerable<high]": vulnerable,
        }.items():
            with self.subTest(directive=directive):
                file = self.dependency_toml_file("package>=1.0", marker=directive)
                self.check_dependencies(get, file)
                self.assert_redundant_directive_logged(reason, "package", Location(file.path, 2), directive)

    @kills(
        Mutation(
            delegated_module,
            "    yield from _redundant_cooldown_directive(reference)\n",
            "",
            "a cooldown of a dependency's own reads as applying to it, though uv takes one per run",
        ),
    )
    def test_a_cooldown_is_reported_as_applying_per_run(self, get: Mock):
        """Test that a `cooldown` item is reported for either spec, since uv takes one per run."""
        for spec in ("package==1.0", "package>=1.0"):
            with self.subTest(spec=spec):
                file = self.dependency_toml_file(spec, marker="ignore[cooldown<30]")
                self.check_dependencies(get, file)
                self.assert_redundant_directive_logged(
                    Reason.COOLDOWN_PER_RUN, "package", Location(file.path, 2), "ignore[cooldown<30]"
                )

    @kills(
        Mutation(
            delegated_module,
            "        if written := as_written.directive_for(directive.scope):\n            yield written, reason",
            "        if written := as_written.directive_for(directive.scope):\n            yield written, reason\n"
            "    if cooldown := as_written.cooldown_directive:\n        yield cooldown, reason",
            "the rule for a dependency no source reports on sweeps the cooldown in with the warnings, so one item "
            "is reported twice",
        )
    )
    def test_a_cooldown_on_a_dependency_pypi_serves_no_release_for_keeps_its_own_words(self, get: Mock):
        """Test that a `cooldown` item on a dependency uv resolves from a path is reported once, as applying per run."""
        file = self.file_holding(pyproject("local==1.0", marker="ignore[cooldown<30]") + _UV_SOURCE)
        self.check_dependencies(get, file)
        self.assert_redundant_directive_logged(
            Reason.COOLDOWN_PER_RUN, "local", Location(file.path, 2), "ignore[cooldown<30]"
        )

    def test_an_ignore_update_is_reported_when_it_freezes_no_pin(self, get: Mock):
        """Test that an `ignore[update]` on a dependency that pins no version is reported, since uv resolves it."""
        file = self.dependency_toml_file("package>=1.0", marker="ignore[update]")
        self.check_dependencies(get, file)
        self.assert_redundant_directive_logged(
            Reason.MANAGER_RESOLVES_THE_VERSION, "package", Location(file.path, 2), "ignore[update]"
        )

    @staticmethod
    def unserved_declarations(directive: str) -> dict[str, str]:
        """Return the contents of a file per way a declaration escapes PyPI, the directive steering its dependency."""
        return {
            "uv source": pyproject("local==1.0", marker=directive) + _UV_SOURCE,
            "direct URL": pyproject("local @ https://example.com/local-1.0.tar.gz", marker=directive),
        }

    @kills(
        Mutation(
            delegated_module,
            "    as_written = reference.marker.as_written\n    if as_written.ignores(Scope.UPDATE)",
            "    as_written = reference.marker\n    if as_written.ignores(Scope.UPDATE)",
            "the update rules read the scopes a bare `ignore` holds back without naming, so they report an "
            "`ignore[update]` the reader never wrote",
        ),
        Mutation(
            delegated_module,
            "    if reference.current_version:\n        return\n    as_written = reference.marker.as_written",
            "    if reference.current_version:\n        return\n    as_written = reference.marker",
            "the scopes rule reads the scopes a bare `ignore` holds back without naming, so it reports scopes the "
            "reader never wrote",
        ),
    )
    def test_a_bare_ignore_is_not_reported(self, get: Mock):
        """Test that a bare `ignore` on a dependency that pins no version names no scope to report as redundant.

        The dependency declared below it carries a scope that is reported, so that warning shows the pass ran.
        """
        get.return_value = pypi_index("1.0")
        contents = (
            '[project]\ndependencies = [\n    "package>=1.0",  # update-time: ignore\n'
            '    "other>=1.0",  # update-time: ignore[yanked]\n]\n'
        )
        file = self.file_holding(contents)
        warn_about_pins([file], _LOG)
        self.assert_redundant_directive_logged(
            Reason.NO_VERSION_TO_CHECK_FOR_A_YANK, "other", Location(file.path, 4), "ignore[yanked]"
        )

    def test_an_ignore_update_freezing_an_unserved_pin_is_not_reported(self, get: Mock):
        """Test that an `ignore[update]` steering two dependencies is reported for the one it freezes not."""
        file = self.file_holding(pyproject("local==1.0", "other>=1.0", marker="ignore[update]") + _UV_SOURCE)
        self.check_dependencies(get, file)
        self.assert_redundant_directive_logged(
            Reason.MANAGER_RESOLVES_THE_VERSION, "other", Location(file.path, 2), "ignore[update]"
        )

    @kills(
        Mutation(
            uv_module,
            "    return Reason.NO_PYPI_RELEASE if declaration.names_no_release else None",
            "    return None",
            "a dependency PyPI serves no release for is judged as if it did, so a warning scope on it is reported "
            "in the wrong words or not at all",
        )
    )
    def test_a_scope_is_reported_for_a_dependency_pypi_serves_no_release_for(self, get: Mock):
        """Test that each warning scope is reported for a dependency PyPI is asked about nowhere."""
        for directive in ("ignore[stale]", "ignore[yanked]", "ignore[vulnerable]", "ignore[archived]"):
            for kind, contents in self.unserved_declarations(directive).items():
                with self.subTest(directive=directive, kind=kind):
                    file = self.file_holding(contents)
                    self.check_dependencies(get, file)
                    self.assert_redundant_directive_logged(
                        Reason.NO_PYPI_RELEASE, "local", Location(file.path, 2), directive
                    )

    @kills(
        Mutation(
            delegated_module,
            "    if bound := as_written.version_bound_directive:",
            "    if as_written.ignores(Scope.UPDATE) and (bound := as_written.version_bound_directive):",
            "a bound goes on deciding nothing without a word, as it did before uv-resolved dependencies took a marker",
        )
    )
    def test_a_bound_is_reported_as_deciding_nothing(self, get: Mock):
        """Test that a bound is reported for either spec, in whichever form the marker sets it."""
        for spec in ("package==1.0", "package>=1.0"):
            for bound in ("allow[update<5]", "ignore[minor-update]"):
                with self.subTest(spec=spec, bound=bound):
                    file = self.dependency_toml_file(spec, marker=bound)
                    self.check_dependencies(get, file)
                    self.assert_redundant_directive_logged(
                        Reason.BOUND_DECIDES_NOTHING, "package", Location(file.path, 2), bound
                    )

    @kills(
        Mutation(
            delegated_module,
            "    if bound := as_written.version_bound_directive:",
            "    if not as_written.ignores(Scope.UPDATE) and (bound := as_written.version_bound_directive):",
            "the `ignore[update]` beside a bound hides it, so the directive that decides nothing goes unreported",
        )
    )
    def test_a_bound_beside_a_freeze_is_reported(self, get: Mock):
        """Test that a bound on a pin is reported, though the `ignore[update]` beside it does freeze that pin."""
        file = self.dependency_toml_file("package==1.0", marker="ignore[update] allow[update<5]")
        self.check_dependencies(get, file)
        self.assert_redundant_directive_logged(
            Reason.BOUND_DECIDES_NOTHING, "package", Location(file.path, 2), "allow[update<5]"
        )

    @kills(
        Mutation(
            delegated_module,
            "floating_pin_redundancy(reference.marker, floats=False)",
            "floating_pin_redundancy(reference.marker, floats=None)",
            "a Python dependency's pin reads as one that might float, so an `allow[floating-pin]` keeping nothing "
            "floating is left unreported",
        )
    )
    def test_a_floating_pin_directive_is_reported(self, get: Mock):
        """Test that an `allow[floating-pin]` is reported, since a Python dependency carries no tag that floats."""
        file = self.dependency_toml_file("package==1.0", marker="allow[floating-pin]")
        self.check_dependencies(get, file)
        self.assert_redundant_directive_logged(
            Reason.PIN_NOT_FLOATING, "package", Location(file.path, 2), "allow[floating-pin]"
        )

    @kills(
        Mutation(
            delegated_module,
            "floating_pin_redundancy(reference.marker, floats=False)",
            "floating_pin_redundancy(reference.marker.as_written, floats=False)",
            "the rule reads the marker as written, so a bare `ignore` reads as holding the update back not, and its "
            "`allow[floating-pin]` is reported as a pin that does not float",
        )
    )
    def test_a_floating_pin_directive_is_reported_as_a_held_back_update(self, get: Mock):
        """Test that a held-back update is the reason reported, whichever directive holds the update back."""
        for marker in ("ignore[update] allow[floating-pin]", "ignore allow[floating-pin]"):
            with self.subTest(marker=marker):
                file = self.dependency_toml_file("package==1.0", marker=marker)
                self.check_dependencies(get, file)
                self.assert_redundant_directive_logged(
                    Reason.UPDATE_HELD_BACK, "package", Location(file.path, 2), "allow[floating-pin]"
                )

    @kills(
        Mutation(
            delegated_module,
            "    for reference in chain.from_iterable(declared):\n"
            "        for written, reason in _redundant_directives(reference, no_source_for(reference)):",
            "    references = list(chain.from_iterable(declared))\n"
            "    for reference in references:\n"
            "        for written, reason in _redundant_directives(references[0], no_source_for(references[0])):",
            "one declaration's marker steers every dependency the file declares, rather than the one carrying it",
        )
    )
    def test_a_marker_reports_for_the_declaration_it_steers_and_no_other(self, get: Mock):
        """Test that a marker on one declaration's line leaves the dependency declared below it unreported."""
        file = self.file_holding(pyproject_per_line("package>=1.0", "other>=1.0", marker="ignore[yanked]"))
        self.check_dependencies(get, file)
        self.assert_redundant_directive_logged(
            Reason.NO_VERSION_TO_CHECK_FOR_A_YANK, "package", Location(file.path, 3), "ignore[yanked]"
        )


@patch("requests.get")
class UvSourcedDependencyTest(DependencyTomlFileTestCase):
    """Unit tests for the dependencies uv resolves from a source of its own, which PyPI serves no release for."""

    @kills(
        Mutation(
            uv_pins_module,
            "    served = [uv.pypi_served(declarations) for declarations in declared]",
            "    served = [list(declarations) for declarations in declared]",
            "the checks are handed every declaration, so PyPI is asked about one it serves no release for",
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
        # The first request batches every pin the check looks up; the ones after it follow from what it answered.
        self.assertEqual(mock_post.call_args_list[0].kwargs["json"], osv_queries(("django", "3.2.0")))
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(file.path, 2))
