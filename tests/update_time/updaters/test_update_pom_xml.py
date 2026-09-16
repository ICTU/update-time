"""Unit tests for the pom.xml updater."""

import re
from pathlib import Path
from subprocess import CalledProcessError  # nosec
from unittest.mock import Mock, patch

from update_time.io.log import Logger
from update_time.package_managers import maven as maven_module
from update_time.primitives.command import Command
from update_time.primitives.location import Location
from update_time.updaters import update_pom_xml as update_pom_xml_module
from update_time.updaters.update_pom_xml import update_pom_xmls

from tests.helpers import mock_path, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.helpers import LoggingTestCase


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


def _pom(*dependencies: str, properties: str = "", managed: str = "") -> str:
    """Return a pom declaring the given properties, managed dependencies, and dependency elements."""
    return (
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        f"{properties}"
        f"{managed}"
        "  <dependencies>\n"
        f"{''.join(dependencies)}"
        "  </dependencies>\n"
        "</project>\n"
    )


def _plugin_version() -> str:
    """Return the versions plugin release the pom Update-time ships declares, read off that pom's own text."""
    pom = (Path(maven_module.__file__).parent / "pom.xml").read_text()
    declared = re.search(r"<versions\.plugin\.version>(?P<version>[^<]+)</versions\.plugin\.version>", pom)
    return declared["version"] if declared else ""


def _maven_command(executable: str = "mvn") -> Command:
    """Return the command Update-time runs over a pom: the plugin's two goals, under the shipped pom's version."""
    return Command(
        executable,
        "--batch-mode",
        "--no-transfer-progress",
        "--non-recursive",
        "--update-snapshots",
        "-DgenerateBackupPoms=false",
        "-Dmaven.version.ignore=(?i).*[-.](alpha|beta|rc|cr|m|pre|preview)[-.]?[0-9]*",
        f"org.codehaus.mojo:versions-maven-plugin:{_plugin_version()}:use-latest-releases",
        f"org.codehaus.mojo:versions-maven-plugin:{_plugin_version()}:update-properties",
    )


# No file sits beside the pom unless a test puts one there, so the wrapper is absent except where it is the case.
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
        """Discover a single mock pom.xml that Maven rewrites, so it reads as `before` and then as `after`."""
        pom = self.find_pom(mock_run, mock_glob, before)
        pom.read_bytes = Mock(side_effect=[before.encode(), after.encode()])
        return pom

    def assert_maven_ran(self, mock_run: Mock, executable: str = "mvn") -> None:
        """Assert that the Maven command ran once, with the given executable, in the pom's own directory."""
        command = _maven_command(executable)
        mock_run.assert_called_once_with(command, capture_output=True, text=True, check=True, cwd=Path("/"))

    def test_maven_runs_in_the_poms_own_directory(self, mock_run: Mock, mock_glob: Mock):
        """Test that Maven runs both goals of the versions plugin Update-time names, in the pom's own directory."""
        self.find_pom(mock_run, mock_glob)
        update_pom_xmls()
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
            "Maven runs on a pom Update-time cannot read, so it is reported twice and run on all the same",
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
