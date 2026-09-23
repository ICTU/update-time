"""Unit tests for the pom.xml updater."""

import contextlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CalledProcessError  # nosec
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import Archival, ArchivedSubject, Project, Release
from update_time.io.log import Logger
from update_time.manifests import pom_xml as pom_xml_module
from update_time.package_managers import maven as maven_module
from update_time.primitives.command import Command
from update_time.primitives.location import Location
from update_time.references import delegated as delegated_module
from update_time.sources import maven_central as maven_central_module
from update_time.sources.maven_central import project as maven_central_project
from update_time.updaters import update_pom_xml as update_pom_xml_module
from update_time.updaters.update_pom_xml import update_pom_xmls

from tests.helpers import mock_path, patch_environ, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.helpers import (
    LoggingTestCase,
    archival_check_disabled,
    maven_central_listing,
    maven_central_pom,
    maven_central_version_row,
    patch_maven_central,
    staleness_disabled,
)
from tests.update_time.updaters.fixtures import ADVISORY, VULNERABILITY
from tests.update_time.updaters.helpers import assert_osv_asked_about, no_vulnerabilities, osv

if TYPE_CHECKING:
    from collections.abc import Iterator

# The rule set file these tests hand Update-time, standing in for the temporary file a real run writes.
_RULES = Path("/rules.xml")

# The listing the repository serves where a test lets it answer for itself: guava's version, dated yesterday.
_LISTING = maven_central_listing(maven_central_version_row("33.0.0-jre", datetime.now(UTC) - timedelta(days=1)))


def _dependency(group: str, artifact: str, version: str) -> str:
    """Return a `<dependency>` element declaring the group, the artifact, and the version."""
    parts = {"groupId": group, "artifactId": artifact, "version": version}
    declared = "".join(f"      <{tag}>{value}</{tag}>\n" for tag, value in parts.items())
    return f"    <dependency>\n{declared}    </dependency>\n"


def _guava(version: str) -> str:
    """Return the `<dependency>` element declaring guava, at the given version."""
    return _dependency("com.google.guava", "guava", version)


def _stale(version: str, days_ago: int) -> Project:
    """Return what the repository reports about an artefact whose newest release is that many days old."""
    return Project(newest=Release(version, datetime.now(UTC) - timedelta(days=days_ago)))


def _archived(_artefact: str, *, check_archival: bool) -> Project:
    """Return what Maven Central reports about an artefact whose GitHub repository is archived.

    The archival is read only when the run asks for it, so a run that does not is told nothing.
    """
    return Project(
        archival=Archival(archived=True, subject=ArchivedSubject.REPOSITORY) if check_archival else Archival()
    )


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


