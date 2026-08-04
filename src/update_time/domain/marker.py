"""Parse the `# update-time:` marker language."""

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from packaging.specifiers import InvalidSpecifier

from update_time.domain.bound import NO_BOUND, Verb, parse_bound

if TYPE_CHECKING:
    from update_time.domain.bound import VersionBound
    from update_time.domain.line import Line


@dataclass(frozen=True)
class Marker:
    """The `# update-time:` directives affecting a line (see `parse_marker`).

    `ignore_update`, `ignore_stale`, and `ignore_yanked` are whether an `ignore` directive holds back the reference's
    update, its staleness warning, and its yank warning. A bare `ignore` holds back all three, while
    `ignore[update]`, `ignore[stale]`, and `ignore[yanked]` each hold back just one.
    `allow_drift` is whether an `allow[hash-drift]` directive opts the reference into adopting a drifted hash pin.
    `version_bound` is the version bound from an `allow`/`ignore` directive (see `VersionBound`), defaulting to
    `NO_BOUND` (keep every candidate) when there is none.
    `stale_after_days` is the staleness threshold in days a `stale` item such as `ignore[stale<90]` sets for this
    reference alone, overriding the global one; None when the reference sets none.
    `inverted_stale_item` is a `stale` item whose comparison runs the wrong way, as the user spelled it, so the
    caller can warn and fall back to the global threshold; None when the reference carries no such item.
    `invalid_specifier` is the raw text of a bracket item that could not be parsed — an invalid version specifier,
    or an unrecognised item in a comma list — so the caller can warn and leave the reference unchanged; None otherwise.
    `raw` is the marker's whole directive text exactly as it appears in the file, so the reference's marker can be
    echoed to the user verbatim: rendering the marker gives all of it, `raw_directives` gives one verb's directives.
    """

    ignore_update: bool = False
    ignore_stale: bool = False
    ignore_yanked: bool = False
    allow_drift: bool = False
    version_bound: VersionBound = NO_BOUND
    stale_after_days: int | None = None
    inverted_stale_item: str | None = None
    invalid_specifier: str | None = None
    raw: str = field(compare=False, default="")

    def __str__(self) -> str:
        """Render the marker as its verbatim directive text, exactly as the user spelled it."""
        return self.raw

    def raw_directives(self, verb: Verb) -> str:
        """Return just the directives of one verb, as the user spelled them."""
        directives = (match for match in _DIRECTIVE.finditer(self.raw) if match.group("verb") == verb)
        return " ".join(match.group().strip() for match in directives)

    def merge(self, other: Marker) -> Marker:
        """Return this marker combined with another one.

        The boolean hold-backs and opt-ins combine as unions, so `ignore[update]` and `ignore[stale]` together hold
        back as much as a bare `ignore`. Of two values that cannot combine — a version bound, a staleness threshold,
        an inverted or invalid item — this marker's wins, and the `raw` texts concatenate in order, this marker's
        first. A default `Marker()` leaves every field unset, so it is the identity: merging it with any marker
        returns that marker's values. This lets markers fold at every level — each item into a bracket's marker,
        each directive into a text's, and the inline and comment-above texts into the line's.
        """
        return Marker(
            self.ignore_update or other.ignore_update,
            self.ignore_stale or other.ignore_stale,
            self.ignore_yanked or other.ignore_yanked,
            self.allow_drift or other.allow_drift,
            other.version_bound if self.version_bound == NO_BOUND else self.version_bound,
            other.stale_after_days if self.stale_after_days is None else self.stale_after_days,
            self.inverted_stale_item if self.inverted_stale_item is not None else other.inverted_stale_item,
            self.invalid_specifier if self.invalid_specifier is not None else other.invalid_specifier,
            " ".join(part for part in (self.raw, other.raw) if part),
        )


# The marker a bare `ignore` expresses: hold everything back.
_BARE_IGNORE = Marker(ignore_update=True, ignore_stale=True, ignore_yanked=True)

# The keyword bracket items each verb recognises, and the marker each expresses.
_KEYWORD_ITEMS = {
    (Verb.IGNORE, "update"): Marker(ignore_update=True),
    (Verb.IGNORE, "stale"): Marker(ignore_stale=True),
    (Verb.IGNORE, "yanked"): Marker(ignore_yanked=True),
    (Verb.ALLOW, "update"): Marker(),  # bare `allow[update]`: the default no-op
    (Verb.ALLOW, "hash-drift"): Marker(allow_drift=True),
}

# The comment leads that can carry a marker: `#` in most formats we update, `//` in devcontainer.json (which is
# JSONC).
_COMMENT_LEADS = ("#", "//")

# An `# update-time:` comment steers what happens to the reference on its line. It works inline on the reference's
# own line (valid in YAML and requirements) or as a standalone comment on the line directly above it (the form
# Dockerfiles need, as they reject inline comments).
_MARKER_PREFIX = re.compile(rf"(?:{'|'.join(re.escape(lead) for lead in _COMMENT_LEADS)})\s*update-time:\s*")

# A single directive in a marker's directive list: a verb, optionally followed by a bracket. The `verb` group's
# text is a `Verb` member value; the `\b` keeps a word merely starting with a verb (`ignores`) from matching. The
# `bracket` group captures everything between the square brackets, and `unterminated` everything after a `[` that
# is never closed, to the end of the line. The lookahead requires a `[` after `allow`, so a bare `allow` is not a
# directive and a match with neither bracket group can only be an `ignore`. The trailing whitespace lets
# consecutive directives be matched one after another.
_DIRECTIVE = re.compile(r"(?P<verb>ignore\b|allow(?=\[))(?:\[(?P<bracket>[^\]]*)\]|\[(?P<unterminated>[^\]]*))?\s*")

