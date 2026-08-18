"""Unit tests for the changelog parsing."""

import unittest

from update_time.domain import changelog
from update_time.domain.changelog import get_version_changes_from_changelog

from tests.mutation import Mutation, kills

# The characters reStructuredText allows a heading to be underlined with, as the specification lists them at
# https://docutils.sourceforge.io/docs/ref/rst/restructuredtext.html#sections.
_SPEC_ADORNMENT_CHARACTERS = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"


class GetChangeFromChangelogTest(unittest.TestCase):
    """Unit tests for getting the change for a version from a changelog."""

    def test_empty_changelog(self):
        """Test that an empty changelog results in an empty change."""
        self.assertEqual(get_version_changes_from_changelog("", "1.0"), "")

    _WHOLE_FILE = Mutation(
        changelog,
        '    if start is None:\n        return ""',
        '    if start is None:\n        return "\\n".join(all_lines)',
        "a changelog file that does not name the version reports its head as this version's changes",
    )

    @kills(_WHOLE_FILE)
    def test_version_number_not_found(self):
        """Test that a changelog that does not name the version yields no changes."""
        text = "Changelog\n\n## Version 0.9\n\n- Fixed ..."
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), "")

    def test_version_number_found(self):
        """Test that a changelog with the version number returns the text after the version number."""
        v1_change = "Version 1.0\n\n- Fixed ...\n- Changed ..."
        text = f"Changelog\n\n{v1_change}"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    def test_skip_older_versions(self):
        """Test that a older versions are not included."""
        v1_change = "## Version 1.0\n\n- Fixed ...\n- Changed ..."
        text = f"Changelog\n\n{v1_change}\n\n## Version 0.9\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _LEVEL_TWO = Mutation(
        changelog,
        '    if hashes and after_hashes.startswith(" "):',
        '    if hashes == "##" and after_hashes.startswith(" "):',
        "a changelog heading its versions at another level than two runs on into the previous version",
    )

    @kills(_LEVEL_TWO)
    def test_next_version_heading_ends_the_section_at_any_level(self):
        """Test that a section ends at the next version's Markdown heading, at whatever level that heading is."""
        for level in ("#", "##", "###"):
            with self.subTest(level=level):
                v1_change = f"{level} 1.0\n\n- Fixed ..."
                text = f"# Changelog\n\n{v1_change}\n\n{level} 0.9\n\n- Changed ...\n"
                self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _DEEPER_LEVEL = Mutation(
        changelog,
        "        if _heading_level(all_lines, index) == level and version not in line:",
        "        if _heading_level(all_lines, index).startswith(level) and version not in line:",
        "a Markdown entry ends at its first subsection, reporting the version's heading alone",
    )

    @kills(_DEEPER_LEVEL)
    def test_deeper_markdown_heading_does_not_end_the_section(self):
        """Test that a Markdown subsection, headed one level deeper than the version, does not end the section."""
        v1_change = "## 1.0\n\n### Fixed\n\n- Fixed ..."
        text = f"# Changelog\n\n{v1_change}\n\n## 0.9\n\n- Changed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _NO_UNDERLINE = Mutation(
        changelog,
        "    return _underline_character(lines, index)",
        '    return ""',
        "a reStructuredText changelog reports the previous version's entry along with this version's",
    )

    @kills(_NO_UNDERLINE)
    def test_underlined_heading_of_next_version_ends_the_section(self):
        """Test that a section ends at the next version's underlined heading, as reStructuredText spells one."""
        v1_change = "1.0\n===\n\n- Fixed ...\n- Changed ..."
        text = f"Changelog\n=========\n\n{v1_change}\n\n0.9\n===\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _ANY_LEVEL = Mutation(
        changelog,
        "        if _heading_level(all_lines, index) == level and version not in line:",
        "        if _heading_level(all_lines, index) and version not in line:",
        "a reStructuredText section ends at its first subsection, reporting the version's heading alone",
    )

    @kills(_ANY_LEVEL)
    def test_subsection_heading_does_not_end_the_section(self):
        """Test that a subsection, underlined with another character than the version, does not end the section."""
        v1_change = "1.0\n===\n\nBugs fixed\n----------\n\n- Fixed ..."
        text = f"Changelog\n=========\n\n{v1_change}\n\n0.9\n===\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _NO_BLANK_GUARD = Mutation(
        changelog,
        "    if not line.strip():",
        "    if False:",
        "a run of punctuation that underlines nothing cuts the entry short where it stands",
    )

    @kills(_NO_BLANK_GUARD)
    def test_punctuation_run_underlining_nothing_does_not_end_the_section(self):
        """Test that a run of punctuation with a blank line above it, underlining nothing, does not end the section."""
        v1_change = "1.0\n===\n\n- Fixed ...\n\n===\n\n- Changed ..."
        text = f"Changelog\n\n{v1_change}\n\n0.9\n===\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _STRIPPED_UNDERLINE = Mutation(
        changelog,
        '    below = lines[index + 1].rstrip() if index + 1 < len(lines) else ""',
        '    below = lines[index + 1].strip() if index + 1 < len(lines) else ""',
        "a run of punctuation indented inside a literal block cuts the entry short above it",
    )

    @kills(_STRIPPED_UNDERLINE)
    def test_indented_punctuation_run_does_not_end_the_section(self):
        """Test that a run of punctuation indented out of column 1, as a literal block holds, does not end it."""
        v1_change = "1.0\n===\n\nExample::\n\n    text\n    ===\n\n- Fixed ..."
        text = f"Changelog\n\n{v1_change}\n\n0.9\n===\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _FEW_ADORNMENTS = Mutation(
        changelog,
        "_ADORNMENT_CHARACTERS = frozenset(string.punctuation)",
        '_ADORNMENT_CHARACTERS = frozenset("=-~")',
        "a section underlined with a character reStructuredText allows but does not recommend runs on into the "
        "previous version",
    )

    @kills(_FEW_ADORNMENTS)
    def test_every_adornment_character_ends_the_section(self):
        """Test that a heading underlined with any character reStructuredText allows ends the section."""
        for adornment in _SPEC_ADORNMENT_CHARACTERS:
            with self.subTest(adornment=adornment):
                underline = adornment * 3
                v1_change = f"1.0\n{underline}\n\n- Fixed ..."
                text = f"Changelog\n\n{v1_change}\n\n0.9\n{underline}\n\n- Changed ...\n"
                self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    def test_max_length(self):
        """Test that a change longer than max_length lines is truncated to that many lines, with a `...` indicator."""
        v1_change = "## Version 1.0\n\n- Fixed ...\n- Changed ..."
        text_after_v1 = "# Some other header\n\n- Some bullet point.\n"
        text = f"Changelog\n\n{v1_change}\n\n{text_after_v1}"
        # max_length counts lines: the first 3 lines are kept and a `...` line marks where the rest was cut.
        expected_v1_change = "## Version 1.0\n\n- Fixed ...\n..."
        self.assertEqual(get_version_changes_from_changelog(text, "1.0", max_length=3), expected_v1_change)

    def test_max_length_is_not_applied_when_previous_version_is_found(self):
        """Test that the max length is not applied if the previous version is found."""
        v1_change = "## Version 1.0\n\n- Fixed ...\n- Changed ..."
        text = f"Changelog\n\n{v1_change}\n\n## Version 0.9\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0", max_length=3), v1_change)

    def test_prose_mention_of_version_does_not_anchor_parsing(self):
        """Test that a prose mention of the version in a newer section doesn't anchor parsing there."""
        v2_change = "## [2.0.0]\n\n- A feature, completing the work started in 1.0.0."
        v1_change = "## [1.0.0]\n\n- Fixed ...\n- Changed ..."
        text = f"# Changelog\n\n{v2_change}\n\n{v1_change}\n\n## [0.9.0]\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), v1_change)

    _HASH_ANCHOR = Mutation(
        changelog,
        "            if _heading_level(lines, index):",
        '            if line.startswith("#"):',
        "a version named in a newer entry's prose anchors the changes reported for it",
    )

    @kills(_HASH_ANCHOR)
    def test_underlined_heading_anchors_parsing_rather_than_a_prose_mention(self):
        """Test that a prose mention in a newer entry doesn't anchor parsing when an underlined heading names it."""
        v2_change = "2.0.0\n=====\n\n- A feature, completing the work started in 1.0.0."
        v1_change = "1.0.0\n=====\n\n- Fixed ...\n- Changed ..."
        text = f"Changelog\n\n{v2_change}\n\n{v1_change}\n\n0.9.0\n=====\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), v1_change)

    def test_repeated_prose_mention_without_heading_anchors_on_first(self):
        """Test that without a heading, parsing anchors on the first of several prose mentions."""
        text = "Upgrade to 1.0.0 is recommended.\nThe 1.0.0 release fixes things."
        self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), text)

    def test_version_in_footer_link_does_not_anchor_parsing(self):
        """Test that a version mention in a footer comparison link doesn't anchor parsing there."""
        v1_change = "## [1.0.0]\n\n- Fixed ...\n- Changed ..."
        footer = "[1.0.0]: https://example.org/compare/v0.9.0...v1.0.0"
        text = f"# Changelog\n\n{v1_change}\n\n## [0.9.0]\n\n- Fixed ...\n\n{footer}\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), v1_change)
