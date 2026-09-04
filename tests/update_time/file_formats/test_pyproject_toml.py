"""Unit tests for the pyproject.toml file format, with file I/O mocked."""

import unittest
from typing import TYPE_CHECKING

from update_time.file_formats import pyproject_toml
from update_time.file_formats.dependency_file import DependencyTomlFile, InlineScript, PyprojectToml
from update_time.file_formats.pyproject_toml import Declaration
from update_time.primitives.location import Location

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time.helpers import declaration, script

if TYPE_CHECKING:
    from unittest.mock import Mock

# A file opening a `# /// script` block and never closing it, whose dependency array is therefore not commented out.
_UNCLOSED_BLOCK = '# /// script\ndependencies = ["pkg=={0}"]\n'


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

    def rewrite(self, contents: str, versions: dict[str, str], kind: type[DependencyTomlFile] = PyprojectToml) -> Mock:
        """Rewrite the pins in the contents and return the mock file."""
        pyproject_file = mock_path(contents)
        pyproject_toml.rewrite_pinned_versions(kind(pyproject_file), versions)
        return pyproject_file

    def test_bumps_known_versions(self):
        """Test that a pin with a known newer version is rewritten."""
        pyproject_file = self.rewrite('dependencies = ["pkg==1.0"]\n', {"pkg": "1.1"})
        self.assertEqual(pyproject_file.write_text.call_args.args[0], 'dependencies = ["pkg==1.1"]\n')

    @kills(
        Mutation(
            pyproject_toml,
            '    return re.sub(rf"(==\\s*){re.escape(current)}", lambda match: match[1] + new_version, spec, count=1)',
            '    return f"{requirement.name}=={new_version}"',
            "a rewrite respells the whole declaration, dropping the extra, environment marker, and spaces around "
            "the pin",
        )
    )
    def test_bumps_the_version_and_keeps_the_extra_marker_and_spaces(self):
        """Test that only the version is rewritten, so the extra, environment marker, and spaces are kept."""
        declarations = (
            '    "package[extra]=={0}",\n    "marked=={1}; python_version < \'3.13\'",\n    "spaced == {2}",\n'
        )
        contents = "[project]\ndependencies = [\n" + declarations.format("1.0", "2.0", "3.0") + "]\n"
        pyproject_file = self.rewrite(contents, {"package": "1.1", "marked": "2.1", "spaced": "3.1"})
        self.assertEqual(
            pyproject_file.write_text.call_args.args[0],
            "[project]\ndependencies = [\n" + declarations.format("1.1", "2.1", "3.1") + "]\n",
        )

    def test_leaves_a_matching_string_outside_the_dependency_arrays(self):
        """Test that a spec spelled the same way outside a dependency array is left as the file wrote it."""
        outside = 'keywords = ["pkg==1.0"]\n[tool.uv]\nconstraint-dependencies = ["pkg==1.0"]\n'
        pyproject_file = self.rewrite(f'[project]\ndependencies = ["pkg==1.0"]\n{outside}', {"pkg": "1.1"})
        self.assertEqual(
            pyproject_file.write_text.call_args.args[0], f'[project]\ndependencies = ["pkg==1.1"]\n{outside}'
        )

    @kills(
        Mutation(
            pyproject_toml,
            "                array[index] = toml.string(new_spec, quoted_as=spec)",
            "                array[index] = new_spec",
            "a rewritten spec is quoted the way tomlkit quotes a plain string, so the file's own quoting is lost",
        )
    )
    def test_preserves_the_formatting_around_a_rewritten_pin(self):
        """Test that the quoting, comment, and indentation of a rewritten declaration come back as the file had them."""
        declaration = "    'pkg=={0}',  # keep\n"
        contents = f"[project]\ndependencies = [\n{declaration.format('1.0')}]\n\n# trailing note\n"
        pyproject_file = self.rewrite(contents, {"pkg": "1.1"})
        self.assertEqual(
            pyproject_file.write_text.call_args.args[0],
            f"[project]\ndependencies = [\n{declaration.format('1.1')}]\n\n# trailing note\n",
        )

    def test_rewrites_a_block_that_is_never_closed_as_toml_throughout(self):
        """Test that a block without its closing `# ///` is rewritten as the TOML the file is, not as a block."""
        pyproject_file = self.rewrite(_UNCLOSED_BLOCK.format("1.0"), {"pkg": "1.1"}, InlineScript)
        self.assertEqual(pyproject_file.write_text.call_args.args[0], _UNCLOSED_BLOCK.format("1.1"))

    def test_leaves_a_file_whose_toml_does_not_parse(self):
        """Test that a file that is not valid TOML is left alone, rather than aborting the run over it."""
        pyproject_file = self.rewrite("this is not = valid = toml", {"pkg": "1.1"})
        pyproject_file.write_text.assert_not_called()

    def test_leaves_a_name_the_mapping_spells_differently(self):
        """Test that a pin whose name the mapping spells another way is left alone."""
        pyproject_file = self.rewrite('dependencies = ["Pkg==1.0"]\n', {"pkg": "1.1"})
        pyproject_file.write_text.assert_not_called()

    def test_leaves_unknown_names_and_writes_nothing(self):
        """Test that a pin with no known newer version is left alone and the file is not rewritten."""
        pyproject_file = self.rewrite('dependencies = ["pkg==1.0"]\n', {})
        pyproject_file.write_text.assert_not_called()


