"""The Maven plugin releases Update-time runs."""

from pathlib import Path

from update_time.manifests import pom_xml as pom_xml_manifest

# The pom Update-time ships beside this module, which declares the plugin releases. Each goal Update-time runs names
# its plugin's release, so the scanned project cannot decide which release runs. An older plugin release drops the
# options Update-time passes to its goals without saying so.
_POM = Path(__file__).parent / "pom.xml"


def _declared_plugin_version(version_property: str) -> str:
    """Return the plugin release the shipped pom declares in the property."""
    declared = pom_xml_manifest.properties(_POM).get(version_property)
    if declared is None:
        message = f"{_POM} does not declare {version_property}, so there is no plugin release to run"
        raise RuntimeError(message)
    return declared


HELP_PLUGIN = f"org.apache.maven.plugins:maven-help-plugin:{_declared_plugin_version('help.plugin.version')}"
VERSIONS_PLUGIN = f"org.codehaus.mojo:versions-maven-plugin:{_declared_plugin_version('versions.plugin.version')}"
