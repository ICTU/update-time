"""Parse the `# update-time:` marker language."""

import re
from typing import TYPE_CHECKING

from packaging.specifiers import InvalidSpecifier

from update_time.domain.bound import Verb
from update_time.domain.marker import Marker, Scope, Threshold
from update_time.domain.marker import directive as _directive
from update_time.domain.vulnerability import NO_RISK_LEVEL, RISK_LEVELS
from update_time.markers.bound import parse_bound

if TYPE_CHECKING:
    from update_time.primitives.line import Line

# An `ignore` directive can hold each of these scopes back outright, with an item of its own.
_IGNORABLE_SCOPES = Scope.UPDATE | Scope.YANKED | Scope.ARCHIVED

# The scopes that are off until an `allow` directive switches them on, whose `ignore` therefore spells the default.
_OPTIONAL_SCOPES = Scope.HASH_DRIFT | Scope.FLOATING_PIN

# The keys of the `set` items, each spelled as the command-line option it overrides.
_STALE_AFTER = "stale-after"
_COOLDOWN = "cooldown"
_VULNERABILITY_LEVEL = "vulnerability-level"


# The keyword bracket items each verb recognises, and the marker each expresses. Each ignorable scope takes one item
# that holds it back, so the scopes decide what is recognised.
_KEYWORD_ITEMS = (
    {(Verb.IGNORE, str(scope)): Marker(ignored_scopes=scope) for scope in _IGNORABLE_SCOPES}
    | {(Verb.ALLOW, str(scope)): Marker(allowed_scopes=scope) for scope in _OPTIONAL_SCOPES}
    | {
        # A bare `allow[update]` and the `ignore` of an optional scope ask for the default, recorded as written.
        item: Marker(default_directives=(_directive(*item),))
        for item in [(Verb.ALLOW, str(Scope.UPDATE)), *((Verb.IGNORE, str(scope)) for scope in _OPTIONAL_SCOPES)]
    }
)

# The comment leads that can carry a marker: `#` in most formats we update, `//` in devcontainer.json (which is
# JSONC), and `<!--` in pom.xml.
_COMMENT_LEADS = ("#", "//", "<!--")

# An `# update-time:` comment steers what happens to the reference on its line. It works inline on the reference's
# own line (valid in YAML and requirements) or as a standalone comment on the line directly above it (the form
# Dockerfiles need, as they reject inline comments).
_MARKER_PREFIX = re.compile(rf"(?:{'|'.join(re.escape(lead) for lead in _COMMENT_LEADS)})\s*update-time:\s*")

# A directive in a marker's directive list is a verb followed by a bracket. The `verb` group's text is a `Verb` member
# value. The `bracket` group captures everything between the square brackets, and `unterminated` everything after a
# `[` that is never closed, to the end of the line. A bare `ignore`, followed by whitespace or the end of the text,
# matches with neither group, so it can be reported. A bare `allow` or `set` does not match, so it reads as the start
# of the reason. The trailing whitespace lets consecutive directives be matched one after another.
_DIRECTIVE = re.compile(
    r"(?P<verb>ignore|allow(?=\[)|set(?=\[))(?:\[(?P<bracket>[^\]]*)\]|\[(?P<unterminated>[^\]]*)|(?=\s|$))\s*"
)

# A day-count `set` item names the option it overrides and a number of days, as in `stale-after=90`.
_SETTING_DAY_COUNT_ITEM = re.compile(rf"(?P<key>{_STALE_AFTER}|{_COOLDOWN})=(?P<days>\d+)")

# The `set` item for a risk level names the option it overrides and a risk level, as in `vulnerability-level=high`.
_SETTING_RISK_LEVEL_ITEM = re.compile(rf"{_VULNERABILITY_LEVEL}=(?P<level>{'|'.join([*RISK_LEVELS, NO_RISK_LEVEL])})")

# An advisory bracket item: the `vulnerable` scope narrowed to the advisory the identifier after the `=` names
# (`vulnerable=GHSA-2gwj-7jmv-h26r`). The identifier is opaque to Update-time, so whatever follows the `=` is taken
# as the user spelled it, and an item with nothing after the `=` names no advisory.
_ADVISORY_ITEM = re.compile(rf"{Scope.VULNERABLE}=(?P<advisory>.+)")


def parse_marker(line: Line) -> Marker:
    """Return the `# update-time:` directives affecting the line as a `Marker`.

    Requiring the preceding line to start with a comment lead keeps an inline marker from also affecting the line below
    it. Where directives conflict, the first one wins, inline directives before those on the line above.
    """
    marker = _parse_marker_contents(line.text)
    if line.previous_text.lstrip().startswith(_COMMENT_LEADS):
        marker = marker.merge(_parse_marker_contents(line.previous_text))  # Inline directives win over those above.
    return marker


