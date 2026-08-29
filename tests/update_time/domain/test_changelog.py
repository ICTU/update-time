"""Unit tests for the changelog parsing."""

import unittest

from update_time.domain import changelog
from update_time.domain.changelog import get_version_changes_from_changelog

from tests.mutation import Mutation, kills

# The characters reStructuredText allows a heading to be underlined with, as the specification lists them at
# https://docutils.sourceforge.io/docs/ref/rst/restructuredtext.html#sections.
_SPEC_ADORNMENT_CHARACTERS = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
# More entry lines than the changelog module cuts a version's changes at.
_MANY_ENTRIES = [f"- Fixed thing {number}." for number in range(40)]


class VersionAnchorTest(unittest.TestCase):
    """Unit tests for the line a version's changes start at."""

    def test_empty_changelog(self):
        """Test that an empty changelog results in an empty change."""
        self.assertEqual(get_version_changes_from_changelog("", "1.0"), "")

    def test_version_number_not_found(self):
        """Test that a changelog that does not name the version yields no changes."""
        text = "Changelog\n\n## Version 0.9\n\n- Fixed ..."
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), "")

    def test_version_number_found(self):
        """Test that a changelog with the version number returns the text after the version number."""
        v1_change = "Version 1.0\n\n- Fixed ...\n- Changed ..."
        text = f"Changelog\n\n{v1_change}"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _LEVEL_TWO = Mutation(
        changelog,
        '    if hashes and after_hashes.startswith(" "):',
        '    if hashes == "##" and after_hashes.startswith(" "):',
        "a changelog heading its versions at another level than two reports the changes from where a newer entry's "
        "prose names the version",
    )

    @kills(_LEVEL_TWO)
    def test_prose_mention_of_version_does_not_anchor_parsing(self):
        """Test that a prose mention in a newer section doesn't anchor parsing there, at any heading level."""
        for level in ("#", "##", "###"):
            with self.subTest(level=level):
                v2_change = f"{level} [2.0.0]\n\n- A feature, completing the work started in 1.0.0."
                v1_change = f"{level} [1.0.0]\n\n- Fixed ...\n- Changed ..."
                text = f"# Changelog\n\n{v2_change}\n\n{v1_change}\n\n{level} [0.9.0]\n\n- Fixed ...\n"
                self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), v1_change)

    _HASH_ANCHOR = Mutation(
        changelog,
        "            if _heading_level(lines, index):",
        '            if line.startswith("#"):',
        "a version named in a newer entry's prose anchors the changes reported for it",
    )

    _NO_UNDERLINE = Mutation(
        changelog,
        "    return _underline_character(lines, index)",
        '    return ""',
        "a reStructuredText changelog reports the changes from where a newer entry's prose names the version",
    )

    _FEW_ADORNMENTS = Mutation(
        changelog,
        "_ADORNMENT_CHARACTERS = frozenset(string.punctuation)",
        '_ADORNMENT_CHARACTERS = frozenset("=-~")',
        "a changelog underlining its headings with a character reStructuredText allows but does not recommend "
        "reports the changes from where a newer entry's prose names the version",
    )

    _FENCE_OVER_UNDERLINE = Mutation(
        changelog,
        "    return index > 0 and _heading_level(lines, index - 1) == lines[index][:1]",
        "    return False",
        "a changelog underlining its headings with backticks or tildes reports no changes at all, its file read "
        "as one fenced code block",
    )

    @kills(_FENCE_OVER_UNDERLINE, _HASH_ANCHOR, _NO_UNDERLINE, _FEW_ADORNMENTS)
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

    _NO_LOOKAHEAD = Mutation(
        changelog,
        '    return re.search(rf"{_VERSION_START}{re.escape(version)}(?!\\.?\\w)", line) is not None',
        '    return re.search(rf"{_VERSION_START}{re.escape(version)}", line) is not None',
        "a changelog naming a longer version that starts with this one reports the longer version's changes",
    )

    _NO_LOOKBEHIND = Mutation(
        changelog,
        '    return re.search(rf"{_VERSION_START}{re.escape(version)}(?!\\.?\\w)", line) is not None',
        '    return re.search(rf"{re.escape(version)}(?!\\.?\\w)", line) is not None',
        "a changelog naming a longer version that ends with this one reports the longer version's changes",
    )

    @kills(_NO_LOOKAHEAD, _NO_LOOKBEHIND)
    def test_longer_version_does_not_anchor_parsing(self):
        """Test that a version spelled inside a longer version doesn't anchor parsing at the longer one."""
        for version, longer_version in (("1.0.0", "1.0.0.post0"), ("1.0", "11.0")):
            with self.subTest(longer_version=longer_version):
                v2_change = f"## {longer_version}\n\n- Fixed the packaging."
                v1_change = f"## {version}\n\n- Fixed ...\n- Changed ..."
                text = f"# Changelog\n\n{v2_change}\n\n{v1_change}\n\n## 0.9.0\n\n- Fixed ...\n"
                self.assertEqual(get_version_changes_from_changelog(text, version), v1_change)

    _NO_SHORTER_HEADING = Mutation(
        changelog,
        '    if start is None and version.endswith(".0") and version.count(".") > 1:\n'
        '        version = version.removesuffix(".0")\n'
        "        start = _find_version_index(all_lines, version)",
        "",
        "a changelog heading a release one component shorter than the version reports no changes for it",
    )

    _SHORTEN_ANY_VERSION = Mutation(
        changelog,
        '    if start is None and version.endswith(".0") and version.count(".") > 1:\n'
        '        version = version.removesuffix(".0")',
        '    if start is None and version.count(".") > 1:\n        version = version.rsplit(".", 1)[0]',
        "a version that is no `.0` release reports the changes of the release one component shorter",
    )

    @kills(_NO_SHORTER_HEADING, _SHORTEN_ANY_VERSION)
    def test_only_a_dot_zero_version_is_found_under_a_shorter_heading(self):
        """Test that only a version ending in `.0` is looked up under a heading naming one component fewer."""
        v1_change = "## 1.11\n\n- Fixed ...\n- Changed ..."
        text = f"# Changelog\n\n{v1_change}\n\n## 1.10\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.11.0"), v1_change)
        self.assertEqual(get_version_changes_from_changelog(text, "1.11.2"), "")

    _SHORTER_VERSION_NOT_REBOUND = Mutation(
        changelog,
        '        version = version.removesuffix(".0")\n        start = _find_version_index(all_lines, version)',
        '        start = _find_version_index(all_lines, version.removesuffix(".0"))',
        "a changelog without heading markup naming a shorter version raises instead of reporting its changes",
        raises="ValueError: substring not found",
    )

    @kills(_SHORTER_VERSION_NOT_REBOUND)
    def test_dot_zero_version_is_found_under_a_shorter_version_named_in_prose(self):
        """Test that a changelog without headings reports a `.0` version under the shorter version its prose names."""
        v1_change = "Version 1.11 released 2026-04-22\n\n- Fixed ...\n- Changed ..."
        text = f"Changes\n\n{v1_change}\n\nVersion 1.10 released 2026-03-08\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.11.0"), v1_change)

    _SHORTER_HEADING_TRIED_FIRST = Mutation(
        changelog,
        "    if start is None and",
        '    if _find_version_index(all_lines, version.removesuffix(".0")) is not None and',
        "a changelog heading both a version and the release one component shorter reports the shorter one's changes",
    )

    @kills(_SHORTER_HEADING_TRIED_FIRST)
    def test_heading_naming_the_version_wins_over_a_shorter_heading(self):
        """Test that a heading naming the version anchors parsing rather than one naming a component fewer."""
        v1_change = "### 1.11.0\n\n- Fixed ...\n- Changed ..."
        text = f"# Changelog\n\n## 1.11\n\nThe 1.11 series.\n\n{v1_change}\n\n## 1.10\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.11.0"), v1_change)

    _SHORTEN_TO_A_BARE_COMPONENT = Mutation(
        changelog,
        '    if start is None and version.endswith(".0") and version.count(".") > 1:',
        '    if start is None and version.endswith(".0"):',
        "a changelog naming a two-component version nowhere reports the entry a stray digit sits in",
    )

    @kills(_SHORTEN_TO_A_BARE_COMPONENT)
    def test_two_component_version_is_not_found_under_a_bare_component(self):
        """Test that a version of two components is not looked up under a bare component, which a stray digit names."""
        text = "# Changelog\n\n## 2.0\n\n- Fixed 1 bug.\n\n## 0.9\n\n- Changed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "2.0"), "## 2.0\n\n- Fixed 1 bug.")
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), "")

    _FENCED_ANCHOR = Mutation(
        changelog,
        "        if index not in fenced and _names_version(line, version):",
        "        if _names_version(line, version):",
        "a version heading inside a fenced code block anchors the changes reported for that version",
    )

    @kills(_FENCED_ANCHOR)
    def test_heading_inside_a_fenced_code_block_does_not_anchor_parsing(self):
        """Test that a version heading inside a fenced code block, being code, does not anchor parsing there."""
        example = "```markdown\n## 1.0\n\n- Fixed ...\n```"
        v2_change = f"## 2.0\n\n- Entries are now written as:\n\n{example}"
        v1_change = "## 1.0\n\n- The real 1.0 entry."
        text = f"# Changelog\n\n{v2_change}\n\n{v1_change}\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    def test_version_in_footer_link_does_not_anchor_parsing(self):
        """Test that a version mention in a footer comparison link doesn't anchor parsing there."""
        v1_change = "## [1.0.0]\n\n- Fixed ...\n- Changed ..."
        footer = "[1.0.0]: https://example.org/compare/v0.9.0...v1.0.0"
        text = f"# Changelog\n\n{v1_change}\n\n## [0.9.0]\n\n- Fixed ...\n\n{footer}\n"
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

    _DEEPER_LEVEL = Mutation(
        changelog,
        "    return heading_level == section_level or (",
        "    return heading_level.startswith(section_level) or (",
        "a Markdown entry ends at its first subsection, reporting the version's heading alone",
    )

    @kills(_DEEPER_LEVEL)
    def test_deeper_markdown_heading_does_not_end_the_section(self):
        """Test that a Markdown subsection, headed one level deeper than the version, does not end the section."""
        v1_change = "## 1.0\n\n### Fixed\n\n- Fixed ..."
        text = f"# Changelog\n\n{v1_change}\n\n## 0.9\n\n- Changed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _NO_LONGER_VERSION_END = Mutation(
        changelog,
        "            or _heads_a_longer_version(heading_level, line, version)",
        "",
        "a changelog heading a longer version inside a version's section reports that longer version's entry as well",
    )

    @kills(_NO_LONGER_VERSION_END)
    def test_deeper_heading_naming_a_longer_version_ends_the_section(self):
        """Test that a section reached through a shorter heading ends at a subsection naming a longer version."""
        v1_change = "## 1.11\n\n- Fixed ..."
        text = f"# Changelog\n\n{v1_change}\n\n### 1.11.1\n\n- Fixed a regression.\n\n## 1.10\n\n- Changed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.11.0"), v1_change)

    _ONLY_TILDE_FENCES = Mutation(
        changelog,
        '_FENCES = ("```", "~~~")',
        '_FENCES = ("~~~",)',
        "a changelog fencing a code block with backticks reports the fence as the end of the version's changes",
    )

    _ONLY_BACKTICK_FENCES = Mutation(
        changelog,
        '_FENCES = ("```", "~~~")',
        '_FENCES = ("```",)',
        "a changelog fencing a code block with tildes reports the fence as the end of the version's changes",
    )

    @kills(_ONLY_TILDE_FENCES, _ONLY_BACKTICK_FENCES)
    def test_heading_inside_a_fenced_code_block_does_not_end_the_section(self):
        """Test that a heading marker inside a fenced code block, which is code rather than a heading, ends none."""
        for fence in ("```", "~~~"):
            with self.subTest(fence=fence):
                code_block = f"{fence}sh\n# install the parser\npip install parser\n{fence}"
                v1_change = f"## 1.0\n\n- Fixed the parser. Install it with:\n\n{code_block}"
                text = f"# Changelog\n\n{v1_change}\n\n## 0.9\n\n- Changed ...\n"
                self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _EQUAL_LEVEL_ONLY = Mutation(
        changelog,
        '    return heading_level == section_level or (heading_level.startswith("#") '
        "and len(heading_level) < len(section_level))",
        "    return heading_level == section_level",
        "a changelog whose last version is followed by a shallower heading reports that heading as its changes",
    )

    @kills(_EQUAL_LEVEL_ONLY)
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

    _ANY_LEVEL = Mutation(
        changelog,
        "    return heading_level == section_level or (",
        "    return bool(heading_level) or (",
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

    _HEADING_PREFIX = Mutation(
        changelog,
        "    return None if _heading_level(lines, index) else line[: line.index(version)]",
        "    return line[: line.index(version)]",
        "a heading-anchored entry ends where it names another version in prose",
    )

    _PROSE_NAMING_A_LONGER_VERSION = Mutation(
        changelog,
        "    return bool(heading_level) and re.search",
        "    return re.search",
        "an entry ends where its prose names a longer version, reporting the lines above that alone",
    )

    @kills(_HEADING_PREFIX, _PROSE_NAMING_A_LONGER_VERSION)
    def test_version_line_does_not_end_a_heading_anchored_section(self):
        """Test that a line naming a longer version in prose does not end a section a heading introduced."""
        v1_change = "1.0\n===\n\n- Fixed ...\n1.0.1 was never released.\n- Changed ..."
        text = f"Changelog\n\n{v1_change}\n\n0.9\n===\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _ANY_VERSION_LINE = Mutation(
        changelog,
        "    if not line.startswith(prefix):\n        return False\n"
        "    other_version = _VERSION.match(line, len(prefix))",
        "    other_version = _VERSION.search(line)",
        "an entry naming a version in prose ends there, reporting the version's own line alone",
    )

    @kills(_ANY_VERSION_LINE)
    def test_prose_naming_a_version_does_not_end_the_section(self):
        """Test that an entry line naming a version in prose does not end a section without heading markup."""
        v1_change = "Version 1.0 released 2026-04-22\n\n- Dropped support for Python 2.7.\n- Fixed ..."
        text = f"Changes\n\n{v1_change}\n\nVersion 0.9 released 2026-03-08\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _NO_PREFIX = Mutation(
        changelog,
        "    return None if _heading_level(lines, index) else line[: line.index(version)]",
        '    return None if _heading_level(lines, index) else ""',
        "a changelog naming its versions with text around them runs on into the previous version",
    )

    @kills(_NO_PREFIX)
    def test_next_version_line_ends_the_section_without_heading_markup(self):
        """Test that without heading markup, a section ends where another version follows the same text."""
        version_lines = (("1.0", "0.9"), ("Version 1.0 released 2026-04-22", "Version 0.9 released 2026-03-08"))
        for v1_line, v0_line in version_lines:
            with self.subTest(version_line=v1_line):
                v1_change = f"{v1_line}\n\n- Fixed ...\n- Changed ..."
                text = f"Changelog\n\n{v1_change}\n\n{v0_line}\n\n- Fixed ...\n"
                self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _NO_HEADING_BOUND = Mutation(
        changelog,
        "        return bool(heading_level)",
        "        return False",
        "a version named in prose rather than in a heading reports the rest of the changelog as its changes",
    )

    @kills(_NO_HEADING_BOUND)
    def test_prose_anchored_section_ends_at_the_next_heading(self):
        """Test that a section a prose mention introduced ends at the next Markdown heading, however deep."""
        v1_change = "Upgrading to 1.0 is recommended.\n\n- Fixed ...\n- Changed ..."
        text = f"# Changelog\n\n{v1_change}\n\n### 0.9\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _SHORTER_MAXIMUM = Mutation(
        changelog,
        "_MAX_LENGTH = 30",
        "_MAX_LENGTH = 20",
        "a long changelog entry is cut at 20 lines rather than at the 30 the module sets",
    )

    @kills(_SHORTER_MAXIMUM)
    def test_maximum_length(self):
        """Test that a changelog naming one version only is cut at the maximum length, with a `...` indicator."""
        section = ["1.0", "", *_MANY_ENTRIES]
        text = "Changes\n\n" + "\n".join(section) + "\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0").splitlines(), [*section[:30], "..."])

    def test_maximum_length_is_not_applied_when_previous_version_is_found(self):
        """Test that the maximum length is not applied if the previous version is found."""
        v1_change = "\n".join(["## Version 1.0", "", *_MANY_ENTRIES])
        text = f"Changelog\n\n{v1_change}\n\n## Version 0.9\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _NO_SAME_VERSION_SKIP = Mutation(
        changelog,
        "        if index in fenced or _names_version(line, version):",
        "        if index in fenced:",
        "a changelog naming a version in two headings reports the first of them's entry alone",
    )

    @kills(_NO_SAME_VERSION_SKIP)
    def test_second_heading_naming_the_version_does_not_end_the_section(self):
        """Test that a later heading naming the version itself, rather than another version, does not end it."""
        v1_change = "## 1.0\n\n- Fixed ...\n\n## 1.0 (yanked)\n\n- Yanked for a broken wheel."
        text = f"# Changelog\n\n{v1_change}\n\n## 0.9\n\n- Changed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0"), v1_change)

    _BOUNDARY_CONTAINMENT = Mutation(
        changelog,
        "        if index in fenced or _names_version(line, version):",
        "        if index in fenced or version in line:",
        "a changelog heading a release candidate below its release reports the candidate's entry as the release's",
    )

    @kills(_BOUNDARY_CONTAINMENT)
    def test_heading_of_longer_version_ends_the_section(self):
        """Test that a section ends at a heading naming a longer version, such as a release candidate's."""
        v1_change = "## 1.0.0\n\n- Fixed ...\n- Changed ..."
        text = f"# Changelog\n\n{v1_change}\n\n## 1.0.0rc1\n\n- A release candidate.\n\n## 0.9.0\n\n- Fixed ...\n"
        self.assertEqual(get_version_changes_from_changelog(text, "1.0.0"), v1_change)
