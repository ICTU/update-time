"""Unit tests for the pom.xml updater."""

import contextlib
import re
from pathlib import Path
from subprocess import CalledProcessError  # nosec
from typing import TYPE_CHECKING
from unittest.mock import Mock, call, patch

from update_time.domain.bound import Verb, _Redundancy
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import NO_CHANGES, Archival, ArchivedSubject, Changes, Project, Release
from update_time.io.log import Logger, LogMessage
from update_time.manifests import pom_xml as pom_xml_module
from update_time.markers.directive import Reason
from update_time.markers.marker import Marker, Scope, Threshold
from update_time.package_managers import maven as maven_module
from update_time.package_managers import maven_range as maven_range_module
from update_time.primitives.command import Command
from update_time.primitives.location import Location
from update_time.references import delegated as delegated_module
from update_time.sources import maven_central as maven_central_module
from update_time.sources.maven_central import lacks_a_github_repository as maven_central_lacks_a_github_repository
from update_time.sources.maven_central import project as maven_central_project
from update_time.sources.maven_central import serves as maven_central_serves
from update_time.updaters import update_pom_xml as update_pom_xml_module
from update_time.updaters.update_pom_xml import update_pom_xmls

from tests.helpers import mock_path, patch_environ, patch_get, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import BARE_IGNORE
from tests.update_time.helpers import (
    EFFECTIVE_GUAVA,
    GITBOX_SCM,
    GUAVA,
    GUAVA_SCM,
    PARENT_POM_ID,
    SCANNED_POM_COORDINATES,
    SCANNED_POM_ID,
    SUREFIRE,
    LoggingTestCase,
    archival_check_disabled,
    bound,
    build_element,
    days_ago,
    dependency_element,
    dependency_management_element,
    effective_dependency_element,
    effective_plugin_element,
    effective_pom_declaring,
    guava_element,
    marked_at_its_version,
    maven_central_listing,
    maven_central_listing_url,
    maven_central_pom,
    maven_central_pom_url,
    maven_central_version_row,
    patch_maven_central,
    plugin_element,
    pom_coordinates,
    pom_declaring,
    properties_element,
    staleness_disabled,
)
from tests.update_time.sources.helpers import requested_urls
from tests.update_time.updaters.fixtures import ADVISORY, VULNERABILITY
from tests.update_time.updaters.helpers import assert_osv_asked_about, no_vulnerabilities, osv

if TYPE_CHECKING:
    from collections.abc import Generator


# The tests discover each pom in this directory or in one below it. The scan runs in `/`, so a Maven run in the wrong
# directory shows.
_PROJECT = Path("/project")

# The rule set file these tests hand Update-time, standing in for the temporary file a real run writes.
_RULES = Path("/rules.xml")

# Maven writes the effective pom to this file in these tests. It stands in for the temporary file a real run names.
_EFFECTIVE_POM = "/effective-pom.xml"

# The listing the repository serves where a test lets it answer for itself: guava's version, dated yesterday.
_LISTING = maven_central_listing(maven_central_version_row("33.0.0-jre", days_ago(1)))

# The poms Maven Central serves beside guava's version where a test has it name a repository on GitHub, or outside it.
_POM_ON_GITHUB = maven_central_pom(GUAVA_SCM)
_POM_OUTSIDE_GITHUB = maven_central_pom(GITBOX_SCM)


@contextlib.contextmanager
def _stale_release(version: str = "33.0.0-jre") -> Generator[None]:
    """Have Maven Central report the version as the newest release of every artefact, published 500 days ago."""
    with patch.object(
        maven_central_module, "project", Mock(return_value=Project(newest=Release(version, days_ago(500))))
    ):
        yield


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


def _rule(group: str, artifact: str, current: str, *versions: str) -> str:
    """Return the rule Update-time writes for the artefact on the current version, naming the given versions."""
    return _rule_ignoring(group, artifact, _variant_element(current), *map(_exact_version_element, versions))


def _exact_version_element(version: str) -> str:
    """Return the `<ignoreVersion>` element matching the version exactly."""
    return f"<ignoreVersion>{version}</ignoreVersion>"


def _variant_element(*currents: str) -> str:
    """Return the `<ignoreVersion>` element matching every version on another variant than the current versions."""
    return f'<ignoreVersion type="regex">{maven_module._other_variants(*currents)}</ignoreVersion>'


def _rule_ignoring(group: str, artifact: str, *ignore_versions: str) -> str:
    """Return a rule for the artefact holding the given `<ignoreVersion>` elements."""
    ignored = "".join(f"        {ignore_version}\n" for ignore_version in ignore_versions)
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


# The rule set a run writes where a marker holds back every version of guava.
_GUAVA_HELD_BACK = _rule_set(
    _rule_ignoring("com.google.guava", "guava", '<ignoreVersion type="regex">.*</ignoreVersion>')
)


def _guava_range_held_back(*maven_ranges: str, versions: tuple[str, ...] = ()) -> str:
    """Return the rule set a run writes for guava: its markers' ranges, its variant, then the cooldown's versions.

    Each guava the callers declare is on the `-jre` variant, so one variant element serves each current version.
    """
    ranges = (f'<ignoreVersion type="range">{maven_range}</ignoreVersion>' for maven_range in maven_ranges)
    variant = _variant_element("33.0.0-jre")
    return _rule_set(
        _rule_ignoring("com.google.guava", "guava", *ranges, variant, *map(_exact_version_element, versions))
    )


# The rule set a run writes where the cooldown holds back guava 33.7.1-jre.
_GUAVA_33_7_1_HELD_BACK = _rule_set(_rule("com.google.guava", "guava", "33.0.0-jre", "33.7.1-jre"))

# The rule set a run writes where nothing but guava's `-jre` variant holds a version back.
_GUAVA_ON_ITS_VARIANT = _rule_set(_rule("com.google.guava", "guava", "33.0.0-jre"))


# The goal writing the effective pom, and its options. Each goal names the plugin release the shipped pom declares.
_EFFECTIVE_POM_GOAL = f"org.apache.maven.plugins:maven-help-plugin:{_plugin_version('help')}:effective-pom"
_EFFECTIVE_POM_OPTIONS = ("-Dverbose", f"-Doutput={_EFFECTIVE_POM}")

# The versions plugin's two goals.
_VERSIONS_GOALS = (
    f"org.codehaus.mojo:versions-maven-plugin:{_plugin_version('versions')}:use-latest-releases",
    f"org.codehaus.mojo:versions-maven-plugin:{_plugin_version('versions')}:update-properties",
)


def _versions_options(rules: Path | None, properties: str = "") -> tuple[str, ...]:
    """Return the options the versions plugin's goals take, naming the rule set file where a run holds versions back."""
    rule_set = (f"-Dmaven.version.rules={rules.as_uri()}",) if rules else ()
    pre_releases = f"-Dmaven.version.ignore={maven_module._PRE_RELEASES}"
    return ("-DgenerateBackupPoms=false", pre_releases, f"-DincludeProperties={properties}", *rule_set)


def _options(directory: str = ".") -> tuple[str, ...]:
    """Return the options opening each Maven command, which name the pom's directory relative to the reactor root."""
    return ("--batch-mode", "--no-transfer-progress", "--projects", directory)


def _maven_command(
    executable: str = "mvn", rules: Path | None = None, directory: str = ".", properties: str = ""
) -> Command:
    """Return the command Update-time runs over a pom whose coordinates resolve: the effective pom, then the updates."""
    return Command(
        executable,
        *_options(directory),
        *_versions_options(rules, properties),
        *_EFFECTIVE_POM_OPTIONS,
        _EFFECTIVE_POM_GOAL,
        *_VERSIONS_GOALS,
    )


def _effective_pom_command() -> Command:
    """Return the command writing the effective pom of a pom leaving a group or an artifact to its parent."""
    return Command("mvn", *_options(), *_EFFECTIVE_POM_OPTIONS, _EFFECTIVE_POM_GOAL)


def _versions_command(rules: Path, properties: str = "") -> Command:
    """Return the command updating a pom leaving a group or an artifact to its parent, after its effective pom."""
    return Command("mvn", *_options(), *_versions_options(rules, properties), *_VERSIONS_GOALS)


def _maven_run(command: Command, directory: Path = _PROJECT) -> object:
    """Return the call that runs the Maven command in the directory."""
    return call(command, capture_output=True, text=True, check=True, cwd=directory)


def _aggregator(*paths: str) -> str:
    """Return a pom listing a module at each of the paths in its own `<modules>` element."""
    modules = "".join(f"<module>{path}</module>" for path in paths)
    return f"<project><modules>{modules}</modules></project>"


def _maven_succeeded() -> Mock:
    """Return the result of a Maven run that exits zero, having written nothing."""
    return Mock(stdout="", stderr="")


def _maven_failed(output: str) -> CalledProcessError:
    """Return the error a Maven run raises when it exits non-zero, having written its output to stdout."""
    return CalledProcessError(cmd="", returncode=1, output=output, stderr="")


# What a Maven run writes where it cannot read a pom of its reactor.
_UNREADABLE_REACTOR_OUTPUT = "[ERROR] The build could not read 1 project -> [Help 1]"


def _marked_property(name: str, value: str, directives: str) -> str:
    """Return a `<properties>` element declaring the property, with a marker carrying the directives on its line."""
    return properties_element({name: value}).replace(f"</{name}>", f"</{name}>  <!-- update-time: {directives} -->")


def _marked_at_its_element(element: str, directives: str) -> str:
    """Return the element with a marker carrying the directives at the end of its opening tag's line."""
    return element.replace(">\n", f">  <!-- update-time: {directives} -->\n", 1)


def _pom_marking_guava(directives: str) -> str:
    """Return a pom declaring guava 33.0.0-jre, with a marker carrying the directives at its version on line 6."""
    return pom_declaring(marked_at_its_version(guava_element("33.0.0-jre"), directives))


def _assert_osv_asked_about_maven(mock_post: Mock, *pins: tuple[str, str]) -> None:
    """Assert that OSV was asked once about the pins, each in the Maven ecosystem."""
    assert_osv_asked_about(mock_post, *pins, ecosystem="Maven")


def _guava_twice_by_property(properties: str) -> str:
    """Return a pom declaring guava as managed and as a dependency, both at the version `${guava.version}` names."""
    guava = guava_element("${guava.version}")
    return pom_declaring(guava, properties=properties, managed=dependency_management_element(guava))


def _versions_goals_ran(mock_run: Mock) -> bool:
    """Return whether a Maven run so far ran the versions plugin's goals, which rewrite the pom."""
    return any(_VERSIONS_GOALS[0] in run.args[0] for run in mock_run.call_args_list)


def _rewrite_on_its_own_run(mock_run: Mock, pom: Mock, after: str) -> None:
    """Have the pom read as it does until a Maven run selects its directory, and as `after` from then on."""
    before = pom.read_bytes.return_value
    directory = str(pom.parent.relative_to(_PROJECT))

    def contents() -> bytes:
        """Return what the pom holds, which the run selecting its directory changes."""
        selected = any(_selected_directory(run.args[0]) == directory for run in mock_run.call_args_list)
        return after.encode() if selected else before

    pom.read_bytes = Mock(side_effect=contents)


def _selected_directory(command: Command) -> str:
    """Return the directory the Maven command selects in the reactor."""
    return command[command.index("--projects") + 1]


def _artefacts_asked(mock: Mock) -> list[str]:
    """Return the artefact each call to the mock named, in the order of the calls."""
    return [call.args[0] for call in mock.call_args_list]


def _on_jersey_core(artefact: str) -> bool:
    """Stand in for Maven Central, serving the artefacts of Jersey's core group alone."""
    return artefact.startswith("org.glassfish.jersey.core:")


# The tests hand every run this effective pom by default. It does not list a dependency, so it resolves nothing.
_EMPTY_EFFECTIVE_POM = effective_pom_declaring()

# A pom declaring guava 33.0.0-jre, its `<version>` on line 6.
_POM_DECLARING_GUAVA = pom_declaring(guava_element("33.0.0-jre"))

# A dependency whose group names a property the parent declares, and the entry the effective pom lists for it where a
# pom declares that dependency alone.
_SPRING_LEAVING_ITS_GROUP = dependency_element("${spring.group}", "spring-core", "6.1.0")
_EFFECTIVE_SPRING = effective_dependency_element("org.springframework:spring-core", "6.1.0", line=5)