def parse_directives(directives: str) -> Marker:
    """Return the marker a list of directives expresses, written without the `# update-time:` prefix.

    The first token that is not a directive ends the list, so a trailing reason is allowed. Each directive folds
    into the marker with `Marker.merge`, so earlier directives win over later ones. The text up to the last
    directive is folded in as the marker's `raw` text, so the marker can later be echoed back to the user exactly
    as they spelled it (see `Marker.raw`).
    """
    marker = Marker()
    position = 0
    while directive := _DIRECTIVE.match(directives, position):
        position = directive.end()
        marker = marker.merge(_parse_directive(directive))
    return marker.merge(Marker(raw=directives[:position].strip()))


def _parse_marker_contents(text: str) -> Marker:
    """Return the marker expressed by the `# update-time:` directives in one line of text.

    A line may carry more than one prefix, and the directives behind each of them fold into one marker, the
    earliest winning.
    """
    marker = Marker()
    for prefix in _MARKER_PREFIX.finditer(text):
        marker = marker.merge(parse_directives(text[prefix.end() :]))
    return marker


def _parse_directive(directive: re.Match[str]) -> Marker:
    """Return the marker expressed by a single parsed directive.

    A bracket left unclosed is reported as an invalid item, keeping its `[` so the message shows that the bracket
    was never closed. A bare `ignore` is reported as an invalid item too. A closed bracket, even one holding
    only an unrecognised item, is handled by `_parse_bracket`.
    """
    if (unterminated := directive.group("unterminated")) is not None:
        return Marker(invalid_item=f"[{unterminated}")
    if (bracket := directive.group("bracket")) is None:
        return Marker(invalid_item=directive.group("verb"))
    return _parse_bracket(Verb(directive.group("verb")), bracket)


def _parse_bracket(verb: Verb, bracket: str) -> Marker:
    """Return the marker for a directive's bracket: its comma-separated items parsed for the verb and merged.

    An unrecognised item becomes an `invalid_item`, so a mistyped scope or bound (`ignore[updaet]`,
    `allow[patch-updates]`) can be warned about.
    """
    marker = Marker()
    for item in _bracket_items(bracket):
        item_marker = _parse_setting_item(item) if verb is Verb.SET else _parse_bracket_item(verb, item)
        marker = marker.merge(item_marker if item_marker is not None else Marker(invalid_item=item))
    return marker


def _bracket_items(bracket: str) -> list[str]:
    """Split a bracket's contents into its comma-separated items, keeping compound specifiers together.

    A segment that starts with a specifier operator continues the previous item's specifier rather than starting a
    new item, so `update>=3.10,<3.13, yanked` is two items: the compound bound `update>=3.10,<3.13` and `yanked`.
    """
    items: list[str] = []
    for segment in bracket.split(","):
        stripped = segment.strip()
        if items and stripped.startswith(("<", ">", "=", "~", "!")):
            items[-1] += f",{stripped}"  # A specifier clause continues the previous item.
        else:
            items.append(stripped)
    return items


def _parse_bracket_item(verb: Verb, item: str) -> Marker | None:
    """Return the marker for one bracket item of the given verb, or None when the item is unrecognised."""
    if (keyword_marker := _KEYWORD_ITEMS.get((verb, item))) is not None:
        return keyword_marker
    if (advisory_marker := _parse_advisory_item(verb, item)) is not None:
        return advisory_marker
    try:
        version_bound = parse_bound(verb, item)
    except InvalidSpecifier:
        # A bound's `update` names the item rather than the version it bounds, so the item reports its specifier.
        return Marker(invalid_item=item.removeprefix(str(Scope.UPDATE)))
    return Marker(version_bound=version_bound) if version_bound is not None else None


def _parse_setting_item(item: str) -> Marker | None:
    """Return the marker a `set` bracket item expresses, or None when the item sets nothing Update-time knows."""
    directive = _directive(Verb.SET, item)
    if (match := _SETTING_RISK_LEVEL_ITEM.fullmatch(item)) is not None:
        return Marker(vulnerable=Threshold(value=match.group("level"), directive=directive))
    if (match := _SETTING_DAY_COUNT_ITEM.fullmatch(item)) is None:
        return None
    day_count = Threshold(value=int(match.group("days")), directive=directive)
    return Marker(stale=day_count) if match.group("key") == _STALE_AFTER else Marker(cooldown=day_count)


def _parse_advisory_item(verb: Verb, item: str) -> Marker | None:
    """Return the marker an advisory bracket item expresses, or None when the item names no advisory.

    Only `ignore` names an advisory, dropping the warning about the one it names. `allow` naming one would keep that
    warning and drop the warning about every other advisory, which is a rule the language does not offer, so
    `allow[vulnerable=ID]` names no advisory.
    """
    match = _ADVISORY_ITEM.fullmatch(item)
    if match is None or verb is not Verb.IGNORE:
        return None
    return Marker(ignored_advisories=frozenset({match.group("advisory")}))
