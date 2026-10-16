"""The Maven runs Update-time makes over a pom.xml: the effective pom, the versions plugin's goals, and options."""

import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import starmap
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain.bound import NO_BOUND
from update_time.formats import xml
from update_time.io.log import get_logger
from update_time.io.process import run
from update_time.manifests import pom_xml as pom_xml_format
from update_time.markers.cooldown import cooldown_days
from update_time.package_managers.maven_range import maven_range
from update_time.primitives.command import Command
from update_time.primitives.iterables import unique
from update_time.sources.maven_central import versions_held_back

if TYPE_CHECKING:
    from collections.abc import Generator

    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.formats.xml import XmlElement
    from update_time.manifests.pom_xml import Declaration
    from update_time.primitives.command import Result

_LOG = get_logger("pom.xml")

# The plugin releases Update-time runs, read from the pom it ships beside this module. Update-time names these
# versions in each goal below, so the scanned project cannot decide which plugin release runs. An older plugin
# release drops the options below without saying so.
_POM = Path(__file__).parent / "pom.xml"


def _declared_plugin_version(version_property: str) -> str:
    """Return the plugin release the shipped pom declares in the property."""
    declared = pom_xml_format.properties(_POM).get(version_property)
    if declared is None:
        message = f"{_POM} does not declare {version_property}, so there is no plugin release to run"
        raise RuntimeError(message)
    return declared


_HELP_PLUGIN = f"org.apache.maven.plugins:maven-help-plugin:{_declared_plugin_version('help.plugin.version')}"
_VERSIONS_PLUGIN = f"org.codehaus.mojo:versions-maven-plugin:{_declared_plugin_version('versions.plugin.version')}"

# The qualifier of a pre-release. Maven reads an `a`, `b`, or `m` followed directly by a number as `alpha`, `beta`,
# or `milestone`: https://maven.apache.org/pom.html#version-order-specification.
_PRE_RELEASE_QUALIFIER = "[-.](alpha|beta|milestone|rc|cr|m|pre|preview|[ab][0-9])[-.]?[0-9]*"

# The versions no goal may adopt. The versions plugin reads every version but a snapshot as a release, so both of
# its goals adopt a pre-release otherwise. The pattern must match a whole version string.
_PRE_RELEASES = f"(?i).*{_PRE_RELEASE_QUALIFIER}"

# The suffix Maven marks a snapshot with.
_SNAPSHOT = "-SNAPSHOT"

# The digits and dots a version starts with.
_NUMBER = r"[0-9]+(\.[0-9]+)*"

# What follows the number of a plain release: nothing, a qualifier Maven reads as the release itself, such as the
# `.Final` of `4.2.19.Final`, or the date of the build, such as the `.v20250814` of `9.4.58.v20250814`. The date can
# have the time after it, such as the `-1726` of `3.10.0.v20140604-1726`. A service pack, such as the `.SP2` of
# `5.1.1.SP2`, and a revision, such as the `-1` of the WebJar `3.3.7-1`, leave the release plain too.
_PLAIN_RELEASE_QUALIFIER = r"(?i:[-.](ga|final|release|sp[0-9]*|v[0-9]{8}(-[0-9]{4})?)|-[0-9]+)?"


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
    def selection(self) -> tuple[str, ...]:
        """Return the options that select the pom in the reactor, by its directory relative to the root."""
        return ("--projects", str(self.pom_xml.parent.relative_to(self.root, walk_up=True)))


def update_pom_xml(pom_xml: Path, reactor_root: Path) -> XmlElement | None:
    """Update the dependencies and plugins the pom declares, running Maven in the root directory of its reactor.

    Return the effective pom Maven wrote before it updated the pom, or None where it wrote none. A pom leaving a group
    or an artifact to its parent gets the effective pom in a Maven run of its own, so the rule set names the
    coordinates Maven resolves.
    """
    module = _ReactorModule(pom_xml, reactor_root)
    if not pom_xml_format.leaves_coordinates_unresolved(pom_xml):
        with _versions_options(pom_xml_format.versioned_declarations(pom_xml)) as options:
            _, effective_pom = _run_writing_effective_pom(module, options, _VERSIONS_GOALS)
            return effective_pom
    succeeded, effective_pom = _run_writing_effective_pom(module)
    if succeeded:  # A single run failing at the same point would not reach the updates either.
        with _versions_options(pom_xml_format.versioned_declarations(pom_xml, effective_pom)) as options:
            _run(module, options, _VERSIONS_GOALS)
    return effective_pom


