"""Unit tests for recompiling a requirements file that uv or pip-compile compiled."""

from pathlib import Path
from subprocess import CalledProcessError  # nosec
from typing import TYPE_CHECKING
from unittest.mock import ANY, DEFAULT, Mock, patch

from update_time.domain import version_scheme
from update_time.domain.cooldown import COOLDOWN
from update_time.io.log import Logger
from update_time.lock_files import compiled_requirements_txt
from update_time.lock_files.compiled_requirements_txt import CompiledRequirementsTxt
from update_time.package_managers import moved_versions, pip_tools, requirements_compiler
from update_time.package_managers.uv import uv
from update_time.primitives.command import Command
from update_time.primitives.location import Location
from update_time.updaters.update_requirements_txt import _compiler_for

from tests.helpers import FakeFiles, patch_environ
from tests.mutation import Mutation, kills
from tests.update_time.helpers import message_of, published_version
from tests.update_time.lock_files.helpers import requirements_compiled_by_pip_compile, requirements_compiled_by_uv
from tests.update_time.package_managers.helpers import (
    CONFIGURED_EXCLUDE_NEWER,
    UNREADABLE_SETTINGS,
    UvSettingsTestCase,
)
from tests.update_time.sources.helpers import PYPI_OLD_UPLOAD, pypi_release

if TYPE_CHECKING:
    from collections.abc import Mapping

# The line Update-time writes into a source to say it manages the `exclude-newer` the compiled file records.
_UV_MANAGED_BY_COMMENT_LINE = (
    "# Update-time sets the exclude-newer in the compiled file's header; remove this line to set the "
    "exclude-newer yourself"
)
# The line Update-time writes into a source to say it manages the `uploaded-prior-to` a pip-compile file records.
_PIP_MANAGED_BY_COMMENT_LINE = (
    "# Update-time sets the uploaded-prior-to in the compiled file's header; remove this line to set the "
    "uploaded-prior-to yourself"
)

# The compiled file sits in the directory it was compiled in, and names `base.in` there as its source.
_ROOT = Path.cwd()
_COMPILED = _ROOT / "requirements.txt"
_SOURCE = _ROOT / "base.in"
# `requirements_compiled_by_uv("base.in")` records these arguments in its header, after uv's own words.
_UV_TOOL = ("uv", "pip", "compile")
_UV_HEADER_ARGUMENTS = ("base.in", "-o", "requirements.txt")
_UV_COMMAND = (*_UV_TOOL, *_UV_HEADER_ARGUMENTS)
# `requirements_compiled_by_pip_compile("base.in")` records this command in its header.
_PIP_COMMAND = ("pip-compile", "--output-file=requirements.txt", "base.in")
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
    """Return whether the command is the uv or pip-compile run that recompiles the file, not a settings query."""
    return (command[:3] == _UV_TOOL and "--show-settings" not in command) or command[0] == "pip-compile"


