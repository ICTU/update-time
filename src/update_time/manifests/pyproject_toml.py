"""Read and edit what only a pyproject.toml holds: its `[tool]` tables, and whether it declares dependencies."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from update_time.formats import toml
from update_time.manifests.python_dependency_toml import PythonDependencyToml, uv_dependency_arrays

if TYPE_CHECKING:
    from collections.abc import Mapping

# The `[project]` fields that a project can list as `dynamic`, leaving the build backend to fill in the dependencies
# that uv then resolves.
_DYNAMIC_DEPENDENCY_FIELDS = ("dependencies", "optional-dependencies")


@dataclass(frozen=True)
class PyprojectToml(PythonDependencyToml):
    """A file that is TOML throughout, so it declares its dependencies in the file itself."""

    def toml(self, contents: str) -> str:
        """Return the contents, which are TOML."""
        return contents

    def with_toml(self, _contents: str, /, toml_text: str) -> str:
        """Return the TOML, which is the whole file."""
        return toml_text

    @staticmethod
    def declares_a_dependency(config: Mapping) -> bool:
        """Return whether the config declares a dependency uv resolves, in an array or through the build backend."""
        dynamic = config.get("project", {}).get("dynamic", [])
        return any(uv_dependency_arrays(config)) or any(field in dynamic for field in _DYNAMIC_DEPENDENCY_FIELDS)

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
