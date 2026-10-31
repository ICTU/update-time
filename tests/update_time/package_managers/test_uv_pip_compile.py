"""Unit tests for recompiling a requirements file that uv compiled."""

from pathlib import Path
from subprocess import CalledProcessError  # nosec
from typing import TYPE_CHECKING
from unittest.mock import ANY, DEFAULT, Mock, patch

from update_time.domain.cooldown import COOLDOWN
from update_time.io.log import Logger
from update_time.manifests.requirements_txt import CompiledRequirementsTxt
from update_time.package_managers import uv_pip_compile
from update_time.primitives.command import Command
from update_time.primitives.location import Location

from tests.helpers import FakeFiles, patch_environ
from tests.mutation import Mutation, kills
from tests.update_time.helpers import message_of, published_version
from tests.update_time.manifests.helpers import requirements_compiled_by_pip_compile, requirements_compiled_by_uv
from tests.update_time.package_managers.helpers import (
    CONFIGURED_EXCLUDE_NEWER,
    MANAGED_BY_COMMENT,
    UNREADABLE_SETTINGS,
    UvSettingsTestCase,
)
from tests.update_time.sources.helpers import PYPI_OLD_UPLOAD, pypi_release

if TYPE_CHECKING:
    from collections.abc import Mapping

# The line Update-time writes into a source to say it manages the `exclude-newer` the compiled file records.
_MANAGED_BY_COMMENT_LINE = f"# exclude-newer: {MANAGED_BY_COMMENT}"

# The compiled file sits in the directory it was compiled in, and names `base.in` there as its source.
_ROOT = Path.cwd()
_COMPILED = _ROOT / "requirements.txt"
_SOURCE = _ROOT / "base.in"
# `requirements_compiled_by_uv("base.in")` records these arguments in its header.
_HEADER_ARGUMENTS = ("base.in", "-o", "requirements.txt")
# The compiled file pins werkzeug for flask, while the source does not declare it.
_TRANSITIVE_PIN = "werkzeug==3.0\n    # via flask\n"
# The compiled file can name `dev.in` as a second source, and hold this pin for it.
_DEV = _ROOT / "dev.in"
_DEV_PIN = "werkzeug==3.0\n    # via -r dev.in\n"


def _compiled_from_dev_and_base(options: str = "") -> str:
    """Return a file uv compiled from `dev.in` and `base.in`, in that order, whose annotations name `base.in` first."""
    compiled = requirements_compiled_by_uv("base.in", options=options)
    return compiled.replace("uv pip compile base.in", "uv pip compile dev.in base.in") + _DEV_PIN


def _is_the_recompile(command: Command) -> bool:
    """Return whether the command is the `uv pip compile` that recompiles the file, rather than a settings query."""
    return command[:3] == ("uv", "pip", "compile") and "--show-settings" not in command