@no_vulnerabilities
@patch.object(maven_central_module, "project", Mock(return_value=Project()))
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
        """Collect the artefacts the cooldown asks the repository about, and do not hold any version back."""
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

    @kills(
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "_warn_about_vulnerabilities(after)",
            "",
            "a pom's dependencies reach OSV never, so an advisory naming the version a run lands on goes unreported",
        )
    )
    def test_a_vulnerable_dependency_is_warned_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency an advisory names is warned about, at the line its `<version>` element sits on."""
        pom = self.find_pom(mock_run, mock_glob, _pom(_guava("33.0.0-jre")))
        with osv(ADVISORY):
            update_pom_xmls()
        self.assert_vulnerable_dependency_logged(
            "com.google.guava:guava", "33.0.0-jre", VULNERABILITY, Location(pom, 6)
        )

    @kills(
        Mutation(
            update_pom_xml_module._warn_about_vulnerabilities,
            "Ecosystem.MAVEN",
            "Ecosystem.PYPI",
            "a pom's coordinates are matched against the advisories of another ecosystem, which holds none of them",
        ),
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "_warn_about_vulnerabilities(after)",
            "_warn_about_vulnerabilities(before)",
            "the version asked about is the one Maven updated away from rather than the one the run lands on",
        ),
    )
    def test_osv_is_asked_about_the_version_the_run_lands_on(self, mock_run: Mock, mock_glob: Mock):
        """Test that OSV is asked in the Maven ecosystem, about the version the pom holds once Maven has run."""
        before, after = _pom(_guava("33.0.0-jre")), _pom(_guava("33.7.1-jre"))
        self.find_rewritten_pom(mock_run, mock_glob, before, after)
        with osv() as mock_post:
            update_pom_xmls()
        assert_osv_asked_about(mock_post, ("com.google.guava:guava", "33.7.1-jre"), ecosystem="Maven")

    @kills(
        Mutation(
            pom_xml_module.fully_resolved,
            " and _is_resolved(reference.current_version)",
            "",
            "OSV is asked about a version holding an unresolved property, which it matches nothing to",
        ),
        Mutation(
            pom_xml_module.artefact_references,
            "_is_resolved(reference.dependency)",
            "fully_resolved(reference)",
            "staleness judges the version too, so a dependency its parent versions goes unchecked for years",
        ),
    )
    def test_a_version_naming_a_property_is_asked_about_for_staleness_alone(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency whose `<version>` names a property the pom lacks reaches Maven Central, not OSV."""
        # A pom can spell a dependency's version as a property its parent declares, which this pom does not hold.
        inherited = _dependency("org.springframework", "spring-core", "${spring.version}")
        self.find_pom(mock_run, mock_glob, _pom(_guava("33.0.0-jre"), inherited))
        mock_project = Mock(return_value=Project())
        with osv() as mock_post, patch.object(maven_central_module, "project", mock_project):
            update_pom_xmls()
        # Guava is asked about, so the dependency beside it going unasked says something about its version.
        assert_osv_asked_about(mock_post, ("com.google.guava:guava", "33.0.0-jre"), ecosystem="Maven")
        # Staleness judges the coordinates alone, so the dependency OSV skips still reaches Maven Central.
        asked = ["com.google.guava:guava", "org.springframework:spring-core"]
        self.assertEqual([call.args[0] for call in mock_project.call_args_list], asked)

    @kills(
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "pom_xml_format.artefact_references(pom_xml)",
            "[]",
            "a pom's dependencies reach Maven Central never, so one that stopped releasing goes unreported",
        )
    )
    def test_a_stale_dependency_is_warned_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency whose newest release is old is warned about, at its `<version>` element's line."""
        pom = self.find_pom(mock_run, mock_glob, _pom(_guava("33.0.0-jre")))
        with patch.object(maven_central_module, "project", Mock(return_value=_stale("33.0.0-jre", 500))):
            update_pom_xmls()
        self.assert_stale_dependency_logged("com.google.guava:guava", "33.0.0-jre", Location(pom, 6))

    @kills(
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "pom_xml_format.artefact_references(pom_xml)",
            "after",
            "staleness reads the pom's dependencies alone, so a plugin that stopped releasing goes unreported",
        )
    )
    def test_a_stale_plugin_is_warned_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that a plugin whose newest release is old is warned about, at its `<version>` element's line."""
        surefire = _plugin("maven-surefire-plugin", "3.5.0")
        pom = self.find_pom(mock_run, mock_glob, _pom(build=_build(surefire)))
        with patch.object(maven_central_module, "project", Mock(return_value=_stale("3.5.0", 500))):
            update_pom_xmls()
        surefire_plugin = "org.apache.maven.plugins:maven-surefire-plugin"
        self.assert_stale_dependency_logged(surefire_plugin, "3.5.0", Location(pom, 9))

    @kills(
        Mutation(
            delegated_module.project_resolver,
            "check_archival=check_archival",
            "check_archival=False",
            "a delegated source is told the run checks nothing for archival, so it reads none and reports none",
        )
    )
    def test_an_archived_dependency_is_warned_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency whose repository is archived is warned about, at its `<version>` element's line."""
        pom = self.find_pom(mock_run, mock_glob, _pom(_guava("33.0.0-jre")))
        with patch.object(maven_central_module, "project", Mock(side_effect=_archived)):
            update_pom_xmls()
        self.assert_archived_repository_logged("com.google.guava:guava", Location(pom, 6))

    @kills(
        Mutation(
            maven_central_module,
            "@archival_reporting\ndef project",
            "def project",
            "the repository reports no archival, so a switched-off staleness check skips the pom it reads",
        ),
        Mutation(
            delegated_module.project_resolver,
            "archival_reporting(resolve, when=partial(reports_archival, get_project))",
            "resolve",
            "a resolver reports no archival though its source does, so the same switched-off check skips it",
        ),
    )
    @staleness_disabled
    def test_staleness_disabled_still_warns_about_an_archived_dependency(self, mock_run: Mock, mock_glob: Mock):
        """Test that an archived dependency is warned about when the staleness check is switched off.

        Maven Central answers here, so the archival comes from the source itself rather than from a stub.
        """
        pom = self.find_pom(mock_run, mock_glob, _pom(_guava("33.0.0-jre")))
        served = maven_central_pom("scm:git:https://github.com/google/guava.git")
        with (
            patch.object(maven_central_module, "project", maven_central_project),
            patch_maven_central(_LISTING, served, archived=True),
        ):
            update_pom_xmls()
        self.assert_archived_repository_logged("com.google.guava:guava", Location(pom, 6))

    @kills(
        Mutation(
            delegated_module.project_resolver,
            "archival_is_checked()",
            "True",
            "every delegated source reads archival, so --ignore-archived costs the requests it exists to save",
        )
    )
    @archival_check_disabled
    def test_a_run_that_checks_nothing_for_archival(self, mock_run: Mock, mock_glob: Mock):
        """Test that a run with the archival check switched off tells the repository so, and warns about nothing."""
        self.find_pom(mock_run, mock_glob, _pom(_guava("33.0.0-jre")))
        mock_project = Mock(side_effect=_archived)
        with patch.object(maven_central_module, "project", mock_project):
            update_pom_xmls()
        mock_project.assert_called_once_with("com.google.guava:guava", check_archival=False)
        self.assert_no_warnings_logged()

    def test_maven_runs_in_the_poms_own_directory(self, mock_run: Mock, mock_glob: Mock):
        """Test that Maven runs both goals of the versions plugin Update-time names, in the pom's own directory."""
        self.find_pom(mock_run, mock_glob)
        update_pom_xmls()
        self.assert_maven_ran(mock_run)

    @kills(
        Mutation(
            maven_module._rule,
            'groupId="{group_id}" artifactId="{artifact_id}"',
            'groupId="*" artifactId="*"',
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
            pom_xml_module.artefact_references,
            " if _is_resolved(reference.dependency)",
            "",
            "a pom inheriting a group from its parent is asked about coordinates that resolve to nothing",
        ),
        Mutation(
            pom_xml_module.fully_resolved,
            "_is_resolved(reference.dependency) and ",
            "",
            "OSV is asked about coordinates holding an unresolved property, which it matches nothing to",
        ),
    )
    def test_coordinates_naming_a_property_are_not_asked_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that every check passes over a dependency whose coordinates name a property the pom lacks."""
        # A pom can spell a dependency's group as a property its parent declares, which this pom does not hold.
        inherited = _dependency("${spring.group}", "spring-core", "6.1.0")
        self.find_pom(mock_run, mock_glob, _pom(_guava("33.0.0-jre"), inherited))
        mock_project = Mock(return_value=Project())
        with (
            self.asked_about() as artefacts,
            osv() as mock_post,
            patch.object(maven_central_module, "project", mock_project),
        ):
            update_pom_xmls()
        # Guava reaches every check, so the dependency beside it reaching none says something about its coordinates.
        self.assertEqual(artefacts, ["com.google.guava:guava"])
        assert_osv_asked_about(mock_post, ("com.google.guava:guava", "33.0.0-jre"), ecosystem="Maven")
        self.assertEqual([call.args[0] for call in mock_project.call_args_list], ["com.google.guava:guava"])

    @kills(
        Mutation(
            maven_module._rules,
            "COOLDOWN.get()",
            "7",
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
            pom_xml_module.artefact_references,
            ', "plugin": _PLUGIN_GROUP',
            "",
            "the rule set leaves out the pom's plugins, so a property versioning one is advanced without a cooldown",
        ),
        Mutation(
            pom_xml_module.artefact_references,
            "_PLUGIN_GROUP",
            '""',
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
            maven_module._rules,
            "cooldown_days <= 0",
            "cooldown_days < 0",
            "a run with the cooldown switched off asks the repository about every artefact and ignores every answer",
        )
    )
    @patch_environ({COOLDOWN.name: "0"})
    def test_the_cooldown_skips_the_repository_when_it_is_switched_off(self, mock_run: Mock, mock_glob: Mock):
        """Test that a switched-off cooldown skips the repository, and leaves the pom updated."""
        self.find_pom(mock_run, mock_glob, _pom(_guava("33.0.0-jre")))
        with self.asked_about() as artefacts:
            update_pom_xmls()
        self.assertEqual(artefacts, [])
        # Asserting Maven ran, so the window rather than a pom that was never read is what left the artefacts alone.
        # The staleness check asks the repository whatever the window is, so this says nothing about that request.
        self.assert_maven_ran(mock_run)

    @kills(
        Mutation(
            maven_module.update_pom_xml,
            "not result.succeeded",
            "result.succeeded",
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
            update_pom_xml_module._report_new_versions,
            "_LOG.new_version(updated, DependencyVersion(new.current_version))",
            "_LOG.new_version(updated, DependencyVersion(new.current_version))\n        return",
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
            update_pom_xml_module._report_new_versions,
            "zip(before, after, strict=True)",
            "{n.location: (o, n) for o, n in zip(before, after, strict=True)}.values()",
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
            update_pom_xml_module._report_new_versions,
            "zip(before, after",
            "zip(reversed(before), after",
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
            update_pom_xml_module._report_new_versions,
            "old.current_version == new.current_version",
            "False",
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
            update_pom_xml_module._update_pom_xml,
            "        return\n    maven",
            "    maven",
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
            update_pom_xml_module._update_pom_xml,
            "invalid_xml_after_update(pom_xml)",
            'invalid_file(pom_xml, "XML")',
            "a failed update reads as a skipped pom, though Maven ran and left what it wrote unreadable",
        ),
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "_LOG.invalid_xml_after_update(pom_xml)",
            "_warn_about_vulnerabilities(before)\n        _LOG.invalid_xml_after_update(pom_xml)",
            "a pom left unreadable is checked on its pre-run reading, against versions the file may no longer hold",
        ),
    )
    def test_a_pom_that_no_longer_parses_after_the_run_is_an_error(self, mock_run: Mock, mock_glob: Mock):
        """Test that a pom Maven left unparsable is reported as a failed update, and then left alone."""
        pom = self.find_rewritten_pom(mock_run, mock_glob, _pom(_guava("33.0.0-jre")), "<project><broken>")
        with osv() as mock_post:
            update_pom_xmls()
        self.assert_error_logged(Logger._MESSAGE_INVALID_XML_AFTER_UPDATE, location=Location(pom))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()  # The pom was not skipped: Maven ran and rewrote it.
        mock_post.assert_not_called()  # There is no reading to check, so OSV is asked about nothing.

    @kills(
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "!= len(after)",
            "!= len(before)",
            "a reading of another length than its own pairs up all the same, taking the whole run down with it",
            raises="ValueError: zip() argument 2 is shorter than argument 1",
        ),
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "_LOG.declarations_changed(pom_xml, len(before), len(after))",
            "_LOG.declarations_changed(pom_xml, len(before), len(after))"
            "\n        _check_projects(pom_xml_format.artefact_references(pom_xml))",
            "a pom Update-time gave up on is checked for staleness all the same, beside the error saying it was not",
        ),
    )
    def test_a_pom_declaring_fewer_dependencies_after_the_run_is_an_error(self, mock_run: Mock, mock_glob: Mock):
        """Test that a pom Maven added a declaration to or removed one from is reported as a failed update."""
        before = _pom(_guava("33.0.0-jre"), _dependency("org.springframework", "spring-core", "6.1.0"))
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, _pom(_guava("33.7.1-jre")))
        mock_project = Mock(return_value=Project())
        with patch.object(maven_central_module, "project", mock_project):
            update_pom_xmls()
        self.assert_error_logged(Logger._MESSAGE_DECLARATIONS_CHANGED, location=Location(pom), before=2, after=1)
        self.assert_no_new_version_logged()
        mock_project.assert_not_called()  # The pom is reported, not checked on a reading it cannot pair up.

    @kills(
        Mutation(
            maven_module.update_pom_xml,
            " and result.stdout:",
            ":",
            "a missing Maven is reported twice: once by `run`, and again as a failed run that wrote nothing",
        ),
        Mutation(
            update_pom_xml_module.update_pom_xmls,
            "_update_pom_xml(pom_xml)",
            "_update_pom_xml(pom_xml)\n        return",
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
            maven_module._maven,
            'f"./{_WRAPPER}" if (pom_xml.parent / _WRAPPER).exists() else "mvn"',
            '"mvn"',
            "a project's own Maven wrapper is passed over, so another Maven than it builds with updates it",
        )
    )
    def test_the_maven_wrapper_runs_when_it_sits_beside_the_pom(self, mock_run: Mock, mock_glob: Mock):
        """Test that the project's own Maven wrapper runs, rather than the mvn on the path."""
        self.find_pom(mock_run, mock_glob)
        with patch("pathlib.Path.exists", lambda self: self.name == "mvnw"):
            update_pom_xmls()
        self.assert_maven_ran(mock_run, "./mvnw")
