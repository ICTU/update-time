"""Unit tests for the pyproject.toml file format, with file I/O mocked."""

import unittest

from update_time.formats import toml
from update_time.manifests.pyproject_toml import PyprojectToml

from tests.helpers import mock_path
from tests.mutation import Mutation, kills

# The `[tool.uv]` table `SetToolKeyTest` writes, and a table for a file to declare after its tool tables.
_UV_TABLE = '[tool.uv]\nexclude-newer = "7 days"\n'
_NEXT_TABLE = '[dependency-groups]\ndev = ["a"]\n'


class ToolKeyTest(unittest.TestCase):
    """Unit tests for reading a `[tool.<table>]` key and its trailing comment."""

    def test_value_with_comment(self):
        """Test that both the value and its trailing comment are returned."""
        contents = '[tool.uv]\nexclude-newer = "7 days" # a note\n'
        self.assertEqual(PyprojectToml(mock_path(contents)).tool_key("uv", "exclude-newer"), ("7 days", "# a note"))

    def test_value_without_comment(self):
        """Test that a key without a trailing comment returns an empty comment string."""
        self.assertEqual(
            PyprojectToml(mock_path(_UV_TABLE)).tool_key("uv", "exclude-newer"),
            ("7 days", ""),
        )

    def test_absent_table(self):
        """Test that a missing `[tool.<table>]` table yields None."""
        self.assertIsNone(PyprojectToml(mock_path("[tool.other]\nx = 1\n")).tool_key("uv", "exclude-newer"))

    def test_absent_key(self):
        """Test that a missing key in an existing table yields None."""
        self.assertIsNone(PyprojectToml(mock_path("[tool.uv]\nmanaged = true\n")).tool_key("uv", "exclude-newer"))