class RecompileTest(UvSettingsTestCase):
    """Unit tests for recompiling a requirements file that uv compiled."""

    def run_uv(self, command: Command, /, **_options: object) -> object:
        """Answer `--show-settings` with the `exclude-newer` the command or the project sets, the rest with a stub."""
        if "--show-settings" not in command:
            return DEFAULT
        exclude_newer = CONFIGURED_EXCLUDE_NEWER if "--exclude-newer" in command else self.configured_exclude_newer
        return self.uv_settings(exclude_newer)

    def recompile(self, files: Mapping[str | Path, str], compiled: Path = _COMPILED) -> FakeFiles:
        """Recompile the compiled file among the files, and return the files the recompile left."""
        fake_files = FakeFiles(files)
        self.mock_run.reset_mock()  # So a case of a table sees only the processes its own recompile started
        with fake_files.patched():
            uv_pip_compile.recompile(CompiledRequirementsTxt.read(compiled))
        return fake_files

    def have_uv_compile(self, contents: str) -> None:
        """Have the stubbed uv write the contents to the compiled file when it compiles."""

        def compile_the_contents(command: Command, **options: object) -> object:
            if _is_the_recompile(command):
                _COMPILED.write_text(contents)
            return self.run_uv(command, **options)

        self.mock_run.side_effect = compile_the_contents

    def assert_recompiled(self, *options: str, header: tuple[str, ...] = _HEADER_ARGUMENTS, cwd: Path = _ROOT) -> None:
        """Assert that uv ran the command the header records, quietly and with the options, in the directory."""
        command = Command("uv", "pip", "compile", *header, "--quiet", *options)
        self.mock_run.assert_any_call(command, capture_output=True, text=True, check=True, cwd=cwd)

    @kills(
        Mutation(
            uv_pip_compile._managed_exclude_newer,
            "compiled.compile_directory, compiled.path",
            "compiled.path.parent, compiled.path",
            "uv reads its settings where the compiled file sits rather than where it was compiled",
        )
    )
    def test_a_compiled_file_is_recompiled_with_the_command_its_header_records(self):
        """Test that uv reads its settings for the header's command, and runs it, where the file was compiled."""
        compiled, source = _ROOT / "docs" / "requirements.txt", _ROOT / "docs" / "requirements.in"
        cases = {
            "compiled beside its source": (
                requirements_compiled_by_uv("requirements.in"),
                compiled.parent,
                "requirements",
            ),
            "compiled from another directory": (
                requirements_compiled_by_uv("docs/requirements.in", output="docs/requirements.txt"),
                _ROOT,
                "docs/requirements",
            ),
        }
        for case, (contents, directory, stem) in cases.items():
            with self.subTest(case):
                self.recompile({compiled: contents + _TRANSITIVE_PIN, source: "flask==1.0\n"}, compiled)
                upgrades = ("--upgrade-package", "flask", "--upgrade-package", "werkzeug")
                header = (f"{stem}.in", "-o", f"{stem}.txt")
                self.assert_recompiled("--exclude-newer", "7 days", *upgrades, header=header, cwd=directory)
                settings = Command("uv", "pip", "compile", *header, "--show-settings")
                self.mock_run.assert_any_call(settings, capture_output=True, text=True, check=True, cwd=directory)

    def test_a_compiled_file_uv_cannot_recompile_does_not_start_a_process(self):
        """Test that a file pip-compile compiled, or one uv compiled to standard output, is left to the user."""
        cases = {
            "compiled by pip-compile": requirements_compiled_by_pip_compile("base.in"),
            "compiled by uv to standard output": requirements_compiled_by_uv("base.in", output=None),
        }
        for case, compiled in cases.items():
            with self.subTest(case):
                self.recompile({_COMPILED: compiled, _SOURCE: "flask==1.0\n"})
                self.mock_run.assert_not_called()

    @patch_environ({COOLDOWN.name: "14"})
    def test_the_cooldown_is_passed_and_the_managed_by_comment_written_where_the_header_records_no_exclude_newer(self):
        """Test that uv gets the cooldown as its `exclude-newer`, and the source says Update-time manages that."""
        files = self.recompile({_COMPILED: requirements_compiled_by_uv("base.in"), _SOURCE: "flask==1.0\n"})
        self.assert_recompiled("--exclude-newer", "14 days", "--upgrade-package", "flask")
        self.assertEqual(files[_SOURCE], f"{_MANAGED_BY_COMMENT_LINE}\nflask==1.0\n")
        self.assert_logged_among_others(
            message_of(Logger.managed_by_comment_written),
            location=Location(_SOURCE),
            compiled=Location(_COMPILED),
            cooldown="14 days",
        )

    @patch_environ({COOLDOWN.name: "14"})
    def test_the_cooldown_replaces_the_exclude_newer_the_header_records_where_a_source_carries_the_managed_by_comment(
        self,
    ):
        """Test that uv gets the cooldown, not an earlier run's `exclude-newer`, and the comment is not repeated."""
        cases = {
            "an exclude-newer set by an earlier run": "--exclude-newer 7 days",
            "no exclude-newer": "",
        }
        for case, options in cases.items():
            with self.subTest(case):
                compiled = requirements_compiled_by_uv("base.in", options=options)
                source = f"{_MANAGED_BY_COMMENT_LINE}\nflask==1.0\n"
                files = self.recompile({_COMPILED: compiled, _SOURCE: source})
                self.assert_recompiled("--exclude-newer", "14 days", "--upgrade-package", "flask")
                self.assertEqual(files[_SOURCE], source)
                self.assertEqual(self.records_of(message_of(Logger.managed_by_comment_written)), [])

    def test_an_exclude_newer_the_header_records_is_passed_on_where_no_source_carries_the_managed_by_comment(self):
        """Test that uv gets the `exclude-newer` the user compiled with, and that the source stays without a comment."""
        compiled = requirements_compiled_by_uv("base.in", options="--exclude-newer 2024-06-01")
        files = self.recompile({_COMPILED: compiled, _SOURCE: "flask==1.0\n"})
        header = (*_HEADER_ARGUMENTS, "--exclude-newer", "2024-06-01")
        self.assert_recompiled("--upgrade-package", "flask", header=header)
        self.assertEqual(files[_SOURCE], "flask==1.0\n")

    def test_no_exclude_newer_is_passed_where_the_project_configures_one(self):
        """Test that uv does not get the cooldown where its settings hold an `exclude-newer`, whatever a source says."""
        self.configured_exclude_newer = CONFIGURED_EXCLUDE_NEWER
        cases = {
            "without the managed-by comment": "flask==1.0\n",
            "with it": f"{_MANAGED_BY_COMMENT_LINE}\nflask==1.0\n",
        }
        for case, source in cases.items():
            with self.subTest(case):
                files = self.recompile({_COMPILED: requirements_compiled_by_uv("base.in"), _SOURCE: source})
                self.assert_recompiled("--upgrade-package", "flask")
                self.assertEqual(files[_SOURCE], source)

    def test_the_cooldown_is_passed_where_uvs_settings_cannot_be_read(self):
        """Test that Update-time warns, and passes the cooldown, where uv does not say whether its settings hold one."""
        for case, answer in UNREADABLE_SETTINGS.items():
            with self.subTest(case):
                self.settings_answer = answer
                files = self.recompile({_COMPILED: requirements_compiled_by_uv("base.in"), _SOURCE: "flask==1.0\n"})
                self.assert_recompiled("--exclude-newer", "7 days", "--upgrade-package", "flask")
                self.assertEqual(files[_SOURCE], f"{_MANAGED_BY_COMMENT_LINE}\nflask==1.0\n")
                self.assert_logged_among_others(message_of(Logger.uv_settings_unreadable), location=Location(_COMPILED))

    def test_a_package_one_of_its_declarations_holds_back_is_not_upgraded(self):
        """Test that an `ignore[update]` in either source holds back a package that both sources declare."""
        held_back, declared = "flask>=1  # update-time: ignore[update]\n", "flask>=1\n"
        cases = {
            "in the source annotated first": (held_back, declared),
            "in the source annotated last": (declared, held_back),
        }
        for case, (base, dev) in cases.items():
            with self.subTest(case):
                self.recompile({_COMPILED: _compiled_from_dev_and_base(), _SOURCE: base, _DEV: f"werkzeug==3.0\n{dev}"})
                upgrades = ("--upgrade-package", "werkzeug")
                self.assert_recompiled("--exclude-newer", "7 days", *upgrades, header=("dev.in", *_HEADER_ARGUMENTS))

    def test_the_bounds_of_each_declaration_of_a_package_narrow_its_upgrade_together(self):
        """Test that uv may upgrade a package that both sources declare only to the versions each bound keeps."""
        below_five = "flask>=0.1  # update-time: allow[update<5]\n"
        cases = {
            "a bound in each source": (below_five, "flask>=0.1  # update-time: ignore[update<0.5]\n", "flask<5,>=0.5"),
            "a bound in the source annotated first alone": (below_five, "flask>=0.1\n", "flask<5"),
        }
        for case, (base, dev, upgrade) in cases.items():
            with self.subTest(case):
                self.recompile({_COMPILED: _compiled_from_dev_and_base(), _SOURCE: base, _DEV: f"werkzeug==3.0\n{dev}"})
                upgrades = ("--upgrade-package", upgrade, "--upgrade-package", "werkzeug")
                self.assert_recompiled("--exclude-newer", "7 days", *upgrades, header=("dev.in", *_HEADER_ARGUMENTS))

    def test_a_package_the_source_holds_back_is_not_upgraded(self):
        """Test that uv does not upgrade a package whose marker in the source holds its update back."""
        cases = {
            "ignore[update]": "flask>=1  # update-time: ignore[update]\n",
            "ignore[update] on a name spelled otherwise": "Flask>=1  # update-time: ignore[update]\n",
            "an item Update-time cannot read": "flask>=1  # update-time: ignore[stlae]\n",
            "a bound leaving out the pinned version": "flask>=1  # update-time: allow[update<1]\n",
            "a bound dropping a range from the middle": "flask>=1  # update-time: ignore[update>=2,<3]\n",
            "a bound dropping a compatible release": "flask>=1  # update-time: ignore[update~=2.0]\n",
        }
        for case, source in cases.items():
            with self.subTest(case):
                self.recompile({_COMPILED: requirements_compiled_by_uv("base.in") + _TRANSITIVE_PIN, _SOURCE: source})
                self.assert_recompiled("--exclude-newer", "7 days", "--upgrade-package", "werkzeug")

    def test_a_bound_in_the_source_that_decides_nothing_at_the_pinned_version_is_redundant(self):
        """Test that a bound that never has an effect, or blocks every update, at the pinned version is warned about."""
        for directive in ("allow[update>=0.1]", "ignore[patch-update]"):
            with self.subTest(directive):
                source = f"flask>=0.1  # update-time: {directive}\n"
                self.recompile({_COMPILED: requirements_compiled_by_uv("base.in"), _SOURCE: source})
                self.assert_logged_among_others(
                    message_of(Logger.warn_if_redundant_bound),
                    dependency="flask",
                    location=Location(_SOURCE, 1),
                    directive=directive,
                    version="1.0",
                    redundancy=ANY,
                )

    @kills(
        Mutation(
            uv_pip_compile._warn_if_the_bound_is_redundant,
            "if not declaration.current_version:",
            "if True:",
            "a bound on an exact pin is warned about twice, by the recompile as well as by the update",
        )
    )
    def test_a_bound_in_the_source_is_not_judged_by_the_recompile_where_it_decides_or_sits_on_an_exact_pin(self):
        """Test that the recompile does not warn about a bound keeping some versions, nor about one on an exact pin."""
        cases = {
            "a bound keeping some versions": "flask>=0.1  # update-time: allow[update<2]\n",
            "a bound on an exact pin": "flask==1.0  # update-time: allow[update>=0.1]\n",
        }
        for case, source in cases.items():
            with self.subTest(case):
                self.recompile({_COMPILED: requirements_compiled_by_uv("base.in"), _SOURCE: source})
                self.assertEqual(self.records_of(message_of(Logger.warn_if_redundant_bound)), [])

    def test_a_bound_in_the_source_narrows_the_upgrade(self):
        """Test that uv may upgrade a package only to the versions its bound in the source keeps."""
        cases = {
            "allow[update<2]": "flask<2",
            "ignore[update>=2]": "flask<2",
            "ignore[update>2]": "flask<=2",
            "ignore[update<0.5]": "flask>=0.5",
            "ignore[update<=0.5]": "flask>0.5",
            "ignore[update==2.*]": "flask!=2.*",
            "ignore[update!=1.0]": "flask==1.0",
            "ignore[minor-update]": "flask==1.0.*",
            "allow[minor-update]": "flask==1.*",
        }
        for bound, upgrade in cases.items():
            with self.subTest(bound):
                source = f"flask>=0.1  # update-time: {bound}\n"
                self.recompile({_COMPILED: requirements_compiled_by_uv("base.in"), _SOURCE: source})
                self.assert_recompiled("--exclude-newer", "7 days", "--upgrade-package", upgrade)

    def test_a_cooldown_in_the_source_applies_to_its_package(self):
        """Test that uv gets the cooldown a requirement's marker sets as the `exclude-newer` of that package alone."""
        source = "flask>=1  # update-time: set[cooldown=30]\n"
        self.recompile({_COMPILED: requirements_compiled_by_uv("base.in"), _SOURCE: source})
        cooldowns = ("--exclude-newer", "7 days", "--exclude-newer-package", "flask=30 days")
        self.assert_recompiled(*cooldowns, "--upgrade-package", "flask")

    def test_the_longest_cooldown_among_the_declarations_of_a_package_applies(self):
        """Test that a package both sources declare gets their longest cooldown, the run's for an unmarked one."""
        thirty, sixty = "flask>=1  # update-time: set[cooldown=30]\n", "flask>=1  # update-time: set[cooldown=60]\n"
        cases = {
            "the longer one annotated last": (thirty, sixty, "flask=60 days"),
            "the longer one annotated first": (sixty, thirty, "flask=60 days"),
            "one in the source annotated first alone": (thirty, "flask>=1\n", "flask=30 days"),
        }
        for case, (base, dev, package_cooldown) in cases.items():
            with self.subTest(case):
                self.recompile({_COMPILED: _compiled_from_dev_and_base(), _SOURCE: base, _DEV: f"werkzeug==3.0\n{dev}"})
                cooldowns = ("--exclude-newer", "7 days", "--exclude-newer-package", package_cooldown)
                upgrades = ("--upgrade-package", "flask", "--upgrade-package", "werkzeug")
                self.assert_recompiled(*cooldowns, *upgrades, header=("dev.in", *_HEADER_ARGUMENTS))

    def test_a_package_cooldown_the_header_records_is_replaced_by_the_cooldowns_the_markers_set(self):
        """Test that each package cooldown the header records is dropped, and uv gets those the markers set instead."""
        cooldown = ("--exclude-newer", "7 days")
        cases = {
            "a package whose marker sets a cooldown": (
                "--exclude-newer-package flask=60 days",
                "flask>=1  # update-time: set[cooldown=30]\n",
                _HEADER_ARGUMENTS,
                (*cooldown, "--exclude-newer-package", "flask=30 days"),
            ),
            "a package without a marker": (
                "--exclude-newer-package werkzeug=60 days",
                "flask>=1\n",
                _HEADER_ARGUMENTS,
                cooldown,
            ),
            "a package without a marker, beside the user's exclude-newer": (
                "--exclude-newer 2026-10-01 --exclude-newer-package werkzeug=60 days",
                "flask>=1\n",
                (*_HEADER_ARGUMENTS, "--exclude-newer", "2026-10-01"),
                (),
            ),
        }
        for case, (options, source, header, cooldowns) in cases.items():
            with self.subTest(case):
                self.recompile({_COMPILED: requirements_compiled_by_uv("base.in", options=options), _SOURCE: source})
                self.assert_recompiled(*cooldowns, "--upgrade-package", "flask", header=header)

    def test_a_recompile_uv_fails_is_reported(self):
        """Test that a recompile uv fails is reported as an error, and does not write the managed-by comment."""

        def fail_the_compile(command: Command, **options: object) -> object:
            if _is_the_recompile(command):
                raise CalledProcessError(1, "uv", "", "error: No solution found when resolving dependencies")
            return self.run_uv(command, **options)

        self.mock_run.side_effect = fail_the_compile
        files = self.recompile({_COMPILED: requirements_compiled_by_uv("base.in"), _SOURCE: "flask==1.0\n"})
        self.assert_error_logged(message_of(Logger.compiled_file_not_recompiled), location=Location(_COMPILED))
        self.assertEqual(files[_SOURCE], "flask==1.0\n")
        self.assertEqual(self.records_of(message_of(Logger.managed_by_comment_written)), [])

    @patch("requests.Session.get", Mock(side_effect=[pypi_release(PYPI_OLD_UPLOAD)]))
    def test_a_version_the_recompile_moves_is_reported_at_the_source(self):
        """Test that a new version uv locks for a requirement the source declares is reported on the source's line."""
        recompiled = requirements_compiled_by_uv("base.in").replace("flask==1.0", "flask==1.1")
        self.have_uv_compile(recompiled + "werkzeug==3.1\n    # via flask\n")
        source = f"{_MANAGED_BY_COMMENT_LINE}\nflask>=1\n"
        self.recompile({_COMPILED: requirements_compiled_by_uv("base.in") + _TRANSITIVE_PIN, _SOURCE: source})
        self.assert_new_versions_logged(published_version("1.1", PYPI_OLD_UPLOAD), Location(_SOURCE, 2), "flask")

    @patch("requests.Session.get", Mock(side_effect=[pypi_release(PYPI_OLD_UPLOAD)]))
    def test_a_version_an_exact_pin_in_the_source_moves_is_not_reported_again(self):
        """Test that the compiled file catching up with an exact pin in the source is not reported again."""
        self.have_uv_compile(requirements_compiled_by_uv("base.in").replace("flask==1.0", "flask==1.1"))
        source = f"{_MANAGED_BY_COMMENT_LINE}\nflask==1.1\n"
        self.recompile({_COMPILED: requirements_compiled_by_uv("base.in"), _SOURCE: source})
        self.assert_no_new_version_logged()

    def test_a_package_whose_pinned_version_cannot_be_read_is_not_upgraded(self):
        """Test that a package pinned to a version that is not PEP 440 is held back with a warning."""
        compiled = requirements_compiled_by_uv("base.in").replace("flask==1.0", "flask==2004d") + _TRANSITIVE_PIN
        self.recompile({_COMPILED: compiled, _SOURCE: "flask>=1\n"})
        self.assert_recompiled("--exclude-newer", "7 days", "--upgrade-package", "werkzeug")
        self.assert_logged_among_others(
            message_of(Logger.pinned_version_unreadable),
            location=Location(_COMPILED),
            dependency="flask",
            version="2004d",
        )

    def test_the_managed_by_comment_goes_into_the_first_source_the_command_names(self):
        """Test that the comment goes into the source the header's command names first, not the one annotated first."""
        files = self.recompile(
            {_COMPILED: _compiled_from_dev_and_base(), _SOURCE: "flask==1.0\n", _DEV: "werkzeug==3.0\n"}
        )
        self.assertEqual(
            (files[_DEV], files[_SOURCE]), (f"{_MANAGED_BY_COMMENT_LINE}\nwerkzeug==3.0\n", "flask==1.0\n")
        )

    @kills(
        Mutation(
            uv_pip_compile.recompile,
            "_carries_the_managed_by_comment(compiled.sources)",
            "_carries_the_managed_by_comment(compiled.sources[:1])",
            "a managed-by comment in a source other than the first is missed, so the cooldown stops being applied",
        )
    )
    @patch_environ({COOLDOWN.name: "14"})
    def test_the_managed_by_comment_counts_in_a_source_other_than_the_first_one_annotated(self):
        """Test that the cooldown replaces the header's `exclude-newer` where a later source carries the comment."""
        dev = f"{_MANAGED_BY_COMMENT_LINE}\nwerkzeug==3.0\n"
        compiled = _compiled_from_dev_and_base("--exclude-newer 7 days")
        self.recompile({_COMPILED: compiled, _SOURCE: "flask==1.0\n", _DEV: dev})
        upgrades = ("--upgrade-package", "flask", "--upgrade-package", "werkzeug")
        self.assert_recompiled("--exclude-newer", "14 days", *upgrades, header=("dev.in", *_HEADER_ARGUMENTS))
