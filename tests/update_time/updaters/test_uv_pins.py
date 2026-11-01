"""Unit tests for the checks the updaters that delegate to uv run over the dependencies their files declare."""

from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock, patch

from update_time.domain.dependency import Yank
from update_time.formats import toml as toml_module
from update_time.io.log import Logger, get_logger
from update_time.manifests.inline_script import InlineScript
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.markers.directive import Reason
from update_time.package_managers import uv as uv_module
from update_time.primitives.location import Location
from update_time.references import delegated as delegated_module
from update_time.updaters.uv_pins import warn_about_pins

from tests.helpers import mock_path, requested_urls
from tests.mutation import Mutation, kills
from tests.update_time.helpers import (
    LoggingTestCase,
    message_of,
    pyproject,
    staleness_disabled,
)
from tests.update_time.sources.helpers import PYPI_OLD_UPLOAD, pypi_index, pypi_release, yanked_file
from tests.update_time.updaters.fixtures import (
    ADVISORY,
    DJANGO_ADVISORY,
    DJANGO_VULNERABILITY,
    OTHER_DJANGO_ADVISORY,
    OTHER_DJANGO_VULNERABILITY,
    PYPI_PAST_COOLDOWN_UPLOAD,
    PYPI_RECENT_UPLOAD,
    VULNERABILITY,
)
from tests.update_time.updaters.helpers import (
    dated_pypi_index,
    no_vulnerabilities,
    osv,
    osv_queries,
    pyproject_per_line,
)

_LOG = get_logger("uv pins")

# The `[tool.uv] sources` table naming `local`, so uv resolves that dependency from a path and PyPI serves it not.
_UV_SOURCE = '[tool.uv.sources]\nlocal = {path = "../local"}\n'

# The yank pass asks PyPI for every pin, whichever of the checks a test is about, so the tests that are about
# another check answer it with an index that lists no version and no distribution file to read a yank from.
_no_yanks = patch("requests.Session.get", Mock(return_value=pypi_index()))


class PythonDependencyTomlTestCase(LoggingTestCase):
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
@patch("requests.Session.get")
class StaleDependencyTest(PythonDependencyTomlTestCase):
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
            toml_module.parse_document,
            "except tomlkit.exceptions.TOMLKitError:",
            "except tomlkit.exceptions.ParseError:",
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
        """Test that a `set[stale-after=0]` marker silences the staleness warning, and is reported as silencing it."""
        get.return_value = dated_pypi_index("1.0")
        file = self.dependency_toml_file("package==1.0", marker="set[stale-after=0]")
        warn_about_pins([file], _LOG)
        self.assert_no_warnings_logged()
        self.assert_ignored_staleness_logged("package", Location(file.path, 2), "set[stale-after=0]")

    def test_a_marker_silences_the_declaration_it_steers_and_no_other(self, get: Mock):
        """Test that a marker on one declaration's line leaves the dependency declared below it warned about."""
        get.return_value = dated_pypi_index("1.0")
        file = self.file_holding(pyproject_per_line("package==1.0", "other==1.0", marker="set[stale-after=0]"))
        warn_about_pins([file], _LOG)
        self.assert_stale_dependency_logged("other", "1.0", Location(file.path, 4))
        self.assert_no_staleness_warning_logged("package", Location(file.path, 3))

    def test_the_markers_threshold_is_used(self, get: Mock):
        """Test that a pin carrying a staleness threshold of its own is judged by that one, not by the run's.

        The newest release is 100 days old, which is stale against the marker's 90 and not against the run's 365,
        so the warning is given only when the marker's threshold is the one applied.
        """
        get.return_value = dated_pypi_index("1.0", upload_time=PYPI_PAST_COOLDOWN_UPLOAD)
        file = self.dependency_toml_file("package==1.0", marker="set[stale-after=90]")
        warn_about_pins([file], _LOG)
        self.assert_stale_dependency_logged("package", "1.0", Location(file.path, 2))

    def test_recent_pin_not_warned(self, get: Mock):
        """Test that a pin whose newest release is recent is not warned about as stale."""
        get.return_value = dated_pypi_index("1.0", upload_time=PYPI_RECENT_UPLOAD)
        warn_about_pins([self.dependency_toml_file("package==1.0")], _LOG)
        self.assert_no_warnings_logged()


