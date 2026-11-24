"""Unit tests for the changelog parsing."""

import unittest
from typing import Final

from mutation_time.mutation import kills
from update_time.domain import changelog
from update_time.domain.changelog import (
    _find_anchor,
    _find_version_index,
    _heading_length,
    _heads_another_version,
    _names_version,
    _next_version_index,
    _underlines_the_line_above,
    _version_line_prefix,
    get_version_changes_from_changelog,
)
from update_time.formats import markdown, restructured_text
from update_time.formats.html import is_anchor
from update_time.formats.restructured_text import heading_underline

# The characters reStructuredText allows a heading to be underlined with, as the specification lists them at
# https://docutils.sourceforge.io/docs/ref/rst/restructuredtext.html#sections.
_SPEC_ADORNMENT_CHARACTERS: Final[str] = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
# More entry lines than the changelog module cuts a version's changes at.
_MANY_ENTRIES: Final[tuple[str, ...]] = tuple(f"- Fixed thing {number}." for number in range(40))


class VersionAnchorTest(unittest.TestCase):
    """Unit tests for the line a version's changes start at."""

    def test_empty_changelog(self):
        """Test that an empty changelog results in an empty change."""
        self.assertEqual(get_version_changes_from_changelog("", "1.0"), "")

    def test_version_number_not_found(self):
        """Test that a changelog that does not name the version yields no changes."""
        self.assertEqual(get_version_changes_from_changelog("Changelog\n\n## Version 0.9\n\n- Fixed ...", "1.0"), "")

    def test_version_in_prose_link_target_does_not_anchor_parsing(self):
        """Test that a version named in a prose link's target alone does not anchor parsing."""
        text = "# Changelog\n\n## 1.0.1\n\n- Fixed a regression, see [the diff](https://example.org/compare/v1.0.0...v1.0.1)."
        self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), "")

    def test_version_number_found(self):
        """Test that a changelog with the version number returns the text after the version number."""
        v1_change = "Version 1.0\n\n- Fixed ...\n- Changed ..."
        self.assertEqual(get_version_changes_from_changelog(f"Changelog\n\n{v1_change}", "1.0"), v1_change)

    def test_prose_mention_of_version_does_not_anchor_parsing(self):
        """Test that a prose mention in a newer section doesn't anchor parsing there, at any heading level."""
        for level in ("#", "##", "###"):
            with self.subTest(level=level):
                v2_change = f"{level} [2.0.0]\n\n- A feature, completing the work started in 1.0.0."
                v1_change = f"{level} [1.0.0]\n\n- Fixed ...\n- Changed ..."
                text = f"# Changelog\n\n{v2_change}\n\n{v1_change}\n\n{level} [0.9.0]\n\n- Fixed ...\n"
                self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), v1_change)

    @kills.force_false(
        _underlines_the_line_above,
        "index > 0 and _heading_level(lines, index - 1) == lines[index][:1]",
        "a changelog underlining its headings with backticks or tildes reports no changes at all, its file read "
        "as one fenced code block",
        case="`",
    )
    @kills.replace(
        _find_version_index,
        "if _heading_level(lines, index):",
        'if line.startswith("#"):',
        "a version named in a newer entry's prose anchors the changes reported for it",
        case="=",
    )
    @kills.replace(
        restructured_text,
        "_ADORNMENT_CHARACTERS: Final[frozenset[str]] = frozenset(string.punctuation)",
        '_ADORNMENT_CHARACTERS: Final[frozenset[str]] = frozenset("=-~")',
        "a changelog underlining its headings with a character reStructuredText allows but does not recommend "
        "reports the changes from where a newer entry's prose names the version",
        case="!",
    )
    @kills.each(
        _SPEC_ADORNMENT_CHARACTERS,
        lambda adornment: kills.replace(
            restructured_text,
            "_ADORNMENT_CHARACTERS: Final[frozenset[str]] = frozenset(string.punctuation)",
            f"_ADORNMENT_CHARACTERS: Final[frozenset[str]] = frozenset(string.punctuation) - {{{adornment!r}}}",
            f"a changelog underlining its headings with {adornment} reports the changes from where a newer entry's "
            "prose names the version, though reStructuredText allows that underline",
            case=adornment,
        ),
    )
    def test_underlined_heading_anchors_parsing_rather_than_a_prose_mention(self):
        """Test that a prose mention doesn't anchor parsing when a heading names the version, whatever underlines it."""
        for adornment in _SPEC_ADORNMENT_CHARACTERS:
            with self.subTest(adornment=adornment):
                underline = adornment * 5
                v2_change = f"2.0.0\n{underline}\n\n- A feature, completing the work started in 1.0.0."
                v1_change = f"1.0.0\n{underline}\n\n- Fixed ...\n- Changed ..."
                text = f"Changelog\n\n{v2_change}\n\n{v1_change}\n\n0.9.0\n{underline}\n\n- Fixed ...\n"
                self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), v1_change)

    def test_repeated_prose_mention_without_heading_anchors_on_first(self):
        """Test that without a heading, parsing anchors on the first of several prose mentions."""
        text = "Upgrade to 1.0.0 is recommended.\nThe 1.0.0 release fixes things."
        self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), text)

    @kills.delete(
        _names_version,
        "{_VERSION_START}",
        "a changelog naming a longer version that ends with this one reports the longer version's changes",
    )
    def test_longer_version_does_not_anchor_parsing(self):
        """Test that a version spelled at the end of a longer version doesn't anchor parsing at the longer one."""
        v2_change = "## 11.0\n\n- Fixed the packaging."
        v1_change = "## 1.0\n\n- Fixed ...\n- Changed ..."
        text = f"# Changelog\n\n{v2_change}\n\n{v1_change}\n\n## 0.9.0\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    @kills.replace(
        _find_anchor,
        'version.endswith(".0") and version.count(".") > 1:\n        short_version = version.removesuffix(".0")',
        'version.count(".") > 1:\n        short_version = version.rsplit(".", 1)[0]',
        "a version that is no `.0` release reports the changes of the release one component shorter",
    )
    def test_only_a_dot_zero_version_is_found_under_a_shorter_heading(self):
        """Test that only a version ending in `.0` is looked up under a heading naming one component fewer."""
        v1_change = "## 1.11\n\n- Fixed ...\n- Changed ..."
        text = f"# Changelog\n\n{v1_change}\n\n## 1.10\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.11.0"), v1_change)
        self.assertEqual(get_version_changes_from_changelog(text, "1.11.2"), "")

    def test_dot_zero_version_is_found_under_a_shorter_version_named_in_prose(self):
        """Test that a changelog without headings reports a `.0` version under the shorter version its prose names."""
        v1_change = "Version 1.11 released 2026-04-22\n\n- Fixed ...\n- Changed ..."
        text = f"Changes\n\n{v1_change}\n\nVersion 1.10 released 2026-03-08\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.11.0"), v1_change)

    @kills.force_false(
        _find_anchor,
        "start is not None and _heading_level(lines, start)",
        "a changelog heading both a version and the release one component shorter reports the shorter one's changes",
    )
    def test_heading_naming_the_version_wins_over_a_shorter_heading(self):
        """Test that a heading naming the version anchors parsing rather than one naming a component fewer."""
        v1_change = "### 1.11.0\n\n- Fixed ...\n- Changed ..."
        text = f"# Changelog\n\n## 1.11\n\nThe 1.11 series.\n\n{v1_change}\n\n## 1.10\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.11.0"), v1_change)

    @kills.delete(
        _find_anchor,
        " and _heading_level(lines, start)",
        "a changelog heading a release one component shorter than the version, and naming the version in its prose, "
        "reports the prose line alone",
    )
    def test_shorter_heading_anchors_parsing_rather_than_prose_naming_the_dot_zero_version(self):
        """Test that a heading naming one component fewer anchors parsing rather than prose naming the version."""
        v1_change = "## Example 1.11\n\nWe released example 1.11.0.\n\n### New feature\n\n- Added ..."
        text = f"# Changelog\n\n{v1_change}\n\n## Example 1.10\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.11.0"), v1_change)

    @kills.delete(
        _find_anchor,
        " and (start is None or _heading_level(lines, short_start))",
        "a changelog without heading markup reports a `.0` version from wherever the version one component shorter "
        "is first named",
    )
    def test_dot_zero_version_line_anchors_parsing_rather_than_prose_naming_a_shorter_version(self):
        """Test that without headings, a `.0` version's line anchors parsing, not a line naming a component fewer."""
        v1_change = "Version 1.11.0 released 2026-04-22\n\n- Fixed ...\n- Changed ..."
        v2_change = "Version 1.12.0 released 2026-05-01\n\n- Dropped support for 1.11."
        text = f"Changes\n\n{v2_change}\n\n{v1_change}\n\nVersion 1.10.0 released 2026-03-08\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.11.0"), v1_change)

    @kills.delete(
        _find_anchor,
        ' and version.count(".") > 1',
        "a changelog naming a two-component version nowhere reports the entry a stray digit sits in",
    )
    def test_two_component_version_is_not_found_under_a_bare_component(self):
        """Test that a version of two components is not looked up under a bare component, which a stray digit names."""
        text = "# Changelog\n\n## 2.0\n\n- Fixed 1 bug.\n\n## 0.9\n\n- Changed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "2.0"), "## 2.0\n\n- Fixed 1 bug.")
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), "")

    @kills.delete(
        _find_version_index,
        "index not in fenced and ",
        "a version heading inside a fenced code block anchors the changes reported for that version",
    )
    def test_heading_inside_a_fenced_code_block_does_not_anchor_parsing(self):
        """Test that a version heading inside a fenced code block, being code, does not anchor parsing there."""
        v2_change = "## 2.0\n\n- Entries are now written as:\n\n```markdown\n## 1.0\n\n- Fixed ...\n```"
        v1_change = "## 1.0\n\n- The real 1.0 entry."
        text = f"# Changelog\n\n{v2_change}\n\n{v1_change}\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    def test_version_in_footer_link_does_not_anchor_parsing(self):
        """Test that a version mention in a footer comparison link doesn't anchor parsing there."""
        v1_change = "## [1.0.0]\n\n- Fixed ...\n- Changed ..."
        footer = "[1.0.0]: https://example.org/compare/v0.9.0...v1.0.0"
        text = f"# Changelog\n\n{v1_change}\n\n## [0.9.0]\n\n- Fixed ...\n\n{footer}\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), v1_change)

    def test_version_in_heading_link_target_does_not_anchor_parsing(self):
        """Test that a newer version's heading, whose comparison link targets the version, doesn't anchor parsing."""
        v1_0_1_change = "## [1.0.1](https://example.org/compare/v1.0.0...v1.0.1)\n\n- Fixed ..."
        v1_change = "## [1.0.0](https://example.org/compare/v0.9.0...v1.0.0)\n\n- Added ..."
        v0_9_change = "## [0.9.0](https://example.org/compare/v0.8.0...v0.9.0)\n\n- Changed ..."
        text = f"# Changelog\n\n{v1_0_1_change}\n\n{v1_change}\n\n{v0_9_change}\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), v1_change)

    @kills.replace(
        _names_version,
        "without_restructured_text_link_targets(without_markdown_link_targets(line))",
        "without_markdown_link_targets(line)",
        "a reStructuredText changelog linking each release heading to a comparison with the release before it "
        "reports the newer release's changes first",
    )
    def test_version_in_underlined_heading_link_target_does_not_anchor_parsing(self):
        """Test that a newer version's underlined heading, whose link targets the version, doesn't anchor parsing."""
        v1_0_1_change = "`1.0.1 <https://example.org/compare/v1.0.0...v1.0.1>`_\n=====\n\n- Fixed ..."
        v1_change = "`1.0.0 <https://example.org/compare/v0.9.0...v1.0.0>`_\n=====\n\n- Added ..."
        v0_9_change = "`0.9.0 <https://example.org/compare/v0.8.0...v0.9.0>`_\n=====\n\n- Changed ..."
        text = f"Changelog\n\n{v1_0_1_change}\n\n{v1_change}\n\n{v0_9_change}\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), v1_change)