def _run_writing_effective_pom(
    module: _ReactorModule, options: tuple[str, ...] = (), goals: tuple[str, ...] = ()
) -> tuple[bool, XmlElement | None]:
    """Run Maven over the pom, the effective pom's goal first, and return whether it succeeded and the effective pom."""
    with _effective_pom_file() as output:
        effective_pom_options = (_EFFECTIVE_POM_OPTION, f"-Doutput={output}")
        result = _run(module, (*options, *effective_pom_options), (_EFFECTIVE_POM_GOAL, *goals))
        # Maven builds the model before any goal runs, so the effective pom holds even when a later goal fails.
        effective_pom = xml.read(output)
    if effective_pom is None:
        effective_pom = _effective_pom_written_elsewhere(result.stdout)
    if result.succeeded and effective_pom is None:
        _LOG.effective_pom_unreadable(module.pom_xml)
    if effective_pom is not None and not pom_xml_format.has_input_locations(effective_pom):
        _LOG.effective_pom_without_input_locations(module.pom_xml)
    return result.succeeded, effective_pom


def _effective_pom_written_elsewhere(stdout: str) -> XmlElement | None:
    """Return the effective pom at the path the help plugin reports writing it to, or None where it reports none."""
    written = _EFFECTIVE_POM_WRITTEN.search(stdout)
    return None if written is None else xml.read(Path(written["path"]))


def _run(module: _ReactorModule, options: tuple[str, ...], goals: tuple[str, ...]) -> Result:
    """Run Maven over the pom, and return what the run wrote and whether it succeeded."""
    command = Command(_maven(module.root), *_OPTIONS, *module.selection, *options, *goals)
    result = run(command, cwd=module.root)
    # Maven writes its [ERROR] lines to stdout and leaves stderr empty, so `run` surfaces nothing for a failed run.
    # A run whose executable was missing wrote nothing at all, and `run` reported that itself.
    if not result.succeeded and result.stdout:
        _LOG.command_failed(command, result.stdout)
    return result


@contextmanager
def _versions_options(declarations: list[Declaration]) -> Generator[tuple[str, ...]]:
    """Yield the options the versions plugin's goals take, naming a rule set where anything is held back."""
    rules = _rules(declarations)
    if not rules:
        yield _VERSIONS_OPTIONS
        return
    with _rule_set_file(_RULE_SET.format(rules=rules)) as path:
        yield (*_VERSIONS_OPTIONS, f"-Dmaven.version.rules={path.as_uri()}")


def _rules(declarations: list[Declaration]) -> str:
    """Return a rule per artefact whose versions anything holds back, or nothing where nothing does.

    An artefact several declarations name gets one rule. The rule holds back what each of their markers holds back, and
    keeps the artefact on any of their variants.
    """
    declarations_by_artefact: dict[DependencyName, list[Declaration]] = {}
    for declaration in declarations:
        declarations_by_artefact.setdefault(declaration.dependency, []).append(declaration)
    return "".join(starmap(_artefact_rule, declarations_by_artefact.items()))


# The `ignoreVersion` matching every version, which holds an artefact's update back whatever the repository offers.
_EVERY_VERSION = '<ignoreVersion type="regex">.*</ignoreVersion>'


def _artefact_rule(artefact: DependencyName, declarations: list[Declaration]) -> str:
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


def _variant_versions(declarations: list[Declaration]) -> tuple[str, ...]:
    """Return an `ignoreVersion` matching every version on a variant none of the declarations is on, if any."""
    other_variants = _other_variants(*(declaration.current_version for declaration in declarations))
    return (f'<ignoreVersion type="regex">{other_variants}</ignoreVersion>',) if other_variants else ()


def range_held_back(declaration: Declaration) -> str | None:
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
    return tuple(f"<ignoreVersion>{version}</ignoreVersion>" for version in versions_held_back(artefact, days))


def _rule(artefact: DependencyName, *ignore_versions: str) -> str:
    """Return the rule for the artefact, holding the given `ignoreVersion` elements."""
    group_id, artifact_id = pom_xml_format.coordinates(artefact)
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