class DeclaredDependenciesTest(unittest.TestCase):
    """Unit tests for reading every dependency a file declares: the exact pins, and the declarations without one."""

    def references(self, path: Mock, kind: type[DependencyTomlFile] = PyprojectToml) -> list[Declaration]:
        """Return the reference each dependency the file declares makes."""
        return list(pyproject_toml.declared_dependencies(kind(path)))

    def test_reads_declarations_across_arrays(self):
        """Test that a declaration without an exact pin is read from every array, each with the line it sits on."""
        contents = (
            "[project]\n"
            'dependencies = ["pkg==1.0", "other>=2.0", "bare"]\n'  # the pins lead, whichever array declares them
            '[project.optional-dependencies]\ndocs = ["sphinx~=7.4"]\n'
            '[dependency-groups]\ndev = ["ruff<0.7", {include-group = "docs"}]\n'
            '[tool.uv]\ndev-dependencies = ["mypy>=1.0"]\n'  # uv's legacy array, which uv still resolves
            '[build-system]\nrequires = ["uv-build>=0.12"]\n'
        )
        path = mock_path(contents)
        self.assertEqual(
            self.references(path),
            [
                declaration("pkg", "1.0", path, 2),
                declaration("other", "", path, 2),
                declaration("bare", "", path, 2),
                declaration("sphinx", "", path, 4),
                declaration("ruff", "", path, 6),
                declaration("mypy", "", path, 8),
                declaration("uv-build", "", path, 10),
            ],
        )

    def test_reads_an_inline_script_metadata_block(self):
        """Test that a declaration in a `# /// script` block is read, although the block is commented out."""
        path = mock_path(script("pkg==1.0", "other>=2.0"))
        self.assertEqual(
            self.references(path, InlineScript),
            [declaration("pkg", "1.0", path, 4), declaration("other", "", path, 5)],
        )

    def test_reads_a_block_that_is_never_closed_as_toml_throughout(self):
        """Test that a block without its closing `# ///` comments out no TOML, so the file is read as TOML itself."""
        path = mock_path(_UNCLOSED_BLOCK.format("1.0"))
        self.assertEqual(self.references(path, InlineScript), [declaration("pkg", "1.0", path, 2)])

    @kills(
        Mutation(
            pyproject_toml,
            '    if len(specifiers) == 1 and specifiers[0].operator == "==" and is_valid(specifiers[0].version):',
            '    if (exact := [s for s in specifiers if s.operator == "=="]) and is_valid(exact[0].version):',
            "a declaration combining an equals with another specifier is read as a pin on the version the equals names",
        )
    )
    def test_a_declaration_pinning_no_single_version_is_read_without_one(self):
        """Test that a wildcard, an arbitrary equality, and a combined specifier are read without a version."""
        path = mock_path('dependencies = ["wild==1.0.*", "arbitrary===nightly", "combined==1.0,!=1.0.1"]\n')
        self.assertEqual(
            self.references(path),
            [
                declaration("wild", "", path, 1),
                declaration("arbitrary", "", path, 1),
                declaration("combined", "", path, 1),
            ],
        )

    def test_a_declaration_in_a_literal_string(self):
        """Test that a declaration quoted the other way TOML allows is located at its line too."""
        path = mock_path("dependencies = ['pkg>=1.0']\n")
        self.assertEqual(self.references(path), [declaration("pkg", "", path, 1)])

    def test_a_quoted_name_outside_a_dependency_array_is_not_read(self):
        """Test that neither a name nor a pin quoted elsewhere in the file is read as a dependency."""
        contents = (
            "[project]\n"
            'dependencies = ["other>=2.0"]\n'
            'keywords = ["pytest==1.0"]\n'
            '[tool.ruff.lint.isort]\nknown-first-party = ["rich"]\n'
        )
        path = mock_path(contents)
        self.assertEqual(self.references(path), [declaration("other", "", path, 2)])

    def test_a_dependency_with_a_uv_source_or_a_url_is_read(self):
        """Test that a dependency uv resolves from a source of its own, or from a URL, is read like any other."""
        contents = (
            '[project]\ndependencies = ["local", "pkg @ git+https://github.com/org/repo.git", "other>=2.0"]\n'
            '[tool.uv.sources]\nlocal = {path = "../local"}\n'
        )
        path = mock_path(contents)
        self.assertEqual(
            self.references(path),
            [
                Declaration("local", "", Location(path, 2), uv_sourced=True, direct_url=False),
                Declaration("pkg", "", Location(path, 2), uv_sourced=False, direct_url=True),
                declaration("other", "", path, 2),
            ],
        )

    @kills(
        Mutation(
            pyproject_toml,
            "        for spec, m in located\n        if (requirement := _requirement(spec)) is not None\n    ]",
            "        for spec, m in located\n"
            "        if (requirement := _requirement(spec)) is not None\n"
            "        and not (requirement.name in sourced and _pinned_version(requirement))\n"
            "    ]",
            "a pin uv resolves from a source of its own is dropped, so an update uv resolves for it is never written",
        )
    )
    def test_a_pin_with_a_uv_source_is_read(self):
        """Test that a pin uv resolves from a source of its own is read, since uv can resolve an update for it."""
        contents = '[project]\ndependencies = ["local==1.0"]\n[tool.uv.sources]\nlocal = {path = "../local"}\n'
        path = mock_path(contents)
        expected = Declaration("local", "1.0", Location(path, 2), uv_sourced=True, direct_url=False)
        self.assertEqual(self.references(path), [expected])

    def test_a_declaration_that_does_not_parse(self):
        """Test that a spec that does not parse as a requirement leaves the file's other declarations read."""
        path = mock_path('dependencies = ["pkg=1.0", "other>=2.0"]\n')  # `=` is no PEP 440 operator
        self.assertEqual(self.references(path), [declaration("other", "", path, 1)])

    def test_a_declaration_toml_spells_with_an_escape(self):
        """Test that a declaration whose TOML string carries an escape is located at its line like any other."""
        path = mock_path('dependencies = ["pkg\\u003e=1.0"]\n')  # a TOML escape: the spec parses as `pkg>=1.0`
        self.assertEqual(self.references(path), [declaration("pkg", "", path, 1)])


