"""Where in a file its dependency TOML sits: the whole file, or a PEP 723 block."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from update_time.file_formats import inline_script_metadata

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class DependencyTomlFile:
    """A file that declares its dependencies in TOML.

    The contents are read per operation rather than held, so a file the run has rewritten reads back as it now is.
    """

    path: Path

    def read(self) -> str:
        """Return the file's contents."""
        return self.path.read_text()

    def write(self, contents: str) -> None:
        """Write the contents to the file."""
        self.path.write_text(contents)

    def toml(self, contents: str) -> str:
        """Return the TOML the contents declare the dependencies in."""
        raise NotImplementedError

    def with_toml(self, contents: str, /, toml_text: str) -> str:
        """Return the contents with the TOML declaring the dependencies replaced."""
        raise NotImplementedError


@dataclass(frozen=True)
class PyprojectToml(DependencyTomlFile):
    """A file that is TOML throughout, so it declares its dependencies in the file itself."""

    def toml(self, contents: str) -> str:
        """Return the contents, which are TOML."""
        return contents

    def with_toml(self, _contents: str, /, toml_text: str) -> str:
        """Return the TOML, which is the whole file."""
        return toml_text


@dataclass(frozen=True)
class InlineScript(DependencyTomlFile):
    """A Python script that declares its dependencies in a PEP 723 `# /// script` block."""

    def toml(self, contents: str) -> str:
        """Return the TOML the script's block comments out, or the contents when the block is never closed."""
        block = inline_script_metadata.toml_block(contents)
        return contents if block is None else block

    def with_toml(self, contents: str, /, toml_text: str) -> str:
        """Return the script with the TOML of its block replaced, or the TOML when the block is never closed."""
        if inline_script_metadata.toml_block(contents) is None:
            return toml_text
        return inline_script_metadata.replace_toml_block(contents, toml_text)
