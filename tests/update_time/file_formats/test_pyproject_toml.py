"""Unit tests for the pyproject.toml file format, with file I/O mocked."""

import unittest
from unittest.mock import Mock

from update_time.domain.version import Reference
from update_time.file_formats import pyproject_toml
from update_time.primitives.location import Location

from tests.helpers import mock_path


class ReadTest(unittest.TestCase):
    """Unit tests for parsing a pyproject.toml."""

    def test_valid(self):
        """Test that a valid pyproject.toml is parsed into a dict."""
        self.assertEqual(pyproject_toml.read(mock_path('[project]\nname = "x"\n')), {"project": {"name": "x"}})

    def test_malformed(self):
        """Test that a malformed pyproject.toml reads back as None instead of raising."""
        self.assertIsNone(pyproject_toml.read(mock_path("this is not = valid = toml")))

    def test_missing(self):
        """Test that a missing/unreadable pyproject.toml reads back as None instead of raising."""
        self.assertIsNone(pyproject_toml.read(Mock(read_text=Mock(side_effect=OSError))))


class ToolKeyTest(unittest.TestCase):
    """Unit tests for reading a `[tool.<table>]` key and its trailing comment."""

    def test_value_with_comment(self):
        """Test that both the value and its trailing comment are returned."""
        contents = '[tool.uv]\nexclude-newer = "7 days" # a note\n'
        self.assertEqual(pyproject_toml.tool_key(mock_path(contents), "uv", "exclude-newer"), ("7 days", "# a note"))

    def test_value_without_comment(self):
        """Test that a key without a trailing comment returns an empty comment string."""
        self.assertEqual(
            pyproject_toml.tool_key(mock_path('[tool.uv]\nexclude-newer = "7 days"\n'), "uv", "exclude-newer"),
            ("7 days", ""),
        )

    def test_absent_table(self):
        """Test that a missing `[tool.<table>]` table yields None."""
        self.assertIsNone(pyproject_toml.tool_key(mock_path("[tool.other]\nx = 1\n"), "uv", "exclude-newer"))

    def test_absent_key(self):
        """Test that a missing key in an existing table yields None."""
        self.assertIsNone(pyproject_toml.tool_key(mock_path("[tool.uv]\nmanaged = true\n"), "uv", "exclude-newer"))


class SetToolKeyTest(unittest.TestCase):
    """Unit tests for setting a `[tool.<table>]` key while preserving the rest of the file."""

    def written(self, contents: str, *, comment: str = "") -> str:
        """Set `[tool.uv] exclude-newer = "7 days"` on the contents and return what was written back."""
        pyproject_file = mock_path(contents)
        pyproject_toml.set_tool_key(pyproject_file, "uv", "exclude-newer", "7 days", comment=comment)
        return pyproject_file.write_text.call_args.args[0]

    def test_creates_tool_and_table_when_absent(self):
        """Test that `[tool.uv]` is created when the file has no `[tool]` section at all."""
        written = self.written('[project]\nname = "x"\n')
        self.assertIn("[tool.uv]", written)
        self.assertIn('exclude-newer = "7 days"', written)

    def test_creates_table_in_existing_tool(self):
        """Test that a `[tool.uv]` table is added alongside an existing, different tool table."""
        written = self.written("[tool.other]\nx = 1\n")
        self.assertIn("x = 1", written)
        self.assertIn('exclude-newer = "7 days"', written)

    def test_preserves_other_keys_in_the_table(self):
        """Test that other keys in an existing `[tool.uv]` table are preserved."""
        written = self.written("[tool.uv]\nexclude-newer-package = { msgpack = false }\n")
        self.assertIn("exclude-newer-package = { msgpack = false }", written)
        self.assertIn('exclude-newer = "7 days"', written)

    def test_with_comment(self):
        """Test that a trailing comment is attached when given."""
        self.assertIn('exclude-newer = "7 days" # a note', self.written('[project]\nname = "x"\n', comment="a note"))

    def test_without_comment(self):
        """Test that no trailing comment is attached when none is given."""
        written = self.written('[project]\nname = "x"\n')
        self.assertIn('exclude-newer = "7 days"\n', written)
        self.assertNotIn("#", written)


