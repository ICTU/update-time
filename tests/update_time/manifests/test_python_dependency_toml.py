"""Unit tests for reading and rewriting the Python dependencies a file declares in TOML, with file I/O mocked."""

import unittest
from typing import TYPE_CHECKING

from update_time.manifests import python_dependency_toml
from update_time.manifests.inline_script import InlineScript
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.markers.marker import Marker, Scope
from update_time.primitives.location import Location

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time.helpers import declaration
from tests.update_time.manifests.helpers import script

if TYPE_CHECKING:
    from unittest.mock import Mock

# A file opening a `# /// script` block and never closing it, whose dependency array is therefore not commented out.
_UNCLOSED_BLOCK = '# /// script\ndependencies = ["pkg=={0}"]\n'


class RewritePinnedVersionsTest(unittest.TestCase):
    """Unit tests for rewriting pinned dependency versions."""

    def rewrite(
        self,
        contents: str,
        versions: dict[int, str],
        kind: type[python_dependency_toml.PythonDependencyToml] = PyprojectToml,
    ) -> Mock:
        """Rewrite the declaration at each of the positions to the version given, and return the mock file."""
        pyproject_file = mock_path(contents)
        file = kind(pyproject_file)
        declared = {declaration.position: declaration for declaration in file.declarations()}
        pins = {declared[position]: version for position, version in versions.items()}
        file.rewrite_pinned_versions(pins)
        return pyproject_file

    def test_bumps_known_versions(self):
        """Test that a pin with a known newer version is rewritten."""
        pyproject_file = self.rewrite('dependencies = ["pkg==1.0"]\n', {1: "1.1"})
        self.assertEqual(pyproject_file.write_text.call_args.args[0], 'dependencies = ["pkg==1.1"]\n')

    @kills(
        Mutation(
            python_dependency_toml._rewritten_spec,
            'return re.sub(rf"(==\\s*){re.escape(current)}", lambda match: match[1] + new_version, spec, count=1)',
            'return f"{declaration.dependency}=={new_version}"',
            "a rewrite replaces the whole declaration, dropping the extra, environment marker, and spaces around "
            "the pin",
        )
    )
    def test_bumps_the_version_and_keeps_the_extra_marker_and_spaces(self):
        """Test that only the version is rewritten, so the extra, environment marker, and spaces are kept."""
        declarations = (
            '    "package[extra]=={0}",\n    "marked=={1}; python_version < \'3.13\'",\n    "spaced == {2}",\n'
        )
        contents = "[project]\ndependencies = [\n" + declarations.format("1.0", "2.0", "3.0") + "]\n"
        pyproject_file = self.rewrite(contents, {1: "1.1", 2: "2.1", 3: "3.1"})
        self.assertEqual(
            pyproject_file.write_text.call_args.args[0],
            "[project]\ndependencies = [\n" + declarations.format("1.1", "2.1", "3.1") + "]\n",
        )

    def test_leaves_a_matching_string_outside_the_dependency_arrays(self):
        """Test that a spec spelled the same way outside a dependency array is left as the file wrote it."""
        outside = 'keywords = ["pkg==1.0"]\n[tool.uv]\nconstraint-dependencies = ["pkg==1.0"]\n'
        pyproject_file = self.rewrite(f'[project]\ndependencies = ["pkg==1.0"]\n{outside}', {1: "1.1"})
        self.assertEqual(
            pyproject_file.write_text.call_args.args[0], f'[project]\ndependencies = ["pkg==1.1"]\n{outside}'
        )

    @kills(
        Mutation(
            python_dependency_toml.PythonDependencyToml._replaced_specs,
            "array[index] = toml.string(new_spec, quoted_as=spec)",
            "array[index] = new_spec",
            "a rewritten spec is quoted the way tomlkit quotes a plain string, so the file's own quoting is lost",
        )
    )
    def test_preserves_the_formatting_around_a_rewritten_pin(self):
        """Test that the quoting, comment, and indentation of a rewritten declaration come back as the file had them."""
        declaration = "    'pkg=={0}',  # keep\n"
        contents = f"[project]\ndependencies = [\n{declaration.format('1.0')}]\n\n# trailing note\n"
        pyproject_file = self.rewrite(contents, {1: "1.1"})
        self.assertEqual(
            pyproject_file.write_text.call_args.args[0],
            f"[project]\ndependencies = [\n{declaration.format('1.1')}]\n\n# trailing note\n",
        )

    def test_rewrites_a_block_that_is_never_closed_as_toml_throughout(self):
        """Test that a block without its closing `# ///` is rewritten as the TOML the file is, not as a block."""
        pyproject_file = self.rewrite(_UNCLOSED_BLOCK.format("1.0"), {1: "1.1"}, InlineScript)
        self.assertEqual(pyproject_file.write_text.call_args.args[0], _UNCLOSED_BLOCK.format("1.1"))

    def test_leaves_a_declaration_that_pins_no_exact_version(self):
        """Test that a declaration pinning no exact version is left alone."""
        pyproject_file = self.rewrite('dependencies = ["pkg>=1.0"]\n', {1: "1.1"})
        pyproject_file.write_text.assert_not_called()

    def test_rewrites_one_declaration_of_a_name_and_leaves_the_other(self):
        """Test that one declaration of a name is rewritten while another declaration of that name is left alone."""
        contents = '[project]\ndependencies = ["pkg==1.0"]\n[dependency-groups]\ndev = ["pkg==1.0"]\n'
        pyproject_file = self.rewrite(contents, {2: "1.1"})
        self.assertEqual(
            pyproject_file.write_text.call_args.args[0],
            '[project]\ndependencies = ["pkg==1.0"]\n[dependency-groups]\ndev = ["pkg==1.1"]\n',
        )

    def test_leaves_a_declaration_the_mapping_does_not_hold_and_writes_nothing(self):
        """Test that a declaration the mapping holds no version for is left alone, so the file is not written."""
        pyproject_file = self.rewrite('dependencies = ["pkg==1.0"]\n', {})
        pyproject_file.write_text.assert_not_called()


