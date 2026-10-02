"""Unit tests for the pom.xml updater."""

import contextlib
import re
from functools import partial
from pathlib import Path
from subprocess import CalledProcessError  # nosec
from typing import TYPE_CHECKING
from unittest.mock import Mock, call, patch

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import NO_CHANGES, Archival, ArchivedSubject, Changes, Project, Release
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
    EFFECTIVE_GUAVA,
    GUAVA,
    PARENT_POM_ID,
    SCANNED_POM_COORDINATES,
    SCANNED_POM_ID,
    SUREFIRE,
    LoggingTestCase,
    archival_check_disabled,
    build_element,
    days_ago,
    dependency_element,
    dependency_management_element,
    effective_dependency_element,
    effective_plugin_element,
    effective_pom_declaring,
    guava_element,
    maven_central_listing,
    maven_central_pom,
    maven_central_version_row,
    patch_maven_central,
    plugin_element,
    pom_coordinates,
    pom_declaring,
    properties_element,
    staleness_disabled,
)
from tests.update_time.updaters.fixtures import ADVISORY, VULNERABILITY
from tests.update_time.updaters.helpers import assert_osv_asked_about, no_vulnerabilities, osv

if TYPE_CHECKING:
    from collections.abc import Iterator

# The tests discover each pom in this directory. The scan runs in `/`, so a Maven run in the wrong one shows.
_PROJECT = Path("/project")

# The rule set file these tests hand Update-time, standing in for the temporary file a real run writes.
_RULES = Path("/rules.xml")

# Maven writes the effective pom to this file in these tests. It stands in for the temporary file a real run names.
_EFFECTIVE_POM = "/effective-pom.xml"

# The listing the repository serves where a test lets it answer for itself: guava's version, dated yesterday.
_LISTING = maven_central_listing(maven_central_version_row("33.0.0-jre", days_ago(1)))


def _stale(version: str, age: int) -> Project:
    """Return what the repository reports about an artefact whose newest release is that many days old."""
    return Project(newest=Release(version, days_ago(age)))


def _archived(_artefact: str, *, check_archival: bool) -> Project:
    """Return what Maven Central reports about an artefact whose GitHub repository is archived.

    The archival is read only when the run asks for it, so a run that does not is told nothing.
    """
    return Project(
        archival=Archival(archived=True, subject=ArchivedSubject.REPOSITORY) if check_archival else Archival()
    )


def _plugin_version(plugin: str) -> str:
    """Return the release of the plugin that the pom Update-time ships declares, read off that pom's own text."""
    pom = (Path(maven_module.__file__).parent / "pom.xml").read_text()
    declared = re.search(rf"<{plugin}\.plugin\.version>(?P<version>[^<]+)</{plugin}\.plugin\.version>", pom)
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


# The options that open every Maven command these tests expect.
_OPTIONS = ("--batch-mode", "--no-transfer-progress", "--non-recursive", "--update-snapshots")

# The goal writing the effective pom, and its options. Each goal names the plugin release the shipped pom declares.
_EFFECTIVE_POM_GOAL = f"org.apache.maven.plugins:maven-help-plugin:{_plugin_version('help')}:effective-pom"
_EFFECTIVE_POM_OPTIONS = ("-Dverbose", f"-Doutput={_EFFECTIVE_POM}")

# The versions plugin's two goals.
_VERSIONS_GOALS = (
    f"org.codehaus.mojo:versions-maven-plugin:{_plugin_version('versions')}:use-latest-releases",
    f"org.codehaus.mojo:versions-maven-plugin:{_plugin_version('versions')}:update-properties",
)


def _versions_options(rules: Path | None) -> tuple[str, ...]:
    """Return the options the versions plugin's goals take, naming the rule set file where a run holds versions back."""
    rule_set = (f"-Dmaven.version.rules={rules.as_uri()}",) if rules else ()
    return ("-DgenerateBackupPoms=false", f"-Dmaven.version.ignore={maven_module._PRE_RELEASES}", *rule_set)


def _maven_command(executable: str = "mvn", rules: Path | None = None) -> Command:
    """Return the command Update-time runs over a pom whose coordinates resolve: the effective pom, then the updates."""
    return Command(
        executable, *_OPTIONS, *_versions_options(rules), *_EFFECTIVE_POM_OPTIONS, _EFFECTIVE_POM_GOAL, *_VERSIONS_GOALS
    )


def _effective_pom_command() -> Command:
    """Return the command writing the effective pom of a pom leaving a group or an artifact to its parent."""
    return Command("mvn", *_OPTIONS, *_EFFECTIVE_POM_OPTIONS, _EFFECTIVE_POM_GOAL)


def _versions_command(rules: Path | None = None) -> Command:
    """Return the command updating a pom leaving a group or an artifact to its parent, after its effective pom."""
    return Command("mvn", *_OPTIONS, *_versions_options(rules), *_VERSIONS_GOALS)


def _maven_failed(output: str) -> CalledProcessError:
    """Return the error a Maven run raises when it exits non-zero, having written its output to stdout."""
    return CalledProcessError(cmd="", returncode=1, output=output, stderr="")


def _artefacts_asked(mock: Mock) -> list[str]:
    """Return the artefact each call to the mock named, in the order of the calls."""
    return [call.args[0] for call in mock.call_args_list]


# The tests hand every run this effective pom by default. It does not list a dependency, so it resolves nothing.
_EMPTY_EFFECTIVE_POM = effective_pom_declaring()

# A dependency whose group names a property the parent declares, and the entry the effective pom lists for it where a
# pom declares that dependency alone.
_SPRING_LEAVING_ITS_GROUP = dependency_element("${spring.group}", "spring-core", "6.1.0")
_EFFECTIVE_SPRING = effective_dependency_element("org.springframework:spring-core", "6.1.0", line=5)

# A plugin in a group of its own, declared by a group the parent declares, and the effective pom listing it where a
# pom declares that plugin alone. Maven lists the parent's property in the effective pom.
_VERSIONS = "org.codehaus.mojo:versions-maven-plugin"
_VERSIONS_LEAVING_ITS_GROUP = plugin_element("versions-maven-plugin", "2.18.0", "${mojo.group}")
_EFFECTIVE_VERSIONS = effective_pom_declaring(
    properties=properties_element({"mojo.group": "org.codehaus.mojo"}),
    build=build_element(effective_plugin_element(_VERSIONS, "2.18.0", line=8)),
)
_EFFECTIVE_SUREFIRE = effective_pom_declaring(
    properties=properties_element({"plugin.group": "org.apache.maven.plugins"}),
    build=build_element(effective_plugin_element(SUREFIRE, "3.5.0", line=8)),
)

# A parent and a child scanned together. Both runs read the child's effective pom, which locates the child's guava on
# a line the parent declares nothing on, so the parent's run reads guava's version off the parent alone.
# The parent, named `PARENT_POM_ID`, manages guava's version on line 10.
_PARENT_MANAGING_GUAVA = pom_declaring(
    coordinates=pom_coordinates("parent", "org.example", "1.0"),
    managed=dependency_management_element(guava_element("33.0.0-jre")),
)
# The parent managing guava's version on line 13 by its own property, on line 6.
_PARENT_MANAGING_GUAVA_BY_PROPERTY = pom_declaring(
    coordinates=pom_coordinates("parent", "org.example", "1.0"),
    properties=properties_element({"guava.version": "33.0.0-jre"}),
    managed=dependency_management_element(guava_element("${guava.version}")),
)
# The child, which declares guava without a version on line 5, and its `<artifactId>` on line 7.
_CHILD_LEAVING_GUAVAS_VERSION = pom_declaring(guava_element(None), coordinates=SCANNED_POM_COORDINATES)