class RewritePinnedVersionsTest(unittest.TestCase):
    """Unit tests for rewriting pinned dependency versions."""

    def rewrite(self, contents: str, versions: dict[str, str]) -> Mock:
        """Rewrite the pins in the contents and return the mock pyproject.toml."""
        pyproject_file = mock_path(contents)
        pyproject_toml.rewrite_pinned_versions(pyproject_file, versions)
        return pyproject_file

    def test_bumps_known_versions(self):
        """Test that a pin with a known newer version is rewritten."""
        pyproject_file = self.rewrite('dependencies = ["pkg==1.0"]\n', {"pkg": "1.1"})
        self.assertEqual(pyproject_file.write_text.call_args.args[0], 'dependencies = ["pkg==1.1"]\n')

    def test_matches_name_case_insensitively(self):
        """Test that the name is matched case-insensitively while its original casing is preserved."""
        pyproject_file = self.rewrite('dependencies = ["Pkg==1.0"]\n', {"pkg": "1.1"})
        self.assertEqual(pyproject_file.write_text.call_args.args[0], 'dependencies = ["Pkg==1.1"]\n')

    def test_matches_the_normalized_name(self):
        """Test that a pin is matched however it spells the name's separators, keeping the file's own spelling."""
        for spelling in ("typing-extensions", "typing_extensions", "typing.extensions", "Typing_Extensions"):
            with self.subTest(spelling=spelling):
                pyproject_file = self.rewrite(f'dependencies = ["{spelling}==1.0"]\n', {"typing-extensions": "1.1"})
                self.assertEqual(pyproject_file.write_text.call_args.args[0], f'dependencies = ["{spelling}==1.1"]\n')

    def test_leaves_unknown_names_and_writes_nothing(self):
        """Test that a pin with no known newer version is left alone and the file is not rewritten."""
        pyproject_file = self.rewrite('dependencies = ["pkg==1.0"]\n', {})
        pyproject_file.write_text.assert_not_called()


class PinnedVersionsTest(unittest.TestCase):
    """Unit tests for reading the exact pins from a pyproject.toml."""

    def test_reads_exact_pins_across_arrays(self):
        """Test that `==` pins are read from every dependency array, each with the line it sits on."""
        contents = (
            "[project]\n"
            'dependencies = ["pkg==1.0", "other>=2.0"]\n'  # only the `==` pin is returned
            '[project.optional-dependencies]\ndocs = ["sphinx==7.4"]\n'
            '[dependency-groups]\ndev = ["ruff==0.6.0"]\n'
        )
        path = mock_path(contents)
        self.assertEqual(
            pyproject_toml.pinned_versions(path),
            [
                (Reference("pkg", "1.0"), Location(path, 2)),
                (Reference("sphinx", "7.4"), Location(path, 4)),
                (Reference("ruff", "0.6.0"), Location(path, 6)),
            ],
        )

    def test_reads_a_name_pinned_more_than_once(self):
        """Test that a name pinned in two arrays is returned once per pin, so neither pin hides the other."""
        contents = '[project]\ndependencies = ["pkg==1.0"]\n[dependency-groups]\ndev = ["pkg==2.0"]\n'
        path = mock_path(contents)
        self.assertEqual(
            pyproject_toml.pinned_versions(path),
            [(Reference("pkg", "1.0"), Location(path, 2)), (Reference("pkg", "2.0"), Location(path, 4))],
        )

    def test_no_pins(self):
        """Test that a file with no exact pins yields nothing."""
        self.assertEqual(pyproject_toml.pinned_versions(mock_path('dependencies = ["pkg>=1.0"]\n')), [])
