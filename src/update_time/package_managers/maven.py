"""The Maven command Update-time runs over a pom.xml: the versions plugin's goals, and the options they run under."""

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain.cooldown import COOLDOWN
from update_time.io.log import get_logger
from update_time.io.process import run
from update_time.manifests import pom_xml as pom_xml_format
from update_time.primitives.command import Command
from update_time.sources.maven_central import versions_within_cooldown

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from update_time.domain.dependency import DependencyName

_LOG = get_logger("pom.xml")

# The versions plugin release Update-time runs, read from the pom it ships beside this module. Update-time names
# this version in each goal below, so the scanned project cannot decide which plugin release runs. An older plugin
# release drops the options below without saying so.
_POM = Path(__file__).parent / "pom.xml"
_PLUGIN_VERSION_PROPERTY = "versions.plugin.version"


def _declared_plugin_version() -> str:
    """Return the versions plugin release the shipped pom declares."""
    declared = pom_xml_format.properties(_POM).get(_PLUGIN_VERSION_PROPERTY)
    if declared is None:
        message = f"{_POM} does not declare {_PLUGIN_VERSION_PROPERTY}, so there is no versions plugin release to run"
        raise RuntimeError(message)
    return declared


_PLUGIN = f"org.codehaus.mojo:versions-maven-plugin:{_declared_plugin_version()}"

# The versions no goal may adopt. Maven reads a release candidate as a release, so both goals adopt one otherwise.
# The pattern must match a whole version string.
_PRE_RELEASES = "(?i).*[-.](alpha|beta|rc|cr|m|pre|preview)[-.]?[0-9]*"

# `--update-snapshots` forces a fresh read of the version metadata Maven caches for a day. `generateBackupPoms`
# stops the plugin writing a backup pom beside the one it rewrites.
_OPTIONS = (
    "--batch-mode",
    "--no-transfer-progress",
    "--non-recursive",
    "--update-snapshots",
    "-DgenerateBackupPoms=false",
    f"-Dmaven.version.ignore={_PRE_RELEASES}",
)

# `use-latest-releases` advances the version in a dependency's `<version>` element, `update-properties` the property
# a `<version>` element names.
_GOALS = (f"{_PLUGIN}:use-latest-releases", f"{_PLUGIN}:update-properties")

# The rule set the plugin reads, which holds a rule per artefact. The plugin does not filter releases by age, so
# this rule set is how the cooldown reaches it. Both goals honour it, beside the pre-release pattern above rather
# than instead of it.
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


def update_pom_xml(pom_xml: Path) -> None:
    """Update the dependencies the pom declares, running Maven in the pom's own directory."""
    with _rule_set_option(pom_xml) as option:
        command = Command(_maven(pom_xml), *_OPTIONS, *option, *_GOALS)
        result = run(command, cwd=pom_xml.parent)
    # Maven writes its [ERROR] lines to stdout and leaves stderr empty, so `run` surfaces nothing for a failed run.
    # A run whose executable was missing wrote nothing at all, and `run` reported that itself.
    if not result.succeeded and result.stdout:
        _LOG.command_failed(command, result.stdout)


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
