"""Read the dependencies a Python script declares in a PEP 723 inline script metadata block."""

from dataclasses import dataclass

from update_time.formats import inline_script_metadata
from update_time.manifests.python_dependency_toml import PythonDependencyToml


@dataclass(frozen=True)
class InlineScript(PythonDependencyToml):
    """A Python script that declares its dependencies in a PEP 723 `# /// script` block."""

    def has_metadata_block(self) -> bool:
        """Return whether the script carries a `# /// script` block, closed or not."""
        return inline_script_metadata.has_block(self.read())

    def toml(self, contents: str) -> str:
        """Return the TOML the script's block comments out, or the contents when the block is never closed."""
        block = inline_script_metadata.toml_block(contents)
        return contents if block is None else block

    def with_toml(self, contents: str, /, toml_text: str) -> str:
        """Return the script with the TOML of its block replaced, or the TOML when the block is never closed."""
        if inline_script_metadata.toml_block(contents) is None:
            return toml_text
        return inline_script_metadata.replace_toml_block(contents, toml_text)