class SetToolKeyTest(unittest.TestCase):
    """Unit tests for setting a `[tool.<table>]` key while preserving the rest of the file."""

    def written(self, contents: str, *, comment: str = "") -> str:
        """Set `[tool.uv] exclude-newer = "7 days"` on the contents and return what was written back."""
        pyproject_file = mock_path(contents)
        self.assertTrue(PyprojectToml(pyproject_file).set_tool_key("uv", "exclude-newer", "7 days", comment=comment))
        return pyproject_file.write_text.call_args.args[0]

    def test_creates_tool_and_table_when_absent(self):
        """Test that a `[tool.uv]` added to a file that does not have a `[tool]` section ends the file on its key."""
        self.assertEqual(self.written('[project]\nname = "x"\n'), f'[project]\nname = "x"\n\n{_UV_TABLE}')

    def test_creates_table_in_existing_tool(self):
        """Test that a `[tool.uv]` table is added alongside an existing, different tool table."""
        written = self.written("[tool.other]\nx = 1\n")
        self.assertIn("x = 1", written)
        self.assertIn('exclude-newer = "7 days"', written)

    def test_preserves_other_keys_in_the_table(self):
        """Test that other keys in an existing `[tool.uv]`, and the blank line after it, are preserved."""
        other_key = "exclude-newer-package = { msgpack = false }\n"
        written = self.written(f"[tool.uv]\n{other_key}\n{_NEXT_TABLE}")
        self.assertEqual(written, f'[tool.uv]\n{other_key}exclude-newer = "7 days"\n\n{_NEXT_TABLE}')

    @kills(
        Mutation(
            toml._add_table,
            "table.add(tomlkit.nl())",
            "pass",
            "an inserted table runs into a next table the file wrote without a blank line",
        )
    )
    def test_separates_an_inserted_table_from_a_next_table_written_without_blank_line(self):
        """Test that a blank line separates the inserted `[tool.uv]` from a next table the file wrote without one."""
        written = self.written(f"[tool.ruff]\nx = 1\n{_NEXT_TABLE}")
        self.assertEqual(written, f"[tool.ruff]\nx = 1\n\n{_UV_TABLE}\n{_NEXT_TABLE}")

    def test_keeps_one_blank_line_between_an_inserted_table_and_a_next_table_after_dotted_tool_keys(self):
        """Test that the blank line after dotted tool keys stays the one blank line before the next table."""
        written = self.written(f"tool.ruff.x = 1\n\n{_NEXT_TABLE}")
        self.assertEqual(written, f"tool.ruff.x = 1\n\n{_UV_TABLE}\n{_NEXT_TABLE}")

    @kills(
        Mutation(
            toml._add_table,
            'cast("tomlkit.items.Table", document.body[index][1])',
            "document[super_table]",
            "a table inserted between split tool tables goes through tomlkit's proxy, which does not have a body",
            raises="AttributeError: 'dict' object has no attribute 'body'",
        )
    )
    def test_separates_a_table_inserted_between_out_of_order_tool_tables_from_the_next_table(self):
        """Test that a blank line separates a `[tool.uv]` added to the first of split tool tables from what follows."""
        written = self.written(f"[tool.ruff]\nx = 1\n\n{_NEXT_TABLE}\n[tool.black]\ny = 2\n")
        self.assertEqual(written, f"[tool.ruff]\nx = 1\n\n{_UV_TABLE}\n{_NEXT_TABLE}\n[tool.black]\ny = 2\n")

    @kills(
        Mutation(
            toml._last_table,
            "table = last",
            "return last",
            "a comment above the next table ends up above a table inserted after a nested tool table",
        ),
        Mutation(
            toml._last_table,
            "last = last.body[-1]",
            "pass",
            "a comment above the next table ends up above a table inserted after an array of tool tables",
        ),
    )
    def test_keeps_a_comment_above_the_next_table(self):
        """Test that a comment above the table after the tool tables stays above that table."""
        for header in ("[tool.ruff]", "[tool.ruff.lint]", "[[tool.foo]]"):
            with self.subTest(header=header):
                written = self.written(f"{header}\nx = 1\n\n# groups\n{_NEXT_TABLE}")
                self.assertEqual(written, f"{header}\nx = 1\n\n{_UV_TABLE}\n# groups\n{_NEXT_TABLE}")

    @kills(
        Mutation(
            toml._remove_root_key,
            "return",
            "pass",
            "removing an inline tool table drops the blank line between the root keys before it and the first table",
        ),
        Mutation(
            toml._expand_inline_tables,
            "_table_from(parent, expanding=table)",
            "_table_from(parent)",
            "an inline uv table inside an inline tool table stays inline",
        ),
        Mutation(
            toml._expand_inline_tables,
            "del parent[table]\n        _add_table(document, super_table, table, _table_from(child))",
            "parent[table] = _table_from(child)",
            "an inline uv table turned into a table runs into the table after it",
        ),
    )
    def test_turns_inline_tool_tables_into_tables(self):
        """Test that an inline `[tool]` or `[tool.uv]` becomes a table, leaving the other tools as they were."""
        uv_table = '[tool.uv]\npackage = false\nexclude-newer = "7 days"\n'
        project = '[project]\nname = "x"\n'
        for name, contents, expected in (
            (
                "inline tool after a root key",
                f"top = 1\ntool = {{ ruff = {{ x = 1 }} }}\n\n{project}",
                f"top = 1\n\n{project}\n[tool]\nruff = {{ x = 1 }}\n\n{_UV_TABLE}",
            ),
            (
                "inline uv in inline tool",
                f"tool = {{ uv = {{ package = false }} }}\n\n{project}",
                f"{project}\n{uv_table}",
            ),
            (
                "inline uv under tool",
                f"[tool]\nuv = {{ package = false }}\n\n{_NEXT_TABLE}",
                f"[tool]\n\n{uv_table}\n{_NEXT_TABLE}",
            ),
        ):
            with self.subTest(name):
                self.assertEqual(self.written(contents), expected)

    def test_leaves_a_file_whose_tool_tables_cannot_hold_a_commented_key(self):
        """Test that nothing is written to a file whose `[tool]` or `[tool.uv]` is not a table."""
        for name, contents in (
            ("tool not a table", 'tool = "x"\n'),
            ("array of uv tables", "[[tool.uv]]\nx = 1\n"),
        ):
            with self.subTest(name):
                pyproject_file = mock_path(contents)
                self.assertFalse(PyprojectToml(pyproject_file).set_tool_key("uv", "exclude-newer", "7 days"))
                pyproject_file.write_text.assert_not_called()

    def test_with_comment(self):
        """Test that a trailing comment is attached when given."""
        self.assertIn('exclude-newer = "7 days" # a note', self.written('[project]\nname = "x"\n', comment="a note"))

    def test_without_comment(self):
        """Test that no trailing comment is attached when none is given."""
        written = self.written('[project]\nname = "x"\n')
        self.assertIn('exclude-newer = "7 days"\n', written)
        self.assertNotIn("#", written)