# A pom declaring a plugin in a group of its own, by a group the parent declares and a version the pom's own property
# gives, and the effective pom listing that plugin. Maven lists the parent's property in the effective pom.
_VERSIONS = "org.codehaus.mojo:versions-maven-plugin"
_POM_LEAVING_THE_VERSIONS_PLUGINS_GROUP = pom_declaring(
    properties=properties_element({"versions.version": "2.18.0"}),
    build=build_element(plugin_element("versions-maven-plugin", "${versions.version}", "${mojo.group}")),
)
_EFFECTIVE_VERSIONS = effective_pom_declaring(
    properties=properties_element({"mojo.group": "org.codehaus.mojo"}),
    build=build_element(effective_plugin_element(_VERSIONS, "2.18.0", line=11)),
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
# The child's effective pom, which locates guava's version where the parent manages it, on line 10.
_CHILDS_EFFECTIVE_POM = effective_pom_declaring(
    effective_dependency_element(GUAVA, "33.0.0-jre", line=7, managed_at=(PARENT_POM_ID, 10))
)


@no_vulnerabilities
@patch.object(maven_central_module, "project", Mock(return_value=Project()))
@patch.object(maven_central_module, "serves", Mock(return_value=True))
@patch.object(maven_central_module, "lacks_a_github_repository", Mock(return_value=False))
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
        the run ends, so the file is stood in for here. The rule set file is stood in for too, and `rule_sets` collects
        the rule set each Maven run reads.
        """
        poms = [mock_path(text, parent=_PROJECT, name="pom.xml") for text in contents]
        mock_glob.return_value = poms
        mock_run.reset_mock()
        mock_run.return_value = _maven_succeeded()
        written = mock_path(effective_pom, name=_EFFECTIVE_POM)
        self.rule_sets: list[str] = []

        @contextlib.contextmanager
        def effective_pom_file() -> Generator[Mock]:
            yield written

        @contextlib.contextmanager
        def rule_set_file(rule_set: str) -> Generator[Path]:
            self.rule_sets.append(rule_set)
            yield _RULES

        self.enterContext(patch.object(maven_module, "_effective_pom_file", effective_pom_file))
        self.enterContext(patch.object(maven_module, "_rule_set_file", rule_set_file))
        return poms

    def find_pom(
        self, mock_run: Mock, mock_glob: Mock, contents: str = "<project/>", effective_pom: str = _EMPTY_EFFECTIVE_POM
    ) -> Mock:
        """Discover a single mock pom.xml holding the contents, with Maven stubbed to print nothing."""
        return self.find_poms(mock_run, mock_glob, contents, effective_pom=effective_pom)[0]

    def find_reactor(self, mock_run: Mock, mock_glob: Mock, poms: dict[str, str]) -> list[Mock]:
        """Discover a mock pom.xml per directory below the project directory, holding the contents given for it."""
        found = self.find_poms(mock_run, mock_glob, *poms.values())
        for pom, directory in zip(found, poms, strict=True):
            pom.parent = _PROJECT / directory
        return found

    def find_reactor_updated_by_bom(self, mock_run: Mock, mock_glob: Mock, *later: str, failing: str) -> Mock:
        """Discover a reactor whose run over `bom` moves guava, and whose run over `failing` cannot read it.

        The other runs change nothing. The poms in the `later` directories, `failing` among them, come after `bom`, in
        this order.
        """
        poms = {".": _aggregator("bom", *later), "bom": _POM_DECLARING_GUAVA}
        _, bom, *_ = self.find_reactor(mock_run, mock_glob, poms | dict.fromkeys(later, "<project/>"))
        _rewrite_on_its_own_run(mock_run, bom, pom_declaring(guava_element("33.7.1-jre")))
        runs_read = [_maven_succeeded()] * (".", "bom", *later).index(failing)
        mock_run.side_effect = [*runs_read, _maven_failed(_UNREADABLE_REACTOR_OUTPUT)]
        return bom

    def find_marked_pom(self, mock_run: Mock, mock_glob: Mock, directives: str) -> Mock:
        """Discover a single mock pom.xml declaring guava, with a marker carrying the directives at its version."""
        return self.find_pom(mock_run, mock_glob, _pom_marking_guava(directives))

    def find_pom_versioned_by_a_parent(self, mock_run: Mock, mock_glob: Mock, directives: str) -> Mock:
        """Discover a single mock pom.xml declaring guava at a property its parent declares, marked on line 6."""
        declared = pom_declaring(marked_at_its_version(guava_element("${guava.version}"), directives))
        return self.find_pom(mock_run, mock_glob, declared, effective_pom_declaring(EFFECTIVE_GUAVA))

    def find_unresolved_pom(self, mock_run: Mock, mock_glob: Mock, directives: str) -> Mock:
        """Discover a single mock pom.xml managing guava at an undeclared property, marked on line 7.

        The effective pom copies the managed version from the pom, without resolving it.
        """
        managed = dependency_management_element(marked_at_its_version(guava_element("${guava.version}"), directives))
        effective_guava = effective_dependency_element(GUAVA, "${guava.version}", line=6)
        effective_pom = effective_pom_declaring(managed=dependency_management_element(effective_guava))
        return self.find_pom(mock_run, mock_glob, pom_declaring(managed=managed), effective_pom)

    def find_child_leaving_its_version(self, mock_run: Mock, mock_glob: Mock, directives: str) -> Mock:
        """Discover a child leaving guava's version to the scanned parent, marked on line 5, and return the child."""
        marked = _marked_at_its_element(guava_element(None), directives)
        child = pom_declaring(marked, coordinates=SCANNED_POM_COORDINATES)
        child_pom, _ = self.find_poms(
            mock_run, mock_glob, child, _PARENT_MANAGING_GUAVA, effective_pom=_CHILDS_EFFECTIVE_POM
        )
        return child_pom

    def find_rewritten_pom(
        self, mock_run: Mock, mock_glob: Mock, before: str, after: str, effective_pom: str = _EMPTY_EFFECTIVE_POM
    ) -> Mock:
        """Discover a single mock pom.xml that reads as `before` until the versions plugin's goals rewrite it."""

        def contents() -> bytes:
            """Return what the pom holds, which the versions plugin's goals change."""
            return (after if _versions_goals_ran(mock_run) else before).encode()

        pom = self.find_pom(mock_run, mock_glob, before, effective_pom)
        pom.read_bytes = Mock(side_effect=contents)
        return pom

    def assert_maven_ran(self, mock_run: Mock, executable: str = "mvn", rules: Path | None = None) -> None:
        """Assert that the Maven command ran once, with the given executable, in the project directory."""
        self.assert_maven_runs(mock_run, _maven_command(executable, rules))

    def assert_maven_runs(self, mock_run: Mock, *commands: Command, directory: Path = _PROJECT) -> None:
        """Assert that Maven ran the given commands, in this order, in the directory."""
        self.assertEqual(mock_run.call_args_list, [_maven_run(command, directory) for command in commands])

    def assert_unreadable_reactor_logged(
        self, message: LogMessage, directory: str = ".", output: str = _UNREADABLE_REACTOR_OUTPUT, **fields: object
    ) -> None:
        """Assert that the run's errors were the failed run over the directory, then the message about the reactor."""
        failed = _maven_command(directory=directory)
        self.assert_errors_logged(
            (Logger._MESSAGE_COMMAND_FAILED, {"command": failed, "output": output}),
            (message, {"location": Location(_PROJECT), **fields}),
        )

    def assert_maven_not_run_logged(self, *poms: Mock) -> None:
        """Assert that the poms, and no others, were logged as left without a Maven run, in this order."""
        message = Logger._MESSAGE_MAVEN_NOT_RUN
        expected = [self._expected_call(message, {"location": Location(pom)}) for pom in poms]
        self.assertEqual(self.records_of(message), expected)

    def assert_no_redundant_directive_logged(self) -> None:
        """Assert that no directive was reported as redundant, whatever else was logged."""
        self.assert_none_logged(Logger._MESSAGE_REDUNDANT_DIRECTIVE, "redundant directive")

    def assert_reported_as_held_elsewhere(self, location: Location, marker: Marker, directive: str) -> None:
        """Assert that guava's marker was recognised and reported as redundant, not as holding the update back."""
        self.assert_recognised_marker_logged(GUAVA, location, marker)
        self.assert_none_logged(Logger._MESSAGE_IGNORED, "update held back")
        self.assert_redundant_directive_logged(Reason.VERSION_HELD_ELSEWHERE, GUAVA, location, directive)

    @contextlib.contextmanager
    def asked_about(self) -> Generator[list[str]]:
        """Collect the artefacts the cooldown asks the repository about, and do not hold any version back."""
        asked: list[str] = []

        def versions_held_back(artefact: str, _days: int) -> tuple[str, ...]:
            asked.append(artefact)
            return ()

        with patch.object(maven_module, "versions_held_back", versions_held_back):
            yield asked

    @contextlib.contextmanager
    def maven_central_answering(self, pom: str, *, archived: bool = False) -> Generator[Mock]:
        """Let Maven Central answer for itself, serving guava's listing and the pom, and yield the mock Session.get."""
        with (
            patch.object(maven_central_module, "project", maven_central_project),
            patch.object(maven_central_module, "serves", maven_central_serves),
            patch.object(maven_central_module, "lacks_a_github_repository", maven_central_lacks_a_github_repository),
            patch_maven_central(_LISTING, pom, archived=archived) as mock_get,
        ):
            yield mock_get

    @contextlib.contextmanager
    def hold_back(self, versions: dict[str, tuple[str, ...]], cooldown_days: int = COOLDOWN.default) -> Generator[None]:
        """Hold the named versions back per artefact.

        The stub answers with the named versions only where the cooldown is `cooldown_days`, and with an empty tuple
        otherwise.
        """

        def versions_held_back(artefact: str, days: int) -> tuple[str, ...]:
            return versions.get(artefact, ()) if days == cooldown_days else ()

        with patch.object(maven_module, "versions_held_back", versions_held_back):
            yield

    def test_a_marker_holding_every_version_back_is_reported_as_holding_it_back(self, mock_run: Mock, mock_glob: Mock):
        """Test that a marker holding every version back is reported as holding the update, naming its directive."""
        own_property = _marked_property("guava.version", "33.0.0-jre", "ignore[update]")
        surefires_property = _marked_property("surefire.version", "3.5.0", "ignore[update]")
        surefire = build_element(plugin_element("maven-surefire-plugin", "${surefire.version}"))
        # The marker sits on the line of the `<version>` element, or of the property the element names.
        cases = {
            "a version": (
                _pom_marking_guava("ignore[update]"),
                GUAVA,
                6,
                "ignore[update]",
            ),
            "a version under a bare ignore": (
                _pom_marking_guava("ignore"),
                GUAVA,
                6,
                "ignore",
            ),
            "an update ignored beside a bound": (
                _pom_marking_guava("ignore[update] allow[update<34]"),
                GUAVA,
                6,
                "ignore[update]",
            ),
            "a property of its own": (
                pom_declaring(guava_element("${guava.version}"), properties=own_property),
                GUAVA,
                3,
                "ignore[update]",
            ),
            "a plugin's property of its own": (
                pom_declaring(properties=surefires_property, build=surefire),
                SUREFIRE,
                3,
                "ignore[update]",
            ),
            "a bound naming a pre-release": (
                _pom_marking_guava("ignore[update<=34.1rc1]"),
                GUAVA,
                6,
                "ignore[update<=34.1rc1]",
            ),
            "a level bound on a version without numbers": (
                pom_declaring(marked_at_its_version(guava_element("RELEASE"), "ignore[major-update]")),
                GUAVA,
                6,
                "ignore[major-update]",
            ),
        }
        for case, (declared, artefact, line, directive) in cases.items():
            with self.subTest(case=case):
                pom = self.find_pom(mock_run, mock_glob, declared)
                update_pom_xmls()
                self.assert_ignored_logged(artefact, Location(pom, line), directive, among_others=True)
                self.assert_no_redundant_directive_logged()

    def test_a_cooldown_or_a_bound_is_not_reported_as_holding_the_update_back(self, mock_run: Mock, mock_glob: Mock):
        """Test that a marker setting a cooldown or a bound is reported as recognised, and not as holding back."""
        for directives, marker in {
            "ignore[cooldown<30]": Marker(cooldown=Threshold(value=30)),
            "allow[update<34]": Marker(version_bound=bound(Verb.ALLOW, "update<34")),
        }.items():
            with self.subTest(directives=directives):
                pom = self.find_marked_pom(mock_run, mock_glob, directives)
                update_pom_xmls()
                self.assert_recognised_marker_logged(GUAVA, Location(pom, 6), marker)
                self.assert_none_logged(Logger._MESSAGE_IGNORED, "update held back")

    def test_a_redundant_bound_on_a_version_maven_updates_is_warned_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that a bound that never has an effect or blocks every update is warned about."""
        for directives, redundancy in {
            "allow[update>=33]": _Redundancy.NO_EFFECT,
            "ignore[update>=33]": _Redundancy.BLOCKS_ALL,
        }.items():
            with self.subTest(directives=directives):
                pom = self.find_marked_pom(mock_run, mock_glob, directives)
                update_pom_xmls()
                self.assert_logged(
                    Logger._MESSAGE_REDUNDANT_BOUND,
                    dependency=GUAVA,
                    location=Location(pom, 6),
                    bound=directives,
                    version="33.0.0-jre",
                    redundancy=redundancy,
                )

    @kills(
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "_report_markers(distinct)",
            "_report_markers(resolved)",
            "the marker on a property two declarations of one artefact name is reported once per declaration",
        )
    )
    def test_a_marker_two_declarations_of_one_artefact_share_is_reported_once(self, mock_run: Mock, mock_glob: Mock):
        """Test that a marker on a property both declarations of an artefact name, and its range, are reported once."""
        properties = _marked_property("guava.version", "33.0.0-jre", "ignore[stale, update>=34]")
        pom = self.find_pom(mock_run, mock_glob, _guava_twice_by_property(properties))
        update_pom_xmls()
        marker = Marker(ignored_scopes=Scope.STALE, version_bound=bound(Verb.IGNORE, "update>=34"))
        self.assert_recognised_marker_logged(GUAVA, Location(pom, 3), marker)
        self.assertEqual(len(self.records_of(Logger._MESSAGE_RECOGNISED_MARKER)), 1)
        self.assertEqual(len(self.records_of(Logger._MESSAGE_HOLDING_BACK_RANGE)), 1)

    @kills(
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "with_resolved_coordinates(distinct)",
            "with_resolved_coordinates(resolved)",
            "an artefact two declarations name by one property is warned about once per declaration, at one line",
        )
    )
    def test_two_declarations_of_one_artefact_sharing_a_line_are_checked_once(self, mock_run: Mock, mock_glob: Mock):
        """Test that two declarations of an artefact naming one property are warned about once, at the property."""
        properties = properties_element({"guava.version": "33.0.0-jre"})
        pom = self.find_pom(mock_run, mock_glob, _guava_twice_by_property(properties))
        with osv(ADVISORY), _stale_release():
            update_pom_xmls()
        self.assert_stale_dependency_logged(GUAVA, "33.0.0-jre", Location(pom, 3), among_others=True)
        self.assertEqual(len(self.records_of(Logger._MESSAGE_VULNERABLE_DEPENDENCY)), 1)

    @kills(
        Mutation(
            update_pom_xml_module._report_scopes_on_artefacts_maven_central_does_not_serve,
            'as_written.cooldown_directive if declaration.maven_updates_the_version else ""',
            "as_written.cooldown_directive",
            "a `cooldown` on a version Maven does not update is reported twice, once for each reason",
        )
    )
    def test_a_marker_on_a_parents_property_is_redundant(self, mock_run: Mock, mock_glob: Mock):
        """Test that a marker steering the update of a version a parent's property gives is redundant."""
        markers = {
            "ignore[update]": Marker(ignored_scopes=Scope.UPDATE),
            "allow[update>=33]": Marker(version_bound=bound(Verb.ALLOW, "update>=33")),
            "ignore[cooldown<30]": Marker(cooldown=Threshold(value=30)),
        }
        for directive, marker in markers.items():
            with self.subTest(directive=directive):
                pom = self.find_pom_versioned_by_a_parent(mock_run, mock_glob, directive)
                with patch.object(maven_central_module, "serves", Mock(return_value=False)):
                    update_pom_xmls()
                self.assert_reported_as_held_elsewhere(Location(pom, 6), marker, directive)

    def test_an_ignored_update_beside_a_bound_on_a_parents_property_is_redundant(self, mock_run: Mock, mock_glob: Mock):
        """Test that a marker ignoring the update and setting a bound is reported as a redundant `ignore[update]`."""
        pom = self.find_pom_versioned_by_a_parent(mock_run, mock_glob, "ignore[update] allow[update<34]")
        update_pom_xmls()
        self.assert_redundant_directive_logged(Reason.VERSION_HELD_ELSEWHERE, GUAVA, Location(pom, 6), "ignore[update]")

    def test_a_marker_on_a_dependency_without_a_version_is_redundant(self, mock_run: Mock, mock_glob: Mock):
        """Test that a marker steering the update of a version the parent pom manages is redundant here."""
        unversioned = _marked_at_its_element(guava_element(None), "ignore[update]")
        managed_guava = effective_dependency_element(GUAVA, "33.0.0-jre", line=5, managed_at=(PARENT_POM_ID, 12))
        pom = self.find_pom(mock_run, mock_glob, pom_declaring(unversioned), effective_pom_declaring(managed_guava))
        update_pom_xmls()
        self.assert_reported_as_held_elsewhere(Location(pom, 3), Marker(ignored_scopes=Scope.UPDATE), "ignore[update]")

    @kills(
        Mutation(
            pom_xml_module._declaration,
            "literal_plugin_version=versioned_by_its_pom and ",
            "literal_plugin_version=",
            "a plugin naming a parent's property reads as a literal version, so its marker gets the wrong reason",
        )
    )
    def test_a_marker_on_a_plugin_maven_does_not_update_is_redundant(self, mock_run: Mock, mock_glob: Mock):
        """Test that a marker steering a plugin's update is redundant where Maven does not update its version."""
        cases = {
            "a literal version": ("3.5.0", Reason.PLUGIN_NOT_UPDATED),
            "a parent's property": ("${parent.surefire.version}", Reason.VERSION_HELD_ELSEWHERE),
        }
        for case, (version, reason) in cases.items():
            with self.subTest(case=case):
                surefire = marked_at_its_version(plugin_element("maven-surefire-plugin", version), "ignore[update]")
                pom = self.find_pom(mock_run, mock_glob, pom_declaring(build=build_element(surefire)))
                update_pom_xmls()
                location = Location(pom, 9)
                self.assert_recognised_marker_logged(SUREFIRE, location, Marker(ignored_scopes=Scope.UPDATE))
                self.assert_none_logged(Logger._MESSAGE_IGNORED, "update held back")
                self.assert_redundant_directive_logged(reason, SUREFIRE, location, "ignore[update]")

    def test_an_unreadable_item_is_warned_about_rather_than_recognised(self, mock_run: Mock, mock_glob: Mock):
        """Test that a marker item that does not parse is warned about as invalid, and not reported as recognised."""
        pom = self.find_marked_pom(mock_run, mock_glob, "ignore[stlae]")
        update_pom_xmls()
        self.assert_invalid_bracket_item_logged(GUAVA, Location(pom, 6), "stlae")
        self.assert_none_logged(Logger._MESSAGE_RECOGNISED_MARKER, "recognised marker")

    def test_an_inverted_cooldown_is_reported_and_leaves_the_runs_cooldown(self, mock_run: Mock, mock_glob: Mock):
        """Test that a cooldown comparing the wrong way is reported as incorrect, and the run's cooldown applies."""
        pom = self.find_marked_pom(mock_run, mock_glob, "ignore[cooldown>=30]")
        with self.hold_back({GUAVA: ("33.7.1-jre",)}):
            update_pom_xmls()
        self.assert_logged(
            Logger._MESSAGE_INVERTED_COOLDOWN_ITEM, item="cooldown>=30", dependency=GUAVA, location=Location(pom, 6)
        )
        self.assertEqual(self.rule_sets, [_GUAVA_33_7_1_HELD_BACK])

    def test_a_marker_silences_the_staleness_warning(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency whose marker ignores staleness is reported as silenced rather than as stale."""
        pom = self.find_marked_pom(mock_run, mock_glob, "ignore[stale]")
        with _stale_release():
            update_pom_xmls()
        self.assert_ignored_staleness_logged(GUAVA, Location(pom, 6))
        self.assert_no_warnings_logged()

    def test_a_marker_silences_the_vulnerability_warning(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency whose marker ignores vulnerabilities is reported as silenced, not as vulnerable."""
        pom = self.find_marked_pom(mock_run, mock_glob, "ignore[vulnerable]")
        with osv(ADVISORY):
            update_pom_xmls()
        self.assert_ignored_vulnerability_logged(GUAVA, Location(pom, 6), VULNERABILITY.advisory, "ignore[vulnerable]")
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_pom_xml_module._report_redundant_directives,
            "declaration, yanked, Reason",
            "declaration, declaration.marker.raw, Reason",
            "the warning quotes every item of the marker, so it calls an `ignore[update]` beside the yank redundant",
        )
    )
    def test_the_yanked_scope_is_reported_as_redundant(self, mock_run: Mock, mock_glob: Mock):
        """Test that the `yanked` scope is reported as redundant, named alone beside an item or a bare `ignore`."""
        for directives in ("ignore[update,yanked]", "ignore ignore[yanked]"):
            with self.subTest(directives=directives):
                pom = self.find_marked_pom(mock_run, mock_glob, directives)
                update_pom_xmls()
                self.assert_redundant_directive_logged(
                    Reason.NO_YANK_CONCEPT, GUAVA, Location(pom, 6), "ignore[yanked]"
                )

    def test_an_opt_in_is_reported_as_redundant(self, mock_run: Mock, mock_glob: Mock):
        """Test that each opt-in is reported for the reason it decides nothing."""
        for directive, reason in {
            "allow[floating-pin]": Reason.PIN_NOT_FLOATING,
            "allow[hash-drift]": Reason.NO_HASH_PIN,
        }.items():
            with self.subTest(directive=directive):
                pom = self.find_marked_pom(mock_run, mock_glob, directive)
                update_pom_xmls()
                self.assert_redundant_directive_logged(reason, GUAVA, Location(pom, 6), directive)

    @kills(
        Mutation(
            maven_central_module._listing,
            "absent=True",
            "absent=False",
            "a listing Maven Central does not serve reads as a failed request, so nothing is reported as redundant",
        ),
        Mutation(
            update_pom_xml_module._report_scopes_on_artefacts_maven_central_does_not_serve,
            "directive_for(Scope.STALE)",
            "scope_directive(Scope.STALE)",
            "only a bare `ignore[stale]` is reported, so a threshold on an unserved artefact is not",
        ),
    )
    def test_a_scope_on_an_artefact_maven_central_does_not_serve_is_redundant(self, mock_run: Mock, mock_glob: Mock):
        """Test that a scope on an artefact whose listing Maven Central does not serve is reported as redundant."""
        for directive in ("ignore[cooldown<30]", "ignore[stale]", "ignore[stale<90]", "ignore[archived]"):
            with self.subTest(directive=directive):
                pom = self.find_marked_pom(mock_run, mock_glob, directive)
                with (
                    patch.object(maven_central_module, "serves", maven_central_serves),
                    patch_get(ok=False, status_code=404, reason="Not Found", text=""),
                ):
                    update_pom_xmls()
                self.assert_redundant_directive_logged(Reason.NOT_ON_MAVEN_CENTRAL, GUAVA, Location(pom, 6), directive)

    def test_a_vulnerable_scope_on_an_artefact_maven_central_does_not_serve_is_not_redundant(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a `vulnerable` scope on an artefact Maven Central does not serve is not redundant."""
        pom = self.find_marked_pom(mock_run, mock_glob, "ignore[stale, vulnerable]")
        with osv(ADVISORY), patch.object(maven_central_module, "serves", Mock(return_value=False)):
            update_pom_xmls()
        self.assert_redundant_directive_logged(Reason.NOT_ON_MAVEN_CENTRAL, GUAVA, Location(pom, 6), "ignore[stale]")

    def test_an_archived_scope_on_an_artefact_without_a_github_repository_is_redundant(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that an `archived` scope is redundant where the artefact's pom names a repository outside GitHub."""
        pom = self.find_marked_pom(mock_run, mock_glob, "ignore[archived]")
        with self.maven_central_answering(_POM_OUTSIDE_GITHUB):
            update_pom_xmls()
        self.assert_redundant_directive_logged(Reason.NO_GITHUB_REPOSITORY, GUAVA, Location(pom, 6), "ignore[archived]")

    def test_an_archived_scope_on_an_artefact_with_a_github_repository_is_not_redundant(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that an `archived` scope is not redundant where the artefact's pom names a repository on GitHub."""
        self.find_marked_pom(mock_run, mock_glob, "ignore[archived]")
        with self.maven_central_answering(_POM_ON_GITHUB) as mock_get:
            update_pom_xmls()
        self.assertIn(maven_central_pom_url(GUAVA, "33.0.0-jre"), requested_urls(mock_get))
        self.assert_no_redundant_directive_logged()

    @kills(
        Mutation(
            update_pom_xml_module._report_archived_scopes_without_a_github_repository,
            "    if not archival_is_checked():\n        return\n",
            "",
            "a run that checks nothing for archival fetches poms all the same, to judge an `archived` scope",
        )
    )
    @archival_check_disabled
    def test_a_run_that_checks_nothing_for_archival_reads_no_pom_to_judge_an_archived_scope(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a run with the archival check switched off does not fetch a pom to judge an `archived` scope."""
        self.find_marked_pom(mock_run, mock_glob, "ignore[archived]")
        with self.maven_central_answering(_POM_OUTSIDE_GITHUB) as mock_get:
            update_pom_xmls()
        self.assertEqual(requested_urls(mock_get), [maven_central_listing_url(GUAVA)])
        self.assert_no_redundant_directive_logged()

    @kills(
        Mutation(
            update_pom_xml_module._report_scopes_on_artefacts_maven_central_does_not_serve,
            "if directives and not maven_central.serves",
            "if not maven_central.serves",
            "Maven Central is asked whether it serves every artefact, whatever its marker carries",
        ),
        Mutation(
            update_pom_xml_module._report_archived_scopes_without_a_github_repository,
            "if archived and maven_central.lacks_a_github_repository",
            "if maven_central.lacks_a_github_repository",
            "Maven Central's poms are read for every artefact, whether or not its marker carries an `archived` scope",
        ),
    )
    def test_an_artefact_without_a_cooldown_stale_or_archived_scope_is_not_asked_about(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that Maven Central is asked only about the artefact marked with a `cooldown`, `stale`, or `archived`."""
        commons_io = dependency_element("commons-io", "commons-io", "2.16.1")
        declared = pom_declaring(
            marked_at_its_version(guava_element("33.0.0-jre"), "ignore[vulnerable]"),
            marked_at_its_version(commons_io, "ignore[stale, archived]"),
        )
        self.find_pom(mock_run, mock_glob, declared)
        with (
            patch.object(maven_central_module, "serves", Mock(return_value=True)) as mock_serves,
            patch.object(maven_central_module, "lacks_a_github_repository", Mock(return_value=False)) as mock_lacks,
        ):
            update_pom_xmls()
        self.assertEqual(_artefacts_asked(mock_serves), ["commons-io:commons-io"])
        self.assertEqual(_artefacts_asked(mock_lacks), ["commons-io:commons-io"])

    @kills(
        Mutation(
            update_pom_xml_module._warn_about_vulnerabilities,
            "as_written.directive_for(Scope.VULNERABLE)",
            "as_written.scope_directive(Scope.VULNERABLE)",
            "only a bare `ignore[vulnerable]` is reported, so a level or an advisory on an unresolved version is not",
        )
    )
    def test_a_vulnerable_scope_on_an_unresolved_version_is_redundant(self, mock_run: Mock, mock_glob: Mock):
        """Test that a `vulnerable` scope on a version Maven leaves unresolved is reported as redundant."""
        pom = self.find_unresolved_pom(mock_run, mock_glob, "ignore[vulnerable<high]")
        update_pom_xmls()
        self.assert_redundant_directive_logged(
            Reason.NO_RESOLVED_VERSION_TO_CHECK_FOR_A_VULNERABILITY, GUAVA, Location(pom, 7), "ignore[vulnerable<high]"
        )

    @kills(
        Mutation(
            update_pom_xml_module._warn_about_vulnerabilities,
            "elif declaration.listed_in_effective_pom and (",
            "elif (",
            "without an effective pom, a version from a parent's property reads as one Maven leaves unresolved",
        )
    )
    def test_a_vulnerable_scope_without_an_effective_pom_is_not_redundant(self, mock_run: Mock, mock_glob: Mock):
        """Test that a `vulnerable` scope on a parent's group or version, without an effective pom, is not reported."""
        cases = {
            "a parent's group": (_SPRING_LEAVING_ITS_GROUP, "${spring.group}:spring-core"),
            "a parent's version": (guava_element("${guava.version}"), GUAVA),
        }
        for case, (element, artefact) in cases.items():
            with self.subTest(case=case):
                declared = pom_declaring(marked_at_its_version(element, "ignore[vulnerable]"))
                pom = self.find_pom(mock_run, mock_glob, declared, effective_pom="")
                update_pom_xmls()
                marker = Marker(ignored_scopes=Scope.VULNERABLE)
                self.assert_recognised_marker_logged(artefact, Location(pom, 6), marker)
                self.assert_no_redundant_directive_logged()

    @kills(
        Mutation(
            update_pom_xml_module._report_redundant_directives,
            "declaration.marker.as_written",
            "declaration.marker",
            "a bare `ignore` reads as naming the scopes it holds back, so its `yanked` and `update` read as redundant",
        ),
        Mutation(
            update_pom_xml_module._warn_about_vulnerabilities,
            "declaration.marker.as_written.directive_for(Scope.VULNERABLE)",
            "declaration.marker.directive_for(Scope.VULNERABLE)",
            "a bare `ignore` reads as naming `vulnerable`, so it is reported as redundant on an unresolved version",
        ),
        Mutation(
            update_pom_xml_module._report_scopes_checked_at_the_managing_declaration,
            "declaration.marker.as_written",
            "declaration.marker",
            "a bare `ignore` reads as naming each check's scope, so it is redundant on a version a scanned pom manages",
        ),
    )
    def test_a_bare_ignore_is_not_reported_as_redundant(self, mock_run: Mock, mock_glob: Mock):
        """Test that a bare `ignore` is not reported as redundant, on a version the pom does not hold or resolve."""
        cases = {
            "an unresolved version": (self.find_unresolved_pom, 7),
            "a version a scanned parent manages": (self.find_child_leaving_its_version, 5),
        }
        for case, (find, line) in cases.items():
            with self.subTest(case=case):
                pom = find(mock_run, mock_glob, "ignore")
                update_pom_xmls()
                self.assert_recognised_marker_logged(GUAVA, Location(pom, line), BARE_IGNORE)
                self.assert_no_redundant_directive_logged()

    def test_osv_is_asked_about_the_version_the_run_lands_on(self, mock_run: Mock, mock_glob: Mock):
        """Test that OSV is asked in the Maven ecosystem about the version after the run, not the effective pom's."""
        before, after = _POM_DECLARING_GUAVA, pom_declaring(guava_element("33.7.1-jre"))
        self.find_rewritten_pom(mock_run, mock_glob, before, after, effective_pom_declaring(EFFECTIVE_GUAVA))
        with osv() as mock_post:
            update_pom_xmls()
        _assert_osv_asked_about_maven(mock_post, (GUAVA, "33.7.1-jre"))

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
                self.assert_command_failed_logged(_maven_command(rules=_RULES), output)
                # Guava is asked about, so the dependency beside it going unasked says something about its version.
                _assert_osv_asked_about_maven(mock_post, (GUAVA, "33.0.0-jre"))
                # Staleness judges the coordinates alone, so the dependency OSV skips still reaches Maven Central.
                self.assertEqual(_artefacts_asked(mock_project), [GUAVA, "org.springframework:spring-core"])

    def test_a_vulnerable_artefact_is_warned_about_as_maven_resolves_it(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency or plugin an advisory names is warned about as Maven resolves it."""
        # Maven lists the properties the parent declares in the effective pom.
        effective_guava_pom = effective_pom_declaring(
            effective_dependency_element(GUAVA, "33.0.0-jre", line=5),
            properties=properties_element({"guava.artifact": "guava"}),
        )
        # The parent manages surefire's version. Surefire is in Maven's default plugin group, so the effective pom
        # leaves out its `<groupId>`.
        surefire = effective_plugin_element(SUREFIRE, "3.5.0", line=8, managed_at=(PARENT_POM_ID, 12))
        # The warning names the line of the `<version>` element, or of the `<plugin>` lacking one.
        cases = {
            "a dependency's version from the parent": (
                pom_declaring(guava_element("${guava.version}")),
                effective_guava_pom,
                GUAVA,
                "33.0.0-jre",
                6,
            ),
            "a dependency's artifact from the parent": (
                pom_declaring(dependency_element("com.google.guava", "${guava.artifact}", "33.0.0-jre")),
                effective_guava_pom,
                GUAVA,
                "33.0.0-jre",
                6,
            ),
            "a plugin without a version": (
                pom_declaring(build=build_element(plugin_element("maven-surefire-plugin", None))),
                effective_pom_declaring(build=build_element(surefire)),
                SUREFIRE,
                "3.5.0",
                6,
            ),
        }
        for case, (declared, effective_pom, name, version, line) in cases.items():
            with self.subTest(case=case):
                pom = self.find_pom(mock_run, mock_glob, declared, effective_pom)
                with osv(ADVISORY):
                    update_pom_xmls()
                self.assert_vulnerable_dependency_logged(name, version, VULNERABILITY, Location(pom, line))

    @kills(
        Mutation(
            pom_xml_module._effective_artefacts,
            "_EffectiveArtefact(pinned, managed_by)",
            "_EffectiveArtefact(pinned, _InputLocation() if default_group else managed_by)",
            "a plugin is warned about twice: where the pom manages its version, and where it declares none",
        )
    )
    def test_a_plugin_its_own_pom_manages_is_warned_about_once(self, mock_run: Mock, mock_glob: Mock):
        """Test that a plugin the pom declares without a version, and manages, is warned about once."""
        managed = plugin_element("maven-surefire-plugin", "3.5.0")
        build = build_element(plugin_element("maven-surefire-plugin", None), managed=managed)
        declared = pom_declaring(coordinates=SCANNED_POM_COORDINATES, build=build)
        # The pom manages surefire's version on line 12, and declares surefire without a version on line 17.
        effective_managed = effective_plugin_element(SUREFIRE, "3.5.0", line=11)
        unversioned = effective_plugin_element(SUREFIRE, "3.5.0", line=19, managed_at=(SCANNED_POM_ID, 12))
        effective_pom = effective_pom_declaring(build=build_element(unversioned, managed=effective_managed))
        pom = self.find_pom(mock_run, mock_glob, declared, effective_pom)
        with _stale_release("3.5.0"):
            update_pom_xmls()
        self.assert_stale_dependency_logged(SUREFIRE, "3.5.0", Location(pom, 12))

    def test_a_version_a_scanned_pom_manages_is_warned_about_where_it_resolves(self, mock_run: Mock, mock_glob: Mock):
        """Test that a version a scanned parent manages is warned about in the parent, or in both poms.

        The child is warned about as well where the parent manages the version by a property the parent inherits. The
        scan finds the child first, so the child's run needs the parent's name before the parent's own run.
        """
        # This parent manages guava's version on line 10, by a property a pom above it declares.
        inheriting = pom_declaring(
            coordinates=pom_coordinates("parent", "org.example", "1.0"),
            managed=dependency_management_element(guava_element("${guava.version}")),
        )
        cases = {"a version of its own": (_PARENT_MANAGING_GUAVA, False), "an inherited property": (inheriting, True)}
        for case, (parent, child_warned_about) in cases.items():
            with self.subTest(case=case):
                child, parent_pom = self.find_poms(
                    mock_run, mock_glob, _CHILD_LEAVING_GUAVAS_VERSION, parent, effective_pom=_CHILDS_EFFECTIVE_POM
                )
                with _stale_release():
                    update_pom_xmls()
                children = [Location(child, 5)] if child_warned_about else []
                self.assert_stale_dependency_logged(GUAVA, "33.0.0-jre", *children, Location(parent_pom, 10))

    @kills(
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "_report_scopes_on_artefacts_maven_central_does_not_serve(declared)",
            "_report_scopes_on_artefacts_maven_central_does_not_serve(resolvable)",
            "a scope checked at the managing declaration is reported twice, once for each reason",
        )
    )
    def test_a_check_scope_on_a_version_a_scanned_pom_manages_is_redundant(self, mock_run: Mock, mock_glob: Mock):
        """Test that a scope steering a check of a child leaving its version to a scanned parent is redundant."""
        for directive in ("ignore[stale<90]", "ignore[archived]", "ignore[vulnerable<high]"):
            with self.subTest(directive=directive):
                child_pom = self.find_child_leaving_its_version(mock_run, mock_glob, directive)
                with patch.object(maven_central_module, "serves", Mock(return_value=False)):
                    update_pom_xmls()
                self.assert_redundant_directive_logged(
                    Reason.CHECKED_AT_THE_MANAGING_DECLARATION, GUAVA, Location(child_pom, 5), directive
                )

    def test_a_child_overriding_its_managed_version_keeps_its_checks_and_marker(self, mock_run: Mock, mock_glob: Mock):
        """Test that a child overriding its managed version is checked at its own, and its marker is not redundant."""
        child = pom_declaring(
            _marked_at_its_element(guava_element(None), "ignore[stale]"),
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
        self.assert_no_redundant_directive_logged()

    @kills(
        Mutation(
            pom_xml_module._managed_versions,
            "_interpolated(version.text, property_elements) for",
            "version.text for",
            "a managed version naming a property never matches a child's, so every child is warned about again",
        )
    )
    def test_a_version_managed_by_a_parents_own_property_is_warned_about_there(self, mock_run: Mock, mock_glob: Mock):
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

    def test_a_dependency_whose_managing_pom_no_longer_parses_is_warned_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that a child's dependency is checked when Maven left the parent managing its version unparsable."""
        parent_pom, child_pom = self.find_poms(
            mock_run,
            mock_glob,
            _PARENT_MANAGING_GUAVA,
            _CHILD_LEAVING_GUAVAS_VERSION,
            effective_pom=_CHILDS_EFFECTIVE_POM,
        )

        def parent_contents() -> bytes:
            """Return what the parent holds, which the versions plugin's goals of its own run leave unparsable."""
            return b"<project><broken>" if _versions_goals_ran(mock_run) else _PARENT_MANAGING_GUAVA.encode()

        parent_pom.read_bytes = Mock(side_effect=parent_contents)
        with _stale_release():
            update_pom_xmls()
        self.assert_error_logged(Logger._MESSAGE_INVALID_XML_AFTER_UPDATE, location=Location(parent_pom))
        self.assert_stale_dependency_logged(GUAVA, "33.0.0-jre", Location(child_pom, 5))

    @kills(
        Mutation(
            maven_module._ReactorModule._run_goals_writing_effective_pom,
            "_LOG.effective_pom_unreadable(self.pom_xml)",
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
            maven_module._ReactorModule._run_goals_writing_effective_pom,
            "_LOG.effective_pom_without_input_locations(self.pom_xml)",
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
    def test_effective_pom_without_input_locations_resolves_nothing_and_warns(self, mock_run: Mock, mock_glob: Mock):
        """Test that an effective pom lacking input locations leaves each version to the pom, and is warned about."""
        # A project configuring the help plugin's `<verbose>` as false gets an effective pom without input locations.
        effective_pom = _POM_DECLARING_GUAVA
        declared = pom_declaring(guava_element("${guava.version}"), dependency_element("junit", "junit", "4.13.2"))
        pom = self.find_pom(mock_run, mock_glob, declared, effective_pom)
        with osv() as mock_post:
            update_pom_xmls()
        self.assert_logged(Logger._MESSAGE_EFFECTIVE_POM_WITHOUT_INPUT_LOCATIONS, location=Location(pom))
        _assert_osv_asked_about_maven(mock_post, ("junit:junit", "4.13.2"))

    @kills(
        Mutation(
            maven_module._ReactorModule._run_goals_writing_effective_pom,
            "effective_pom = xml.read(output)",
            "effective_pom = xml.read(output) if result.succeeded else None",
            "a failed run discards the effective pom it wrote, so the versions the parent declares go unchecked",
        )
    )
    def test_a_run_failing_after_the_effective_pom_resolves_a_parents_version(self, mock_run: Mock, mock_glob: Mock):
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
        _assert_osv_asked_about_maven(mock_post, (GUAVA, "33.0.0-jre"))

    @kills(
        Mutation(
            maven_module._ReactorModule._run_goals_writing_effective_pom,
            "effective_pom = _effective_pom_written_elsewhere(result.stdout)",
            "effective_pom = None",
            "the effective pom of a project configuring the help plugin's output goes unread",
        ),
        Mutation(
            maven_module._ReactorModule._run_goals_writing_effective_pom,
            "_effective_pom_written_elsewhere(result.stdout)",
            "_effective_pom_written_elsewhere(result.stdout) if result.succeeded else None",
            "a failed run discards the effective pom it wrote elsewhere, so what the parent declares goes unchecked",
        ),
    )
    def test_an_effective_pom_written_elsewhere_is_read_where_maven_says(self, mock_run: Mock, mock_glob: Mock):
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
                _assert_osv_asked_about_maven(mock_post, (GUAVA, "33.0.0-jre"))
                self.assert_no_warnings_logged()

    @kills(
        Mutation(
            delegated_module.project_resolver,
            "check_archival=check_archival",
            "check_archival=False",
            "a delegated source is told the run checks nothing for archival, so it reads none and reports none",
        ),
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
        pom = self.find_pom(mock_run, mock_glob, _POM_DECLARING_GUAVA)
        with self.maven_central_answering(_POM_ON_GITHUB, archived=True):
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
        self.find_pom(mock_run, mock_glob, _POM_DECLARING_GUAVA)
        mock_project = Mock(side_effect=_archived)
        with patch.object(maven_central_module, "project", mock_project):
            update_pom_xmls()
        mock_project.assert_called_once_with(GUAVA, check_archival=False)
        self.assert_no_warnings_logged()

    def test_an_artefact_holding_nothing_back_gets_a_rule_keeping_it_on_its_variant(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that the rule set Maven reads keeps an artefact on its variant where nothing else is held back."""
        self.find_pom(mock_run, mock_glob, _POM_DECLARING_GUAVA)
        update_pom_xmls()
        self.assertEqual(self.rule_sets, [_GUAVA_ON_ITS_VARIANT])
        self.assert_maven_ran(mock_run, rules=_RULES)

    @kills(
        Mutation(
            maven_module._artefact_rule,
            'return _rule(artefact, *ignored) if ignored else ""',
            "return _rule(artefact, *ignored)",
            "a pom holding nothing back gets a rule set holding an empty rule",
        )
    )
    def test_a_pom_holding_nothing_back_gets_no_rule_set(self, mock_run: Mock, mock_glob: Mock):
        """Test that a run over a pom where nothing holds a version back does not write a rule set or hand Maven one."""
        self.find_pom(mock_run, mock_glob, pom_declaring(guava_element("r09")))
        update_pom_xmls()
        self.assertEqual(self.rule_sets, [])
        self.assert_maven_ran(mock_run)

    def test_a_run_advances_the_properties_its_pom_declares_and_names_alone(self, mock_run: Mock, mock_glob: Mock):
        """Test that the run lists each property the pom declares and its declarations name, once, and those alone."""
        pom = pom_declaring(
            guava_element("${guava.version}"),
            dependency_element("commons-io", "commons-io", "${commons-io.version}"),
            dependency_element("junit", "junit", "4.13.2"),
            properties=properties_element({"guava.version": "33.0.0-jre"}),
            managed=dependency_management_element(guava_element("${guava.version}")),
        )
        self.find_pom(mock_run, mock_glob, pom)
        update_pom_xmls()
        self.assert_maven_runs(mock_run, _maven_command(rules=_RULES, properties="guava.version"))

    @kills(
        Mutation(
            maven_module._variant_versions,
            ",) if other_variants else ()",
            ",)",
            "an artefact on a version without a number gets an empty pattern that Maven reads as matching nothing",
        )
    )
    def test_a_version_without_a_number_puts_no_variant_in_its_rule(self, mock_run: Mock, mock_glob: Mock):
        """Test that the rule for an artefact on a version without a number holds the cooldown's versions alone."""
        self.find_pom(mock_run, mock_glob, pom_declaring(guava_element("r09")))
        with self.hold_back({GUAVA: ("33.7.1-jre",)}):
            update_pom_xmls()
        held_back_rule = _rule_ignoring("com.google.guava", "guava", _exact_version_element("33.7.1-jre"))
        self.assertEqual(self.rule_sets, [_rule_set(held_back_rule)])

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
        with self.hold_back(held_back):
            update_pom_xmls()
        guava = _rule("com.google.guava", "guava", "33.0.0-jre", "33.7.0-jre", "33.7.1-jre")
        spring = _rule("org.springframework", "spring-core", "6.1.0", "7.1.0")
        self.assertEqual(self.rule_sets, [_rule_set(guava, spring)])
        self.assert_maven_ran(mock_run, rules=_RULES)

    def test_a_marker_holding_every_version_back_gets_a_rule_for_every_version(self, mock_run: Mock, mock_glob: Mock):
        """Test that an `ignore`, an unreadable item, or a bound naming a non-release holds every version back."""
        for directives in (
            "ignore[update]",
            "ignore[update] allow[update<34]",
            "ignore",
            "ignore[stlae]",
            "ignore[cooldown<30, stlae]",
            "ignore[update<=34.1rc1]",
            "ignore[update===nightly]",
        ):
            with self.subTest(directives=directives):
                self.find_marked_pom(mock_run, mock_glob, directives)
                with self.asked_about() as asked:
                    update_pom_xmls()
                self.assertEqual(self.rule_sets, [_GUAVA_HELD_BACK])
                self.assertEqual(asked, [])  # The repository is not asked which versions to hold back.
                self.assert_maven_ran(mock_run, rules=_RULES)

    def test_a_bound_holds_back_the_range_it_names(self, mock_run: Mock, mock_glob: Mock):
        """Test that a bound holds back the range of versions each kind of clause drops, rather than every version."""
        for directive, maven_range in {
            "ignore[update<34]": "(,34)",
            "ignore[update<=34]": "(,34.0.0.0.1)",
            "ignore[update>34]": "[34.0.0.0.1,)",
            "ignore[update>=34]": "[34,)",
            "ignore[update==34.1]": "[34.1,34.1.0.0.0.1)",
            "ignore[update===34.1]": "[34.1]",
            "ignore[update!=34.1]": "(,34.1),[34.1.0.0.0.1,)",
            "ignore[update==34.*]": "[34,35)",
            "ignore[update!=34.*]": "(,34),[35,)",
            "ignore[update~=34.1]": "[34.1,35)",
        }.items():
            with self.subTest(directive=directive):
                self.find_marked_pom(mock_run, mock_glob, directive)
                update_pom_xmls()
                self.assertEqual(self.rule_sets, [_guava_range_held_back(maven_range)])
                self.assert_maven_ran(mock_run, rules=_RULES)

    @kills(
        Mutation(
            maven_range_module._tighter,
            "return other if boundary.included else boundary",
            "return boundary",
            "two clauses ending at one version keep the first one's end, so `===34.1` holds 34.1 back despite `!=34.1`",
        )
    )
    def test_a_bound_holds_back_the_versions_all_its_clauses_match(self, mock_run: Mock, mock_glob: Mock):
        """Test that a bound of several clauses holds back the versions every clause matches, and nothing where none."""
        for directive, rule_sets_written in {
            "ignore[update!=34.1,<35]": [_guava_range_held_back("(,34.1),[34.1.0.0.0.1,35)")],
            "ignore[update===34.1,!=34.1]": [_GUAVA_ON_ITS_VARIANT],
        }.items():
            with self.subTest(directive=directive):
                self.find_marked_pom(mock_run, mock_glob, directive)
                update_pom_xmls()
                self.assertEqual(self.rule_sets, rule_sets_written)

    @kills(
        Mutation(
            maven_range_module._Boundary.lower_end,
            "'[' if self.included else '('",
            "'['",
            "a range starting past a version starts at it, so `allow[update===34.1]` holds 34.1 back",
        ),
    )
    def test_an_allow_bound_holds_back_the_versions_outside_it(self, mock_run: Mock, mock_glob: Mock):
        """Test that an `allow` bound holds back the ranges of versions its clauses do not match."""
        for directive, maven_range in {
            "allow[update!=34.1]": "[34.1,34.1.0.0.0.1)",
            "allow[update===34.1]": "(,34.1),(34.1,)",
        }.items():
            with self.subTest(directive=directive):
                self.find_marked_pom(mock_run, mock_glob, directive)
                update_pom_xmls()
                self.assertEqual(self.rule_sets, [_guava_range_held_back(maven_range)])

    @kills(
        Mutation(
            maven_range_module.maven_range,
            "current_version)) is None:\n        return None",
            'current_version)) is None:\n        return ""',
            "a level bound with no version to anchor to holds nothing back, so Maven crosses it",
        ),
    )
    def test_a_level_bound_holds_back_what_it_drops_from_its_version(self, mock_run: Mock, mock_glob: Mock):
        """Test that a level bound holds back what it drops from its declaration's version on, or every version."""
        own_property = _marked_property("guava.version", "32.1.0-jre", "ignore[minor-update]")
        cases = {
            "a version": (
                _pom_marking_guava("ignore[major-update]"),
                [_guava_range_held_back("(,33),[34,)")],
            ),
            "a property of its own": (
                pom_declaring(guava_element("${guava.version}"), properties=own_property),
                [_guava_range_held_back("(,32.1),[32.2,)")],
            ),
            "a version without numbers": (
                pom_declaring(marked_at_its_version(guava_element("RELEASE"), "ignore[major-update]")),
                [_GUAVA_HELD_BACK],
            ),
            "a level keeping every version": (
                _pom_marking_guava("allow[major-update]"),
                [_GUAVA_ON_ITS_VARIANT],
            ),
        }
        for case, (pom, rule_sets_written) in cases.items():
            with self.subTest(case=case):
                self.find_pom(mock_run, mock_glob, pom)
                update_pom_xmls()
                self.assertEqual(self.rule_sets, rule_sets_written)

    def test_the_range_a_bound_holds_back_is_reported(self, mock_run: Mock, mock_glob: Mock):
        """Test that the range of versions a bound holds back is reported at its declaration, naming the bound."""
        pom = self.find_marked_pom(mock_run, mock_glob, "ignore[update>=34]")
        update_pom_xmls()
        self.assert_logged_among_others(
            Logger._MESSAGE_HOLDING_BACK_RANGE,
            dependency=GUAVA,
            location=Location(pom, 6),
            range="[34,)",
            directive="ignore[update>=34]",
        )

    def test_a_marker_on_a_version_maven_leaves_holds_back_nothing(self, mock_run: Mock, mock_glob: Mock):
        """Test that a marker on a version Maven does not update leaves the artefact to the cooldown."""
        parents = marked_at_its_version(guava_element("${parent.guava.version}"), "ignore[update]")
        marked_surefire = marked_at_its_version(plugin_element("maven-surefire-plugin", "3.5.0"), "ignore[update]")
        surefire_by_its_pom_property = plugin_element("maven-surefire-plugin", "${surefire.version}")
        cases = {
            "a parent's property": (
                pom_declaring(guava_element("32.0.0-jre"), managed=dependency_management_element(parents)),
                GUAVA,
                "32.0.0-jre",
                "33.7.1-jre",
            ),
            "a plugin's literal version": (
                pom_declaring(
                    properties=properties_element({"surefire.version": "3.5.0"}),
                    build=build_element(marked_surefire, managed=surefire_by_its_pom_property),
                ),
                SUREFIRE,
                "3.5.0",
                "3.5.1",
            ),
        }
        for case, (pom, artefact, current, held_back) in cases.items():
            with self.subTest(case=case):
                self.find_pom(mock_run, mock_glob, pom)
                with self.hold_back({artefact: (held_back,)}):
                    update_pom_xmls()
                self.assertEqual(self.rule_sets, [_rule_set(_rule(*artefact.split(":"), current, held_back))])

    @kills(
        Mutation(
            maven_module._artefact_rule,
            "max(cooldown_days(declaration.marker) for declaration in declarations)",
            "cooldown_days(declarations[-1].marker)",
            "the last declaration of an artefact sets its cooldown, so a shorter one undoes a longer one before it",
        ),
        Mutation(
            maven_module._artefact_rule,
            "max(cooldown_days(declaration.marker) for declaration in declarations)",
            "cooldown_days(declarations[0].marker)",
            "the first declaration of an artefact sets its cooldown, so a shorter one undoes a longer one after it",
        ),
        Mutation(
            maven_module._artefact_rule,
            "for declaration in declarations if",
            "for declaration in declarations[-1:] if",
            "the last declaration's bound replaces the ones before it, so Maven crosses an earlier declaration's bound",
        ),
        Mutation(
            maven_module._artefact_rule,
            "ignored = unique(",
            "ignored = (",
            "two declarations setting one bound name its range twice in the artefact's rule",
        ),
        Mutation(
            maven_module._variant_versions,
            "_other_variants(*(declaration.current_version for declaration in declarations))",
            "_other_variants(declarations[0].current_version)",
            "the first declaration of an artefact sets its variant, so the second declaration's variant is held back",
        ),
    )
    def test_an_artefact_declared_twice_gets_one_rule_for_both_declarations(self, mock_run: Mock, mock_glob: Mock):
        """Test that a twice-declared artefact's rule holds back what either marker does, and keeps either variant."""
        guava = guava_element("33.0.0-jre")
        ignoring = marked_at_its_version(guava, "ignore[update]")
        shorter = marked_at_its_version(guava, "allow[cooldown>=0]")
        from_34 = marked_at_its_version(guava, "ignore[update>=34]")
        from_35 = marked_at_its_version(guava, "ignore[update>=35]")
        on_34 = marked_at_its_version(guava, "ignore[update==34.*]")
        # The pom declares the managed guava first, and the other guava after it.
        cases = {
            "the first declaration ignoring the update": (ignoring, guava, [_GUAVA_HELD_BACK]),
            "the second declaration ignoring the update": (guava, ignoring, [_GUAVA_HELD_BACK]),
            "the first declaration setting a shorter cooldown": (shorter, guava, [_GUAVA_33_7_1_HELD_BACK]),
            "the second declaration setting a shorter cooldown": (guava, shorter, [_GUAVA_33_7_1_HELD_BACK]),
            "each declaration setting a bound of its own": (
                from_35,
                on_34,
                [_guava_range_held_back("[35,)", "[34,35)", versions=("33.7.1-jre",))],
            ),
            "both declarations setting one bound": (
                from_34,
                from_34,
                [_guava_range_held_back("[34,)", versions=("33.7.1-jre",))],
            ),
            "the declarations on two variants": (
                guava,
                guava_element("33.0.0-android"),
                [
                    _rule_set(
                        _rule_ignoring(
                            "com.google.guava",
                            "guava",
                            _variant_element("33.0.0-jre", "33.0.0-android"),
                            _exact_version_element("33.7.1-jre"),
                        )
                    )
                ],
            ),
        }
        for case, (managed, declared, rule_set) in cases.items():
            with self.subTest(case=case):
                pom = pom_declaring(declared, managed=dependency_management_element(managed))
                self.find_pom(mock_run, mock_glob, pom)
                with self.hold_back({GUAVA: ("33.7.1-jre",)}):
                    update_pom_xmls()
                self.assertEqual(self.rule_sets, rule_set)

    @patch_environ({COOLDOWN.name: "0"})
    def test_a_marker_holds_the_update_back_with_the_cooldown_switched_off(self, mock_run: Mock, mock_glob: Mock):
        """Test that the rule set holds back every version of a marked artefact when the cooldown holds nothing back."""
        self.find_marked_pom(mock_run, mock_glob, "ignore[update]")
        update_pom_xmls()
        self.assertEqual(self.rule_sets, [_GUAVA_HELD_BACK])
        self.assert_maven_ran(mock_run, rules=_RULES)

    @kills(
        Mutation(
            pom_xml_module.with_resolved_coordinates,
            " if is_resolved(declaration.dependency)",
            "",
            "a pom inheriting a group from its parent is asked about coordinates that resolve to nothing",
        ),
        Mutation(
            update_pom_xml_module._report_new_versions,
            "is_resolved = pom_xml_format.fully_resolved(update.effective.pinned)",
            "is_resolved = True",
            "Maven Central is asked for the changes of coordinates holding an unresolved property",
        ),
        Mutation(
            maven_module._ReactorModule.update,
            "if result.succeeded:",
            "if effective_pom is not None:",
            "Update-time skips the updates wherever it cannot read the effective pom, although the run succeeded",
        ),
        Mutation(
            pom_xml_module.versioned_declarations,
            " and is_resolved(declaration.dependency)",
            "",
            "the repository is asked which versions to hold back for coordinates holding a parent's property",
        ),
    )
    def test_coordinates_without_an_effective_pom_are_not_asked_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that every check passes over a dependency whose group the parent declares, without an effective pom."""
        # A pom can spell a dependency's group as a property its parent declares, which this pom does not hold. Maven
        # rewrote the pom, but Update-time cannot read the effective pom that would resolve the group.
        inherited = dependency_element("${spring.group}", "spring-core", "${spring.version}")
        before = pom_declaring(
            marked_at_its_version(guava_element("33.0.0-jre"), "ignore[cooldown<30]"),
            inherited,
            properties=_marked_property("spring.version", "6.1.0", "ignore[cooldown<30]"),
        )
        after = pom_declaring(
            marked_at_its_version(guava_element("33.7.1-jre"), "ignore[cooldown<30]"),
            inherited,
            properties=_marked_property("spring.version", "7.1.0", "ignore[cooldown<30]"),
        )
        self.find_rewritten_pom(mock_run, mock_glob, before, after, effective_pom="")
        mock_project = Mock(return_value=Project())
        with (
            self.asked_about() as asked,
            osv() as mock_post,
            patch.object(maven_central_module, "project", mock_project),
            patch.object(maven_central_module, "get_changes", Mock(return_value=NO_CHANGES)) as mock_changes,
            patch.object(maven_central_module, "serves", Mock(return_value=True)) as mock_serves,
        ):
            update_pom_xmls()
        # Guava reaches every check, so the dependency beside it reaching none says something about its coordinates.
        self.assertEqual(asked, [GUAVA])
        self.assertEqual(_artefacts_asked(mock_serves), [GUAVA])
        _assert_osv_asked_about_maven(mock_post, (GUAVA, "33.7.1-jre"))
        self.assertEqual(_artefacts_asked(mock_project), [GUAVA])
        self.assertEqual(_artefacts_asked(mock_changes), [GUAVA])

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
                with self.asked_about() as asked:
                    update_pom_xmls()
                # Guava is asked about, so the dependency beside it going unasked says something about its version.
                self.assertEqual(asked, [GUAVA])

    @patch_environ({COOLDOWN.name: "30"})
    def test_the_repository_is_asked_about_the_window_the_run_sets(self, mock_run: Mock, mock_glob: Mock):
        """Test that the cooldown the repository is asked about is the one --cooldown sets, not the default one."""
        self.find_pom(mock_run, mock_glob, _POM_DECLARING_GUAVA)
        with self.hold_back({GUAVA: ("33.7.1-jre",)}, cooldown_days=30):
            update_pom_xmls()
        self.assertEqual(self.rule_sets, [_GUAVA_33_7_1_HELD_BACK])

    def test_a_marker_cooldown_holds_back_the_versions_inside_its_window(self, mock_run: Mock, mock_glob: Mock):
        """Test that the rule names the versions inside the cooldown the marker sets, whatever cooldown the run sets."""
        for case, run_cooldown in {"no cooldown": "0", "a longer cooldown": "60"}.items():
            with self.subTest(case=case), patch_environ({COOLDOWN.name: run_cooldown}):
                self.find_marked_pom(mock_run, mock_glob, "ignore[cooldown<30]")
                with self.hold_back({GUAVA: ("33.7.1-jre",)}, cooldown_days=30):
                    update_pom_xmls()
                self.assertEqual(self.rule_sets, [_GUAVA_33_7_1_HELD_BACK])

    @kills(
        Mutation(
            maven_module._ReactorModule.update,
            "self._run_goals(options, _VERSIONS_GOALS)",
            "self._run_goals(_VERSIONS_OPTIONS, _VERSIONS_GOALS)",
            "the rule set stays out of the versions run, so a group the parent declares escapes the cooldown",
        ),
        Mutation(
            maven_module._ReactorModule.update,
            "self._run_goals(options, _VERSIONS_GOALS)",
            "self._run_goals_writing_effective_pom(options, _VERSIONS_GOALS)",
            "the updates write the effective pom again, costing a goal per pom that the first run already paid for",
        ),
    )
    def test_coordinates_from_a_parent_get_a_rule_as_maven_resolves_them(self, mock_run: Mock, mock_glob: Mock):
        """Test that the rule set names a dependency or plugin whose group or artifact the parent declares."""
        spring = "org.springframework:spring-core"
        # Maven lists the properties the parent declares in the effective pom.
        properties = properties_element({"spring.artifact": "spring-core"})
        effective_spring = effective_pom_declaring(_EFFECTIVE_SPRING, properties=properties)
        cases = {
            "a dependency's group": (
                pom_declaring(_SPRING_LEAVING_ITS_GROUP),
                effective_spring,
                _rule("org.springframework", "spring-core", "6.1.0", "7.1.0"),
                "",
            ),
            "a dependency's artifact": (
                pom_declaring(dependency_element("org.springframework", "${spring.artifact}", "6.1.0")),
                effective_spring,
                _rule("org.springframework", "spring-core", "6.1.0", "7.1.0"),
                "",
            ),
            "a plugin's group": (
                _POM_LEAVING_THE_VERSIONS_PLUGINS_GROUP,
                _EFFECTIVE_VERSIONS,
                _rule("org.codehaus.mojo", "versions-maven-plugin", "2.18.0", "2.19.0"),
                "versions.version",
            ),
        }
        for case, (declared, effective_pom, rule, properties) in cases.items():
            with self.subTest(case=case):
                self.find_pom(mock_run, mock_glob, declared, effective_pom)
                with self.hold_back({spring: ("7.1.0",), _VERSIONS: ("2.19.0",)}):
                    update_pom_xmls()
                self.assertEqual(self.rule_sets, [_rule_set(rule)])
                self.assert_maven_runs(mock_run, _effective_pom_command(), _versions_command(_RULES, properties))

    @kills(
        Mutation(
            maven_module._ReactorModule.update,
            "if result.succeeded:",
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
            maven_module._cooldown_versions,
            "days <= 0",
            "days < 0",
            "a cooldown of zero days asks the repository about the artefact and ignores the answer",
        )
    )
    def test_the_cooldown_skips_the_repository_when_it_is_switched_off(self, mock_run: Mock, mock_glob: Mock):
        """Test that a switched-off cooldown, the run's or a marker's, skips the repository while Maven still runs."""
        guava = guava_element("33.0.0-jre")
        cases = {
            "the run's cooldown": ({COOLDOWN.name: "0"}, guava),
            "a marker's cooldown": ({}, marked_at_its_version(guava, "allow[cooldown>=0]")),
        }
        for case, (environment, declared) in cases.items():
            with self.subTest(case=case), patch_environ(environment):
                self.find_pom(mock_run, mock_glob, pom_declaring(declared))
                with self.asked_about() as asked:
                    update_pom_xmls()
                self.assertEqual(asked, [])
                # Asserting Maven ran, so the cooldown rather than a pom that was never read is what left the artefacts
                # alone. The staleness check asks the repository whatever the cooldown is, so this says nothing about
                # that request.
                self.assert_maven_ran(mock_run, rules=_RULES)

    def test_a_failed_maven_run_is_reported_with_its_output(self, mock_run: Mock, mock_glob: Mock):
        """Test that a Maven run exiting non-zero is reported with what it wrote, and with nothing else."""
        # Maven stops before any goal runs, so the file for the effective pom stays empty.
        self.find_pom(mock_run, mock_glob, _POM_DECLARING_GUAVA, effective_pom="")
        # Maven writes its errors to stdout and leaves stderr empty, so the failure travels in the output.
        output = "[ERROR] Non-readable POM /project/pom.xml"
        mock_run.side_effect = _maven_failed(output)
        update_pom_xmls()
        self.assert_command_failed_logged(_maven_command(rules=_RULES), output)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_pom_xml_module._report_new_versions,
            "get_changes(reference.dependency, new.current_version)",
            "get_changes(reference.dependency, update.old.current_version)",
            "a dependency Maven moved is reported with the changes of the version it moved away from",
        ),
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "_report_new_versions(before, after, resolved)",
            "_report_new_versions(before, after, after)",
            "the report names a dependency as the pom spells it, so a group the parent declares stays a property",
        ),
    )
    def test_each_moved_dependency_is_reported_as_maven_names_it_with_changes(self, mock_run: Mock, mock_glob: Mock):
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
            return Changes(f"Changes in {artefact} {version}", markdown=False, url="")

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
    def test_a_dependency_and_plugin_sharing_a_property_are_each_reported(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency and a plugin naming the same property are both reported, at that property's line."""
        provider = dependency_element("org.apache.maven.surefire", "surefire-junit-platform", "${surefire.version}")
        plugin = build_element(plugin_element("maven-surefire-plugin", "${surefire.version}", group=None))
        before = pom_declaring(provider, properties=properties_element({"surefire.version": "3.5.0"}), build=plugin)
        after = pom_declaring(provider, properties=properties_element({"surefire.version": "3.5.2"}), build=plugin)
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, after)
        update_pom_xmls()
        self.assert_new_versions_logged(
            "3.5.2", Location(pom, 3), "org.apache.maven.surefire:surefire-junit-platform", SUREFIRE
        )

    def test_unserved_artefacts_naming_a_served_artefacts_property_are_not_reported(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that no new version is reported for an unserved artefact whose property a served artefact names."""
        client = dependency_element("org.glassfish.jersey.core", "jersey-client", "${jersey.version}")
        mistaken = [
            dependency_element("org.glassfish.jersey", artifact, "${jersey.version}")
            for artifact in ("jersey-client", "jersey-common")
        ]
        before = pom_declaring(client, *mistaken, properties=properties_element({"jersey.version": "3.1.11"}))
        after = pom_declaring(client, *mistaken, properties=properties_element({"jersey.version": "3.1.12"}))
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, after)
        with (
            patch.object(maven_central_module, "serves", Mock(side_effect=_on_jersey_core)),
            patch.object(maven_central_module, "get_changes", Mock(return_value=NO_CHANGES)) as mock_changes,
        ):
            update_pom_xmls()
        self.assert_new_versions_logged("3.1.12", Location(pom, 3), "org.glassfish.jersey.core:jersey-client")
        mock_changes.assert_called_once_with("org.glassfish.jersey.core:jersey-client", "3.1.12")
        unreported = [
            self._expected_call(
                Logger._MESSAGE_NEW_VERSION_NOT_REPORTED,
                {
                    "dependency": artefact,
                    "location": Location(pom, 3),
                    "property": "jersey.version",
                    "version": "3.1.12",
                },
            )
            for artefact in ("org.glassfish.jersey:jersey-client", "org.glassfish.jersey:jersey-common")
        ]
        self.assertEqual(self.records_of(Logger._MESSAGE_NEW_VERSION_NOT_REPORTED), unreported)

    def test_unserved_artefacts_whose_property_no_served_artefact_names_are_reported(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that unserved artefacts get a new version where every artefact naming their property is unserved."""
        lib = dependency_element("nl.example", "lib", "${internal.version}")
        tools = dependency_element("nl.example", "tools", "${internal.version}")
        before = pom_declaring(lib, tools, properties=properties_element({"internal.version": "1.0"}))
        after = pom_declaring(lib, tools, properties=properties_element({"internal.version": "1.1"}))
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, after)
        with patch.object(maven_central_module, "serves", Mock(return_value=False)):
            update_pom_xmls()
        self.assert_new_versions_logged("1.1", Location(pom, 3), "nl.example:lib", "nl.example:tools")
        self.assert_none_logged(Logger._MESSAGE_NEW_VERSION_NOT_REPORTED, "unreported new version")

    @kills(
        Mutation(
            update_pom_xml_module._unreportable_updates,
            "update.new.version_property in properties_on_central",
            "properties_on_central",
            "an unserved artefact goes unreported whenever a served artefact moved, whatever property each names",
        )
    )
    def test_unserved_artefacts_with_a_version_or_a_property_of_their_own_are_reported(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that unserved artefacts with a version or a property of their own are reported beside a served one."""
        client = dependency_element("org.glassfish.jersey.core", "jersey-client", "${jersey.version}")
        tools = dependency_element("nl.example", "tools", "${internal.version}")
        before = pom_declaring(
            client,
            dependency_element("nl.example", "lib", "1.0"),
            tools,
            properties=properties_element({"jersey.version": "3.1.11", "internal.version": "1.0"}),
        )
        after = pom_declaring(
            client,
            dependency_element("nl.example", "lib", "1.1"),
            tools,
            properties=properties_element({"jersey.version": "3.1.12", "internal.version": "1.1"}),
        )
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, after)
        with patch.object(maven_central_module, "serves", Mock(side_effect=_on_jersey_core)):
            update_pom_xmls()
        self.assert_new_version_logged_among_others(
            "org.glassfish.jersey.core:jersey-client", "3.1.12", Location(pom, 3)
        )
        self.assert_new_version_logged_among_others("nl.example:lib", "1.1", Location(pom, 15))
        self.assert_new_version_logged_among_others("nl.example:tools", "1.1", Location(pom, 4))
        self.assert_none_logged(Logger._MESSAGE_NEW_VERSION_NOT_REPORTED, "unreported new version")

    @kills(
        Mutation(
            update_pom_xml_module._report_new_versions,
            "in unique(zip(before, after, resolved, strict=True))",
            "in zip(before, after, resolved, strict=True)",
            "a property two declarations of one artefact name is reported as moved once per declaration, at one line",
        )
    )
    def test_a_new_version_two_declarations_share_is_reported_once(self, mock_run: Mock, mock_glob: Mock):
        """Test that the new version of a property two declarations of one artefact name is reported once, at it."""
        before = _guava_twice_by_property(properties_element({"guava.version": "33.0.0-jre"}))
        after = _guava_twice_by_property(properties_element({"guava.version": "33.7.1-jre"}))
        pom = self.find_rewritten_pom(mock_run, mock_glob, before, after)
        update_pom_xmls()
        self.assert_new_version_logged(GUAVA, "33.7.1-jre", Location(pom, 3))

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
            update_pom_xml_module._update_pom_xml,
            "_report_new_versions(before, after, resolved)",
            "_report_new_versions(before, resolved, resolved)",
            "Update-time reports a move from the parent's property to the version it holds, though Maven moved nothing",
        ),
    )
    def test_a_dependency_maven_left_alone_is_neither_reported_nor_asked_about(self, mock_run: Mock, mock_glob: Mock):
        """Test that nothing in a pom Maven left alone is reported or asked about, what the parent versions included."""
        spring = dependency_element("org.springframework", "spring-core", "${spring.version}")
        unchanged = pom_declaring(guava_element("33.7.1-jre"), spring, dependency_element("junit", "junit", None))
        effective_guava = effective_dependency_element(GUAVA, "33.7.1-jre", line=5)
        effective_spring = effective_dependency_element("org.springframework:spring-core", "6.1.0", line=10)
        effective_junit = effective_dependency_element("junit:junit", "4.13.2", line=15, managed_at=(PARENT_POM_ID, 20))
        effective_pom = effective_pom_declaring(effective_guava, effective_spring, effective_junit)
        self.find_rewritten_pom(mock_run, mock_glob, unchanged, unchanged, effective_pom)
        with patch.object(maven_central_module, "get_changes") as get_changes:
            update_pom_xmls()
        self.assert_maven_ran(mock_run, rules=_RULES)  # The pom was examined, so reporting nothing says something.
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
        poms = {"broken": "<project><broken>", "app": pom_declaring(guava_element("33.7.1-jre"))}
        broken, second = self.find_reactor(mock_run, mock_glob, poms)
        update_pom_xmls()
        self.assert_invalid_file_logged(broken, "XML")
        self.assert_path_logged(second)
        # Maven ran for the pom that parsed, and not for the one that did not.
        self.assert_maven_runs(mock_run, _maven_command(rules=_RULES), directory=_PROJECT / "app")

    @kills(
        Mutation(
            update_pom_xml_module._update_pom_xml,
            "_LOG.invalid_xml_after_update(pom_xml)",
            "_warn_about_vulnerabilities(before)\n        _LOG.invalid_xml_after_update(pom_xml)",
            "a pom left unreadable is checked on its pre-run reading, against versions the file may no longer hold",
        ),
    )
    def test_a_pom_that_no_longer_parses_after_the_run_is_an_error(self, mock_run: Mock, mock_glob: Mock):
        """Test that a pom Maven left unparsable is reported as a failed update, and then left alone."""
        pom = self.find_rewritten_pom(mock_run, mock_glob, _POM_DECLARING_GUAVA, "<project><broken>")
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
            maven_module._ReactorModule._run_goals,
            " and result.stdout:",
            ":",
            "a missing Maven is reported twice: once by `run`, and again as a failed run that wrote nothing",
        ),
    )
    def test_a_missing_maven_is_reported_and_the_next_pom_is_still_checked(self, mock_run: Mock, mock_glob: Mock):
        """Test that a missing Maven executable is reported once, and leaves the next pom checked as usual."""
        second = self.find_poms(mock_run, mock_glob, "<project/>", "<project/>")[1]
        mock_run.side_effect = [FileNotFoundError, _maven_succeeded()]
        update_pom_xmls()
        self.assert_error_logged(Logger._MESSAGE_COMMAND_NOT_FOUND, command=_maven_command(), executable="mvn")
        self.assertEqual(mock_run.call_count, 2)
        self.assert_path_logged(second)

    @kills(
        Mutation(
            maven_module._maven,
            'f"./{_WRAPPER}" if (directory / _WRAPPER).exists() else "mvn"',
            '"mvn"',
            "a project's own Maven wrapper is passed over, so another Maven than it builds with updates it",
        ),
        Mutation(
            maven_module._maven,
            "(directory / _WRAPPER).exists()",
            "Path(_WRAPPER).exists()",
            "the wrapper is looked for in the directory the scan runs in, so a reactor's own wrapper goes unused",
        ),
        Mutation(
            maven_module._ReactorModule._run_goals,
            "_maven(self.root)",
            "_maven(self.pom_xml.parent)",
            "the wrapper is looked for beside the module, so a module's run from the reactor root passes it over",
        ),
    )
    def test_the_maven_wrapper_runs_when_it_sits_in_the_reactor_root(self, mock_run: Mock, mock_glob: Mock):
        """Test that the wrapper in the reactor root runs for each pom of the reactor, not the mvn on the path."""
        self.find_reactor(mock_run, mock_glob, {".": _aggregator("app"), "app": "<project/>"})
        with patch("pathlib.Path.exists", lambda self: self == _PROJECT / "mvnw"):
            update_pom_xmls()
        self.assert_maven_runs(mock_run, _maven_command("./mvnw"), _maven_command("./mvnw", directory="app"))

    @kills(
        Mutation(
            pom_xml_module.module_directories,
            'project.child("modules")',
            'next(project.descendants("modules"), None)',
            "Maven runs a module from a reactor root whose inactive profile leaves that module out, so the run fails",
        )
    )
    def test_a_module_a_profile_lists_runs_maven_on_its_own(self, mock_run: Mock, mock_glob: Mock):
        """Test that a module only a profile lists runs Maven from its own directory."""
        root = "<project><profiles><profile><modules><module>app</module></modules></profile></profiles></project>"
        self.find_reactor(mock_run, mock_glob, {".": root, "app": "<project/>"})
        update_pom_xmls()
        self.assertEqual(mock_run.call_args_list[1], _maven_run(_maven_command(), _PROJECT / "app"))

    def test_a_module_listed_by_another_path_runs_maven_from_the_reactor_root(self, mock_run: Mock, mock_glob: Mock):
        """Test that a module listed by a path outside its aggregator, or by its pom, runs from the reactor root."""
        root = _PROJECT / "root"
        # Each case lists a module by the path given, puts it in the directory given, and selects it by the path given.
        cases = {"a sibling": ("../sibling", "sibling", "../sibling"), "a pom file": ("app/pom.xml", "root/app", "app")}
        for case, (listed, directory, selected) in cases.items():
            with self.subTest(case=case):
                self.find_reactor(mock_run, mock_glob, {"root": _aggregator(listed), directory: "<project/>"})
                update_pom_xmls()
                commands = (_maven_command(), _maven_command(directory=selected))
                self.assert_maven_runs(mock_run, *commands, directory=root)

    def test_a_pom_listing_itself_as_a_module_runs_maven_selecting_itself(self, mock_run: Mock, mock_glob: Mock):
        """Test that a pom listing its own directory as a module runs Maven once, from its own directory."""
        self.find_pom(mock_run, mock_glob, _aggregator("."))
        update_pom_xmls()
        self.assert_maven_ran(mock_run)

    def test_each_pom_of_a_reactor_runs_maven_from_the_topmost_aggregator(self, mock_run: Mock, mock_glob: Mock):
        """Test that each pom of a reactor runs Maven from the topmost aggregator's directory, selecting itself."""
        reactor = {".": _aggregator("config"), "config": _aggregator("spring"), "config/spring": "<project/>"}
        self.find_reactor(mock_run, mock_glob, reactor)
        update_pom_xmls()
        self.assert_maven_runs(mock_run, *(_maven_command(directory=directory) for directory in reactor))

    @kills(
        Mutation(
            maven_module,
            r"\d+ projects?",
            "1 project",
            "a reactor holding more than one pom Maven cannot read is taken for a readable one",
        )
    )
    def test_an_unreadable_reactor_stops_maven_for_its_later_poms_alone(self, mock_run: Mock, mock_glob: Mock):
        """Test that a run that cannot read its reactor stops Maven for the reactor's later poms, not another's."""
        poms = {".": _aggregator("app", "lib"), "app": "<project/>", "lib": "<project/>", "other": "<project/>"}
        _, *later_poms, _ = self.find_reactor(mock_run, mock_glob, poms)
        output = "[ERROR] The build could not read 2 projects -> [Help 1]"
        mock_run.side_effect = [_maven_failed(output), *[_maven_succeeded()] * (len(poms) - 1)]
        update_pom_xmls()
        message = Logger._MESSAGE_UNREADABLE_REACTOR_AS_FOUND
        self.assert_unreadable_reactor_logged(message, output=output, poms="2 other poms")
        runs = [_maven_run(_maven_command()), _maven_run(_maven_command(), _PROJECT / "other")]
        self.assertEqual(mock_run.call_args_list, runs)
        self.assert_maven_not_run_logged(*later_poms)

    def test_an_unreadable_reactor_is_reported_as_broken_by_the_pom_update_time_updated(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that the error names the pom an earlier run changed, and the poms left where there are any."""
        cases = {
            "poms left": (("app", "lib"), Logger._MESSAGE_UNREADABLE_REACTOR_SINCE_UPDATE, {"poms": "1 pom"}),
            "the reactor's last pom": (("app",), Logger._MESSAGE_UNREADABLE_REACTOR_SINCE_UPDATE_WITHOUT_POMS_LEFT, {}),
        }
        for case, (later, message, fields) in cases.items():
            with self.subTest(case=case):
                bom = self.find_reactor_updated_by_bom(mock_run, mock_glob, *later, failing="app")
                update_pom_xmls()
                self.assert_unreadable_reactor_logged(message, "app", updated=Location(bom), **fields)

    @kills(
        Mutation(
            maven_module.Reactor.update,
            "else None",
            "else self._changed_pom",
            "the error blames an update that a later run read the reactor after",
        ),
        Mutation(
            maven_module.Reactor._report_unreadable,
            "and self._first_run",
            "and True",
            "the error says the scan found the reactor unreadable, although an earlier run read it",
        ),
    )
    def test_a_reactor_read_after_the_update_is_reported_without_a_cause(self, mock_run: Mock, mock_glob: Mock):
        """Test that the error leaves the update out where a later run read the reactor without changing a pom."""
        self.find_reactor_updated_by_bom(mock_run, mock_glob, "app", "lib", "web", failing="lib")
        update_pom_xmls()
        self.assert_unreadable_reactor_logged(Logger._MESSAGE_UNREADABLE_REACTOR, "lib", poms="1 pom")

    def test_a_reactor_holding_a_pom_that_does_not_parse_runs_maven_over_none_of_its_poms(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a pom that does not parse leaves every pom of its reactor without a Maven run, logging why once."""
        poms = {".": _aggregator("app", "lib"), "app": "<project>", "lib": "<project/>"}
        root, app, lib = self.find_reactor(mock_run, mock_glob, poms)
        update_pom_xmls()
        self.assertEqual(mock_run.call_args_list, [])
        self.assert_error_logged(
            Logger._MESSAGE_UNPARSABLE_REACTOR,
            location=Location(_PROJECT),
            unparsable=Location(app),
            poms="2 other poms",
        )
        self.assert_maven_not_run_logged(root, lib)

    def test_a_single_pom_maven_cannot_read_gets_mavens_error_alone(self, mock_run: Mock, mock_glob: Mock):
        """Test that a pom forming a reactor of its own, which Maven cannot read, gets Maven's error alone."""
        self.find_pom(mock_run, mock_glob)
        mock_run.side_effect = _maven_failed(_UNREADABLE_REACTOR_OUTPUT)
        update_pom_xmls()
        self.assert_command_failed_logged(_maven_command(), _UNREADABLE_REACTOR_OUTPUT)

    def test_a_pom_left_without_a_maven_run_is_still_checked(self, mock_run: Mock, mock_glob: Mock):
        """Test that the dependencies of a pom Maven does not run over still reach Maven Central and OSV."""
        self.find_reactor(mock_run, mock_glob, {".": _aggregator("app"), "app": _POM_DECLARING_GUAVA})
        mock_run.side_effect = _maven_failed(_UNREADABLE_REACTOR_OUTPUT)
        mock_project = Mock(return_value=Project())
        with osv() as mock_post, patch.object(maven_central_module, "project", mock_project):
            update_pom_xmls()
        self.assert_unreadable_reactor_logged(Logger._MESSAGE_UNREADABLE_REACTOR_AS_FOUND, poms="1 other pom")
        self.assert_maven_runs(mock_run, _maven_command())
        _assert_osv_asked_about_maven(mock_post, (GUAVA, "33.0.0-jre"))
        self.assertEqual(_artefacts_asked(mock_project), [GUAVA])

    @kills(
        Mutation(
            maven_module,
            r'"The build could not read \d+ projects?"',
            r'"\[ERROR\]"',
            "any failed run that reports an error stops Maven for the rest of its reactor",
        )
    )
    def test_a_goal_failing_for_one_pom_still_runs_maven_over_the_next(self, mock_run: Mock, mock_glob: Mock):
        """Test that Maven still runs over the next pom of the reactor after a run that failed a goal."""
        self.find_reactor(mock_run, mock_glob, {".": _aggregator("app"), "app": "<project/>"})
        output = "[ERROR] Failed to execute goal org.codehaus.mojo:versions-maven-plugin on project root"
        mock_run.side_effect = [_maven_failed(output), _maven_succeeded()]
        update_pom_xmls()
        self.assert_command_failed_logged(_maven_command(), output)
        self.assert_maven_runs(mock_run, _maven_command(), _maven_command(directory="app"))
        self.assert_maven_not_run_logged()
