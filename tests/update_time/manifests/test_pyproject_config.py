"""Unit tests for the config of a pyproject.toml or of inline script metadata."""

import unittest

from mutation_time.mutation import kills
from update_time.manifests.pyproject_config import PyprojectConfig, _PathSource, _UvTable, parse


class UvTableTest(unittest.TestCase):
    """Unit tests for the `[tool.uv]` table."""

    @kills.delete(
        _UvTable.from_mapping,
        "isinstance(source, dict) and ",
        "a source that is a string stops the run",
        raises="AttributeError: 'str' object has no attribute 'get'",
    )
    @kills.replace(
        _UvTable.from_mapping,
        'isinstance(source.get("path"), str)',
        '"path" in source',
        "a path that is a number is joined to the file's directory, which stops the run",
    )
    def test_a_source_or_a_path_of_another_type_is_passed_over(self):
        """Test that a source path comes from a table that names its path in a string, and the rest are passed over."""
        sources = 'string = "../string"\nnumber = {path = 5}\nlisted = [{path = "../listed"}, "../string"]\n'
        config = parse(f"[tool.uv.sources]\n{sources}") or PyprojectConfig()
        self.assertEqual(config.uv.path_sources, (_PathSource(dependency="listed", path="../listed"),))
