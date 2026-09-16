"""The Maven command Update-time runs over a pom.xml: the versions plugin's goals, and the options they run under."""

from pathlib import Path

from update_time.io.log import get_logger
from update_time.io.process import run
from update_time.manifests import pom_xml as pom_xml_format
from update_time.primitives.command import Command

_LOG = get_logger("pom.xml")

# The versions plugin release Update-time runs, read from the pom it ships beside this module. Each goal below
# names this version, so the scanned project cannot decide which plugin release runs. An older plugin release
# drops the options below without saying so.
_POM = Path(__file__).parent / "pom.xml"
_PLUGIN_VERSION_PROPERTY = "versions.plugin.version"


def _declared_plugin_version() -> str:
    """Return the versions plugin release the shipped pom declares."""
    declared = pom_xml_format.properties(_POM).get(_PLUGIN_VERSION_PROPERTY)
    if declared is None:
        message = f"{_POM} declares no {_PLUGIN_VERSION_PROPERTY}, so there is no versions plugin release to run"
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


# The wrapper script a project ships beside its pom, which runs the Maven version that project builds with.
_WRAPPER = "mvnw"


def _maven(pom_xml: Path) -> str:
    """Return the Maven to run for the pom: the project's own wrapper, or the mvn on the path where it ships none."""
    return f"./{_WRAPPER}" if (pom_xml.parent / _WRAPPER).exists() else "mvn"


def update_pom_xml(pom_xml: Path) -> None:
    """Update the dependencies the pom declares, running Maven in the pom's own directory."""
    command = Command(_maven(pom_xml), *_OPTIONS, *_GOALS)
    result = run(command, cwd=pom_xml.parent)
    # Maven writes its [ERROR] lines to stdout and leaves stderr empty, so `run` surfaces nothing for a failed run.
    # A run whose executable was missing wrote nothing at all, and `run` reported that itself.
    if not result.succeeded and result.stdout:
        _LOG.command_failed(command, result.stdout)