class SectionEndTest(unittest.TestCase):
    """Unit tests for the line a version's changes end at."""

    def test_skip_older_versions(self):
        """Test that an older version's entry, which sits below the version's own, is left out."""
        v1_change = "## Version 1.0\n\n- Fixed ...\n- Changed ..."
        text = f"Changelog\n\n{v1_change}\n\n## Version 0.9\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    def test_next_version_heading_ends_the_section_at_any_level(self):
        """Test that a section ends at the next version's Markdown heading, at whatever level that heading is."""
        for level in ("#", "##", "###"):
            with self.subTest(level=level):
                v1_change = f"{level} 1.0\n\n- Fixed ..."
                text = f"# Changelog\n\n{v1_change}\n\n{level} 0.9\n\n- Changed ...\n"
                self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    def test_deeper_markdown_heading_does_not_end_the_section(self):
        """Test that a Markdown subsection, headed one level deeper than the version, does not end the section."""
        v1_change = "## 1.0\n\n### Fixed\n\n- Fixed ..."
        text = f"# Changelog\n\n{v1_change}\n\n## 0.9\n\n- Changed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    @kills.delete(
        _next_version_index,
        "or _heads_a_longer_version(heading_level, line, version)",
        "a changelog heading a longer version inside a version's section reports that longer version's entry as well",
    )
    def test_deeper_heading_naming_a_longer_version_ends_the_section(self):
        """Test that a section reached through a shorter heading ends at a subsection naming a longer version."""
        v1_change = "## 1.11\n\n- Fixed ..."
        text = f"# Changelog\n\n{v1_change}\n\n### 1.11.1\n\n- Fixed a regression.\n\n## 1.10\n\n- Changed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.11.0"), v1_change)

    @kills.delete(
        markdown,
        ' "~~~"',
        "a changelog fencing a code block with tildes reports the fence as the end of the version's changes",
    )
    def test_heading_inside_a_fenced_code_block_does_not_end_the_section(self):
        """Test that a heading marker inside a code block fenced with tildes is code, so it does not end the section."""
        code_block = "~~~sh\n# install the parser\npip install parser\n~~~"
        v1_change = f"## 1.0\n\n- Fixed the parser. Install it with:\n\n{code_block}"
        text = f"# Changelog\n\n{v1_change}\n\n## 0.9\n\n- Changed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    def test_shallower_heading_ends_the_section(self):
        """Test that a section ends at a heading shallower than the version's, such as a footer's."""
        v1_change = "## 1.0\n\n- Fixed ..."
        text = f"# Changelog\n\n{v1_change}\n\n# Links\n\n[1.0]: https://example.org/releases/1.0\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    def test_underlined_heading_of_next_version_ends_the_section(self):
        """Test that a section ends at the next version's underlined heading, as reStructuredText spells one."""
        v1_change = "1.0\n===\n\n- Fixed ...\n- Changed ..."
        text = f"Changelog\n=========\n\n{v1_change}\n\n0.9\n===\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    @kills.force_false(
        restructured_text.is_label,
        "_LABEL.fullmatch(line) is not None",
        "a reStructuredText changelog's changes end with the label of the next version's section",
        case="reStructuredText label",
    )
    @kills.force_false(
        is_anchor,
        "_ANCHOR.fullmatch(line) is not None",
        "a towncrier changelog's changes end with the HTML anchor of the next version's section",
        case="HTML anchor",
    )
    def test_changes_end_before_the_anchor_of_the_next_section(self):
        """Test that the changes leave out the anchor a changelog puts above the next version's heading."""
        cases = {
            "reStructuredText label": ("1.0\n===\n\n- Fixed ...", ".. _v0-9:\n\n0.9\n===\n\n- Changed ...\n"),
            "HTML anchor": ("## 1.0\n\n- Fixed ...", '<a name="release-0-9"></a>\n\n## 0.9\n\n- Changed ...\n'),
        }
        for case, (v1_change, next_section) in cases.items():
            with self.subTest(case=case):
                text = f"Changelog\n\n{v1_change}\n\n{next_section}"
                self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    @kills.force_false(
        get_version_changes_from_changelog,
        "not entries",
        "a version whose section holds its heading alone is reported with the heading as its changes",
        case="Markdown heading",
    )
    @kills.replace(
        _heading_length,
        "return 1 if heading",
        "return 0 if heading",
        "a version whose section holds its Markdown heading alone is reported with the heading as its changes",
        case="Markdown heading",
    )
    @kills.delete(
        _heading_length,
        " if heading(lines[index]) else 2",
        "a version whose section holds its underlined heading alone is reported with the underline as its changes",
        case="underlined heading",
    )
    def test_section_holding_only_its_heading_has_no_changes(self):
        """Test that a section holding nothing but its heading has no changes, whether Markdown or underlined."""
        cases = {"Markdown heading": "## 1.0\n\n## 0.9\n", "underlined heading": "1.0\n===\n\n0.9\n===\n"}
        for case, sections in cases.items():
            with self.subTest(case=case):
                text = f"Changelog\n\n{sections}\n- Changed ...\n"
                self.assertEqual(get_version_changes_from_changelog(text, "1.0"), "")

    @kills.replace(
        _heading_length,
        "return 0",
        "return 1",
        "a changelog naming the version in a line of prose alone reports its changes as empty",
    )
    def test_prose_line_naming_the_version_alone_is_the_changes(self):
        """Test that a line of prose naming the version is its changes when the next heading follows it."""
        v1_change = "Version 1.0 fixes the crash on startup."
        text = f"Changes\n\n{v1_change}\n\n## 0.9\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    def test_subsection_heading_does_not_end_the_section(self):
        """Test that a subsection, underlined with another character than the version, does not end the section."""
        v1_change = "1.0\n===\n\nBugs fixed\n----------\n\n- Fixed ..."
        text = f"Changelog\n=========\n\n{v1_change}\n\n0.9\n===\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    @kills.delete(
        heading_underline,
        "title.strip()\n        and ",
        "a run of punctuation that underlines nothing cuts the entry short where it stands",
    )
    def test_punctuation_run_underlining_nothing_does_not_end_the_section(self):
        """Test that a run of punctuation with a blank line above it, underlining nothing, does not end the section."""
        v1_change = "1.0\n===\n\n- Fixed ...\n\n===\n\n- Changed ..."
        text = f"Changelog\n\n{v1_change}\n\n0.9\n===\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    @kills.replace(
        heading_underline,
        "underline = line.rstrip()",
        "underline = line.strip()",
        "a run of punctuation indented inside a literal block cuts the entry short above it",
    )
    def test_indented_punctuation_run_does_not_end_the_section(self):
        """Test that a run of punctuation indented out of column 1, as a literal block holds, does not end it."""
        v1_change = "1.0\n===\n\nExample::\n\n    text\n    ====\n\n- Fixed ..."
        text = f"Changelog\n\n{v1_change}\n\n0.9\n===\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    @kills.delete(
        _version_line_prefix,
        "None if _heading_level(lines, index) else ",
        "a heading-anchored entry ends where it names another version in prose",
    )
    def test_version_line_does_not_end_a_heading_anchored_section(self):
        """Test that a line naming a longer version in prose does not end a section a heading introduced."""
        v1_change = "1.0\n===\n\n- Fixed ...\n1.0.1 was never released.\n- Changed ..."
        text = f"Changelog\n\n{v1_change}\n\n0.9\n===\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    @kills.replace(
        _heads_another_version,
        "if not line.startswith(prefix):\n        return False\n    other_version = _VERSION.match(line, len(prefix))",
        "other_version = _VERSION.search(line)",
        "an entry naming a version in prose ends there, reporting the version's own line alone",
    )
    def test_prose_naming_a_version_does_not_end_the_section(self):
        """Test that an entry line naming a version in prose does not end a section without heading markup."""
        v1_change = "Version 1.0 released 2026-04-22\n\n- Dropped support for Python 2.7.\n- Fixed ..."
        text = f"Changes\n\n{v1_change}\n\nVersion 0.9 released 2026-03-08\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    def test_next_version_line_ends_the_section_without_heading_markup(self):
        """Test that without heading markup, a section ends where another version follows the same text."""
        version_lines = (("1.0", "0.9"), ("Version 1.0 released 2026-04-22", "Version 0.9 released 2026-03-08"))
        for v1_line, v0_line in version_lines:
            with self.subTest(version_line=v1_line):
                v1_change = f"{v1_line}\n\n- Fixed ...\n- Changed ..."
                text = f"Changelog\n\n{v1_change}\n\n{v0_line}\n\n- Fixed ...\n"
                self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    def test_prose_anchored_section_ends_at_the_next_heading(self):
        """Test that a section a prose mention introduced ends at the next Markdown heading, however deep."""
        v1_change = "Upgrading to 1.0 is recommended.\n\n- Fixed ...\n- Changed ..."
        text = f"# Changelog\n\n{v1_change}\n\n### 0.9\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    @kills.replace(
        changelog,
        "_MAX_LENGTH: Final[int] = 30",
        "_MAX_LENGTH: Final[int] = 20",
        "a long changelog entry is cut at 20 lines rather than at the 30 the module sets",
    )
    def test_maximum_length(self):
        """Test that a changelog naming one version only is cut at the maximum length, with a `...` indicator."""
        section = ["1.0", "", *_MANY_ENTRIES]
        text = f"Changes\n\n{'\n'.join(section)}\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0").splitlines(), [*section[:30], "..."])

    def test_maximum_length_is_not_applied_when_previous_version_is_found(self):
        """Test that the maximum length is not applied if the previous version is found."""
        v1_change = "\n".join(["## Version 1.0", "", *_MANY_ENTRIES])
        text = f"Changelog\n\n{v1_change}\n\n## Version 0.9\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    @kills.delete(
        _next_version_index,
        "or _names_version(line, version)",
        "a changelog naming a version in two headings reports the first of them's entry alone",
    )
    def test_second_heading_naming_the_version_does_not_end_the_section(self):
        """Test that a later heading naming the version itself, rather than another version, does not end it."""
        v1_change = "## 1.0\n\n- Fixed ...\n\n## 1.0 (yanked)\n\n- Yanked for a broken wheel."
        text = f"# Changelog\n\n{v1_change}\n\n## 0.9\n\n- Changed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    @kills.delete(
        _names_version,
        r"?\w",
        "a changelog heading a release candidate below its release reports the candidate's entry with the release's",
    )
    def test_heading_of_longer_version_ends_the_section(self):
        """Test that a section ends at a heading naming a longer version, such as a release candidate's."""
        v1_change = "## 1.0.0\n\n- Fixed ...\n- Changed ..."
        text = f"# Changelog\n\n{v1_change}\n\n## 1.0.0rc1\n\n- A release candidate.\n\n## 0.9.0\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), v1_change)