class RecompileTest(UvSettingsTestCase):
    """Unit tests for recompiling a requirements file that uv or pip-compile compiled."""

    # What `pip config list` answers: no setting at all, unless a test sets one or makes pip fail.
    pip_config: object = ""

    def run_tool(self, command: Command, /, **_options: object) -> object:
        """Answer `--show-settings` and `pip config list` with the settings the test sets, the rest with a stub."""
        if command[:3] == ("pip", "config", "list"):
            if isinstance(self.pip_config, BaseException):
                raise self.pip_config
            return Mock(stdout=self.pip_config, stderr="")
        if "--show-settings" not in command:
            return DEFAULT
        exclude_newer = CONFIGURED_EXCLUDE_NEWER if "--exclude-newer" in command else self.configured_exclude_newer
        return self.uv_settings(exclude_newer)

    def recompile(self, files: Mapping[Path, str], compiled: Path = _COMPILED) -> FakeFiles:
        """Recompile the compiled file among the files, and return the files the recompile left."""
        fake_files = FakeFiles(files)
        self.mock_run.reset_mock()  # So a case of a table sees only the processes its own recompile started
        with fake_files.patched():
            _compiler_for(CompiledRequirementsTxt.read(compiled)).recompile()
        return fake_files

    def have_uv_compile(self, contents: str) -> None:
        """Have the stubbed uv write the contents to the compiled file when it compiles."""

        def compile_the_contents(command: Command, **options: object) -> object:
            if _is_the_recompile(command):
                _COMPILED.write_text(contents)
            return self.run_tool(command, **options)

        self.mock_run.side_effect = compile_the_contents

    def have_uv_print(self, contents: str) -> None:
        """Have the stubbed uv print the contents to standard output when it compiles."""

        def print_the_contents(command: Command, **options: object) -> object:
            if _is_the_recompile(command):
                return Mock(stdout=contents, stderr="")
            return self.run_tool(command, **options)

        self.mock_run.side_effect = print_the_contents

    def assert_recompiled(self, *options: str, command: tuple[str, ...] = _UV_COMMAND, cwd: Path = _ROOT) -> None:
        """Assert that the tool ran the command the header records, quietly and with the options, in the directory."""
        recompile = Command(*command, "--quiet", *options)
        self.mock_run.assert_any_call(recompile, capture_output=True, text=True, check=True, cwd=cwd)

    @kills(
        Mutation(
            uv.UvPipCompile.settings_hold_cutoff,
            "self.compiled.compile_directory, self.compiled.path",
            "self.compiled.path.parent, self.compiled.path",
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
                self.assert_recompiled(
                    "--exclude-newer", "7 days", *upgrades, command=(*_UV_TOOL, *header), cwd=directory
                )
                settings = Command(*_UV_TOOL, *header, "--show-settings")
                self.mock_run.assert_any_call(settings, capture_output=True, text=True, check=True, cwd=directory)

    def test_a_file_pip_compile_compiled_is_recompiled_with_the_command_its_header_records(self):
        """Test that pip-compile runs the header's command where the file was compiled, upgrading every pin."""
        compiled = requirements_compiled_by_pip_compile("base.in") + _TRANSITIVE_PIN
        self.recompile({_COMPILED: compiled, _SOURCE: "flask==1.0\n"})
        upgrades = ("--upgrade-package", "flask", "--upgrade-package", "werkzeug")
        self.assert_recompiled("--uploaded-prior-to", "P7D", *upgrades, command=_PIP_COMMAND)

    @kills(
        Mutation(
            compiled_requirements_txt,
            r'(?:uv pip compile|pip-compile)(?:[ \t].*)?)$"',
            r'(?:uv pip compile|pip-compile)\s.*)$"',
            "Update-time reads the next header line into a bare pip-compile command",
        )
    )
    def test_a_file_pip_compile_compiled_without_arguments_is_recompiled_without_arguments(self):
        """Test that a header recording a bare `pip-compile` has pip-compile recompile the file in its directory."""
        source = _ROOT / "requirements.in"
        contents = requirements_compiled_by_pip_compile("requirements.in", output=None)
        bare = contents.replace("#    pip-compile requirements.in\n", "#    pip-compile\n")
        self.recompile({_COMPILED: bare, source: "flask==1.0\n"})
        self.assert_recompiled("--uploaded-prior-to", "P7D", "--upgrade-package", "flask", command=("pip-compile",))

    def test_a_file_pip_compile_compiled_without_an_output_is_recompiled_where_the_named_source_implies(self):
        """Test that pip-compile runs in the directory its named source implies, not its first annotated one."""
        compiled = _ROOT / "sub" / "requirements.txt"
        contents = requirements_compiled_by_pip_compile("sub/requirements.in", output=None)
        contents = contents.replace("flask==1.0\n", "aaa==1.0\n    # via -r sub/base.in\nflask==1.0\n")
        files: dict[Path, str] = {
            compiled: contents,
            _ROOT / "sub" / "requirements.in": "-r base.in\nflask==1.0\n",
            _ROOT / "sub" / "base.in": "aaa==1.0\n",
        }
        self.recompile(files, compiled)
        upgrades = ("--upgrade-package", "aaa", "--upgrade-package", "flask")
        self.assert_recompiled("--uploaded-prior-to", "P7D", *upgrades, command=("pip-compile", "sub/requirements.in"))

    def test_a_file_uv_compiled_to_another_file_does_not_start_a_process(self):
        """Test that a file whose header records another file as its output is left to the user."""
        compiled = requirements_compiled_by_uv("base.in", output="other.txt")
        self.recompile({_COMPILED: compiled, _SOURCE: "flask==1.0\n"})
        self.mock_run.assert_not_called()

    @kills(
        Mutation(
            requirements_compiler.RequirementsCompiler._recompiled,
            "compiled.path.write_text(result.stdout)",
            "pass",
            "a file compiled to standard output keeps its old pins, though uv printed new ones",
        )
    )
    def test_a_file_uv_compiled_to_standard_output_becomes_what_uv_prints(self):
        """Test that uv recompiles a file it printed, in the file's directory, and that what it prints is the file."""
        compiled = requirements_compiled_by_uv("base.in", output=None)
        printed = compiled + "werkzeug==3.1\n    # via flask\n"
        self.have_uv_print(printed)
        files = self.recompile({_COMPILED: compiled, _SOURCE: "flask==1.0\n"})
        self.assert_recompiled(
            "--exclude-newer", "7 days", "--upgrade-package", "flask", command=(*_UV_TOOL, "base.in")
        )
        self.assertEqual(files[_COMPILED], printed)

    @patch_environ({COOLDOWN.name: "14"})
    def test_the_cooldown_is_passed_and_the_managed_by_comment_written_where_the_header_records_no_exclude_newer(self):
        """Test that uv gets the cooldown as its `exclude-newer`, and the source says Update-time manages that."""
        files = self.recompile({_COMPILED: requirements_compiled_by_uv("base.in"), _SOURCE: "flask==1.0\n"})
        self.assert_recompiled("--exclude-newer", "14 days", "--upgrade-package", "flask")
        self.assertEqual(files[_SOURCE], f"{_UV_MANAGED_BY_COMMENT_LINE}\nflask==1.0\n")
        self.assert_logged_among_others(
            message_of(Logger.managed_by_comment_written),
            location=Location(_SOURCE),
            compiled=Location(_COMPILED),
            cooldown="14 days",
            setting="uv exclude-newer",
        )

    @patch_environ({COOLDOWN.name: "14"})
    def test_pip_compile_gets_the_cooldown_and_the_managed_by_comment_written_where_the_header_records_no_cutoff(self):
        """Test that pip-compile gets the cooldown, and the source says Update-time manages its `uploaded-prior-to`."""
        files = self.recompile({_COMPILED: requirements_compiled_by_pip_compile("base.in"), _SOURCE: "flask==1.0\n"})
        self.assert_recompiled("--uploaded-prior-to", "P14D", "--upgrade-package", "flask", command=_PIP_COMMAND)
        self.assertEqual(files[_SOURCE], f"{_PIP_MANAGED_BY_COMMENT_LINE}\nflask==1.0\n")
        self.assert_logged_among_others(
            message_of(Logger.managed_by_comment_written),
            location=Location(_SOURCE),
            compiled=Location(_COMPILED),
            cooldown="P14D",
            setting="pip-compile uploaded-prior-to",
        )

    @patch_environ({COOLDOWN.name: "14"})
    def test_pip_compile_gets_the_cooldown_in_place_of_the_cutoff_in_the_header_where_a_source_carries_the_comment(
        self,
    ):
        """Test that pip-compile gets the cooldown, not an earlier run's cutoff, and the comment is not repeated."""
        compiled = requirements_compiled_by_pip_compile("base.in", options="--uploaded-prior-to=P7D")
        source = f"{_PIP_MANAGED_BY_COMMENT_LINE}\nflask==1.0\n"
        files = self.recompile({_COMPILED: compiled, _SOURCE: source})
        self.assert_recompiled("--uploaded-prior-to", "P14D", "--upgrade-package", "flask", command=_PIP_COMMAND)
        self.assertEqual(files[_SOURCE], source)

    @kills(
        Mutation(
            pip_tools,
            r"^[^.\n]*\.uploaded",
            r"^\w*\.uploaded",
            "Update-time does not see an uploaded-prior-to that pip reads from the environment",
        )
    )
    def test_pip_compile_gets_no_cooldown_where_pips_settings_hold_an_uploaded_prior_to(self):
        """Test that pip-compile does not get the cooldown where pip's config or environment sets its cutoff."""
        cases = {
            "a config file": "global.uploaded-prior-to='P700D'\n",
            "the environment": ":env:.uploaded-prior-to='P700D'\n",
        }
        for case, pip_config in cases.items():
            with self.subTest(case):
                self.pip_config = pip_config
                files = self.recompile(
                    {_COMPILED: requirements_compiled_by_pip_compile("base.in"), _SOURCE: "flask==1.0\n"}
                )
                self.assert_recompiled("--upgrade-package", "flask", command=_PIP_COMMAND)
                self.assertEqual(files[_SOURCE], "flask==1.0\n")

    @kills(
        Mutation(
            pip_tools.PipCompile.settings_hold_cutoff,
            'config.stderr.strip() or "pip failed without saying why"',
            "config.stderr.strip()",
            "the warning about a pip that failed without saying why leaves its reason empty",
        )
    )
    def test_pip_compile_gets_the_cooldown_with_a_warning_where_pips_settings_cannot_be_read(self):
        """Test that pip-compile gets the cooldown, with a warning, where pip does not list its settings."""
        no_pip = FileNotFoundError(2, "No such file or directory", "pip")
        cases = {
            "a pip that fails": (
                CalledProcessError(1, "pip", "", "ERROR: unknown command\n"),
                "ERROR: unknown command",
            ),
            "a pip that fails without saying why": (
                CalledProcessError(1, "pip", "", ""),
                "pip failed without saying why",
            ),
            "no pip on the path": (no_pip, str(no_pip)),
        }
        for case, (answer, reason) in cases.items():
            with self.subTest(case):
                self.pip_config = answer
                files = self.recompile(
                    {_COMPILED: requirements_compiled_by_pip_compile("base.in"), _SOURCE: "flask==1.0\n"}
                )
                self.assert_recompiled("--uploaded-prior-to", "P7D", "--upgrade-package", "flask", command=_PIP_COMMAND)
                self.assertEqual(files[_SOURCE], f"{_PIP_MANAGED_BY_COMMENT_LINE}\nflask==1.0\n")
                self.assert_logged(
                    message_of(Logger.pip_settings_unreadable), location=Location(_COMPILED), reason=reason
                )

    def test_a_cutoff_the_header_records_is_passed_on_to_pip_compile_where_no_source_carries_the_comment(self):
        """Test that pip-compile gets the user's cutoff as the header records it, and the source stays as it is."""
        cutoff = "--uploaded-prior-to=2026-10-01"
        compiled = requirements_compiled_by_pip_compile("base.in", options=cutoff)
        files = self.recompile({_COMPILED: compiled, _SOURCE: "flask==1.0\n"})
        self.assert_recompiled(
            "--upgrade-package", "flask", command=("pip-compile", "--output-file=requirements.txt", cutoff, "base.in")
        )
        self.assertEqual(files[_SOURCE], "flask==1.0\n")

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
                source = f"{_UV_MANAGED_BY_COMMENT_LINE}\nflask==1.0\n"
                files = self.recompile({_COMPILED: compiled, _SOURCE: source})
                self.assert_recompiled("--exclude-newer", "14 days", "--upgrade-package", "flask")
                self.assertEqual(files[_SOURCE], source)
                self.assertEqual(self.records_of(message_of(Logger.managed_by_comment_written)), [])

    def test_an_exclude_newer_the_header_records_is_passed_on_where_no_source_carries_the_managed_by_comment(self):
        """Test that uv gets the `exclude-newer` the user compiled with, and that the source stays without a comment."""
        compiled = requirements_compiled_by_uv("base.in", options="--exclude-newer 2024-06-01")
        files = self.recompile({_COMPILED: compiled, _SOURCE: "flask==1.0\n"})
        header = (*_UV_HEADER_ARGUMENTS, "--exclude-newer", "2024-06-01")
        self.assert_recompiled("--upgrade-package", "flask", command=(*_UV_TOOL, *header))
        self.assertEqual(files[_SOURCE], "flask==1.0\n")

    def test_no_exclude_newer_is_passed_where_the_project_configures_one(self):
        """Test that uv does not get the cooldown where its settings hold an `exclude-newer`, whatever a source says."""
        self.configured_exclude_newer = CONFIGURED_EXCLUDE_NEWER
        cases = {
            "without the managed-by comment": "flask==1.0\n",
            "with it": f"{_UV_MANAGED_BY_COMMENT_LINE}\nflask==1.0\n",
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
                self.assertEqual(files[_SOURCE], f"{_UV_MANAGED_BY_COMMENT_LINE}\nflask==1.0\n")
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
                self.assert_recompiled(
                    "--exclude-newer", "7 days", *upgrades, command=(*_UV_TOOL, "dev.in", *_UV_HEADER_ARGUMENTS)
                )

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
                self.assert_recompiled(
                    "--exclude-newer", "7 days", *upgrades, command=(*_UV_TOOL, "dev.in", *_UV_HEADER_ARGUMENTS)
                )

    def test_a_package_the_source_holds_back_is_not_upgraded(self):
        """Test that uv does not upgrade a package whose marker in the source holds its update back."""
        cases = {
            "ignore[update]": "flask>=1  # update-time: ignore[update]\n",
            "ignore[update] on a name spelled otherwise": "Flask>=1  # update-time: ignore[update]\n",
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
            requirements_compiler.RequirementsCompiler._warn_if_the_bound_is_redundant,
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

    @kills(
        Mutation(
            pip_tools.PipCompile,
            "package_cooldown_option: ClassVar[str | None] = None",
            'package_cooldown_option: ClassVar[str | None] = "--exclude-newer-package"',
            "pip-compile gets uv's package cooldown option, which it rejects",
        )
    )
    def test_pip_compile_gets_no_cooldown_for_a_package_alone(self):
        """Test that pip-compile gets the run's cooldown alone where a requirement's marker sets one of its own."""
        source = "flask>=1  # update-time: set[cooldown=30]\n"
        self.recompile({_COMPILED: requirements_compiled_by_pip_compile("base.in"), _SOURCE: source})
        self.assert_recompiled("--uploaded-prior-to", "P7D", "--upgrade-package", "flask", command=_PIP_COMMAND)

    def test_the_longest_cooldown_among_the_declarations_of_a_package_applies(self):
        """Test that a package both sources declare gets their longest cooldown, the run's for an unmarked one."""
        thirty, sixty = "flask>=1  # update-time: set[cooldown=30]\n", "flask>=1  # update-time: set[cooldown=60]\n"
        three = "flask>=1  # update-time: set[cooldown=3]\n"
        cases = {
            "the longer one annotated last": (thirty, sixty, "flask=60 days"),
            "the longer one annotated first": (sixty, thirty, "flask=60 days"),
            "one in the source annotated first alone": (thirty, "flask>=1\n", "flask=30 days"),
            "one shorter than the run's beside an unmarked one": (three, "flask>=1\n", "flask=7 days"),
        }
        for case, (base, dev, package_cooldown) in cases.items():
            with self.subTest(case):
                self.recompile({_COMPILED: _compiled_from_dev_and_base(), _SOURCE: base, _DEV: f"werkzeug==3.0\n{dev}"})
                cooldowns = ("--exclude-newer", "7 days", "--exclude-newer-package", package_cooldown)
                upgrades = ("--upgrade-package", "flask", "--upgrade-package", "werkzeug")
                self.assert_recompiled(*cooldowns, *upgrades, command=(*_UV_TOOL, "dev.in", *_UV_HEADER_ARGUMENTS))

    def test_a_package_cooldown_the_header_records_is_replaced_by_the_cooldowns_the_markers_set(self):
        """Test that each package cooldown the header records is dropped, and uv gets those the markers set instead."""
        cooldown = ("--exclude-newer", "7 days")
        cases = {
            "a package whose marker sets a cooldown": (
                "--exclude-newer-package flask=60 days",
                "flask>=1  # update-time: set[cooldown=30]\n",
                _UV_HEADER_ARGUMENTS,
                (*cooldown, "--exclude-newer-package", "flask=30 days"),
            ),
            "a package without a marker": (
                "--exclude-newer-package werkzeug=60 days",
                "flask>=1\n",
                _UV_HEADER_ARGUMENTS,
                cooldown,
            ),
            "a package without a marker, beside the user's exclude-newer": (
                "--exclude-newer 2026-10-01 --exclude-newer-package werkzeug=60 days",
                "flask>=1\n",
                (*_UV_HEADER_ARGUMENTS, "--exclude-newer", "2026-10-01"),
                (),
            ),
        }
        for case, (options, source, header, cooldowns) in cases.items():
            with self.subTest(case):
                self.recompile({_COMPILED: requirements_compiled_by_uv("base.in", options=options), _SOURCE: source})
                self.assert_recompiled(*cooldowns, "--upgrade-package", "flask", command=(*_UV_TOOL, *header))

    @kills(
        Mutation(
            requirements_compiler.RequirementsCompiler.recompile,
            "if not is_installed(command.executable):",
            "if False:",
            "Update-time asks a tool missing from the path for its settings, and then runs it",
        ),
        Mutation(
            requirements_compiler.RequirementsCompiler.recompile,
            "self.log.compiler_not_on_path(self.compiled.path, command.executable)",
            "self.log.compiled_file_not_recompiled(self.compiled.path, self.tool)",
            "a tool missing from the path is reported as a tool that failed",
        ),
    )
    def test_a_tool_missing_from_the_path_is_reported_without_asking_for_its_settings(self):
        """Test that Update-time reports a missing pip-compile in one line, and runs neither it nor a settings query."""
        self.leave_off_the_path("pip-compile")
        self.recompile({_COMPILED: requirements_compiled_by_pip_compile("base.in"), _SOURCE: "flask==1.0\n"})
        self.mock_run.assert_not_called()
        self.assert_errors_logged(
            (message_of(Logger.compiler_not_on_path), {"location": Location(_COMPILED), "executable": "pip-compile"}),
        )

    @kills(
        Mutation(
            requirements_compiler.RequirementsCompiler.recompile,
            "self.log.compiled_file_not_recompiled(self.compiled.path, self.tool)",
            'self.log.compiled_file_not_recompiled(self.compiled.path, "uv pip compile")',
            "the error about a failed pip-compile names uv",
        ),
        Mutation(
            requirements_compiler.RequirementsCompiler._recompiled,
            "if not result.succeeded:\n"
            "            return False\n"
            "        if compiled.writes_to_standard_output:\n"
            "            compiled.path.write_text(result.stdout)\n",
            "if compiled.writes_to_standard_output:\n"
            "            compiled.path.write_text(result.stdout)\n"
            "        if not result.succeeded:\n"
            "            return False\n",
            "a failed recompile to standard output empties the file",
        ),
    )
    def test_a_failed_recompile_is_reported_naming_the_tool(self):
        """Test that a failed recompile is reported, naming the tool, and changes neither the source nor the file."""

        def fail_the_compile(command: Command, **options: object) -> object:
            if _is_the_recompile(command):
                raise CalledProcessError(1, command[0], "", "error: No solution found when resolving dependencies")
            return self.run_tool(command, **options)

        cases = {
            "uv pip compile": ("uv pip compile", requirements_compiled_by_uv("base.in")),
            "pip-compile": ("pip-compile", requirements_compiled_by_pip_compile("base.in")),
            "uv pip compile to standard output": (
                "uv pip compile",
                requirements_compiled_by_uv("base.in", output=None),
            ),
        }
        for case, (tool, compiled) in cases.items():
            with self.subTest(case):
                self.mock_run.side_effect = fail_the_compile
                files = self.recompile({_COMPILED: compiled, _SOURCE: "flask==1.0\n"})
                self.assert_error_logged(
                    message_of(Logger.compiled_file_not_recompiled), location=Location(_COMPILED), tool=tool
                )
                self.assertEqual((files[_SOURCE], files[_COMPILED]), ("flask==1.0\n", compiled))
                self.assertEqual(self.records_of(message_of(Logger.managed_by_comment_written)), [])

    @patch("requests.Session.get", Mock(side_effect=[pypi_release(PYPI_OLD_UPLOAD)]))
    def test_a_version_the_recompile_moves_is_reported_at_the_source(self):
        """Test that a new version uv locks for a requirement the source declares is reported on the source's line."""
        recompiled = requirements_compiled_by_uv("base.in").replace("flask==1.0", "flask==1.1")
        self.have_uv_compile(recompiled + "werkzeug==3.1\n    # via flask\n")
        source = f"{_UV_MANAGED_BY_COMMENT_LINE}\nflask>=1\n"
        self.recompile({_COMPILED: requirements_compiled_by_uv("base.in") + _TRANSITIVE_PIN, _SOURCE: source})
        self.assert_new_versions_logged(published_version("1.1", PYPI_OLD_UPLOAD), Location(_SOURCE, 2), "flask")

    @kills(
        Mutation(
            requirements_compiler.RequirementsCompiler._recompiled,
            "self._report_moved_versions(command.executable, declarations)",
            'self._report_moved_versions("uv", declarations)',
            "a version pip-compile moved back is reported as moved back by uv",
        ),
        Mutation(
            moved_versions.log_moved_versions,
            "Reference(pin.name, old, location)",
            "Reference(pin.name, old, locations[name][0])",
            "a version moved back is reported at the first line declaring it alone",
        ),
    )
    @patch("requests.Session.get", return_value=pypi_release(PYPI_OLD_UPLOAD))
    def test_a_version_the_recompile_moves_back_is_reported_as_moved_back_at_each_line_declaring_it(self, get: Mock):
        """Test that a version the tool moves back is reported so at each line declaring it, without a changelog."""
        uv_compiled, pip_compiled = requirements_compiled_by_uv, requirements_compiled_by_pip_compile
        cases = {
            "uv": (uv_compiled, _UV_MANAGED_BY_COMMENT_LINE, "flask>=1\n", "uv", (2,)),
            "pip-compile": (pip_compiled, _PIP_MANAGED_BY_COMMENT_LINE, "flask>=1\n", "pip-compile", (2,)),
            "declared on two lines": (uv_compiled, _UV_MANAGED_BY_COMMENT_LINE, "flask>=1\nflask<2\n", "uv", (2, 3)),
        }
        for case, (compiled_by, managed_by_comment_line, requirements, tool, lines) in cases.items():
            with self.subTest(case):
                self.have_uv_compile(compiled_by("base.in"))
                compiled = compiled_by("base.in").replace("flask==1.0", "flask==1.1")
                self.recompile({_COMPILED: compiled, _SOURCE: f"{managed_by_comment_line}\n{requirements}"})
                locations = (Location(_SOURCE, line) for line in lines)
                self.assert_moved_back_logged(tool, "flask", "1.1", "1.0", *locations)
        get.assert_not_called()

    @kills(
        Mutation(
            version_scheme._pep_440_order,
            "Version(version) if is_valid(version) else None",
            "Version(version)",
            "the recompile parses a pin that is not PEP 440, so the run ends with a traceback",
            raises="packaging.version.InvalidVersion: Invalid version: '2004d'",
        ),
        Mutation(
            moved_versions.log_moved_versions,
            'versions_before.get(name, "")',
            'versions_before.get(name, "9")',
            "the recompile compares a package it pins for the first time with a version the file never pinned",
        ),
    )
    @patch("requests.Session.get", Mock(return_value=pypi_release(PYPI_OLD_UPLOAD)))
    def test_a_version_the_recompile_pins_is_reported_as_new_where_it_does_not_compare_with_the_pin_before(self):
        """Test that a pin is reported as new where the file did not pin it, or pinned it to a non-PEP 440 version."""
        source = f"{_UV_MANAGED_BY_COMMENT_LINE}\nflask>=1\n"
        cases = {
            "from a version that is not PEP 440": ("flask==2004d", "1.0"),
            "from no version": ("werkzeug==3.0", "1.0"),
        }
        for case, (pin_before, new) in cases.items():
            with self.subTest(case):
                self.have_uv_compile(requirements_compiled_by_uv("base.in").replace("flask==1.0", f"flask=={new}"))
                compiled = requirements_compiled_by_uv("base.in").replace("flask==1.0", pin_before)
                self.recompile({_COMPILED: compiled, _SOURCE: source})
                self.assert_new_versions_logged(published_version(new, PYPI_OLD_UPLOAD), Location(_SOURCE, 2), "flask")

    @patch("requests.Session.get", Mock(side_effect=[pypi_release(PYPI_OLD_UPLOAD)]))
    def test_a_version_an_exact_pin_in_the_source_moves_is_not_reported_again(self):
        """Test that the compiled file catching up with an exact pin in the source is not reported again."""
        self.have_uv_compile(requirements_compiled_by_uv("base.in").replace("flask==1.0", "flask==1.1"))
        source = f"{_UV_MANAGED_BY_COMMENT_LINE}\nflask==1.1\n"
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
            (files[_DEV], files[_SOURCE]), (f"{_UV_MANAGED_BY_COMMENT_LINE}\nwerkzeug==3.0\n", "flask==1.0\n")
        )

    @kills(
        Mutation(
            requirements_compiler.RequirementsCompiler._recompiled,
            "for source in compiled.sources)",
            "for source in compiled.sources[:1])",
            "a managed-by comment in a source other than the first is missed, so the cooldown stops being applied",
        )
    )
    @patch_environ({COOLDOWN.name: "14"})
    def test_the_managed_by_comment_counts_in_a_source_other_than_the_first_one_annotated(self):
        """Test that the cooldown replaces the header's `exclude-newer` where a later source carries the comment."""
        dev = f"{_UV_MANAGED_BY_COMMENT_LINE}\nwerkzeug==3.0\n"
        compiled = _compiled_from_dev_and_base("--exclude-newer 7 days")
        self.recompile({_COMPILED: compiled, _SOURCE: "flask==1.0\n", _DEV: dev})
        upgrades = ("--upgrade-package", "flask", "--upgrade-package", "werkzeug")
        self.assert_recompiled(
            "--exclude-newer", "14 days", *upgrades, command=(*_UV_TOOL, "dev.in", *_UV_HEADER_ARGUMENTS)
        )