class DeclarationsTest(unittest.TestCase):
    """Unit tests for reading every dependency a file declares: the exact pins, and the declarations without one."""

    def references(
        self, path: Mock, kind: type[python_dependency_toml.PythonDependencyToml] = PyprojectToml
    ) -> list[python_dependency_toml.Declaration]:
        """Return the reference each dependency the file declares makes."""
        return list(kind(path).declarations())

    def markers(
        self, contents: str, kind: type[python_dependency_toml.PythonDependencyToml] = PyprojectToml
    ) -> list[Marker]:
        """Return the marker steering each dependency the file declares."""
        return [declared.marker for declared in self.references(mock_path(contents), kind)]

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
                declaration("pkg", "1.0", path, 2, 1),
                declaration("other", "", path, 2, 2),
                declaration("bare", "", path, 2, 3),
                declaration("sphinx", "", path, 4, 4),
                declaration("ruff", "", path, 6, 5),
                declaration("mypy", "", path, 8, 6),
                declaration("uv-build", "", path, 10, 7),
            ],
        )

    def test_reads_an_inline_script_metadata_block(self):
        """Test that a declaration in a `# /// script` block is read, although the block is commented out."""
        path = mock_path(script("pkg==1.0", "other>=2.0"))
        self.assertEqual(
            self.references(path, InlineScript),
            [declaration("pkg", "1.0", path, 4, 1), declaration("other", "", path, 5, 2)],
        )

    def test_reads_a_block_that_is_never_closed_as_toml_throughout(self):
        """Test that a block without its closing `# ///` comments out no TOML, so the file is read as TOML itself."""
        path = mock_path(_UNCLOSED_BLOCK.format("1.0"))
        self.assertEqual(self.references(path, InlineScript), [declaration("pkg", "1.0", path, 2, 1)])

    @kills(
        Mutation(
            python_dependency_toml._pinned_version,
            'if len(specifiers) == 1 and specifiers[0].operator == "==" and is_valid(specifiers[0].version):',
            'if (exact := [s for s in specifiers if s.operator == "=="]) and is_valid(exact[0].version):',
            "a declaration combining an equals with another specifier is read as a pin on the version the equals names",
        )
    )
    def test_a_declaration_pinning_no_single_version_is_read_without_one(self):
        """Test that a wildcard, an arbitrary equality, and a combined specifier are read without a version."""
        path = mock_path('dependencies = ["wild==1.0.*", "arbitrary===nightly", "combined==1.0,!=1.0.1"]\n')
        self.assertEqual(
            self.references(path),
            [
                declaration("wild", "", path, 1, 1),
                declaration("arbitrary", "", path, 1, 2),
                declaration("combined", "", path, 1, 3),
            ],
        )

    def test_a_declaration_in_a_literal_string(self):
        """Test that a declaration quoted the other way TOML allows is located at its line too."""
        path = mock_path("dependencies = ['pkg>=1.0']\n")
        self.assertEqual(self.references(path), [declaration("pkg", "", path, 1, 1)])

    def test_a_quoted_name_outside_a_dependency_array_is_not_read(self):
        """Test that neither a name nor a pin quoted elsewhere in the file is read as a dependency."""
        contents = (
            "[project]\n"
            'dependencies = ["other>=2.0"]\n'
            'keywords = ["pytest==1.0"]\n'
            '[tool.ruff.lint.isort]\nknown-first-party = ["rich"]\n'
        )
        path = mock_path(contents)
        self.assertEqual(self.references(path), [declaration("other", "", path, 2, 1)])

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
                python_dependency_toml.Declaration(
                    "local", "", Location(path, 2), uv_sourced=True, direct_url=False, position=1
                ),
                python_dependency_toml.Declaration(
                    "pkg", "", Location(path, 2), uv_sourced=False, direct_url=True, position=2
                ),
                declaration("other", "", path, 2, 3),
            ],
        )

    def test_a_pin_with_a_uv_source_is_read(self):
        """Test that a pin uv resolves from a source of its own is read, since uv can resolve an update for it."""
        contents = '[project]\ndependencies = ["local==1.0"]\n[tool.uv.sources]\nlocal = {path = "../local"}\n'
        path = mock_path(contents)
        expected = python_dependency_toml.Declaration(
            "local", "1.0", Location(path, 2), uv_sourced=True, direct_url=False, position=1
        )
        self.assertEqual(self.references(path), [expected])

    @kills(
        Mutation(
            python_dependency_toml.PythonDependencyToml.declarations,
            "uv_sourced=normalized_python_name(requirement.name) in sourced,",
            "uv_sourced=requirement.name in sourced,",
            "a dependency spelled another way than its `sources` key reads as one PyPI serves, so PyPI is asked "
            "about a name it has no release for",
        ),
        Mutation(
            python_dependency_toml._uv_source_names,
            'return {normalized_python_name(name) for name in _uv_table(config).get("sources", {})}',
            'return set(_uv_table(config).get("sources", {}))',
            "a `sources` key spelled another way than the dependency it names covers it not, so PyPI is asked about "
            "a name it has no release for",
        ),
    )
    def test_a_uv_source_spelled_another_way_is_read(self):
        """Test that a `sources` entry naming the dependency another way is matched, as uv matches it.

        uv resolves a `sources` key to a dependency by the normalised name, so the two are matched whichever of
        them spells the separator which way.
        """
        for declared, sourced in (("local-pkg", "local_pkg"), ("local_pkg", "local-pkg")):
            with self.subTest(declared=declared, sourced=sourced):
                contents = (
                    f'[project]\ndependencies = ["{declared}==1.0"]\n'
                    f'[tool.uv.sources]\n{sourced} = {{path = "../local"}}\n'
                )
                path = mock_path(contents)
                location = Location(path, 2)
                expected = python_dependency_toml.Declaration(
                    declared, "1.0", location, uv_sourced=True, direct_url=False, position=1
                )
                self.assertEqual(self.references(path), [expected])

    def test_a_declaration_that_does_not_parse(self):
        """Test that a spec that does not parse is left out, and the declarations after it keep their position."""
        path = mock_path('dependencies = ["pkg=1.0", "other>=2.0"]\n')  # `=` is no PEP 440 operator
        self.assertEqual(self.references(path), [declaration("other", "", path, 1, 2)])

    def test_a_declaration_toml_spells_with_an_escape(self):
        """Test that a declaration whose TOML string carries an escape is located at its line like any other."""
        path = mock_path('dependencies = ["pkg\\u003e=1.0"]\n')  # a TOML escape: the spec parses as `pkg>=1.0`
        self.assertEqual(self.references(path), [declaration("pkg", "", path, 1, 1)])

    def test_reads_the_marker_a_declaration_spells_on_its_own_line(self):
        """Test that a declaration carries the marker its own line spells, and the declaration below it carries none."""
        declaration = '    "pkg==1.0",  # update-time: ignore[update]\n'
        contents = f'[project]\ndependencies = [\n{declaration}    "other==2.0",\n]\n'
        self.assertEqual(self.markers(contents), [Marker(ignored_scopes=Scope.UPDATE), Marker()])

    def test_reads_the_marker_on_the_line_above_a_declaration(self):
        """Test that a marker steers the declaration on the line below it, and none when that line opens an array."""
        contents = (
            "[project]\n"
            "# update-time: set[stale-after=0]\n"  # the line below declares nothing, so this steers no declaration
            'dependencies = [\n    "pkg==1.0",\n'
            "    # update-time: ignore[update]\n"
            '    "other==2.0",\n]\n'
            "[dependency-groups]\n"
            "# update-time: ignore[yanked]\n"  # the line below declares the array's only dependency
            'dev = ["third==3.0"]\n'
        )
        self.assertEqual(
            self.markers(contents), [Marker(), Marker(ignored_scopes=Scope.UPDATE), Marker(ignored_scopes=Scope.YANKED)]
        )

    def test_reads_the_marker_of_a_declaration_below_a_line_separator(self):
        """Test that a line separator earlier in the file does not shift the line a marker is read from.

        TOML accepts U+2028 inside a string, and Python splits lines on it where a count of newlines does not.
        """
        contents = '[project]\nname = "a\u2028b"\ndependencies = ["pkg==1.0"]  # update-time: ignore[update]\n'
        self.assertEqual(self.markers(contents), [Marker(ignored_scopes=Scope.UPDATE)])

    @kills(
        Mutation(
            python_dependency_toml.PythonDependencyToml.declarations,
            "self.toml(placed)",
            "placed",
            "a marker's lines come from the file, so every line of a `# /// script` block reads as a comment and "
            "an inline marker there steers the declaration below it too",
        )
    )
    def test_reads_a_marker_in_an_inline_script_metadata_block(self):
        """Test that a marker written with a `#` of its own inside the block is read, in both its placements."""
        contents = (
            "# /// script\n# dependencies = [\n"
            '#     "pkg==1.0",  # update-time: ignore[update]\n'  # inline, so it steers it alone
            '#     "other==2.0",\n'
            "#     # update-time: ignore[yanked]\n"  # on the line above, so it steers the declaration below it
            '#     "third==3.0",\n'
            "# ]\n# ///\n"
        )
        markers = self.markers(contents, InlineScript)
        self.assertEqual(markers, [Marker(ignored_scopes=Scope.UPDATE), Marker(), Marker(ignored_scopes=Scope.YANKED)])


