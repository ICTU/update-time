"""Run Maven over the reactors the scanned poms form, with its goals, options, and rule sets, and report what moved."""

import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import starmap
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain.bound import NO_BOUND
from update_time.domain.dependency import DependencyVersion
from update_time.domain.reference import Reference
from update_time.formats import xml
from update_time.io.log import get_logger
from update_time.io.process import run
from update_time.manifests import pom_xml as pom_xml_manifest
from update_time.markers.cooldown import cooldown_days
from update_time.markers.directive import Reason
from update_time.package_managers.maven_range import maven_range
from update_time.primitives.command import Command
from update_time.primitives.iterables import unique
from update_time.sources import maven_central

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.formats.xml import XmlElement
    from update_time.primitives.command import Result

_LOG = get_logger("pom.xml")

# The plugin releases Update-time runs, read from the pom it ships beside this module. Update-time names these
# versions in each goal below, so the scanned project cannot decide which plugin release runs. An older plugin
# release drops the options below without saying so.
_POM = Path(__file__).parent / "pom.xml"


def _declared_plugin_version(version_property: str) -> str:
    """Return the plugin release the shipped pom declares in the property."""
    declared = pom_xml_manifest.properties(_POM).get(version_property)
    if declared is None:
        message = f"{_POM} does not declare {version_property}, so there is no plugin release to run"
        raise RuntimeError(message)
    return declared


_HELP_PLUGIN = f"org.apache.maven.plugins:maven-help-plugin:{_declared_plugin_version('help.plugin.version')}"
_VERSIONS_PLUGIN = f"org.codehaus.mojo:versions-maven-plugin:{_declared_plugin_version('versions.plugin.version')}"

# Numbers separated by dots, such as the `2.4.0` a version starts with. The versions plugin reads the patterns built
# from it as Java regexes. Python's `\d` matches any Unicode digit and Java's matches 0 to 9 alone, so `[0-9]` keeps
# both reading these patterns the same, where `update_time.domain.dependency.VERSION_NUMBERS` would not.
_NUMBER = r"[0-9]+(\.[0-9]+)*"

# The qualifier of a pre-release. Maven reads an `a`, `b`, or `m` followed directly by a number as `alpha`, `beta`,
# or `milestone`: https://maven.apache.org/pom.html#version-order-specification. The number after a qualifier can have
# dots in it, such as the `180830.0359` of `2.4.0-b180830.0359`.
_PRE_RELEASE_QUALIFIER = rf"[-.](alpha|beta|milestone|rc|cr|m|pre|preview|[ab][0-9])[-.]?({_NUMBER})?"

# The versions no goal may adopt. The versions plugin reads every version but a snapshot as a release, so both of
# its goals adopt a pre-release otherwise. The pattern must match a whole version string.
_PRE_RELEASES = f"(?i).*{_PRE_RELEASE_QUALIFIER}"

# The suffix Maven marks a snapshot with.
_SNAPSHOT = "-SNAPSHOT"

# The date of a build, such as the `v20250814` of `9.4.58.v20250814`. The date can have the time after it, such as the
# `-1726` of `3.10.0.v20140604-1726`.
_BUILD_DATE = r"v[0-9]{8}(-[0-9]{4})?"

# What follows the number of a plain release: nothing, a qualifier Maven reads as the release itself, such as the
# `.Final` of `4.2.19.Final`, or the date of the build. A service pack, such as the `.SP2` of `5.1.1.SP2`, and a
# revision, such as the `-1` of the WebJar `3.3.7-1`, leave the release plain too.
_PLAIN_RELEASE_QUALIFIER = rf"(?i:[-.](ga|final|release|sp[0-9]*|{_BUILD_DATE})|-[0-9]+)?"


def _other_variants(*versions: VersionString) -> str:
    """Return the pattern matching every version on a variant none of the versions is on.

    The pattern is empty where any of the versions does not start with a number. Such a version does not name a variant.
    """
    if not all(re.match(_NUMBER, version) for version in versions):
        return ""
    same_variants = "|".join(unique(map(_variant_pattern, versions)))
    return f"(?!{_NUMBER}({same_variants})$).*"


