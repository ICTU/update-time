"""Unit tests for the pom.xml updater."""

import contextlib
import re
from pathlib import Path
from subprocess import CalledProcessError  # nosec
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from update_time.domain.cooldown import COOLDOWN
from update_time.io.log import Logger
from update_time.manifests import pom_xml as pom_xml_module
from update_time.package_managers import maven as maven_module
from update_time.primitives.command import Command
from update_time.primitives.location import Location
from update_time.updaters import update_pom_xml as update_pom_xml_module
from update_time.updaters.update_pom_xml import update_pom_xmls

from tests.helpers import mock_path, patch_environ, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.helpers import LoggingTestCase

if TYPE_CHECKING:
    from collections.abc import Iterator

# The rule set file these tests hand Update-time, standing in for the temporary file a real run writes.
_RULES = Path("/rules.xml")


def _dependency(group: str, artifact: str, version: str) -> str:
    """Return a `<dependency>` element declaring the group, the artifact, and the version."""
    parts = {"groupId": group, "artifactId": artifact, "version": version}
    declared = "".join(f"      <{tag}>{value}</{tag}>\n" for tag, value in parts.items())
    return f"    <dependency>\n{declared}    </dependency>\n"


def _guava(version: str) -> str:
    """Return the `<dependency>` element declaring guava, at the given version."""
    return _dependency("com.google.guava", "guava", version)


def _properties(values: dict[str, str]) -> str:
    """Return a `<properties>` element declaring the given names and values, the first of them on line 3."""
    declared = "".join(f"    <{name}>{value}</{name}>\n" for name, value in values.items())
    return f"  <properties>\n{declared}  </properties>\n"


def _managed(*dependencies: str) -> str:
    """Return a `<dependencyManagement>` element declaring the given dependency elements."""
    declared = "".join(dependencies)
    return f"  <dependencyManagement>\n    <dependencies>\n{declared}    </dependencies>\n  </dependencyManagement>\n"


def _plugin(artifact: str, version: str, group: str = "org.apache.maven.plugins") -> str:
    """Return a `<plugin>` element declaring the artifact, the version, and the group where one is given."""
    parts = ({"groupId": group} if group else {}) | {"artifactId": artifact, "version": version}
    declared = "".join(f"        <{tag}>{value}</{tag}>\n" for tag, value in parts.items())
    return f"      <plugin>\n{declared}      </plugin>\n"


def _build(*plugins: str) -> str:
    """Return a `<build>` element declaring the given plugin elements."""
    return f"  <build>\n    <plugins>\n{''.join(plugins)}    </plugins>\n  </build>\n"


def _pom(*dependencies: str, properties: str = "", managed: str = "", build: str = "") -> str:
    """Return a pom declaring the given properties, managed dependencies, dependency elements, and build plugins."""
    return (
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        f"{properties}"
        f"{managed}"
        "  <dependencies>\n"
        f"{''.join(dependencies)}"
        "  </dependencies>\n"
        f"{build}"
        "</project>\n"
    )


def _plugin_version() -> str:
    """Return the versions plugin release the pom Update-time ships declares, read off that pom's own text."""
    pom = (Path(maven_module.__file__).parent / "pom.xml").read_text()
    declared = re.search(r"<versions\.plugin\.version>(?P<version>[^<]+)</versions\.plugin\.version>", pom)
    return declared["version"] if declared else ""


def _rule(group: str, artifact: str, *versions: str) -> str:
    """Return the rule Update-time writes for the artefact, naming the given versions."""
    ignored = "".join(f"        <ignoreVersion>{version}</ignoreVersion>\n" for version in versions)
    return (
        f'    <rule groupId="{group}" artifactId="{artifact}">\n'
        f"      <ignoreVersions>\n{ignored}      </ignoreVersions>\n"
        "    </rule>\n"
    )