@no_vulnerabilities
@patch.object(maven_central_module, "project", Mock(return_value=Project()))
@patch.object(maven_central_module, "get_changes", Mock(return_value=NO_CHANGES))
@patch.object(maven_module, "versions_held_back", Mock(return_value=()))
@patch_pathlib_path("rglob", cwd=Path("/"), exists=False)
@patch("subprocess.run")
class UpdatePomXmlTest(LoggingTestCase):
    """Unit tests for finding the pom.xml files and running Maven over each of them."""

    def find_poms(
        self, mock_run: Mock, mock_glob: Mock, *contents: str, effective_pom: str = _EMPTY_EFFECTIVE_POM
    ) -> list[Mock]:
        """Discover a mock pom.xml per given contents, with Maven stubbed to print nothing and its past runs forgotten.

        The file Maven writes the effective pom to holds `effective_pom`. That file of a real run is gone by the time
        the run ends, so the file is stood in for here.
        """
        poms = [mock_path(text, parent=_PROJECT, name="pom.xml") for text in contents]
        mock_glob.return_value = poms
        mock_run.reset_mock()
        mock_run.return_value = Mock(stdout="", stderr="")
        written = mock_path(effective_pom, name=_EFFECTIVE_POM)

        @contextlib.contextmanager
        def effective_pom_file() -> Iterator[Mock]:
            yield written

        self.enterContext(patch.object(maven_module, "_effective_pom_file", effective_pom_file))
        return poms

    def find_pom(
        self, mock_run: Mock, mock_glob: Mock, contents: str = "<project/>", effective_pom: str = _EMPTY_EFFECTIVE_POM
    ) -> Mock:
        """Discover a single mock pom.xml holding the contents, with Maven stubbed to print nothing."""
        return self.find_poms(mock_run, mock_glob, contents, effective_pom=effective_pom)[0]

    def find_rewritten_pom(
        self, mock_run: Mock, mock_glob: Mock, before: str, after: str, effective_pom: str = _EMPTY_EFFECTIVE_POM
    ) -> Mock:
        """Discover a single mock pom.xml that reads as `before` until the versions plugin's goals rewrite it."""

        def contents() -> bytes:
            """Return what the pom holds, which the versions plugin's goals change."""
            rewritten = any(_VERSIONS_GOALS[0] in run.args[0] for run in mock_run.call_args_list)
            return (after if rewritten else before).encode()

        pom = self.find_pom(mock_run, mock_glob, before, effective_pom)
        pom.read_bytes = Mock(side_effect=contents)
        return pom

    def assert_maven_ran(self, mock_run: Mock, executable: str = "mvn", rules: Path | None = None) -> None:
        """Assert that the Maven command ran once, with the given executable, in the pom's own directory."""
        self.assert_maven_runs(mock_run, _maven_command(executable, rules))

    def assert_maven_runs(self, mock_run: Mock, *commands: Command) -> None:
        """Assert that Maven ran the given commands, in this order, in the pom's own directory."""
        runs = [call(command, capture_output=True, text=True, check=True, cwd=_PROJECT) for command in commands]
        self.assertEqual(mock_run.call_args_list, runs)

    @contextlib.contextmanager
    def asked_about(self) -> Iterator[list[str]]:
        """Collect the artefacts the cooldown asks the repository about, and do not hold any version back."""
        artefacts: list[str] = []

        def versions_held_back(artefact: str, _days: int) -> tuple[str, ...]:
            artefacts.append(artefact)
            return ()

        with patch.object(maven_module, "versions_held_back", versions_held_back):
            yield artefacts

    @contextlib.contextmanager
    def hold_back(
        self, versions: dict[str, tuple[str, ...]], cooldown_days: int = COOLDOWN.default
    ) -> Iterator[list[str]]:
        """Hold the named versions back per artefact, and collect the rule sets Update-time writes for Maven.

        The stub answers with the named versions only where the cooldown is `cooldown_days`, and with an empty tuple
        otherwise. The rule set file of a real run is gone by the time the run ends, so the file is stood in for here.
        """
        written: list[str] = []

        @contextlib.contextmanager
        def rule_set_file(rule_set: str) -> Iterator[Path]:
            written.append(rule_set)
            yield _RULES

        def versions_held_back(artefact: str, days: int) -> tuple[str, ...]:
            return versions.get(artefact, ()) if days == cooldown_days else ()

        repository = Mock(side_effect=versions_held_back)
        with (
            patch.object(maven_module, "versions_held_back", repository),
            patch.object(maven_module, "_rule_set_file", rule_set_file),
        ):
            yield written

    @kills(
        Mutation(
            update_pom_xml_module._warn_about_vulnerabilities,
            "Ecosystem.MAVEN",
            "Ecosystem.PYPI",
            "a pom's coordinates are matched against the advisories of another ecosystem, which holds none of them",
        ),
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "_warn_about_vulnerabilities(declared)",
            "_warn_about_vulnerabilities(before)",
            "the version asked about is the one Maven updated away from rather than the one the run lands on",
        ),
    )
    def test_osv_is_asked_about_the_version_the_run_lands_on(self, mock_run: Mock, mock_glob: Mock):
        """Test that OSV is asked in the Maven ecosystem about the version after the run, not the effective pom's."""
        before, after = pom_declaring(guava_element("33.0.0-jre")), pom_declaring(guava_element("33.7.1-jre"))
        self.find_rewritten_pom(mock_run, mock_glob, before, after, effective_pom_declaring(EFFECTIVE_GUAVA))
        with osv() as mock_post:
            update_pom_xmls()
        assert_osv_asked_about(mock_post, (GUAVA, "33.7.1-jre"), ecosystem="Maven")

    @kills(
        Mutation(
            pom_xml_module.fully_resolved,
            "(group_id, artifact_id, pinned.version)",
            "(group_id, artifact_id)",
            "OSV is asked about a version holding an unresolved property, which it matches nothing to",
        ),
        Mutation(
            pom_xml_module.with_resolved_coordinates,
            "_is_resolved(declaration.dependency)",
            "fully_resolved(declaration.pinned)",
            "staleness judges the version too, so a dependency its parent versions goes unchecked for years",
        ),
        Mutation(
            pom_xml_module.fully_resolved,
            "part and _is_resolved(part)",
            "_is_resolved(part)",
            "OSV is asked about a dependency at an empty version, which it matches nothing to",
        ),
    )
    def test_a_version_maven_rejects_is_asked_about_for_staleness_alone(self, mock_run: Mock, mock_glob: Mock):
        """Test that a version Maven rejects reaches Maven Central but not OSV, and that Maven's error is reported."""
        # Maven stops before any goal runs, so it prints its error but does not write an effective pom.
        spring = "'dependencies.dependency.version' for org.springframework:spring-core:jar"
        cases = {
            "property": (
                "${spring.version}",
                f"[ERROR] {spring} must be a valid version but is '${{spring.version}}'.",
            ),
            "empty": ("", f"[ERROR] {spring} is missing."),
            "missing": (None, f"[ERROR] {spring} is missing."),
        }
        for case, (version, output) in cases.items():
            with self.subTest(case=case):
                unversioned = dependency_element("org.springframework", "spring-core", version)
                self.find_pom(mock_run, mock_glob, pom_declaring(guava_element("33.0.0-jre"), unversioned), "")
                mock_run.side_effect = _maven_failed(output)
                mock_project = Mock(return_value=Project())
                with osv() as mock_post, patch.object(maven_central_module, "project", mock_project):
                    update_pom_xmls()
                self.assert_command_failed_logged(_maven_command(), output)
                # Guava is asked about, so the dependency beside it going unasked says something about its version.
                assert_osv_asked_about(mock_post, (GUAVA, "33.0.0-jre"), ecosystem="Maven")
                # Staleness judges the coordinates alone, so the dependency OSV skips still reaches Maven Central.
                asked = [GUAVA, "org.springframework:spring-core"]
                self.assertEqual(_artefacts_asked(mock_project), asked)

    @kills(
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "_warn_about_vulnerabilities(declared)",
            "_warn_about_vulnerabilities(after)",
            "OSV is asked about the pom as it spells a dependency, so what the parent declares is never checked",
        ),
        Mutation(
            pom_xml_module._interpolated,
            "reference[0] if element is None else element.text",
            "element.text",
            "an artifact naming the parent's property ends the run, since the pom alone does not declare it",
            raises="AttributeError: 'NoneType' object has no attribute 'text'",
        ),
    )
    def test_a_vulnerable_dependency_is_warned_about_as_maven_resolves_it(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency an advisory names is warned about as Maven resolves it, whichever pom decides it."""
        # The warning names the line of the `<version>` element, or of the `<dependency>` lacking one.
        cases = {
            "the pom's own version": (guava_element("33.0.0-jre"), 6, None),
            "a parent's property": (guava_element("${guava.version}"), 6, None),
            "no version": (guava_element(None), 3, (PARENT_POM_ID, 12)),
            "a parent's group": (dependency_element("${guava.group}", "guava", "33.0.0-jre"), 6, None),
            "a parent's artifact": (dependency_element("com.google.guava", "${guava.artifact}", "33.0.0-jre"), 6, None),
        }
        # Maven lists the properties the parent declares in the effective pom.
        properties = properties_element({"guava.group": "com.google.guava", "guava.artifact": "guava"})
        for case, (declared, line, managed_at) in cases.items():
            with self.subTest(case=case):
                guava = effective_dependency_element(GUAVA, "33.0.0-jre", line=5, managed_at=managed_at)
                effective_pom = effective_pom_declaring(guava, properties=properties)
                pom = self.find_pom(mock_run, mock_glob, pom_declaring(declared), effective_pom)
                with osv(ADVISORY):
                    update_pom_xmls()
                self.assert_vulnerable_dependency_logged(GUAVA, "33.0.0-jre", VULNERABILITY, Location(pom, line))

    def test_a_vulnerable_plugin_is_warned_about_as_maven_resolves_it(self, mock_run: Mock, mock_glob: Mock):
        """Test that a plugin an advisory names is warned about as Maven resolves it, whichever pom decides it."""
        # The warning names the line of the `<version>` element, or of the `<plugin>` lacking one.
        managed_surefire = effective_plugin_element(SUREFIRE, "3.5.0", line=8, managed_at=(PARENT_POM_ID, 12))
        cases = {
            "a version": (
                plugin_element("maven-surefire-plugin", "3.5.0"),
                SUREFIRE,
                "3.5.0",
                9,
                _EMPTY_EFFECTIVE_POM,
            ),
            "Maven's default group": (
                plugin_element("maven-surefire-plugin", "3.5.0", group=None),
                SUREFIRE,
                "3.5.0",
                8,
                _EMPTY_EFFECTIVE_POM,
            ),
            "no version": (
                plugin_element("maven-surefire-plugin", None),
                SUREFIRE,
                "3.5.0",
                6,
                effective_pom_declaring(build=build_element(managed_surefire)),
            ),
        }
        for case, (declared, name, version, line, effective_pom) in cases.items():
            with self.subTest(case=case):
                pom = self.find_pom(mock_run, mock_glob, pom_declaring(build=build_element(declared)), effective_pom)
                with osv(ADVISORY):
                    update_pom_xmls()
                self.assert_vulnerable_dependency_logged(name, version, VULNERABILITY, Location(pom, line))

    def test_a_dependency_its_own_pom_manages_is_warned_about_at_the_managed_declaration_alone(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a dependency the pom declares without a version, and manages, is warned about once per check."""
        managed = dependency_management_element(guava_element("33.0.0-jre"))
        declared = pom_declaring(guava_element(None), coordinates=SCANNED_POM_COORDINATES, managed=managed)
        # The pom manages guava's version on line 9, and declares guava without a version on line 14.
        effective_managed = effective_dependency_element(GUAVA, "33.0.0-jre", line=8)
        unversioned = effective_dependency_element(GUAVA, "33.0.0-jre", line=16, managed_at=(SCANNED_POM_ID, 9))
        effective_pom = effective_pom_declaring(unversioned, managed=dependency_management_element(effective_managed))
        cases = {
            "staleness": (
                patch.object(maven_central_module, "project", Mock(return_value=_stale("33.0.0-jre", 500))),
                partial(self.assert_stale_dependency_logged, GUAVA, "33.0.0-jre"),
            ),
            "vulnerability": (
                osv(ADVISORY),
                partial(self.assert_vulnerable_dependency_logged, GUAVA, "33.0.0-jre", VULNERABILITY),
            ),
        }
        for case, (source, assert_warned) in cases.items():
            with self.subTest(case=case):
                pom = self.find_pom(mock_run, mock_glob, declared, effective_pom)
                with source:
                    update_pom_xmls()
                assert_warned(Location(pom, 9))

    @kills(
        Mutation(
            pom_xml_module._effective_artefacts,
            "_EffectiveArtefact(pinned, managed_by)",
            "_EffectiveArtefact(pinned, _InputLocation() if default_group else managed_by)",
            "a plugin is warned about twice: where the pom manages its version, and where it declares none",
        )
    )
    def test_a_plugin_its_own_pom_manages_is_warned_about_at_the_managed_declaration_alone(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a plugin the pom declares without a version, and manages, is warned about once."""
        managed = plugin_element("maven-surefire-plugin", "3.5.0")
        build = build_element(plugin_element("maven-surefire-plugin", None), managed=managed)
        declared = pom_declaring(coordinates=SCANNED_POM_COORDINATES, build=build)
        # The pom manages surefire's version on line 12, and declares surefire without a version on line 17.
        effective_managed = effective_plugin_element(SUREFIRE, "3.5.0", line=11)
        unversioned = effective_plugin_element(SUREFIRE, "3.5.0", line=19, managed_at=(SCANNED_POM_ID, 12))
        effective_pom = effective_pom_declaring(build=build_element(unversioned, managed=effective_managed))
        pom = self.find_pom(mock_run, mock_glob, declared, effective_pom)
        with patch.object(maven_central_module, "project", Mock(return_value=_stale("3.5.0", 500))):
            update_pom_xmls()
        self.assert_stale_dependency_logged(SUREFIRE, "3.5.0", Location(pom, 12))

    def test_a_dependency_another_scanned_pom_manages_is_warned_about_at_the_managed_declaration_alone(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a dependency a child declares without a version is warned about in the parent alone.

        The scan finds the child first, so the child's run needs the parent's name before the parent's own run.
        """
        unversioned = effective_dependency_element(GUAVA, "33.0.0-jre", line=7, managed_at=(PARENT_POM_ID, 10))
        _, parent_pom = self.find_poms(
            mock_run,
            mock_glob,
            _CHILD_LEAVING_GUAVAS_VERSION,
            _PARENT_MANAGING_GUAVA,
            effective_pom=effective_pom_declaring(unversioned),
        )
        with patch.object(maven_central_module, "project", Mock(return_value=_stale("33.0.0-jre", 500))):
            update_pom_xmls()
        self.assert_stale_dependency_logged(GUAVA, "33.0.0-jre", Location(parent_pom, 10))

    def test_a_dependency_whose_managed_version_its_own_pom_overrides_is_warned_about_at_that_version(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a child overriding the property its parent manages a version with is checked at its own version."""
        child = pom_declaring(
            guava_element(None),
            coordinates=SCANNED_POM_COORDINATES,
            properties=properties_element({"guava.version": "33.7.1-jre"}),
        )
        # The child declares guava without a version on line 8, and its `<artifactId>` on line 10.
        unversioned = effective_dependency_element(GUAVA, "33.7.1-jre", line=10, managed_at=(PARENT_POM_ID, 13))
        child_pom, parent_pom = self.find_poms(
            mock_run,
            mock_glob,
            child,
            _PARENT_MANAGING_GUAVA_BY_PROPERTY,
            effective_pom=effective_pom_declaring(unversioned),
        )
        with osv(ADVISORY):
            update_pom_xmls()
        self.assert_vulnerable_dependency_logged(
            GUAVA, "33.7.1-jre", VULNERABILITY, Location(child_pom, 8), among_others=True
        )
        self.assert_vulnerable_dependency_logged(
            GUAVA, "33.0.0-jre", VULNERABILITY, Location(parent_pom, 6), among_others=True
        )

    @kills(
        Mutation(
            pom_xml_module._managed_versions,
            "_interpolated(version.text, property_elements) for",
            "version.text for",
            "a managed version naming a property never matches a child's, so every child is warned about again",
        )
    )
    def test_a_dependency_whose_managed_version_its_managing_pom_resolves_is_warned_about_there_alone(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a child is skipped where the parent manages its version by a property the parent declares."""
        unversioned = effective_dependency_element(GUAVA, "33.0.0-jre", line=7, managed_at=(PARENT_POM_ID, 13))
        _, parent_pom = self.find_poms(
            mock_run,
            mock_glob,
            _CHILD_LEAVING_GUAVAS_VERSION,
            _PARENT_MANAGING_GUAVA_BY_PROPERTY,
            effective_pom=effective_pom_declaring(unversioned),
        )
        with osv(ADVISORY):
            update_pom_xmls()
        self.assert_vulnerable_dependency_logged(GUAVA, "33.0.0-jre", VULNERABILITY, Location(parent_pom, 6))

    def test_a_dependency_whose_managed_version_names_a_property_its_managing_pom_leaves_out_is_warned_about(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a child is checked where the parent manages its version by a property the parent inherits."""
        # The parent manages guava's version on line 10, by a property a pom above it declares.
        parent = pom_declaring(
            coordinates=pom_coordinates("parent", "org.example", "1.0"),
            managed=dependency_management_element(guava_element("${guava.version}")),
        )
        unversioned = effective_dependency_element(GUAVA, "33.0.0-jre", line=7, managed_at=(PARENT_POM_ID, 10))
        child_pom, parent_pom = self.find_poms(
            mock_run,
            mock_glob,
            _CHILD_LEAVING_GUAVAS_VERSION,
            parent,
            effective_pom=effective_pom_declaring(unversioned),
        )
        with patch.object(maven_central_module, "project", Mock(return_value=_stale("33.0.0-jre", 500))):
            update_pom_xmls()
        self.assert_stale_dependency_logged(GUAVA, "33.0.0-jre", Location(child_pom, 5), Location(parent_pom, 10))

    def test_a_dependency_whose_managing_pom_no_longer_parses_is_warned_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that a child's dependency is checked when Maven left the parent managing its version unparsable."""
        unversioned = effective_dependency_element(GUAVA, "33.0.0-jre", line=7, managed_at=(PARENT_POM_ID, 10))
        parent_pom, child_pom = self.find_poms(
            mock_run,
            mock_glob,
            _PARENT_MANAGING_GUAVA,
            _CHILD_LEAVING_GUAVAS_VERSION,
            effective_pom=effective_pom_declaring(unversioned),
        )

        def parent_contents() -> bytes:
            """Return what the parent holds, which the versions plugin's goals of its own run leave unparsable."""
            rewritten = any(_VERSIONS_GOALS[0] in run.args[0] for run in mock_run.call_args_list)
            return b"<project><broken>" if rewritten else _PARENT_MANAGING_GUAVA.encode()

        parent_pom.read_bytes = Mock(side_effect=parent_contents)
        with patch.object(maven_central_module, "project", Mock(return_value=_stale("33.0.0-jre", 500))):
            update_pom_xmls()
        self.assert_error_logged(Logger._MESSAGE_INVALID_XML_AFTER_UPDATE, location=Location(parent_pom))
        self.assert_stale_dependency_logged(GUAVA, "33.0.0-jre", Location(child_pom, 5))

    @kills(
        Mutation(
            maven_module._run_writing_effective_pom,
            "_LOG.effective_pom_unreadable(pom_xml)",
            "pass",
            "Update-time says nothing when it cannot read the effective pom, so the unchecked versions go unnoticed",
        )
    )
    def test_an_effective_pom_a_successful_run_left_unreadable_is_warned_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that a Maven run that succeeded without writing an effective pom is warned about."""
        pom = self.find_pom(mock_run, mock_glob, pom_declaring(guava_element("${guava.version}")), effective_pom="")
        update_pom_xmls()
        self.assert_logged(Logger._MESSAGE_EFFECTIVE_POM_UNREADABLE, location=Location(pom))

    @kills(
        Mutation(
            maven_module._run_writing_effective_pom,
            "_LOG.effective_pom_without_input_locations(pom_xml)",
            "pass",
            "an effective pom without input locations resolves nothing, and Update-time says nothing about it",
        ),
        Mutation(
            pom_xml_module.has_input_locations,
            "return bool(_effective_pom_name(effective_pom))",
            "return True",
            "every effective pom counts as having input locations, so one without them goes unnoticed",
        ),
        Mutation(
            pom_xml_module._input_location,
            "return _InputLocation()",
            "return _InputLocation(line=5)",
            "without input locations, a dependency takes the version of any entry the line number happens to match",
        ),
    )
    def test_an_effective_pom_without_input_locations_resolves_nothing_and_is_warned_about(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that an effective pom lacking input locations leaves each version to the pom, and is warned about."""
        # A project configuring the help plugin's `<verbose>` as false gets an effective pom without input locations.
        effective_pom = pom_declaring(guava_element("33.0.0-jre"))
        declared = pom_declaring(guava_element("${guava.version}"), dependency_element("junit", "junit", "4.13.2"))
        pom = self.find_pom(mock_run, mock_glob, declared, effective_pom)
        with osv() as mock_post:
            update_pom_xmls()
        self.assert_logged(Logger._MESSAGE_EFFECTIVE_POM_WITHOUT_INPUT_LOCATIONS, location=Location(pom))
        assert_osv_asked_about(mock_post, ("junit:junit", "4.13.2"), ecosystem="Maven")

    @kills(
        Mutation(
            maven_module._run_writing_effective_pom,
            "effective_pom = xml.read(output)",
            "effective_pom = xml.read(output) if result.succeeded else None",
            "a failed run discards the effective pom it wrote, so the versions the parent declares go unchecked",
        )
    )
    def test_a_run_failing_after_the_effective_pom_still_resolves_a_version_the_parent_declares(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that the effective pom a failed run wrote before its failure gives a parent's version all the same."""
        self.find_pom(
            mock_run,
            mock_glob,
            pom_declaring(guava_element("${guava.version}")),
            effective_pom_declaring(EFFECTIVE_GUAVA),
        )
        output = "[ERROR] Failed to execute goal org.codehaus.mojo:versions-maven-plugin:2.22.0:use-latest-releases"
        mock_run.side_effect = _maven_failed(output)
        with osv() as mock_post:
            update_pom_xmls()
        self.assert_command_failed_logged(_maven_command(), output)
        assert_osv_asked_about(mock_post, (GUAVA, "33.0.0-jre"), ecosystem="Maven")

    @kills(
        Mutation(
            maven_module._run_writing_effective_pom,
            "effective_pom = _effective_pom_written_elsewhere(result.stdout)",
            "effective_pom = None",
            "the effective pom of a project configuring the help plugin's output goes unread",
        ),
        Mutation(
            maven_module._run_writing_effective_pom,
            "_effective_pom_written_elsewhere(result.stdout)",
            "_effective_pom_written_elsewhere(result.stdout) if result.succeeded else None",
            "a failed run discards the effective pom it wrote elsewhere, so what the parent declares goes unchecked",
        ),
    )
    def test_an_effective_pom_the_project_writes_elsewhere_is_read_where_maven_says(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that the effective pom is read at the path Maven prints, where the project's help plugin sends it."""
        # The project configures the help plugin's `<output>`, which wins over Update-time's, so its file stays empty.
        printed = "[INFO] Effective-POM written to: /project/target/effective-pom.xml\n"
        failed = "[ERROR] Failed to execute goal org.codehaus.mojo:versions-maven-plugin:2.22.0:use-latest-releases\n"
        # Each case names what Maven's run ends in, and the output of a failed run, which is reported as an error.
        cases = {
            "a run that succeeded": (Mock(stdout=printed, stderr=""), None),
            "a run that failed after writing it": (
                _maven_failed(printed + failed),
                printed + failed,
            ),
        }
        written = effective_pom_declaring(EFFECTIVE_GUAVA).encode()
        for case, (outcome, failure) in cases.items():
            with self.subTest(case=case):
                self.find_pom(mock_run, mock_glob, pom_declaring(guava_element("${guava.version}")), effective_pom="")
                mock_run.side_effect = [outcome]
                read_bytes = patch.object(Path, "read_bytes", autospec=True, return_value=written)
                with read_bytes as mock_read_bytes, osv() as mock_post:
                    update_pom_xmls()
                if failure:
                    self.assert_command_failed_logged(_maven_command(), failure)
                mock_read_bytes.assert_called_once_with(Path("/project/target/effective-pom.xml"))
                assert_osv_asked_about(mock_post, (GUAVA, "33.0.0-jre"), ecosystem="Maven")
                self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "with_resolved_coordinates(resolved)",
            "with_resolved_coordinates(after)",
            "staleness reads the pom alone, so a dependency whose group the parent declares goes unchecked",
        ),
    )
    def test_a_stale_dependency_is_warned_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency whose newest release is old is warned about, as the effective pom names it."""
        # The parent declares guava's group, so only the effective pom names guava in full.
        declared = pom_declaring(dependency_element("${guava.group}", "guava", "33.0.0-jre"))
        pom = self.find_pom(mock_run, mock_glob, declared, effective_pom_declaring(EFFECTIVE_GUAVA))
        with patch.object(maven_central_module, "project", Mock(return_value=_stale("33.0.0-jre", 500))):
            update_pom_xmls()
        self.assert_stale_dependency_logged(GUAVA, "33.0.0-jre", Location(pom, 6))

    @kills(
        Mutation(
            pom_xml_module._effective_artefacts,
            "for tag, default_group in _ARTEFACT_ELEMENTS.items()",
            'for tag, default_group in {"dependency": ""}.items()',
            "the plugin's name keeps the group's property where the parent declares it, so it goes unchecked",
        ),
        Mutation(
            pom_xml_module._effective_artefacts,
            "group_name = default_group if group is None",
            'group_name = "" if group is None',
            "a plugin in Maven's default group is asked about without its group, since the effective pom omits it",
        ),
    )
    def test_a_stale_plugin_is_warned_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that a plugin whose newest release is old is warned about, as the effective pom names it."""
        # The warning names the line of the `<version>` element, or of the `<plugin>` lacking one.
        cases = {
            "a version": (plugin_element("maven-surefire-plugin", "3.5.0"), SUREFIRE, 9, _EMPTY_EFFECTIVE_POM),
            "no version": (plugin_element("maven-surefire-plugin", None), SUREFIRE, 6, _EMPTY_EFFECTIVE_POM),
            "a parent's group": (_VERSIONS_LEAVING_ITS_GROUP, _VERSIONS, 9, _EFFECTIVE_VERSIONS),
            "a parent's default group": (
                plugin_element("maven-surefire-plugin", "3.5.0", "${plugin.group}"),
                SUREFIRE,
                9,
                _EFFECTIVE_SUREFIRE,
            ),
        }
        for case, (declared, name, line, effective_pom) in cases.items():
            with self.subTest(case=case):
                pom = self.find_pom(mock_run, mock_glob, pom_declaring(build=build_element(declared)), effective_pom)
                with patch.object(maven_central_module, "project", Mock(return_value=_stale("3.5.0", 500))):
                    update_pom_xmls()
                self.assert_stale_dependency_logged(name, "3.5.0", Location(pom, line))

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
        pom = self.find_pom(mock_run, mock_glob, pom_declaring(guava_element("33.0.0-jre")))
        with patch.object(maven_central_module, "project", Mock(side_effect=_archived)):
            update_pom_xmls()
        self.assert_archived_repository_logged(GUAVA, Location(pom, 6))

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
        pom = self.find_pom(mock_run, mock_glob, pom_declaring(guava_element("33.0.0-jre")))
        served = maven_central_pom("scm:git:https://github.com/google/guava.git")
        with (
            patch.object(maven_central_module, "project", maven_central_project),
            patch_maven_central(_LISTING, served, archived=True),
        ):
            update_pom_xmls()
        self.assert_archived_repository_logged(GUAVA, Location(pom, 6))

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
        self.find_pom(mock_run, mock_glob, pom_declaring(guava_element("33.0.0-jre")))
        mock_project = Mock(side_effect=_archived)
        with patch.object(maven_central_module, "project", mock_project):
            update_pom_xmls()
        mock_project.assert_called_once_with(GUAVA, check_archival=False)
        self.assert_no_warnings_logged()

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
        pom = pom_declaring(
            guava_element("33.0.0-jre"), dependency_element("org.springframework", "spring-core", "6.1.0")
        )
        self.find_pom(mock_run, mock_glob, pom)
        held_back = {
            GUAVA: ("33.7.0-jre", "33.7.1-jre"),
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
            pom_xml_module.with_resolved_coordinates,
            " if _is_resolved(declaration.dependency)",
            "",
            "a pom inheriting a group from its parent is asked about coordinates that resolve to nothing",
        ),
        Mutation(
            pom_xml_module.fully_resolved,
            "(group_id, artifact_id, pinned.version)",
            "(pinned.version,)",
            "OSV is asked about coordinates holding an unresolved property, which it matches nothing to",
        ),
        Mutation(
            update_pom_xml_module._report_new_versions,
            "is_resolved = pom_xml_format.fully_resolved(named.pinned)",
            "is_resolved = True",
            "Maven Central is asked for the changes of coordinates holding an unresolved property",
        ),
        Mutation(
            maven_module.update_pom_xml,
            "if succeeded:",
            "if effective_pom is not None:",
            "Update-time skips the updates wherever it cannot read the effective pom, although the run succeeded",
        ),
        Mutation(
            pom_xml_module.artefacts,
            " and _is_resolved(resolved.dependency)",
            "",
            "the repository is asked which versions to hold back for coordinates holding a parent's property",
        ),
    )
    def test_coordinates_naming_a_property_without_an_effective_pom_are_not_asked_about(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that every check passes over a dependency whose group the parent declares, without an effective pom."""
        # A pom can spell a dependency's group as a property its parent declares, which this pom does not hold. Maven
        # rewrote the pom, but Update-time cannot read the effective pom that would resolve the group.
        inherited = dependency_element("${spring.group}", "spring-core", "${spring.version}")
        before = pom_declaring(
            guava_element("33.0.0-jre"), inherited, properties=properties_element({"spring.version": "6.1.0"})
        )
        after = pom_declaring(
            guava_element("33.7.1-jre"), inherited, properties=properties_element({"spring.version": "7.1.0"})
        )
        self.find_rewritten_pom(mock_run, mock_glob, before, after, effective_pom="")
        mock_project = Mock(return_value=Project())
        with (
            self.asked_about() as artefacts,
            osv() as mock_post,
            patch.object(maven_central_module, "project", mock_project),
            patch.object(maven_central_module, "get_changes", Mock(return_value=NO_CHANGES)) as mock_changes,
        ):
            update_pom_xmls()
        # Guava reaches every check, so the dependency beside it reaching none says something about its coordinates.
        self.assertEqual(artefacts, [GUAVA])
        assert_osv_asked_about(mock_post, (GUAVA, "33.7.1-jre"), ecosystem="Maven")
        self.assertEqual(_artefacts_asked(mock_project), [GUAVA])
        self.assertEqual(_artefacts_asked(mock_changes), [GUAVA])

    @kills(
        Mutation(
            pom_xml_module.artefacts,
            "own.current_version and ",
            "",
            "the repository is asked which versions to hold back for a dependency whose version Maven never moves",
        ),
        Mutation(
            pom_xml_module.artefacts,
            "own.current_version and",
            "resolved.current_version and",
            "the repository is asked which versions to hold back for a managed version, which Maven never moves here",
        ),
    )
    def test_the_cooldown_skips_a_dependency_without_a_version(self, mock_run: Mock, mock_glob: Mock):
        """Test that the repository is not asked which versions to hold back for a dependency Maven does not update."""
        # In the last case guava's group names a parent's property, so the rule set reads the effective pom, which gives
        # spring the version the parent manages.
        managed = effective_pom_declaring(
            EFFECTIVE_GUAVA,
            effective_dependency_element(
                "org.springframework:spring-core", "6.1.0", line=10, managed_at=(PARENT_POM_ID, 12)
            ),
        )
        cases = {
            "no version": (guava_element("33.0.0-jre"), None, _EMPTY_EFFECTIVE_POM),
            "an empty version": (guava_element("33.0.0-jre"), "", _EMPTY_EFFECTIVE_POM),
            "a version the parent manages": (
                dependency_element("${guava.group}", "guava", "33.0.0-jre"),
                None,
                managed,
            ),
        }
        for case, (guava, version, effective_pom) in cases.items():
            with self.subTest(case=case):
                unversioned = dependency_element("org.springframework", "spring-core", version)
                self.find_pom(mock_run, mock_glob, pom_declaring(guava, unversioned), effective_pom)
                with self.asked_about() as artefacts:
                    update_pom_xmls()
                # Guava is asked about, so the dependency beside it going unasked says something about its version.
                self.assertEqual(artefacts, [GUAVA])

    @kills(
        Mutation(
            maven_module._rules,
            "versions_held_back(artefact, cooldown_days)",
            "versions_held_back(artefact, 7)",
            "every run asks about the default cooldown, so --cooldown never reaches a Maven dependency",
        )
    )
    @patch_environ({COOLDOWN.name: "30"})
    def test_the_repository_is_asked_about_the_window_the_run_sets(self, mock_run: Mock, mock_glob: Mock):
        """Test that the cooldown the repository is asked about is the one --cooldown sets, not the default one."""
        self.find_pom(mock_run, mock_glob, pom_declaring(guava_element("33.0.0-jre")))
        with self.hold_back({GUAVA: ("33.7.1-jre",)}, cooldown_days=30) as rule_sets:
            update_pom_xmls()
        self.assertEqual(rule_sets, [_rule_set(_rule("com.google.guava", "guava", "33.7.1-jre"))])

    def test_a_plugin_versioned_by_a_property_gets_a_rule(self, mock_run: Mock, mock_glob: Mock):
        """Test that the rule set names a plugin the pom versions through a property, group declared or not."""
        cases = {"the pom declares the group": "org.apache.maven.plugins", "Maven defaults the group": None}
        for case, group in cases.items():
            with self.subTest(case=case):
                surefire = plugin_element("maven-surefire-plugin", "${surefire.version}", group=group)
                properties = properties_element({"surefire.version": "3.5.0"})
                pom = pom_declaring(guava_element("33.0.0-jre"), properties=properties, build=build_element(surefire))
                self.find_pom(mock_run, mock_glob, pom)
                with self.hold_back({SUREFIRE: ("3.6.0",)}) as rule_sets:
                    update_pom_xmls()
                surefire_rule = _rule("org.apache.maven.plugins", "maven-surefire-plugin", "3.6.0")
                self.assertEqual(rule_sets, [_rule_set(surefire_rule)])

    @kills(
        Mutation(
            maven_module.update_pom_xml,
            "pom_xml_format.artefacts(pom_xml, effective_pom)",
            "pom_xml_format.artefacts(pom_xml)",
            "the rule set reads the pom alone, so a dependency whose group the parent declares escapes the cooldown",
        ),
        Mutation(
            maven_module.update_pom_xml,
            "_run(pom_xml, options, _VERSIONS_GOALS)",
            "_run(pom_xml, _VERSIONS_OPTIONS, _VERSIONS_GOALS)",
            "the rule set stays out of the versions run, so a group the parent declares escapes the cooldown",
        ),
    )
    def test_coordinates_naming_a_parents_property_get_a_rule_as_maven_resolves_them(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that the rule set names a dependency or plugin whose group or artifact the parent declares."""
        spring = "org.springframework:spring-core"
        # Maven lists the properties the parent declares in the effective pom.
        properties = properties_element({"spring.artifact": "spring-core"})
        effective_spring = effective_pom_declaring(_EFFECTIVE_SPRING, properties=properties)
        cases = {
            "a dependency's group": (
                pom_declaring(_SPRING_LEAVING_ITS_GROUP),
                effective_spring,
                _rule("org.springframework", "spring-core", "7.1.0"),
            ),
            "a dependency's artifact": (
                pom_declaring(dependency_element("org.springframework", "${spring.artifact}", "6.1.0")),
                effective_spring,
                _rule("org.springframework", "spring-core", "7.1.0"),
            ),
            "a plugin's group": (
                pom_declaring(build=build_element(_VERSIONS_LEAVING_ITS_GROUP)),
                _EFFECTIVE_VERSIONS,
                _rule("org.codehaus.mojo", "versions-maven-plugin", "2.19.0"),
            ),
        }
        for case, (declared, effective_pom, rule) in cases.items():
            with self.subTest(case=case):
                self.find_pom(mock_run, mock_glob, declared, effective_pom)
                with self.hold_back({spring: ("7.1.0",), _VERSIONS: ("2.19.0",)}) as rule_sets:
                    update_pom_xmls()
                self.assertEqual(rule_sets, [_rule_set(rule)])
                self.assert_maven_runs(mock_run, _effective_pom_command(), _versions_command(_RULES))

    @kills(
        Mutation(
            maven_module.update_pom_xml,
            "_run(pom_xml, options, _VERSIONS_GOALS)",
            "_run_writing_effective_pom(pom_xml, options, _VERSIONS_GOALS)",
            "the updates write the effective pom again, costing a goal per pom that the first run already paid for",
        ),
        Mutation(
            maven_module.update_pom_xml,
            "if not pom_xml_format.leaves_coordinates_unresolved(pom_xml):",
            "if not pom_xml_format.leaves_coordinates_unresolved(pom_xml) or True:",
            "the rule set is written before Maven resolves what the parent declares, so the cooldown misses it",
        ),
    )
    def test_a_pom_leaving_coordinates_to_its_parent_gets_the_effective_pom_in_a_run_of_its_own(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that the effective pom is written in a Maven run of its own, before the run that updates the pom."""
        self.find_pom(
            mock_run, mock_glob, pom_declaring(_SPRING_LEAVING_ITS_GROUP), effective_pom_declaring(_EFFECTIVE_SPRING)
        )
        update_pom_xmls()
        self.assert_maven_runs(mock_run, _effective_pom_command(), _versions_command())

    @kills(
        Mutation(
            maven_module.update_pom_xml,
            "if succeeded:",
            "if True:",
            "the updates run on a model Maven could not build, so its failure is reported a second time",
        )
    )
    def test_a_failed_effective_pom_run_is_reported_once_and_skips_the_updates(self, mock_run: Mock, mock_glob: Mock):
        """Test that a failed run writing the effective pom is reported with its output, and the updates do not run."""
        # Maven stops before any goal runs, so the file for the effective pom stays empty.
        self.find_pom(mock_run, mock_glob, pom_declaring(_SPRING_LEAVING_ITS_GROUP), effective_pom="")
        output = "[ERROR] Non-resolvable parent POM for org.example:child:1.0"
        mock_run.side_effect = _maven_failed(output)
        update_pom_xmls()
        self.assert_command_failed_logged(_effective_pom_command(), output)
        self.assert_maven_runs(mock_run, _effective_pom_command())
        self.assert_no_warnings_logged()

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
        self.find_pom(mock_run, mock_glob, pom_declaring(guava_element("33.0.0-jre")))
        with self.asked_about() as artefacts:
            update_pom_xmls()
        self.assertEqual(artefacts, [])
        # Asserting Maven ran, so the cooldown rather than a pom that was never read is what left the artefacts alone.
        # The staleness check asks the repository whatever the cooldown is, so this says nothing about that request.
        self.assert_maven_ran(mock_run)

    @kills(
        Mutation(
            maven_module._run,
            "not result.succeeded",
            "result.succeeded",
            "a Maven run that failed passes silently, since Maven writes its errors to stdout rather than stderr",
        ),
        Mutation(
            maven_module._run_writing_effective_pom,
            "if result.succeeded and effective_pom is None:",
            "if effective_pom is None:",
            "a failed run is reported twice: once with Maven's error, and again as an effective pom it could not read",
        ),
    )
    def test_a_failed_maven_run_is_reported_with_its_output(self, mock_run: Mock, mock_glob: Mock):
        """Test that a Maven run exiting non-zero is reported with what it wrote, and with nothing else."""
        # Maven stops before any goal runs, so the file for the effective pom stays empty.
        self.find_pom(mock_run, mock_glob, pom_declaring(guava_element("33.0.0-jre")), effective_pom="")
        # Maven writes its errors to stdout and leaves stderr empty, so the failure travels in the output.
        output = "[ERROR] Non-readable POM /project/pom.xml"
        mock_run.side_effect = _maven_failed(output)
        update_pom_xmls()
        self.assert_command_failed_logged(_maven_command(), output)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_pom_xml_module._report_new_versions,
            "_LOG.new_version(updated, DependencyVersion(new.current_version, changes))",
            "_LOG.new_version(updated, DependencyVersion(new.current_version, changes))\n        return",
            "only the first dependency Maven moved is reported, and the rest of the pom's are lost",
        ),
        Mutation(
            update_pom_xml_module._report_new_versions,
            "get_changes(named.dependency, new.current_version)",
            "get_changes(named.dependency, old.current_version)",
            "a dependency Maven moved is reported with the changes of the version it moved away from",
        ),
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "_report_new_versions(before, after, resolved)",
            "_report_new_versions(before, after, after)",
            "the report names a dependency as the pom spells it, so a group the parent declares stays a property",
        ),
    )
    def test_each_rewritten_dependency_is_reported_at_its_line_as_maven_names_it_with_its_changes(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that each dependency Maven rewrote is reported at its line, as Maven names it, with its changes."""
        # The parent declares spring's group, so the report takes spring's name from the effective pom.
        before = pom_declaring(
            guava_element("33.0.0-jre"), dependency_element("${spring.group}", "spring-core", "6.1.0")
        )
        after = pom_declaring(
            guava_element("33.7.1-jre"), dependency_element("${spring.group}", "spring-core", "7.1.0")
        )
        effective_pom = effective_pom_declaring(
            EFFECTIVE_GUAVA,
            effective_dependency_element("org.springframework:spring-core", "6.1.0", line=10),
        )
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, after, effective_pom)

        def changes(artefact: str, version: str) -> Changes:
            return Changes(f"Changes in {artefact} {version}", markdown=False)

        with patch.object(maven_central_module, "get_changes", changes):
            update_pom_xmls()
        self.assert_new_version_logged_among_others_with_changes(
            GUAVA, "33.7.1-jre", Location(pom, 6), "Changes in com.google.guava:guava 33.7.1-jre"
        )
        self.assert_new_version_logged_among_others_with_changes(
            "org.springframework:spring-core",
            "7.1.0",
            Location(pom, 11),
            "Changes in org.springframework:spring-core 7.1.0",
        )
        self.assertEqual(len(self.new_version_records()), 2)

    @kills(
        Mutation(
            update_pom_xml_module._report_new_versions,
            "zip(before, after, resolved, strict=True)",
            "{n.location: (o, n, r) for o, n, r in zip(before, after, resolved, strict=True)}.values()",
            "one declaration per line is reported, so of two naming the same property only one is",
        )
    )
    def test_a_dependency_and_a_plugin_sharing_a_property_are_each_reported_at_it(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a dependency and a plugin naming the same property are both reported, at that property's line."""
        provider = dependency_element("org.apache.maven.surefire", "surefire-junit-platform", "${surefire.version}")
        plugin = build_element(plugin_element("maven-surefire-plugin", "${surefire.version}", group=None))
        before = pom_declaring(provider, properties=properties_element({"surefire.version": "3.5.0"}), build=plugin)
        after = pom_declaring(provider, properties=properties_element({"surefire.version": "3.5.2"}), build=plugin)
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, after)
        update_pom_xmls()
        self.assert_new_version_logged_among_others(
            "org.apache.maven.surefire:surefire-junit-platform", "3.5.2", Location(pom, 3)
        )
        self.assert_new_version_logged_among_others(SUREFIRE, "3.5.2", Location(pom, 3))
        self.assertEqual(len(self.new_version_records()), 2)

    def test_a_property_no_dependency_names_is_reported_for_none(self, mock_run: Mock, mock_glob: Mock):
        """Test that a property Maven advanced is reported for no dependency when no `<version>` names it."""
        before = pom_declaring(guava_element("33.0.0-jre"), properties=properties_element({"unused.version": "1.0"}))
        after = pom_declaring(guava_element("33.7.1-jre"), properties=properties_element({"unused.version": "2.0"}))
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, after)
        update_pom_xmls()
        # Guava is the only report, so the property Maven advanced beside it was reported for nothing.
        self.assert_new_version_logged(GUAVA, "33.7.1-jre", Location(pom, 9))

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
        before = pom_declaring(
            guava_element("33.7.1-jre"), managed=dependency_management_element(guava_element("33.0.0-jre"))
        )
        after = pom_declaring(
            guava_element("33.7.1-jre"), managed=dependency_management_element(guava_element("33.7.1-jre"))
        )
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, after)
        update_pom_xmls()
        self.assert_new_version_logged(GUAVA, "33.7.1-jre", Location(pom, 7))

    @kills(
        Mutation(
            update_pom_xml_module._report_new_versions,
            "old.current_version == new.current_version",
            "False",
            "every dependency is reported as moved, whether or not Maven changed its version",
        ),
        Mutation(
            update_pom_xml_module._report_new_versions,
            "if old.current_version == new.current_version:\n",
            "maven_central.get_changes(new.dependency, new.current_version)\n"
            "        if old.current_version == new.current_version:\n",
            "Update-time asks for the changes of every dependency the pom declares, whether or not Maven moved it",
        ),
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "_report_new_versions(before, after, resolved)",
            "_report_new_versions(before, resolved, resolved)",
            "Update-time reports a move from the parent's property to the version it holds, though Maven moved nothing",
        ),
    )
    def test_a_dependency_maven_left_alone_is_neither_reported_nor_asked_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that nothing in a pom Maven left alone is reported or asked about, what the parent versions included."""
        spring = dependency_element("org.springframework", "spring-core", "${spring.version}")
        junit = dependency_element("junit", "junit", None)
        unchanged = pom_declaring(guava_element("33.7.1-jre"), spring, junit)
        effective_guava = effective_dependency_element(GUAVA, "33.7.1-jre", line=5)
        effective_spring = effective_dependency_element("org.springframework:spring-core", "6.1.0", line=10)
        parent_managed = (PARENT_POM_ID, 20)
        effective_junit = effective_dependency_element("junit:junit", "4.13.2", line=15, managed_at=parent_managed)
        effective_pom = effective_pom_declaring(effective_guava, effective_spring, effective_junit)
        self.find_rewritten_pom(mock_run, mock_glob, unchanged, unchanged, effective_pom)
        with patch.object(maven_central_module, "get_changes") as get_changes:
            update_pom_xmls()
        self.assert_maven_ran(mock_run)  # The pom was examined, so reporting nothing says something.
        self.assert_no_new_version_logged()
        get_changes.assert_not_called()

    @kills(
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "    return\n    effective_pom",
            "effective_pom",
            "Maven runs on a pom Update-time cannot read, rewriting a file it could not parse",
        )
    )
    def test_an_unparsable_pom_is_skipped(self, mock_run: Mock, mock_glob: Mock):
        """Test that a pom whose XML does not parse is warned about, and leaves the next pom checked as usual."""
        broken, second = self.find_poms(
            mock_run, mock_glob, "<project><broken>", pom_declaring(guava_element("33.7.1-jre"))
        )
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
        pom = self.find_rewritten_pom(
            mock_run, mock_glob, pom_declaring(guava_element("33.0.0-jre")), "<project><broken>"
        )
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
            "_LOG.declarations_changed(pom_xml, len(before), len(after))\n        _check_projects(before)",
            "a pom Update-time gave up on is checked for staleness all the same, beside the error saying it was not",
        ),
    )
    def test_a_pom_holding_fewer_declarations_after_the_run_is_an_error(self, mock_run: Mock, mock_glob: Mock):
        """Test that a pom Maven removed a declaration from is reported as an error, and left unchecked."""
        before = pom_declaring(
            guava_element("33.0.0-jre"), dependency_element("org.springframework", "spring-core", "6.1.0")
        )
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, pom_declaring(guava_element("33.7.1-jre")))
        mock_project = Mock(return_value=Project())
        with patch.object(maven_central_module, "project", mock_project):
            update_pom_xmls()
        self.assert_error_logged(Logger._MESSAGE_DECLARATIONS_CHANGED, location=Location(pom), before=2, after=1)
        self.assert_no_new_version_logged()
        mock_project.assert_not_called()  # The pom is reported, not checked on a reading it cannot pair up.

    @kills(
        Mutation(
            maven_module._run,
            " and result.stdout:",
            ":",
            "a missing Maven is reported twice: once by `run`, and again as a failed run that wrote nothing",
        ),
        Mutation(
            update_pom_xml_module.update_pom_xmls,
            "_update_pom_xml(pom_xml, scanned_poms)",
            "_update_pom_xml(pom_xml, scanned_poms)\n        return",
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
        ),
        Mutation(
            maven_module._maven,
            "(pom_xml.parent / _WRAPPER).exists()",
            "Path(_WRAPPER).exists()",
            "the wrapper is looked for in the directory the scan runs in, so a module's own wrapper goes unused",
        ),
    )
    def test_the_maven_wrapper_runs_when_it_sits_beside_the_pom(self, mock_run: Mock, mock_glob: Mock):
        """Test that the project's own Maven wrapper runs, rather than the mvn on the path."""
        self.find_pom(mock_run, mock_glob)
        with patch("pathlib.Path.exists", lambda self: self == _PROJECT / "mvnw"):
            update_pom_xmls()
        self.assert_maven_ran(mock_run, "./mvnw")