@no_vulnerabilities
@patch("requests.Session.get")
class YankedPinTest(PythonDependencyTomlTestCase):
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
        """Test that an `ignore[yanked]` silences the warning, and is reported as silencing it."""
        file = self.check_a_yanked_pin(get, "ignore[yanked]")
        self.assert_no_warnings_logged()
        self.assert_ignored_yank_logged("package", Location(file.path, 2), "ignore[yanked]")

    @kills(
        Mutation(
            delegated_module.pin_resolver,
            "yank=get_yank(pin.dependency, pin.current_version))",
            "yank=get_yank(pin.dependency, pin.current_version)), marker=next(iter(pins)).marker",
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
        """Test that a pin `ignore[update]` holds back still reports the yank of the version it is left on."""
        file = self.check_a_yanked_pin(get, "ignore[update]")
        self.assert_yanked_dependency_logged("package", "1.0", Location(file.path, 2))

    @staleness_disabled
    def test_yanked_pin_warned_with_the_staleness_check_off(self, get: Mock):
        """Test that `--stale-after 0` leaves the yank check running, so the pin is still warned about."""
        file = self.check_a_yanked_pin(get)
        self.assert_yanked_dependency_logged("package", "1.0", Location(file.path, 2))


@_no_yanks
@staleness_disabled
class VulnerablePinTest(PythonDependencyTomlTestCase):
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
        """Test that a `set[vulnerability-level=none]` marker silences the warning, and is reported as silencing it."""
        file = self.dependency_toml_file("django==3.2.0", marker="set[vulnerability-level=none]")
        self.check_pins(file, DJANGO_ADVISORY)
        self.assert_no_warnings_logged()
        self.assert_ignored_vulnerability_logged(
            "django", Location(file.path, 2), DJANGO_VULNERABILITY.advisory, "set[vulnerability-level=none]"
        )

    def test_a_marker_naming_an_advisory_silences_that_one_alone(self):
        """Test that an `ignore[vulnerable=ID]` marker leaves the pin's other advisories warned about."""
        advisory = DJANGO_VULNERABILITY.advisory
        directive = f"ignore[vulnerable={advisory}]"
        file = self.dependency_toml_file("django==3.2.0", marker=directive)
        self.check_pins(file, DJANGO_ADVISORY, OTHER_DJANGO_ADVISORY)
        self.assert_vulnerable_dependency_logged("django", "3.2.0", OTHER_DJANGO_VULNERABILITY, Location(file.path, 2))
        self.assert_ignored_vulnerability_logged("django", Location(file.path, 2), advisory, directive)

    def test_a_marker_silences_its_own_pin_in_the_batch(self):
        """Test that a marker on one pin's line leaves the pin declared below it in the batch warned about."""
        directive = "set[vulnerability-level=none]"
        file = self.file_holding(pyproject_per_line("package==1.0", "other==1.0", marker=directive))
        self.check_pins(file, ADVISORY)
        self.assert_vulnerable_dependency_logged("other", "1.0", VULNERABILITY, Location(file.path, 4))
        self.assert_ignored_vulnerability_logged("package", Location(file.path, 3), VULNERABILITY.advisory, directive)

    def test_the_markers_risk_level_is_used(self):
        """Test that a pin's own risk level decides what it is warned about, rather than the level of the run."""
        file = self.dependency_toml_file("django==3.2.0", marker="set[vulnerability-level=high]")
        self.check_pins(file, DJANGO_ADVISORY, OTHER_DJANGO_ADVISORY)
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(file.path, 2))

    def test_every_redundant_suppression_is_reported(self):
        """Test that each of a marker's vulnerability suppressions is reported when it silences nothing."""
        advisory = f"ignore[vulnerable={VULNERABILITY.advisory}]"
        file = self.dependency_toml_file("package==1.0", marker=f"{advisory} set[vulnerability-level=high]")
        self.check_pins(file)  # OSV answers with no vulnerability, so both forms silence nothing.
        location = Location(file.path, 2)
        self.assert_redundant_vulnerable_advisory_logged("package", "1.0", location, advisory)
        self.assert_redundant_vulnerable_level_logged(
            "package", "1.0", "high", location, "set[vulnerability-level=high]"
        )


@no_vulnerabilities
@patch("requests.Session.get")
class ArchivedDependencyTest(PythonDependencyTomlTestCase):
    """Unit tests for the archival check, which reads the project status PyPI publishes for each dependency."""

    def test_archived_dependency_without_an_exact_pin_warned(self, get: Mock):
        """Test that a dependency declared without an exact pin is warned about, at the line declaring it."""
        get.return_value = dated_pypi_index("1.0", upload_time=PYPI_RECENT_UPLOAD, archived=True)
        file = self.dependency_toml_file("package>=1.0")
        warn_about_pins([file], _LOG)
        self.assert_archived_dependency_logged("package", Location(file.path, 2))

    def test_archived_dependency_a_marker_silences_not_warned(self, get: Mock):
        """Test that an `ignore[archived]` marker silences the archival warning, and is reported as silencing it."""
        get.return_value = dated_pypi_index("1.0", upload_time=PYPI_RECENT_UPLOAD, archived=True)
        file = self.dependency_toml_file("package==1.0", marker="ignore[archived]")
        warn_about_pins([file], _LOG)
        self.assert_no_warnings_logged()
        self.assert_ignored_archival_logged("package", Location(file.path, 2))

    def test_a_marker_silences_a_dependency_that_pins_no_version(self, get: Mock):
        """Test that both project scopes silence their warning for a dependency declared without an exact pin."""
        cases = {
            "set[stale-after=0]": dated_pypi_index("1.0"),
            "ignore[archived]": dated_pypi_index("1.0", upload_time=PYPI_RECENT_UPLOAD, archived=True),
        }
        for marker, index in cases.items():
            with self.subTest(marker=marker):
                get.return_value = index
                warn_about_pins([self.dependency_toml_file("package>=1.0", marker=marker)], _LOG)
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
            uv_module.pypi_projects,
            "yield SteeredResolvedReference.from_reference(declaration, release=release)",
            "if release.project.newest is not None:\n"
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
@patch("requests.Session.get")
class RedundantDirectiveTest(PythonDependencyTomlTestCase):
    """Unit tests for the directives that hold nothing back for the dependency carrying them."""

    def check_dependencies(self, get: Mock, file: PyprojectToml) -> None:
        """Run the checks over the file, with PyPI listing an undated release, so nothing is warned about as stale."""
        get.return_value = pypi_index("1.0")
        warn_about_pins([file], _LOG)

    def test_a_scope_needing_a_version_is_redundant_without_an_exact_pin(self, get: Mock):
        """Test that a scope whose check needs a version reports that the dependency pins none."""
        vulnerable = Reason.NO_VERSION_TO_CHECK_FOR_A_VULNERABILITY
        for directive, reason in {
            "ignore[yanked]": Reason.NO_VERSION_TO_CHECK_FOR_A_YANK,
            "set[vulnerability-level=none]": vulnerable,
            f"ignore[vulnerable={VULNERABILITY.advisory}]": vulnerable,
            "set[vulnerability-level=high]": vulnerable,
        }.items():
            with self.subTest(directive=directive):
                file = self.dependency_toml_file("package>=1.0", marker=directive)
                self.check_dependencies(get, file)
                self.assert_redundant_directive_logged(reason, "package", Location(file.path, 2), directive)

    def test_a_cooldown_is_reported_as_applying_per_run(self, get: Mock):
        """Test that a `cooldown` item is reported for either spec, since uv takes one per run."""
        for spec in ("package==1.0", "package>=1.0"):
            with self.subTest(spec=spec):
                file = self.dependency_toml_file(spec, marker="set[cooldown=30]")
                self.check_dependencies(get, file)
                self.assert_redundant_directive_logged(
                    Reason.COOLDOWN_PER_RUN, "package", Location(file.path, 2), "set[cooldown=30]"
                )

    def test_an_ignore_update_is_reported_when_it_keeps_no_pin(self, get: Mock):
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

    def test_an_ignore_update_keeping_an_unserved_pin_is_not_reported(self, get: Mock):
        """Test that an `ignore[update]` on two dependencies is reported for the one that does not pin a version."""
        file = self.file_holding(pyproject("local==1.0", "other>=1.0", marker="ignore[update]") + _UV_SOURCE)
        self.check_dependencies(get, file)
        self.assert_redundant_directive_logged(
            Reason.MANAGER_RESOLVES_THE_VERSION, "other", Location(file.path, 2), "ignore[update]"
        )

    @kills(
        Mutation(
            uv_module._unchecked_scopes,
            "return {} if declaration.names_a_release else ManagerPolicy.every_warning_scope(Reason.NO_PYPI_RELEASE)",
            "return {}",
            "a dependency PyPI serves no release for is judged as if it did, so a warning scope on it is reported "
            "in the wrong words or not at all",
        )
    )
    def test_a_scope_is_reported_for_a_dependency_pypi_serves_no_release_for(self, get: Mock):
        """Test that each warning scope is reported for a dependency PyPI is asked about nowhere."""
        for directive in ("set[stale-after=0]", "ignore[yanked]", "set[vulnerability-level=none]", "ignore[archived]"):
            for kind, contents in self.unserved_declarations(directive).items():
                with self.subTest(directive=directive, kind=kind):
                    file = self.file_holding(contents)
                    self.check_dependencies(get, file)
                    self.assert_redundant_directive_logged(
                        Reason.NO_PYPI_RELEASE, "local", Location(file.path, 2), directive
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
            delegated_module._redundant_update_directives,
            "if bound := reference.marker.version_bound_directive:",
            "if not reference.marker.ignores(Scope.UPDATE) and (bound := reference.marker.version_bound_directive):",
            "the `ignore[update]` beside a bound hides it, so the directive that decides nothing goes unreported",
        )
    )
    def test_a_bound_beside_an_ignore_update_is_reported(self, get: Mock):
        """Test that a bound on a pin is reported, though the `ignore[update]` beside it does keep that pin."""
        file = self.dependency_toml_file("package==1.0", marker="ignore[update] allow[update<5]")
        self.check_dependencies(get, file)
        self.assert_redundant_directive_logged(
            Reason.BOUND_DECIDES_NOTHING, "package", Location(file.path, 2), "allow[update<5]"
        )

    def test_an_opt_in_directive_is_reported(self, get: Mock):
        """Test that each opt-in is reported for the reason it decides nothing, on an exact pin and on a range."""
        cases = {
            "floating pin on an exact pin": ("package==1.0", "allow[floating-pin]", Reason.PIN_NOT_FLOATING),
            "floating pin on a range": ("package>=1.0", "allow[floating-pin]", Reason.RANGE_NEVER_PINNED),
            "hash drift on an exact pin": ("package==1.0", "allow[hash-drift]", Reason.NO_HASH_PIN),
        }
        for case, (spec, directive, reason) in cases.items():
            with self.subTest(case=case):
                file = self.dependency_toml_file(spec, marker=directive)
                self.check_dependencies(get, file)
                self.assert_redundant_directive_logged(reason, "package", Location(file.path, 2), directive)

    def test_a_hash_drift_directive_beside_an_ignore_update_that_holds_nothing_back(self, get: Mock):
        """Test that an `allow[hash-drift]` on a range keeps its own reason beside an `ignore[update]`."""
        file = self.dependency_toml_file("package>=1.0", marker="ignore[update] allow[hash-drift]")
        self.check_dependencies(get, file)
        redundant = message_of(Logger.redundant_directive)
        fields = {"dependency": "package", "location": Location(file.path, 2)}
        self.assert_logged_among_others(redundant, directive="allow[hash-drift]", reason=Reason.NO_HASH_PIN, **fields)
        self.assert_logged_among_others(
            redundant, directive="ignore[update]", reason=Reason.MANAGER_RESOLVES_THE_VERSION, **fields
        )
        self.assert_none_logged(redundant, "update held back", reason=Reason.DRIFT_NEVER_ADOPTED)

    def test_a_marker_reports_for_the_declaration_it_steers_and_no_other(self, get: Mock):
        """Test that a marker on one declaration's line leaves the dependency declared below it unreported."""
        file = self.file_holding(pyproject_per_line("package>=1.0", "other>=1.0", marker="ignore[yanked]"))
        self.check_dependencies(get, file)
        self.assert_redundant_directive_logged(
            Reason.NO_VERSION_TO_CHECK_FOR_A_YANK, "package", Location(file.path, 3), "ignore[yanked]"
        )


@patch("requests.Session.get")
class UvSourcedDependencyTest(PythonDependencyTomlTestCase):
    """Unit tests for the dependencies uv resolves from a source of its own, which PyPI serves no release for."""

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
        self.assertEqual(requested_urls(get), ["https://pypi.org/simple/django/"])
        # The first request batches every pin the check looks up; the ones after it follow from what it answered.
        self.assertEqual(mock_post.call_args_list[0].kwargs["json"], osv_queries(("django", "3.2.0")))
        self.assert_vulnerable_dependency_logged("django", "3.2.0", DJANGO_VULNERABILITY, Location(file.path, 2))