class PinnedDeclarationsTest(unittest.TestCase):
    """Unit tests for the versions the declarations a file makes pin."""

    def pins(self, path: Mock) -> list[Declaration]:
        """Return the reference each declaration that pins a version makes."""
        declarations = pyproject_toml.declared_dependencies(PyprojectToml(path))
        return [d for d in declarations if d.current_version]

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
            self.pins(path),
            [
                declaration("pkg", "1.0", path, 2),
                declaration("sphinx", "7.4", path, 4),
                declaration("ruff", "0.6.0", path, 6),
            ],
        )

    def test_reads_a_name_pinned_more_than_once(self):
        """Test that a name pinned in two arrays is returned once per pin, so neither pin hides the other."""
        contents = '[project]\ndependencies = ["pkg==1.0"]\n[dependency-groups]\ndev = ["pkg==2.0"]\n'
        path = mock_path(contents)
        self.assertEqual(
            self.pins(path),
            [
                declaration("pkg", "1.0", path, 2),
                declaration("pkg", "2.0", path, 4),
            ],
        )

    @kills(
        Mutation(
            pyproject_toml,
            "            requirement.name,\n",
            '            requirement.name + "".join(f"[{extra}]" for extra in requirement.extras),\n',
            "a pin's name carries the extra its declaration spells, so it names no package the source knows",
        )
    )
    def test_reads_a_pin_with_an_extra_an_environment_marker_or_spaces(self):
        """Test that the version is read through whatever else a PEP 508 declaration spells around the pin."""
        contents = (
            "[project]\ndependencies = [\n"
            '    "package[extra]==1.0",\n'
            "    \"marked==2.0; python_version < '3.13'\",\n"
            '    "spaced == 3.0",\n]\n'
        )
        path = mock_path(contents)
        self.assertEqual(
            self.pins(path),
            [
                declaration("package", "1.0", path, 3),
                declaration("marked", "2.0", path, 4),
                declaration("spaced", "3.0", path, 5),
            ],
        )

    @kills(
        Mutation(
            pyproject_toml,
            '    if len(specifiers) == 1 and specifiers[0].operator == "==" and is_valid(specifiers[0].version):',
            '    if len(specifiers) == 1 and specifiers[0].operator == "==" '
            'and re.fullmatch("[0-9.]+", specifiers[0].version):',
            "a version is judged by its shape rather than by parsing it, so a local version and an epoch pin nothing",
        )
    )
    def test_reads_a_pin_only_when_it_names_one_version(self):
        """Test that a local version and an epoch are read as pins, where a wildcard and arbitrary equality are not."""
        contents = (
            "[project]\ndependencies = [\n"
            '    "local==1.0+local",\n'
            '    "epoch==1!2.0",\n'
            '    "wild==1.0.*",\n'
            '    "arbitrary===nightly",\n]\n'
        )
        path = mock_path(contents)
        self.assertEqual(
            self.pins(path),
            [declaration("local", "1.0+local", path, 3), declaration("epoch", "1!2.0", path, 4)],
        )

    def test_a_pin_is_located_at_its_declaration(self):
        """Test that a pin is reported at the line declaring it, not at a string spelled the same way above it."""
        path = mock_path('[project]\nkeywords = ["pkg==1.0"]\ndependencies = ["pkg==1.0"]\n')
        self.assertEqual(self.pins(path), [declaration("pkg", "1.0", path, 3)])

    def test_no_pins(self):
        """Test that a file with no exact pins yields nothing."""
        self.assertEqual(self.pins(mock_path('dependencies = ["pkg>=1.0"]\n')), [])
