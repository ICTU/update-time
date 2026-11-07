"""Group the scanned poms into reactors, and run Maven over each pom of a reactor."""

import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.formats import xml
from update_time.io.log import get_logger
from update_time.io.process import is_installed, run
from update_time.manifests import pom_xml as pom_xml_manifest
from update_time.package_managers.maven.plugins import HELP_PLUGIN
from update_time.package_managers.maven.versions_plugin import VERSIONS_GOALS, versions_options
from update_time.primitives.command import Command

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from update_time.formats.xml import XmlElement
    from update_time.primitives.command import Result

_LOG = get_logger("pom.xml")

# Every Maven run takes these options, whichever goals it runs.
_OPTIONS = ("--batch-mode", "--no-transfer-progress")

# `effective-pom` writes the pom as Maven builds it, before any other goal of the same run rewrites it.
_EFFECTIVE_POM_GOAL = f"{HELP_PLUGIN}:effective-pom"


# `verbose` writes the input location of each element of the effective pom after it: the pom and the line declaring
# that element.
_EFFECTIVE_POM_OPTION = "-Dverbose"


# The help plugin reports in this line where it wrote the effective pom. A project that configures the plugin's
# `<output>` overrides `-Doutput`, so the effective pom lands where that project says.
_EFFECTIVE_POM_WRITTEN = re.compile(r"Effective-POM written to: (?P<path>.+)")


# A project ships this wrapper script beside its root pom. The script runs the Maven version that project builds with.
_WRAPPER = "mvnw"

# The Maven on the path, which runs where the project does not ship a wrapper.
_MVN = "mvn"


def _maven(directory: Path) -> str | None:
    """Return the Maven to run in the directory, if any: the project's wrapper, or else the mvn on the path."""
    if (directory / _WRAPPER).exists():
        return f"./{_WRAPPER}"
    return _MVN if is_installed(_MVN) else None


@dataclass(frozen=True)
class _ReactorModule:
    """A pom, the root directory of the reactor Maven builds it in, and the Maven that builds it."""

    pom_xml: Path
    root: Path
    maven: str

    @property
    def _selection(self) -> tuple[str, ...]:
        """Return the options that select the pom in the reactor, by its directory relative to the root."""
        return ("--projects", str(self.pom_xml.parent.relative_to(self.root, walk_up=True)))

    def _run_goals(self, options: tuple[str, ...], goals: tuple[str, ...]) -> Result:
        """Run Maven over the pom, and return what the run wrote and whether it succeeded."""
        command = Command(self.maven, *_OPTIONS, *self._selection, *options, *goals)
        result = run(command, cwd=self.root)
        # Maven writes its [ERROR] lines to stdout and leaves stderr empty, so `run` surfaces nothing for a failed run.
        # A run the system refused to start wrote nothing at all, and `run` reported that itself.
        if not result.succeeded and result.stdout:
            _LOG.command_failed(command, result.stdout)
        return result

    def _run_goals_writing_effective_pom(
        self, options: tuple[str, ...] = (), goals: tuple[str, ...] = ()
    ) -> tuple[Result, XmlElement | None]:
        """Run Maven over the pom, the effective pom's goal first, and return the run's result and the effective pom."""
        with _effective_pom_file() as output:
            effective_pom_options = (_EFFECTIVE_POM_OPTION, f"-Doutput={output}")
            result = self._run_goals((*options, *effective_pom_options), (_EFFECTIVE_POM_GOAL, *goals))
            # Maven builds the model before any goal runs, so the effective pom holds even when a later goal fails.
            effective_pom = xml.read(output)
        if effective_pom is None:
            effective_pom = _effective_pom_written_elsewhere(result.stdout)
        if result.succeeded and effective_pom is None:
            _LOG.effective_pom_unreadable(self.pom_xml)
        if effective_pom is not None and not pom_xml_manifest.has_input_locations(effective_pom):
            _LOG.effective_pom_without_input_locations(self.pom_xml)
        return result, effective_pom

    def update(self) -> tuple[Result, XmlElement | None]:
        """Update the dependencies and plugins the pom declares, and return the first run's result and effective pom.

        The effective pom is the one Maven wrote before it updated the pom. A pom leaving a group or an artifact to its
        parent gets the effective pom in a Maven run of its own, so the rule set names the coordinates Maven resolves.
        """
        if not pom_xml_manifest.leaves_coordinates_unresolved(self.pom_xml):
            with versions_options(pom_xml_manifest.versioned_declarations(self.pom_xml)) as options:
                return self._run_goals_writing_effective_pom(options, VERSIONS_GOALS)
        result, effective_pom = self._run_goals_writing_effective_pom()
        if result.succeeded:  # A single run failing at the same point would not reach the updates either.
            with versions_options(pom_xml_manifest.versioned_declarations(self.pom_xml, effective_pom)) as options:
                self._run_goals(options, VERSIONS_GOALS)
        return result, effective_pom


# Maven reads every pom of the reactor before any goal runs. This line opens its report of the poms it cannot read.
_UNREADABLE_REACTOR = re.compile(r"The build could not read \d+ projects?")


