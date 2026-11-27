"""Group the scanned poms into reactors, and run Maven over each pom of a reactor."""

import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from update_time.formats.xml import read, read_document
from update_time.io.log import NotRunCause, ReactorCause, ToolFailure, ToolTask, get_logger
from update_time.io.process import is_installed, run
from update_time.manifests.pom_xml import (
    has_input_locations,
    leaves_coordinates_unresolved,
    module_directories,
    versioned_declarations,
)
from update_time.primitives.command import Command

from .plugins import HELP_PLUGIN
from .versions_plugin import VERSIONS_GOALS, versions_options

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from update_time.formats.xml import XmlElement
    from update_time.io.log import Logger
    from update_time.primitives.command import Result

_LOG: Final[Logger] = get_logger("pom.xml")

# `effective-pom` writes the pom as Maven builds it, before any other goal of the same run rewrites it.
_EFFECTIVE_POM_GOAL: Final[str] = f"{HELP_PLUGIN}:effective-pom"

# The help plugin reports in this line where it wrote the effective pom. A project that configures the plugin's
# `<output>` overrides `-Doutput`, so the effective pom lands where that project says.
_EFFECTIVE_POM_WRITTEN: Final[re.Pattern[str]] = re.compile(r"Effective-POM written to: (?P<path>.+)")

# A project ships this wrapper script beside its root pom. The script runs the Maven version that project builds with.
_WRAPPER: Final[str] = "mvnw"

# The Maven on the path, which runs where the project does not ship a wrapper.
_MVN: Final[str] = "mvn"

# Maven reads every pom of the reactor before any goal runs. This line opens its report of the poms it cannot read.
_UNREADABLE_REACTOR: Final[re.Pattern[str]] = re.compile(r"The build could not read \d+ projects?")


def reactors(pom_xmls: list[Path]) -> dict[Path, Reactor]:
    """Return the reactor each pom is part of, which holds its poms in the scan's order."""
    # The directory of the pom listing each module, keyed by the module's directory.
    aggregators = {module: pom_xml.parent for pom_xml in pom_xmls for module in module_directories(pom_xml)}
    pom_xmls_by_root: dict[Path, list[Path]] = {}
    for pom_xml in pom_xmls:
        pom_xmls_by_root.setdefault(_reactor_root(pom_xml.parent, aggregators), []).append(pom_xml)
    reactors_by_pom: dict[Path, Reactor] = {}
    for root, reactor_pom_xmls in pom_xmls_by_root.items():
        reactors_by_pom |= dict.fromkeys(reactor_pom_xmls, Reactor(root, reactor_pom_xmls))
    return reactors_by_pom


def _reactor_root(directory: Path, aggregators: Mapping[Path, Path]) -> Path:
    """Return the directory of the topmost pom whose modules reach the directory, or the directory itself.

    The climb stops at a directory it reached before, where poms list each other as modules.
    """
    reached = {directory}
    while (aggregator := aggregators.get(directory)) is not None and aggregator not in reached:
        reached.add(aggregator)
        directory = aggregator
    return directory


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
            _LOG.tool_failed(pom_xml, ToolTask.UPDATE, _MVN, ToolFailure.NOT_ON_PATH)
            return None
        if self._readable is None:
            self._readable = self._every_pom_parses()
        if not self._readable:
            _LOG.manager_not_run(pom_xml, "Maven", NotRunCause.REACTOR_UNREADABLE)
            return None
        before = _lines(pom_xml)
        result, effective_pom = _ReactorModule(pom_xml=pom_xml, root=self._root, maven=maven).update()
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
            _LOG.unreadable_reactor(self._root, ReactorCause.UNPARSABLE_POM, len(self._pom_xmls) - 1, unparsable)
        return unparsable is None

    def _report_unreadable(self, pom_xml: Path) -> None:
        """Report that Maven cannot read the reactor, naming the cause and the poms left, where there are any.

        When the reactor's first run fails, Maven could not read the reactor before Update-time changed any of its poms.
        Update-time does not add to Maven's error where the run was over the reactor's last pom and an update did not
        break the reactor.
        """
        poms_left = len(self._pom_xmls_after(pom_xml))
        if self._changed_pom is not None:
            _LOG.unreadable_reactor(self._root, ReactorCause.UPDATED_POM, poms_left, self._changed_pom)
        elif poms_left and self._first_run:
            _LOG.unreadable_reactor(self._root, ReactorCause.AS_FOUND, poms_left)
        elif poms_left:
            _LOG.unreadable_reactor(self._root, ReactorCause.READ_EARLIER, poms_left)

    def _pom_xmls_after(self, pom_xml: Path) -> list[Path]:
        """Return the reactor's poms that the scan reaches after the pom."""
        return self._pom_xmls[self._pom_xmls.index(pom_xml) + 1 :]