def _rule_set(*rules: str) -> str:
    """Return the rule set Update-time writes for Maven, holding the given rules."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ruleset xmlns="http://mojo.codehaus.org/versions-maven-plugin/rule/2.0.0">\n'
        f"  <rules>\n{''.join(rules)}  </rules>\n"
        "</ruleset>\n"
    )


def _maven_command(executable: str = "mvn", rules: Path | None = None) -> Command:
    """Return the command Update-time runs over a pom: the plugin's two goals, under the shipped pom's version.

    A run holding versions back names the rule set file that lists them; a run holding none back names nothing.
    """
    rule_set = (f"-Dmaven.version.rules={rules.as_uri()}",) if rules else ()
    return Command(
        executable,
        "--batch-mode",
        "--no-transfer-progress",
        "--non-recursive",
        "--update-snapshots",
        "-DgenerateBackupPoms=false",
        "-Dmaven.version.ignore=(?i).*[-.](alpha|beta|rc|cr|m|pre|preview)[-.]?[0-9]*",
        *rule_set,
        f"org.codehaus.mojo:versions-maven-plugin:{_plugin_version()}:use-latest-releases",
        f"org.codehaus.mojo:versions-maven-plugin:{_plugin_version()}:update-properties",
    )


# No file sits beside the pom unless a test puts one there, so the wrapper is absent except where it is the case.
# The repository answers with an empty tuple unless a test says otherwise. Stubbing it keeps the run off the network.
@patch.object(maven_module, "versions_within_cooldown", Mock(return_value=()))
@patch_pathlib_path("rglob", cwd=Path("/"), exists=False)
@patch("subprocess.run")
class UpdatePomXmlTest(LoggingTestCase):
    """Unit tests for finding the pom.xml files and running Maven over each of them."""

    @staticmethod
    def find_poms(mock_run: Mock, mock_glob: Mock, *contents: str) -> list[Mock]:
        """Discover a mock pom.xml per given contents, with Maven stubbed to run without output."""
        poms = [mock_path(text, parent=Path("/"), name="pom.xml") for text in contents]
        mock_glob.return_value = poms
        mock_run.return_value = Mock(stdout="", stderr="")
        return poms

    def find_pom(self, mock_run: Mock, mock_glob: Mock, contents: str = "<project/>") -> Mock:
        """Discover a single mock pom.xml holding the contents, with Maven stubbed to run without output."""
        return self.find_poms(mock_run, mock_glob, contents)[0]

    def find_rewritten_pom(self, mock_run: Mock, mock_glob: Mock, before: str, after: str) -> Mock:
        """Discover a single mock pom.xml that Maven rewrites, so it reads as `before` until Maven runs."""

        def contents() -> bytes:
            """Return what the pom holds, which Maven's run changes."""
            return (after if mock_run.called else before).encode()

        pom = self.find_pom(mock_run, mock_glob, before)
        pom.read_bytes = Mock(side_effect=contents)
        return pom

    def assert_maven_ran(self, mock_run: Mock, executable: str = "mvn", rules: Path | None = None) -> None:
        """Assert that the Maven command ran once, with the given executable, in the pom's own directory."""
        command = _maven_command(executable, rules)
        mock_run.assert_called_once_with(command, capture_output=True, text=True, check=True, cwd=Path("/"))

    @contextlib.contextmanager
    def asked_about(self) -> Iterator[list[str]]:
        """Collect the artefacts the run asks the repository about, and do not hold any version back."""
        artefacts: list[str] = []

        def versions_within_cooldown(artefact: str, _days: int) -> tuple[str, ...]:
            artefacts.append(artefact)
            return ()

        with patch.object(maven_module, "versions_within_cooldown", versions_within_cooldown):
            yield artefacts

    @contextlib.contextmanager
    def hold_back(
        self, versions: dict[str, tuple[str, ...]], cooldown_days: int = COOLDOWN.default
    ) -> Iterator[list[str]]:
        """Hold the named versions back per artefact, and collect the rule sets Update-time writes for Maven.

        The stub answers with the named versions only where the window is `cooldown_days`, and with an empty tuple
        otherwise. The rule set file of a real run is gone by the time the run ends, so the file is stood in for here.
        """
        written: list[str] = []

        @contextlib.contextmanager
        def rule_set_file(rule_set: str) -> Iterator[Path]:
            written.append(rule_set)
            yield _RULES

        def versions_within_cooldown(artefact: str, days: int) -> tuple[str, ...]:
            return versions.get(artefact, ()) if days == cooldown_days else ()

        repository = Mock(side_effect=versions_within_cooldown)
        with (
            patch.object(maven_module, "versions_within_cooldown", repository),
            patch.object(maven_module, "_rule_set_file", rule_set_file),
        ):
            yield written

    def test_maven_runs_in_the_poms_own_directory(self, mock_run: Mock, mock_glob: Mock):
        """Test that Maven runs both goals of the versions plugin Update-time names, in the pom's own directory."""
        self.find_pom(mock_run, mock_glob)
        update_pom_xmls()
        self.assert_maven_ran(mock_run)

    @kills(
        Mutation(
            maven_module,
            '        f\'    <rule groupId="{group_id}" artifactId="{artifact_id}">\\n\'\n',
            '        \'    <rule groupId="*" artifactId="*">\\n\'\n',
            "a version held back for one artefact is held back for every artefact, since each rule matches them all",
        )
    )
    def test_each_artefact_gets_a_rule_naming_its_own_held_back_versions(self, mock_run: Mock, mock_glob: Mock):
        """Test that the rule set Maven reads names each artefact's held-back versions under that artefact alone."""
        pom = _pom(_guava("33.0.0-jre"), _dependency("org.springframework", "spring-core", "6.1.0"))
        self.find_pom(mock_run, mock_glob, pom)
        held_back = {
            "com.google.guava:guava": ("33.7.0-jre", "33.7.1-jre"),
            "org.springframework:spring-core": ("7.1.0",),
        }
        with self.hold_back(held_back) as rule_sets:
            update_pom_xmls()
        guava = _rule("com.google.guava", "guava", "33.7.0-jre", "33.7.1-jre")
        spring = _rule("org.springframework", "spring-core", "7.1.0")
        self.assertEqual(rule_sets, [_rule_set(guava, spring)])
        self.assert_maven_ran(mock_run, rules=_RULES)

    @kills(
        Mutation(
            pom_xml_module,
            "    return [artefact for artefact in named if not _PROPERTY_REFERENCE.search(artefact)]\n",
            "    return list(named)\n",
            "a pom inheriting a group from its parent is asked about coordinates the repository does not serve",
        )
    )
    def test_coordinates_naming_a_property_are_not_asked_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that the repository is not asked about an artefact whose coordinates name a property the pom lacks."""
        # A pom can spell a dependency's group as a property its parent declares, which this pom does not hold.
        inherited = _dependency("${spring.group}", "spring-core", "6.1.0")
        self.find_pom(mock_run, mock_glob, _pom(_guava("33.0.0-jre"), inherited))
        with self.asked_about() as artefacts:
            update_pom_xmls()
        self.assertEqual(artefacts, ["com.google.guava:guava"])

    @kills(
        Mutation(
            maven_module,
            "    cooldown_days = COOLDOWN.get()\n",
            "    cooldown_days = 7\n",
            "every run asks about the default window, so --cooldown never reaches a Maven dependency",
        )
    )
    @patch_environ({COOLDOWN.name: "30"})
    def test_the_repository_is_asked_about_the_window_the_run_sets(self, mock_run: Mock, mock_glob: Mock):
        """Test that the window the repository is asked about is the one --cooldown sets, not the default one."""
        self.find_pom(mock_run, mock_glob, _pom(_guava("33.0.0-jre")))
        with self.hold_back({"com.google.guava:guava": ("33.7.1-jre",)}, cooldown_days=30) as rule_sets:
            update_pom_xmls()
        self.assertEqual(rule_sets, [_rule_set(_rule("com.google.guava", "guava", "33.7.1-jre"))])

    @kills(
        Mutation(
            pom_xml_module,
            ' ("plugin", _PLUGIN_GROUP))',
            ")",
            "the rule set leaves out the pom's plugins, so a property versioning one is advanced without a cooldown",
        ),
        Mutation(
            pom_xml_module,
            '("plugin", _PLUGIN_GROUP)',
            '("plugin", "")',
            "a plugin that leaves its group to Maven is dropped, so the property versioning it escapes the cooldown",
        ),
    )
    def test_a_plugin_versioned_by_a_property_gets_a_rule(self, mock_run: Mock, mock_glob: Mock):
        """Test that the rule set names a plugin the pom versions through a property, group declared or not."""
        cases = {"the pom declares the group": "org.apache.maven.plugins", "Maven defaults the group": ""}
        for case, group in cases.items():
            with self.subTest(case=case):
                surefire = _plugin("maven-surefire-plugin", "${surefire.version}", group=group)
                properties = _properties({"surefire.version": "3.5.0"})
                pom = _pom(_guava("33.0.0-jre"), properties=properties, build=_build(surefire))
                self.find_pom(mock_run, mock_glob, pom)
                with self.hold_back({"org.apache.maven.plugins:maven-surefire-plugin": ("3.6.0",)}) as rule_sets:
                    update_pom_xmls()
                surefire_rule = _rule("org.apache.maven.plugins", "maven-surefire-plugin", "3.6.0")
                self.assertEqual(rule_sets, [_rule_set(surefire_rule)])

    @kills(
        Mutation(
            maven_module,
            '    if cooldown_days <= 0:\n        return ""\n',
            "",
            "a run with the cooldown switched off asks the repository about every artefact and ignores every answer",
        )
    )
    @patch_environ({COOLDOWN.name: "0"})
    def test_the_repository_is_not_asked_when_the_cooldown_is_switched_off(self, mock_run: Mock, mock_glob: Mock):
        """Test that a run with the cooldown switched off does not ask the repository, and updates the pom."""
        self.find_pom(mock_run, mock_glob, _pom(_guava("33.0.0-jre")))
        with self.asked_about() as artefacts:
            update_pom_xmls()
        self.assertEqual(artefacts, [])
        # Asserting Maven ran, so the repository going unasked says something about the window rather than about a
        # pom that was never read.
        self.assert_maven_ran(mock_run)

    @kills(
        Mutation(
            maven_module,
            "    if not result.succeeded and result.stdout:\n",
            "    if result.succeeded and result.stdout:\n",
            "a Maven run that failed passes silently, since Maven writes its errors to stdout rather than stderr",
        )
    )
    def test_a_failed_maven_run_is_reported_with_its_output(self, mock_run: Mock, mock_glob: Mock):
        """Test that a Maven run exiting non-zero is reported with what it wrote, and leaves no new version reported."""
        self.find_pom(mock_run, mock_glob)
        # Maven writes its errors to stdout and leaves stderr empty, so the failure travels in the output.
        output = "[ERROR] Non-readable POM /pom.xml"
        mock_run.side_effect = CalledProcessError(cmd="", returncode=1, output=output, stderr="")
        update_pom_xmls()
        self.assert_command_failed_logged(_maven_command(), output)
        self.assert_no_new_version_logged()

    @kills(
        Mutation(
            update_pom_xml_module,
            "        _LOG.new_version(updated, DependencyVersion(new.current_version))\n",
            "        _LOG.new_version(updated, DependencyVersion(new.current_version))\n        return\n",
            "only the first dependency Maven moved is reported, and the rest of the pom's are lost",
        )
    )
    def test_each_rewritten_version_is_reported_at_its_own_line(self, mock_run: Mock, mock_glob: Mock):
        """Test that two dependencies Maven rewrote are both reported, each at its own version's line."""
        before = _pom(_guava("33.0.0-jre"), _dependency("org.springframework", "spring-core", "6.1.0"))
        after = _pom(_guava("33.7.1-jre"), _dependency("org.springframework", "spring-core", "7.1.0"))
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, after)
        update_pom_xmls()
        self.assert_new_version_logged_among_others("com.google.guava:guava", "33.7.1-jre", Location(pom, 6))
        self.assert_new_version_logged_among_others("org.springframework:spring-core", "7.1.0", Location(pom, 11))
        self.assertEqual(len(self.new_version_records()), 2)

    @kills(
        Mutation(
            update_pom_xml_module,
            "    for old, new in zip(before, after, strict=True):\n",
            "    for old, new in {n.location: (o, n) for o, n in zip(before, after, strict=True)}.values():\n",
            "one dependency per line is reported, so of two naming the same property only one is",
        )
    )
    def test_dependencies_sharing_a_property_are_each_reported_at_it(self, mock_run: Mock, mock_glob: Mock):
        """Test that two dependencies naming the same property are both reported, at that property's line."""
        core = _dependency("org.springframework", "spring-core", "${spring.version}")
        web = _dependency("org.springframework", "spring-web", "${spring.version}")
        before = _pom(core, web, properties=_properties({"spring.version": "6.1.0"}))
        after = _pom(core, web, properties=_properties({"spring.version": "7.1.0"}))
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, after)
        update_pom_xmls()
        self.assert_new_version_logged_among_others("org.springframework:spring-core", "7.1.0", Location(pom, 3))
        self.assert_new_version_logged_among_others("org.springframework:spring-web", "7.1.0", Location(pom, 3))
        self.assertEqual(len(self.new_version_records()), 2)

    def test_a_property_no_dependency_names_is_reported_for_none(self, mock_run: Mock, mock_glob: Mock):
        """Test that a property Maven advanced is reported for no dependency when no `<version>` names it."""
        before = _pom(_guava("33.0.0-jre"), properties=_properties({"unused.version": "1.0"}))
        after = _pom(_guava("33.7.1-jre"), properties=_properties({"unused.version": "2.0"}))
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, after)
        update_pom_xmls()
        # Guava is the only report, so the property Maven advanced beside it was reported for nothing.
        self.assert_new_version_logged("com.google.guava:guava", "33.7.1-jre", Location(pom, 9))

    @kills(
        Mutation(
            update_pom_xml_module,
            "    for old, new in zip(before, after, strict=True):\n",
            "    for old, new in zip(reversed(before), after, strict=True):\n",
            "a declaration pairs with another of the same name, so the one that moved is reported at the wrong line",
        )
    )
    def test_a_name_two_sections_declare_is_reported_per_declaration(self, mock_run: Mock, mock_glob: Mock):
        """Test that a managed dependency Maven moved is reported, though another section declares the same name."""
        before = _pom(_guava("33.7.1-jre"), managed=_managed(_guava("33.0.0-jre")))
        after = _pom(_guava("33.7.1-jre"), managed=_managed(_guava("33.7.1-jre")))
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, after)
        update_pom_xmls()
        self.assert_new_version_logged("com.google.guava:guava", "33.7.1-jre", Location(pom, 7))

    @kills(
        Mutation(
            update_pom_xml_module,
            "        if old.current_version == new.current_version:\n            continue\n",
            "",
            "every dependency is reported as moved, whether or not Maven changed its version",
        )
    )
    def test_a_dependency_maven_left_alone_is_not_reported(self, mock_run: Mock, mock_glob: Mock):
        """Test that a pom Maven changed nothing in gets no new version reported, though Maven ran over it."""
        unchanged = _pom(_guava("33.7.1-jre"))
        self.find_rewritten_pom(mock_run, mock_glob, unchanged, unchanged)
        update_pom_xmls()
        self.assert_maven_ran(mock_run)  # The pom was examined, so reporting nothing says something.
        self.assert_no_new_version_logged()

    @kills(
        Mutation(
            update_pom_xml_module,
            "        return\n    maven.update_pom_xml(pom_xml)\n",
            "    maven.update_pom_xml(pom_xml)\n",
            "Maven runs on a pom Update-time cannot read, rewriting a file it could not parse",
        )
    )
    def test_an_unparsable_pom_is_skipped(self, mock_run: Mock, mock_glob: Mock):
        """Test that a pom whose XML does not parse is warned about, and leaves the next pom checked as usual."""
        broken, second = self.find_poms(mock_run, mock_glob, "<project><broken>", _pom(_guava("33.7.1-jre")))
        update_pom_xmls()
        self.assert_invalid_file_logged(broken, "XML")
        self.assert_path_logged(second)
        self.assert_maven_ran(mock_run)  # Maven ran for the pom that parsed, and not for the one that did not.

    @kills(
        Mutation(
            update_pom_xml_module,
            "        _LOG.invalid_xml_after_update(pom_xml)",
            '        _LOG.invalid_file(pom_xml, "XML")',
            "a failed update reads as a skipped pom, though Maven ran and left what it wrote unreadable",
        )
    )
    def test_a_pom_that_no_longer_parses_after_the_run_is_an_error(self, mock_run: Mock, mock_glob: Mock):
        """Test that a pom Maven left unparsable is reported as a failed update, with no new version reported."""
        pom = self.find_rewritten_pom(mock_run, mock_glob, _pom(_guava("33.0.0-jre")), "<project><broken>")
        update_pom_xmls()
        self.assert_error_logged(Logger._MESSAGE_INVALID_XML_AFTER_UPDATE, location=Location(pom))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()  # The pom was not skipped: Maven ran and rewrote it.

    @kills(
        Mutation(
            update_pom_xml_module,
            "    if len(before) != len(after):\n",
            "    if len(before) != len(before):\n",
            "a reading of another length than its own pairs up all the same, taking the whole run down with it",
            raises="ValueError: zip() argument 2 is shorter than argument 1",
        )
    )
    def test_a_pom_declaring_fewer_dependencies_after_the_run_is_an_error(self, mock_run: Mock, mock_glob: Mock):
        """Test that a pom Maven added a declaration to or removed one from is reported as a failed update."""
        before = _pom(_guava("33.0.0-jre"), _dependency("org.springframework", "spring-core", "6.1.0"))
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, _pom(_guava("33.7.1-jre")))
        update_pom_xmls()
        self.assert_error_logged(Logger._MESSAGE_DECLARATIONS_CHANGED, location=Location(pom), before=2, after=1)
        self.assert_no_new_version_logged()

    @kills(
        Mutation(
            maven_module,
            "    if not result.succeeded and result.stdout:\n",
            "    if not result.succeeded:\n",
            "a missing Maven is reported twice: once by `run`, and again as a failed run that wrote nothing",
        ),
        Mutation(
            update_pom_xml_module,
            "        _update_pom_xml(pom_xml)\n",
            "        _update_pom_xml(pom_xml)\n        return\n",
            "the walk ends at the first pom, so the poms after it are never updated",
        ),
    )
    def test_a_missing_maven_is_reported_and_the_next_pom_is_still_checked(self, mock_run: Mock, mock_glob: Mock):
        """Test that a missing Maven executable is reported once, and leaves the next pom checked as usual."""
        second = self.find_poms(mock_run, mock_glob, "<project/>", "<project/>")[1]
        mock_run.side_effect = [FileNotFoundError, Mock(stdout="", stderr="")]
        update_pom_xmls()
        self.assert_error_logged(Logger._MESSAGE_COMMAND_NOT_FOUND, command=_maven_command(), executable="mvn")
        self.assertEqual(mock_run.call_count, 2)
        self.assert_path_logged(second)

    @kills(
        Mutation(
            maven_module,
            '    return f"./{_WRAPPER}" if (pom_xml.parent / _WRAPPER).exists() else "mvn"\n',
            '    return "mvn"\n',
            "a project's own Maven wrapper is passed over, so another Maven than it builds with updates it",
        )
    )
    def test_the_maven_wrapper_runs_when_it_sits_beside_the_pom(self, mock_run: Mock, mock_glob: Mock):
        """Test that the project's own Maven wrapper runs, rather than the mvn on the path."""
        self.find_pom(mock_run, mock_glob)
        with patch("pathlib.Path.exists", lambda self: self.name == "mvnw"):
            update_pom_xmls()
        self.assert_maven_ran(mock_run, "./mvnw")
