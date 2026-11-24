"""What only a pyproject.toml holds: `[tool]` tables and the `requires-python`."""

from dataclasses import dataclass
from typing import Final, override

from update_time.domain.reference import Reference
from update_time.formats.toml import dumps, line_of, nested_value, read_document, set_nested_value
from update_time.primitives.location import Location

from .pyproject_config import parse
from .python_dependency_toml import PythonDependencyToml
from .python_version import PYTHON

# What a pyproject.toml is called in a message about one that is not well formed.
FORMAT: Final[str] = "pyproject.toml"


@dataclass(frozen=True)
class PyprojectToml(PythonDependencyToml):
    """A file that is TOML throughout, so it declares its dependencies in the file itself."""

    @override
    def toml(self, contents: str) -> str:
        """Return the contents, which are TOML."""
        return contents

    @override
    def with_toml(self, _contents: str, /, toml_text: str) -> str:
        """Return the TOML, which is the whole file."""
        return toml_text

    def tool_key(self, table: str, key: str) -> tuple[str, str] | None:
        """Return the (value, trailing-comment) of `[tool.<table>] <key>`, or None when it isn't set.

        The trailing comment is returned verbatim (with its leading `#`, or empty when there is none), so a caller can
        recognise a line it wrote itself.
        """
        return nested_value(read_document(self.path), "tool", table, key)

    def set_tool_key(self, table: str, key: str, value: str, *, comment: str = "") -> bool:
        """Set `[tool.<table>] <key> = value` (with an optional trailing comment), write the file back, and return True.

        Return False, leaving the file as it is, when its `tool` or `tool.<table>` is not a table.
        """
        document = read_document(self.path)
        if not set_nested_value(document, "tool", table, key, value=value, comment=comment):
            return False
        self.write(dumps(document))
        return True

    def requires_python(self) -> Reference | None:
        """Return the `requires-python` the file declares, as a reference to Python, or None if it declares none."""
        text = self.read()
        if (config := parse(text)) is None or config.requires_python is None:
            return None
        line = line_of(text, "project", "requires-python")
        return Reference(PYTHON, config.requires_python, Location(self.path, line))