def _variant_pattern(version: VersionString) -> str:
    """Return the pattern matching the variant of the version, such as the `-jdk5` of `1.18.14-jdk5`."""
    # A snapshot and a pre-release are on the variant of their release. A qualifier running on into a letter is the
    # start of a variant, such as the `-m` of `-mysql`.
    release = version.removesuffix(_SNAPSHOT)
    variant = re.sub(rf"(?i)^{_NUMBER}({_PRE_RELEASE_QUALIFIER}(?![a-z]))?", "", release)
    # `re.escape` leaves letters, digits, and underscores alone, and Java reads a backslash before any other character
    # as that character itself, so the versions plugin reads the escaped variant literally.
    return _PLAIN_RELEASE_QUALIFIER if re.fullmatch(_PLAIN_RELEASE_QUALIFIER, variant) else re.escape(variant)


# Every Maven run takes these options, whichever goals it runs.
_OPTIONS = ("--batch-mode", "--no-transfer-progress")

# The versions plugin's goals take these options. `generateBackupPoms` stops the versions plugin writing a backup pom
# beside the one it rewrites.
_VERSIONS_OPTIONS = ("-DgenerateBackupPoms=false", f"-Dmaven.version.ignore={_PRE_RELEASES}")

# `verbose` writes the input location of each element of the effective pom after it: the pom and the line declaring
# that element.
_EFFECTIVE_POM_OPTION = "-Dverbose"

# `effective-pom` writes the pom as Maven builds it, before any other goal of the same run rewrites it.
_EFFECTIVE_POM_GOAL = f"{_HELP_PLUGIN}:effective-pom"

# The help plugin reports in this line where it wrote the effective pom. A project that configures the plugin's
# `<output>` overrides `-Doutput`, so the effective pom lands where that project says.
_EFFECTIVE_POM_WRITTEN = re.compile(r"Effective-POM written to: (?P<path>.+)")

# `use-latest-releases` advances the version in a dependency's `<version>` element, `update-properties` the property a
# `<version>` element names.
_VERSIONS_GOALS = (
    f"{_VERSIONS_PLUGIN}:use-latest-releases",
    f"{_VERSIONS_PLUGIN}:update-properties",
)

# The rule set the versions plugin reads, which holds a rule per artefact. The plugin filters releases neither by age
# nor by variant, so the rule set names the versions it may not adopt. Both of its goals honour it, beside the
# pre-release pattern above rather than instead of it.
_RULE_SET = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<ruleset xmlns="http://mojo.codehaus.org/versions-maven-plugin/rule/2.0.0">\n'
    "  <rules>\n{rules}  </rules>\n"
    "</ruleset>\n"
)


# A project ships this wrapper script beside its root pom. The script runs the Maven version that project builds with.
_WRAPPER = "mvnw"


def _maven(directory: Path) -> str:
    """Return the Maven to run in the directory: the project's wrapper, or the mvn on the path where it ships none."""
    return f"./{_WRAPPER}" if (directory / _WRAPPER).exists() else "mvn"


