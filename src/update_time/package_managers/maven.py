"""The Maven command Update-time runs over a pom.xml: the effective pom, the versions plugin's goals, and options."""

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain.cooldown import COOLDOWN
from update_time.formats import xml
from update_time.io.log import get_logger
from update_time.io.process import run
from update_time.manifests import pom_xml as pom_xml_format
from update_time.primitives.command import Command
from update_time.sources.maven_central import versions_within_cooldown

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from update_time.domain.dependency import DependencyName
    from update_time.formats.xml import XmlElement

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

# The versions no goal may adopt. The versions plugin reads every version but a snapshot as a release, so both of
# its goals adopt a pre-release otherwise. The pattern must match a whole version string. Maven reads an `a`, `b`, or
# `m` followed directly by a number as `alpha`, `beta`, or `milestone`:
# https://maven.apache.org/pom.html#version-order-specification.
_PRE_RELEASES = "(?i).*[-.](alpha|beta|milestone|rc|cr|m|pre|preview|[ab][0-9])[-.]?[0-9]*"

# `--update-snapshots` forces a fresh read of the version metadata Maven caches for a day. `generateBackupPoms`
# stops the versions plugin writing a backup pom beside the one it rewrites. `verbose` writes the input location
# of each element of the effective pom after it: the pom and the line declaring that element.
_OPTIONS = (
    "--batch-mode",
    "--no-transfer-progress",
    "--non-recursive",
    "--update-snapshots",
    "-DgenerateBackupPoms=false",
    f"-Dmaven.version.ignore={_PRE_RELEASES}",
    "-Dverbose",
)

# `effective-pom` writes the pom as Maven builds it, before the other goals rewrite it. `use-latest-releases`
# advances the version in a dependency's `<version>` element, `update-properties` the property a `<version>` element
# names.
_GOALS = (
    f"{_HELP_PLUGIN}:effective-pom",
    f"{_VERSIONS_PLUGIN}:use-latest-releases",
    f"{_VERSIONS_PLUGIN}:update-properties",
)

# The rule set the versions plugin reads, which holds a rule per artefact. The plugin does not filter releases by
# age, so this rule set is how the cooldown reaches it. Both of its goals honour it, beside the pre-release pattern
# above rather than instead of it.
_RULE_SET = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<ruleset xmlns="http://mojo.codehaus.org/versions-maven-plugin/rule/2.0.0">\n'
    "  <rules>\n{rules}  </rules>\n"
    "</ruleset>\n"
)


# The wrapper script a project ships beside its pom, which runs the Maven version that project builds with.
_WRAPPER = "mvnw"


def _maven(pom_xml: Path) -> str:
    """Return the Maven to run for the pom: the project's own wrapper, or the mvn on the path where it ships none."""
    return f"./{_WRAPPER}" if (pom_xml.parent / _WRAPPER).exists() else "mvn"


def update_pom_xml(pom_xml: Path) -> XmlElement | None:
    """Update the dependencies the pom declares, running Maven in the pom's own directory.

    Return the effective pom Maven wrote before it updated the pom, or None where it wrote none.
    """
    with _rule_set_option(pom_xml) as option, _effective_pom_file() as output:
        command = Command(_maven(pom_xml), *_OPTIONS, *option, f"-Doutput={output}", *_GOALS)
        result = run(command, cwd=pom_xml.parent)
        # Maven builds the model before any goal runs, so the effective pom holds even when a later goal fails.
        effective_pom = xml.read(output)
    # Maven writes its [ERROR] lines to stdout and leaves stderr empty, so `run` surfaces nothing for a failed run.
    # A run whose executable was missing wrote nothing at all, and `run` reported that itself.
    if not result.succeeded and result.stdout:
        _LOG.command_failed(command, result.stdout)
    if result.succeeded and effective_pom is None:
        _LOG.effective_pom_unreadable(pom_xml)
    return effective_pom


@contextmanager
def _rule_set_option(pom_xml: Path) -> Iterator[tuple[str, ...]]:
    """Yield the option naming the rule set for the pom, or nothing where the cooldown holds nothing back."""
    rules = _rules(pom_xml_format.artefacts(pom_xml))
    if not rules:
        yield ()
        return
    with _rule_set_file(_RULE_SET.format(rules=rules)) as path:
        yield (f"-Dmaven.version.rules={path.as_uri()}",)


def _rules(artefacts: Iterable[DependencyName]) -> str:
    """Return a rule per artefact that has versions inside the cooldown window, or nothing where none has.

    An artefact several declarations name gets one rule, so a pom declaring it twice names its versions once. The
    repository is not asked at all for a window that holds nothing back.
    """
    cooldown_days = COOLDOWN.get()
    if cooldown_days <= 0:
        return ""
    named = dict.fromkeys(artefacts)
    held_back = ((artefact, versions_within_cooldown(artefact, cooldown_days)) for artefact in named)
    return "".join(_rule(artefact, versions) for artefact, versions in held_back if versions)


def _rule(artefact: DependencyName, versions: tuple[str, ...]) -> str:
    """Return the rule for the artefact, naming the versions given.

    An `ignoreVersion` without a `type` attribute matches a version exactly, so a version is never read as a pattern.
    """
    group_id, artifact_id = pom_xml_format.coordinates(artefact)
    ignored = "".join(f"        <ignoreVersion>{version}</ignoreVersion>\n" for version in versions)
    return (
        f'    <rule groupId="{group_id}" artifactId="{artifact_id}">\n'
        f"      <ignoreVersions>\n{ignored}      </ignoreVersions>\n"
        "    </rule>\n"
    )


@contextmanager
def _rule_set_file(rule_set: str) -> Iterator[Path]:
    """Write the rule set to a file for the Maven run, and remove the file once the run is over."""
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete_on_close=False) as file:
        file.write(rule_set)
        file.close()
        yield Path(file.name)


@contextmanager
def _effective_pom_file() -> Iterator[Path]:
    """Name a file for Maven to write the effective pom to, and remove the file once the run is over."""
    with tempfile.NamedTemporaryFile(suffix=".xml", delete_on_close=False) as file:
        file.close()
        yield Path(file.name)