# A `stale` bracket item: the `stale` keyword, a comparison operator, and a number of days (`stale<90`). The day
# count is captured loosely, so a malformed one is still recognised as a `stale` item and can be reported as malformed.
_STALE_ITEM = re.compile(r"stale(?P<operator><|>=)(?P<days>.*)")

_DAY_COUNT = re.compile(r"\d+")

# The operator each verb sets a threshold with: `ignore[stale<90]` and `allow[stale>=90]` both mean "warn once the
# newest release is more than 90 days old".
_STALE_THRESHOLD_OPERATOR = {Verb.IGNORE: "<", Verb.ALLOW: ">="}


def parse_marker(line: Line) -> Marker:
    """Return the `# update-time:` directives affecting the line as a `Marker`.

    Requiring the preceding line to start with a comment lead keeps an inline marker from also affecting the line below
    it. Where directives conflict, the first one wins, inline directives before those on the line above.
    """
    marker = _parse_marker_contents(line.text)
    if line.previous_text.lstrip().startswith(_COMMENT_LEADS):
        marker = marker.merge(_parse_marker_contents(line.previous_text))  # Inline directives win over those above.
    return marker


def _parse_marker_contents(text: str) -> Marker:
    """Return the marker expressed by the `# update-time:` directives in one line of text.

    Each `# update-time:` prefix introduces a whitespace-separated list of directives, so directives combine behind
    a single prefix (`# update-time: ignore[stale] allow[update>=3.13]`); the first token that is not a directive
    ends the list, so a trailing reason is allowed. Each directive folds into the marker with `Marker.merge`, so
    earlier directives win over later ones. Each prefix's whole directive run — the text from the prefix to the last
    directive, without the prefix itself or a trailing reason — is folded in as the marker's `raw` text, so the
    marker can later be echoed back to the user exactly as they spelled it (see `Marker.raw`).
    """
    marker = Marker()
    for prefix in _MARKER_PREFIX.finditer(text):
        position = prefix.end()
        while directive := _DIRECTIVE.match(text, position):
            position = directive.end()
            marker = marker.merge(_parse_directive(directive))
        marker = marker.merge(Marker(raw=text[prefix.end() : position].strip()))
    return marker


def _parse_directive(directive: re.Match[str]) -> Marker:
    """Return the marker expressed by a single parsed directive.

    A bracket left unclosed is reported as an invalid item, keeping its `[` so the message shows that the bracket
    was never closed. A directive with no bracket at all is the documented bare `ignore`, holding back everything.
    A closed bracket, even one holding only an unrecognised item, is handled by `_parse_bracket` instead.
    """
    if (unterminated := directive.group("unterminated")) is not None:
        return Marker(invalid_specifier=f"[{unterminated}")
    if (bracket := directive.group("bracket")) is None:
        return _BARE_IGNORE
    return _parse_bracket(Verb(directive.group("verb")), bracket)


def _parse_bracket(verb: Verb, bracket: str) -> Marker:
    """Return the marker for a directive's bracket: its comma-separated items parsed for the verb and merged.

    An unrecognised item becomes an `invalid_specifier`, so a mistyped scope or bound (`ignore[updaet]`,
    `allow[patch-updates]`) can be warned about.
    """
    marker = Marker()
    for item in _bracket_items(bracket):
        item_marker = _parse_bracket_item(verb, item)
        marker = marker.merge(item_marker if item_marker is not None else Marker(invalid_specifier=item))
    return marker


def _bracket_items(bracket: str) -> list[str]:
    """Split a bracket's contents into its comma-separated items, keeping compound specifiers together.

    A segment that starts with a specifier operator continues the previous item's specifier rather than starting a
    new item, so `update>=3.10,<3.13, stale` is two items: the compound bound `update>=3.10,<3.13` and `stale`.
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
    try:
        if (stale_marker := _parse_stale_item(verb, item)) is not None:
            return stale_marker
        version_bound = parse_bound(verb, item)
    except InvalidSpecifier:
        # The `update` prefix is dropped so a bound reports its specifier; a `stale` item has none and reports whole.
        return Marker(invalid_specifier=item.removeprefix("update"))
    return Marker(version_bound=version_bound) if version_bound is not None else None


def _parse_stale_item(verb: Verb, item: str) -> Marker | None:
    """Return the marker a `stale` bracket item expresses, or None when the item is not one.

    Both `ignore[stale<90]` and `allow[stale>=90]` set a threshold of 90 days. The verb's other operator names the
    fresh ages rather than the old ones, which no threshold can express, so the item is returned for the caller to
    report. An unreadable day count raises `InvalidSpecifier`, and is judged before the direction, so `stale>=1.5`
    is reported as an unreadable count rather than as an inverted comparison.
    """
    match = _STALE_ITEM.fullmatch(item)
    if match is None:
        return None
    if _DAY_COUNT.fullmatch(days := match.group("days")) is None:
        raise InvalidSpecifier(item)
    if match.group("operator") != _STALE_THRESHOLD_OPERATOR[verb]:
        return Marker(inverted_stale_item=item)
    return Marker(stale_after_days=int(days))