@dataclass(frozen=True)
class _ReactorModule:
    """A pom, and the root directory of the reactor Maven builds it in."""

    pom_xml: Path
    root: Path

    @property
    def _selection(self) -> tuple[str, ...]:
        """Return the options that select the pom in the reactor, by its directory relative to the root."""
        return ("--projects", str(self.pom_xml.parent.relative_to(self.root, walk_up=True)))

    def _run_goals(self, options: tuple[str, ...], goals: tuple[str, ...]) -> Result:
        """Run Maven over the pom, and return what the run wrote and whether it succeeded."""
        command = Command(_maven(self.root), *_OPTIONS, *self._selection, *options, *goals)
        result = run(command, cwd=self.root)
        # Maven writes its [ERROR] lines to stdout and leaves stderr empty, so `run` surfaces nothing for a failed run.
        # A run whose executable was missing wrote nothing at all, and `run` reported that itself.
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
            with _versions_options(pom_xml_manifest.versioned_declarations(self.pom_xml)) as options:
                return self._run_goals_writing_effective_pom(options, _VERSIONS_GOALS)
        result, effective_pom = self._run_goals_writing_effective_pom()
        if result.succeeded:  # A single run failing at the same point would not reach the updates either.
            with _versions_options(pom_xml_manifest.versioned_declarations(self.pom_xml, effective_pom)) as options:
                self._run_goals(options, _VERSIONS_GOALS)
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
        that broke the reactor is the one changed by the latest run that read the reactor.
        """
        if self._readable is None:
            self._readable = self._every_pom_parses()
        if not self._readable:
            _LOG.maven_not_run(pom_xml)
            return None
        before = _lines(pom_xml)
        result, effective_pom = _ReactorModule(pom_xml, self._root).update()
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
def _versions_options(declarations: list[pom_xml_manifest.Declaration]) -> Generator[tuple[str, ...]]:
    """Yield the options the versions plugin's goals take, naming a rule set where anything is held back.

    `update-properties` advances a property wherever in the reactor it is declared or named. So a run over a module
    advances its parent's properties, and a run over the parent advances the properties only its modules name. The
    options limit the goal to the properties the declarations name, which the rule set covers. With an empty list, the
    goal does not advance any property.
    """
    properties = unique(filter(None, (declaration.version_property for declaration in declarations)))
    options = (*_VERSIONS_OPTIONS, f"-DincludeProperties={','.join(properties)}")
    rules = _rules(declarations)
    if not rules:
        yield options
        return
    with _rule_set_file(_RULE_SET.format(rules=rules)) as path:
        yield (*options, f"-Dmaven.version.rules={path.as_uri()}")


def _rules(declarations: list[pom_xml_manifest.Declaration]) -> str:
    """Return a rule per artefact whose versions anything holds back, or nothing where nothing does.

    An artefact several declarations name gets one rule. The rule holds back what each of their markers holds back, and
    keeps the artefact on any of their variants.
    """
    declarations_by_artefact: dict[DependencyName, list[pom_xml_manifest.Declaration]] = {}
    for declaration in declarations:
        declarations_by_artefact.setdefault(declaration.dependency, []).append(declaration)
    return "".join(starmap(_artefact_rule, declarations_by_artefact.items()))


# The `ignoreVersion` matching every version, which holds an artefact's update back whatever the repository offers.
_EVERY_VERSION = '<ignoreVersion type="regex">.*</ignoreVersion>'


def _artefact_rule(artefact: DependencyName, declarations: list[pom_xml_manifest.Declaration]) -> str:
    """Return the rule holding back what the markers, the variants, and the cooldown do, or nothing where none does.

    A marker that holds every version back does not have a range, and leaves the repository unasked.
    """
    held_back_ranges = [held_back for declaration in declarations if (held_back := range_held_back(declaration)) != ""]
    if None in held_back_ranges:
        return _rule(artefact, _EVERY_VERSION)
    range_elements = (f'<ignoreVersion type="range">{held_back}</ignoreVersion>' for held_back in held_back_ranges)
    days = max(cooldown_days(declaration.marker) for declaration in declarations)
    ignored = unique((*range_elements, *_variant_versions(declarations), *_cooldown_versions(artefact, days)))
    return _rule(artefact, *ignored) if ignored else ""


def _variant_versions(declarations: list[pom_xml_manifest.Declaration]) -> tuple[str, ...]:
    """Return an `ignoreVersion` matching every version on a variant none of the declarations is on, if any."""
    other_variants = _other_variants(*(declaration.current_version for declaration in declarations))
    return (f'<ignoreVersion type="regex">{other_variants}</ignoreVersion>',) if other_variants else ()


def range_held_back(declaration: pom_xml_manifest.Declaration) -> str | None:
    """Return the Maven range of the versions the declaration's marker holds back, or None where it holds every one.

    The range is empty where the marker does not hold any version back. Where the rule set cannot express a bound,
    the marker holds every version back, so Maven never crosses the bound.
    """
    if declaration.holds_every_version_back:
        return None
    bound = declaration.marker.version_bound
    return "" if bound == NO_BOUND else maven_range(bound, declaration.current_version)


def _cooldown_versions(artefact: DependencyName, days: int) -> tuple[str, ...]:
    """Return an `ignoreVersion` for each of the artefact's versions published inside the cooldown.

    An `ignoreVersion` without a `type` attribute matches a version exactly, so a version is never read as a pattern.
    The repository is not asked at all for a cooldown that holds nothing back.
    """
    if days <= 0:
        return ()
    return tuple(
        f"<ignoreVersion>{version}</ignoreVersion>" for version in maven_central.versions_held_back(artefact, days)
    )


def _rule(artefact: DependencyName, *ignore_versions: str) -> str:
    """Return the rule for the artefact, holding the given `ignoreVersion` elements."""
    group_id, artifact_id = pom_xml_manifest.coordinates(artefact)
    ignored = "".join(f"        {ignore_version}\n" for ignore_version in ignore_versions)
    return (
        f'    <rule groupId="{group_id}" artifactId="{artifact_id}">\n'
        f"      <ignoreVersions>\n{ignored}      </ignoreVersions>\n"
        "    </rule>\n"
    )


@contextmanager
def _rule_set_file(rule_set: str) -> Generator[Path]:
    """Write the rule set to a file for the Maven run, and remove the file once the run is over."""
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete_on_close=False) as file:
        file.write(rule_set)
        file.close()
        yield Path(file.name)


@contextmanager
def _effective_pom_file() -> Generator[Path]:
    """Name a file for Maven to write the effective pom to, and remove the file once the run is over."""
    with tempfile.NamedTemporaryFile(suffix=".xml", delete_on_close=False) as file:
        file.close()
        yield Path(file.name)


def reason_maven_leaves_the_version(declaration: pom_xml_manifest.Declaration) -> Reason | None:
    """Return why Maven does not update the declaration's version, or None where it does."""
    if declaration.maven_updates_the_version:
        return None
    return Reason.PLUGIN_NOT_UPDATED if declaration.literal_plugin_version else Reason.VERSION_HELD_ELSEWHERE


