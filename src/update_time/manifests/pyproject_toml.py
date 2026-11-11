"""What only a pyproject.toml holds: `[tool]` tables and the `requires-python`."""

from dataclasses import dataclass

from update_time.domain.reference import Reference
from update_time.formats import toml
from update_time.manifests import pyproject_config
from update_time.manifests.python_dependency_toml import PythonDependencyToml
from update_time.manifests.python_version import PYTHON
from update_time.primitives.location import Location

# What a pyproject.toml is called in a message about one that is not well formed.
FORMAT = "pyproject.toml"


@dataclass(frozen=True)
class PyprojectToml(PythonDependencyToml):
    """A file that is TOML throughout, so it declares its dependencies in the file itself."""

    def toml(self, contents: str) -> str:
        """Return the contents, which are TOML."""
        return contents

    def with_toml(self, _contents: str, /, toml_text: str) -> str:
        """Return the TOML, which is the whole file."""
        return toml_text

    def tool_key(self, table: str, key: str) -> tuple[str, str] | None:
        """Return the (value, trailing-comment) of `[tool.<table>] <key>`, or None when it isn't set.

        The trailing comment is returned verbatim (with its leading `#`, or empty when there is none), so a caller can
        recognise a line it wrote itself.
        """
        return toml.nested_value(toml.read_document(self.path), "tool", table, key)

    def set_tool_key(self, table: str, key: str, value: str, *, comment: str = "") -> bool:
        """Set `[tool.<table>] <key> = value` (with an optional trailing comment), write the file back, and return True.

        Return False, leaving the file as it is, when its `tool` or `tool.<table>` is not a table.
        """
        document = toml.read_document(self.path)
        if not toml.set_nested_value(document, "tool", table, key, value=value, comment=comment):
            return False
        self.write(toml.dumps(document))
        return True

    def requires_python(self) -> Reference | None:
        """Return the `requires-python` the file declares, as a reference to Python, or None if it declares none."""
        text = self.read()
        if (config := pyproject_config.parse(text)) is None or config.requires_python is None:
            return None
        line = toml.line_of(text, "project", "requires-python")
        return Reference(PYTHON, config.requires_python, Location(self.path, line))