class Reactor:
    """The poms Maven builds together, from the root directory of the topmost pom whose modules reach them."""

    def __init__(self, root: Path, pom_xmls: list[Path]) -> None:
        """Start with the reactor's readability unknown, until the first update reads its poms."""
        self._root = root
        self._pom_xmls = pom_xmls
        self._readable: bool | None = None
        self._changed_pom: Path | None = None
        self._first_run = True

    def update(self, pom_xml: Path) -> XmlElement | None:
        """Update the dependencies and plugins the pom declares, and return the effective pom Maven wrote, if any.

        Maven reads every pom of the reactor in each run, so once a run cannot read them, Maven runs over none of the
        reactor's poms after it. Each run that reads the reactor reads every pom the runs before it changed. So the pom
        that broke the reactor is the one changed by the latest run that read the reactor. Where the project does not
        ship a wrapper and mvn is not on the path, each pom gets an error.
        """
        if (maven := _maven(self._root)) is None:
            _LOG.package_manager_not_on_path(pom_xml, _MVN)
            return None
        if self._readable is None:
            self._readable = self._every_pom_parses()
        if not self._readable:
            _LOG.maven_not_run(pom_xml)
            return None
        before = _lines(pom_xml)
        result, effective_pom = _ReactorModule(pom_xml, self._root, maven).update()
        self._readable = not _UNREADABLE_REACTOR.search(result.stdout)
        if self._readable:
            self._changed_pom = pom_xml if _lines(pom_xml) != before else None
        else:
            self._report_unreadable(pom_xml)
        self._first_run = False
        return effective_pom

    def _every_pom_parses(self) -> bool:
        """Return whether every pom of the reactor parses, reporting the first one that does not.

        Maven cannot read a reactor holding a pom that does not parse, so every Maven run over that reactor fails.
        """
        unparsable = next((pom_xml for pom_xml in self._pom_xmls if _lines(pom_xml) is None), None)
        if unparsable is not None:
            _LOG.unparsable_reactor(self._root, unparsable, len(self._pom_xmls) - 1)
        return unparsable is None

    def _report_unreadable(self, pom_xml: Path) -> None:
        """Report that Maven cannot read the reactor, naming the cause and the poms left, where there are any.

        When the reactor's first run fails, Maven could not read the reactor before Update-time changed any of its poms.
        Update-time does not add to Maven's error where the run was over the reactor's last pom and an update did not
        break the reactor.
        """
        poms_left = len(self._pom_xmls_after(pom_xml))
        if self._changed_pom is not None and poms_left:
            _LOG.unreadable_reactor_since_update(self._root, self._changed_pom, poms_left)
        elif self._changed_pom is not None:
            _LOG.unreadable_reactor_since_update_without_poms_left(self._root, self._changed_pom)
        elif poms_left and self._first_run:
            _LOG.unreadable_reactor_as_found(self._root, poms_left)
        elif poms_left:
            _LOG.unreadable_reactor(self._root, poms_left)

    def _pom_xmls_after(self, pom_xml: Path) -> list[Path]:
        """Return the reactor's poms that the scan reaches after the pom."""
        return self._pom_xmls[self._pom_xmls.index(pom_xml) + 1 :]


def reactors(pom_xmls: list[Path]) -> dict[Path, Reactor]:
    """Return the reactor each pom is part of, which holds its poms in the scan's order."""
    aggregators = _aggregators(pom_xmls)
    pom_xmls_by_root: dict[Path, list[Path]] = {}
    for pom_xml in pom_xmls:
        pom_xmls_by_root.setdefault(_reactor_root(pom_xml.parent, aggregators), []).append(pom_xml)
    reactors_by_pom: dict[Path, Reactor] = {}
    for root, reactor_pom_xmls in pom_xmls_by_root.items():
        reactors_by_pom |= dict.fromkeys(reactor_pom_xmls, Reactor(root, reactor_pom_xmls))
    return reactors_by_pom


def _aggregators(pom_xmls: list[Path]) -> dict[Path, Path]:
    """Return the directory of the pom listing each module, keyed by the module's directory."""
    return {module: pom_xml.parent for pom_xml in pom_xmls for module in pom_xml_manifest.module_directories(pom_xml)}


def _reactor_root(directory: Path, aggregators: Mapping[Path, Path]) -> Path:
    """Return the directory of the topmost pom whose modules reach the directory, or the directory itself.

    The climb stops at a directory it reached before, where poms list each other as modules.
    """
    reached = {directory}
    while (aggregator := aggregators.get(directory)) is not None and aggregator not in reached:
        reached.add(aggregator)
        directory = aggregator
    return directory


def _lines(pom_xml: Path) -> list[str] | None:
    """Return the lines of the pom, or None where it cannot be read or does not parse."""
    document = xml.read_document(pom_xml)
    return None if document is None else document.lines


def _effective_pom_written_elsewhere(stdout: str) -> XmlElement | None:
    """Return the effective pom at the path the help plugin reports writing it to, or None where it reports none."""
    written = _EFFECTIVE_POM_WRITTEN.search(stdout)
    return None if written is None else xml.read(Path(written["path"]))


@contextmanager
def _effective_pom_file() -> Generator[Path]:
    """Name a file for Maven to write the effective pom to, and remove the file once the run is over."""
    with tempfile.NamedTemporaryFile(suffix=".xml", delete_on_close=False) as file:
        file.close()
        yield Path(file.name)