class PinnedDeclarationsTest(unittest.TestCase):
    """Unit tests for the versions the declarations a file makes pin."""

    def pins(self, path: Mock) -> list[python_dependency_toml.Declaration]:
        """Return the reference each declaration that pins a version makes."""
        declarations = PyprojectToml(path).declarations()
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
                declaration("pkg", "1.0", path, 2, 1),
                declaration("sphinx", "7.4", path, 4, 3),
                declaration("ruff", "0.6.0", path, 6, 4),
            ],
        )

    def test_reads_a_name_pinned_more_than_once(self):
        """Test that a name pinned in two arrays is returned once per pin, so neither pin hides the other."""
        contents = '[project]\ndependencies = ["pkg==1.0"]\n[dependency-groups]\ndev = ["pkg==2.0"]\n'
        path = mock_path(contents)
        self.assertEqual(
            self.pins(path),
            [
                declaration("pkg", "1.0", path, 2, 1),
                declaration("pkg", "2.0", path, 4, 2),
            ],
        )

    @kills(
        Mutation(
            python_dependency_toml.PythonDependencyToml.declarations,
            "requirement.name,\n",
            'requirement.name + "".join(f"[{extra}]" for extra in requirement.extras),\n',
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
                declaration("package", "1.0", path, 3, 1),
                declaration("marked", "2.0", path, 4, 2),
                declaration("spaced", "3.0", path, 5, 3),
            ],
        )

    @kills(
        Mutation(
            python_dependency_toml._pinned_version,
            'if len(specifiers) == 1 and specifiers[0].operator == "==" and is_valid(specifiers[0].version):',
            'if len(specifiers) == 1 and specifiers[0].operator == "==" '
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
            [declaration("local", "1.0+local", path, 3, 1), declaration("epoch", "1!2.0", path, 4, 2)],
        )

    def test_a_pin_is_located_at_its_declaration(self):
        """Test that a pin is reported at the line declaring it, not at a string spelled the same way above it."""
        path = mock_path('[project]\nkeywords = ["pkg==1.0"]\ndependencies = ["pkg==1.0"]\n')
        self.assertEqual(self.pins(path), [declaration("pkg", "1.0", path, 3, 1)])

    def test_no_pins(self):
        """Test that a file with no exact pins yields nothing."""
        self.assertEqual(self.pins(mock_path('dependencies = ["pkg>=1.0"]\n')), [])