def _maven(directory: Path) -> str | None:
    """Return the Maven to run in the directory, if any: the project's wrapper, or else the mvn on the path."""
    if (directory / _WRAPPER).exists():
        return f"./{_WRAPPER}"
    return _MVN if is_installed(_MVN) else None


def _lines(pom_xml: Path) -> list[str] | None:
    """Return the lines of the pom, or None where it cannot be read or does not parse."""
    document = read_document(pom_xml)
    return None if document is None else document.lines


@dataclass(frozen=True, slots=True, kw_only=True)
class _ReactorModule:
    """A pom, the root directory of the reactor Maven builds it in, and the Maven that builds it."""

    pom_xml: Path
    root: Path
    maven: str

    def update(self) -> tuple[Result, XmlElement | None]:
        """Update the dependencies and plugins the pom declares, and return the first run's result and effective pom.

        The effective pom is the one Maven wrote before it updated the pom. A pom leaving a group or an artifact to its
        parent gets the effective pom in a Maven run of its own, so the rule set names the coordinates Maven resolves.
        """
        if not leaves_coordinates_unresolved(self.pom_xml):
            with versions_options(versioned_declarations(self.pom_xml)) as options:
                return self._run_goals_writing_effective_pom(options, VERSIONS_GOALS)
        result, effective_pom = self._run_goals_writing_effective_pom()
        if result.succeeded:  # A single run failing at the same point would not reach the updates either.
            with versions_options(versioned_declarations(self.pom_xml, effective_pom)) as options:
                self._run_goals(options, VERSIONS_GOALS)
        return result, effective_pom

    def _run_goals_writing_effective_pom(
        self, options: tuple[str, ...] = (), goals: tuple[str, ...] = ()
    ) -> tuple[Result, XmlElement | None]:
        """Run Maven over the pom, the effective pom's goal first, and return the run's result and the effective pom."""
        with _effective_pom_file() as output:
            # `verbose` writes the input location of each element of the effective pom after it: the pom and the line
            # declaring that element.
            result = self._run_goals((*options, *("-Dverbose", f"-Doutput={output}")), (_EFFECTIVE_POM_GOAL, *goals))
            # Maven builds the model before any goal runs, so the effective pom holds even when a later goal fails.
            effective_pom = read(output)
        # Where the help plugin reports writing the effective pom elsewhere, it is read from the path the plugin names.
        if effective_pom is None and (written := _EFFECTIVE_POM_WRITTEN.search(result.stdout)):
            effective_pom = read(Path(written["path"]))
        if result.succeeded and effective_pom is None:
            _LOG.effective_pom_unreadable(self.pom_xml)
        if effective_pom is not None and not has_input_locations(effective_pom):
            _LOG.effective_pom_without_input_locations(self.pom_xml)
        return result, effective_pom

    def _run_goals(self, options: tuple[str, ...], goals: tuple[str, ...]) -> Result:
        """Run Maven over the pom, and return what the run wrote and whether it succeeded."""
        # Every Maven run takes these options, whichever goals it runs.
        command = Command(self.maven, *("--batch-mode", "--no-transfer-progress"), *self._selection, *options, *goals)
        result = run(command, cwd=self.root)
        # Maven writes its [ERROR] lines to stdout and leaves stderr empty, so `run` surfaces nothing for a failed run.
        # A run the system refused to start wrote nothing at all, and `run` reported that itself.
        if not result.succeeded and result.stdout:
            _LOG.command_failed(command, result.stdout)
        return result

    @property
    def _selection(self) -> tuple[str, ...]:
        """Return the options that select the pom in the reactor, by its directory relative to the root."""
        return ("--projects", str(self.pom_xml.parent.relative_to(self.root, walk_up=True)))


@contextmanager
def _effective_pom_file() -> Generator[Path]:
    """Name a file for Maven to write the effective pom to, and remove the file once the run is over."""
    with tempfile.NamedTemporaryFile(suffix=".xml", delete_on_close=False) as file:
        file.close()
        yield Path(file.name)