@dataclass(frozen=True)
class _Update:
    """A declaration Maven updated, as the pom held it before and after the update and as the effective pom lists it."""

    old: pom_xml_manifest.Declaration
    new: pom_xml_manifest.Declaration
    effective: pom_xml_manifest.Declaration

    @property
    def reference(self) -> Reference:
        """Return the reference at the version it had, named as the effective pom names it."""
        return Reference(self.effective.dependency, self.old.current_version, self.new.location)

    @property
    def is_resolvable(self) -> bool:
        """Return whether the effective pom names the artefact whole, so a repository can be asked about it."""
        return pom_xml_manifest.is_resolved(self.effective.dependency)


def report_new_versions(
    before: list[pom_xml_manifest.Declaration],
    after: list[pom_xml_manifest.Declaration],
    resolved: list[pom_xml_manifest.Declaration],
) -> None:
    """Report each dependency and plugin whose version differs between the two readings, with its changes and date.

    The name is the one the effective pom gives the declaration, where it resolves one. Two declarations of one
    artefact naming one property read the same in each reading, so they are reported once. The new version of an
    artefact Maven Central does not serve goes unreported, where a served artefact names its property too.
    """
    updates = [
        _Update(old, new, effective)
        for old, new, effective in unique(zip(before, after, resolved, strict=True))
        if old.current_version != new.current_version
    ]
    unreportable = _unreportable_updates(updates)
    for update in updates:
        reference, new = update.reference, update.new
        if update in unreportable:
            _LOG.new_version_not_reported(reference, new.version_property, new.current_version)
            continue
        _LOG.new_version(reference, _new_version(update))


def _new_version(update: _Update) -> DependencyVersion:
    """Return the version Maven moved to, with its changes and publication date where the coordinates resolve."""
    artefact, version = update.effective.dependency, update.new.current_version
    if not pom_xml_manifest.fully_resolved(update.effective.pinned):
        return DependencyVersion(version)
    changes = maven_central.get_changes(artefact, version)
    return DependencyVersion(version, changes, publication=maven_central.publication(artefact, version))


def _unreportable_updates(updates: list[_Update]) -> list[_Update]:
    """Return the updates of an artefact Maven Central does not serve, whose property a served artefact names too.

    Maven moves a property to a version that every artefact naming it has, so the move says nothing about an artefact
    whose versions Maven does not find.
    """
    candidates = [update for update in updates if update.new.version_property and update.is_resolvable]
    artefacts = {update.effective.dependency for update in candidates}
    off_central = {artefact for artefact in artefacts if not maven_central.serves(artefact)}
    properties_on_central = {
        update.new.version_property for update in candidates if update.effective.dependency not in off_central
    }
    return [
        update
        for update in candidates
        if update.effective.dependency in off_central and update.new.version_property in properties_on_central
    ]
