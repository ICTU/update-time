"""Parse the `# update-time:` marker language."""

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from packaging.specifiers import InvalidSpecifier

from update_time.domain.bound import NO_BOUND, Verb, parse_bound
from update_time.domain.vulnerability import RISK_LEVELS

if TYPE_CHECKING:
    from update_time.domain.bound import VersionBound
    from update_time.domain.line import Line


# The bracket items naming the checks a marker steers, spelled once here: the item regexps below match them,
# `_KEYWORD_ITEMS` recognises them, and the warnings name them back, so the three cannot come to disagree. The
# spelling itself is fixed, being the language users write in their own repositories.
_STALE = "stale"
_COOLDOWN = "cooldown"
_YANKED = "yanked"
_VULNERABLE = "vulnerable"


@dataclass(frozen=True)
class Threshold[T]:
    """What a comparison bracket item — a `stale`, a `cooldown`, or a `vulnerable` — sets for the reference carrying it.

    `value` is what the item sets for this reference alone, overriding the global setting: a number of days for a
    `stale` or a `cooldown` item, a risk level for a `vulnerable` one; None when the reference sets none.
    `inverted_item` is the item as the user spelled it when its comparison runs the wrong way, so the caller can warn
    and fall back to the global setting; None when the reference carries no such item. An item sets one or the other,
    never both, which is why they travel together.
    `directive` is the directive that sets `value`, as the verb and item the user spelled (`ignore[cooldown<30]`), so
    a warning about this item alone never shows the directives beside it, which may hold plenty back. Empty when the
    reference sets no value, and left out of comparisons, so thresholds that set the same value compare as equal
    however they were spelled.
    """

    value: T | None = None
    inverted_item: str | None = None
    directive: str = field(compare=False, default="")

    def value_or(self, setting: T) -> T:
        """Return what the reference's own item set, or the run's setting when the reference sets none.

        A marker wins over the command line, so this is the one place that precedence is decided, whichever check
        asks.
        """
        return setting if self.value is None else self.value

    def merge(self, other: Threshold[T]) -> Threshold[T]:
        """Return this threshold combined with another one, this one's values winning where it sets them.

        The directive spells the value, so both are taken from the threshold that sets the value, which keeps a
        merged threshold from naming a directive that set nothing.
        """
        sets_value = self if self.value is not None else other
        return Threshold(
            value=sets_value.value,
            inverted_item=other.inverted_item if self.inverted_item is None else self.inverted_item,
            directive=sets_value.directive,
        )


@dataclass(frozen=True)
class Marker:
    """The `# update-time:` directives affecting a line (see `parse_marker`).

    `ignore_update`, `ignore_stale`, `ignore_yanked`, and `ignore_vulnerable` are whether an `ignore` directive holds
    back the reference's update, its staleness warning, its yank warning, and its vulnerability warning. A bare
    `ignore` holds back all four, while `ignore[update]`, `ignore[stale]`, `ignore[yanked]`, and `ignore[vulnerable]`
    each hold back just one.
    `ignored_advisories` are the advisories an `ignore[vulnerable=ID]` directive holds the warning back for, each as
    the identifier the user spelled it by; empty when the reference names none.
    `allow_drift` is whether an `allow[hash-drift]` directive opts the reference into adopting a drifted hash pin.
    `version_bound` is the version bound from an `allow`/`ignore` directive (see `VersionBound`), defaulting to
    `NO_BOUND` (keep every candidate) when there is none.
    `stale`, `cooldown`, and `vulnerable` are what the reference's comparison items set (see `Threshold`).
    `ignore[stale<90]` sets a staleness threshold in days, `ignore[cooldown<30]` sets a cooldown in days, and
    `ignore[vulnerable<high]` sets the risk level to warn from. A comparison running the wrong way sets none of
    them, and is carried as the item to report.
    `invalid_item` is the raw text of a bracket item that could not be parsed — an invalid version specifier,
    or an unrecognised item in a comma list — so the caller can warn and leave the reference unchanged; None otherwise.
    `raw` is the marker's whole directive text exactly as it appears in the file, so the reference's marker can be
    echoed to the user verbatim: rendering the marker gives all of it, `raw_directives` gives one verb's directives.
    """

    ignore_update: bool = False
    ignore_stale: bool = False
    ignore_yanked: bool = False
    ignore_vulnerable: bool = False
    ignored_advisories: frozenset[str] = frozenset()
    allow_drift: bool = False
    version_bound: VersionBound = NO_BOUND
    stale: Threshold[int] = Threshold()
    cooldown: Threshold[int] = Threshold()
    vulnerable: Threshold[str] = Threshold()
    invalid_item: str | None = None
    raw: str = field(compare=False, default="")

    def __str__(self) -> str:
        """Render the marker as its verbatim directive text, exactly as the user spelled it."""
        return self.raw

    @property
    def holds_everything_back(self) -> bool:
        """Return whether the marker leaves no check to run, so no source need be queried for the reference at all.

        The one place the scopes are enumerated, so a scope added to the fields above is weighed here rather than
        leaving each caller to remember its own list. A marker holding back only some of them still needs its
        sources, since the checks it leaves alone have to run.
        """
        return self.ignore_update and self.ignore_stale and self.ignore_yanked and self.ignore_vulnerable

    @property
    def sets_cooldown(self) -> bool:
        """Return whether the marker sets a cooldown for the reference."""
        return self.cooldown.value is not None

    @property
    def decides_staleness(self) -> bool:
        """Return whether the marker decides the staleness check for the reference, in whichever of its forms.

        The one place the `stale` scope's forms are enumerated: silencing the warning altogether, and setting the
        threshold to warn from, which asks for more warnings as often as fewer. A comparison running the wrong way
        sets no threshold, so it decides nothing and is reported as incorrect instead.
        """
        return self.ignore_stale or self.stale.value is not None

    @property
    def suppresses_vulnerabilities(self) -> bool:
        """Return whether the marker holds the vulnerability warning back, in whichever of its forms.

        The one place the `vulnerable` scope's forms are enumerated: holding every warning back, holding back the
        one about a named advisory, and setting the level to warn from. A comparison running the wrong way sets no
        level, so it suppresses nothing and is reported as incorrect instead.
        """
        return self.ignore_vulnerable or bool(self.ignored_advisories) or self.vulnerable.value is not None

    @property
    def cooldown_directive(self) -> str:
        """Return the directive that sets the cooldown, as the language spells it.

        Either verb can set one, so the item the user wrote is named rather than a spelling of our own.
        """
        return self.cooldown.directive

    @property
    def yank_directive(self) -> str:
        """Return the directive that holds the yank warning back, as the language spells it.

        Only `ignore[yanked]` sets the scope, so it is spelled out rather than read back from the text the user
        wrote. Spelling it out leaves out an `ignore` beside it that does hold something back, and names the scope
        alone when it was written in a bracket it shares with other items.
        """
        return _directive(Verb.IGNORE, _YANKED)

    @property
    def stale_directive(self) -> str:
        """Return the directive that decides the staleness check, as the language spells it.

        A bare scope has one spelling only, so `ignore[stale]` is spelled out; a threshold is named as the item the
        user wrote, since either verb can set one. Where a reference carries both, the bare scope is named, since
        it silences the warning whatever the threshold says.
        """
        return _directive(Verb.IGNORE, _STALE) if self.ignore_stale else self.stale.directive

    @property
    def vulnerable_scope_directive(self) -> str:
        """Return the bare `vulnerable` scope, as the language spells it, or nothing when the marker sets none.

        Only `ignore[vulnerable]` sets the scope, so it is spelled out rather than read back from the user's text.
        """
        return _directive(Verb.IGNORE, _VULNERABLE) if self.ignore_vulnerable else ""

    @property
    def advisory_directives(self) -> str:
        """Return the directive silencing the advisories the marker names, as the language spells it.

        A marker naming several advisories is judged as one, so they share a bracket, which is how the language
        spells a second advisory. They are sorted, since a `frozenset` keeps no order, and a marker split over two
        placements reaches them in the order they merged rather than the order they were written.
        """
        items = ", ".join(f"{_VULNERABLE}={advisory}" for advisory in sorted(self.ignored_advisories))
        return _directive(Verb.IGNORE, items) if items else ""

    @property
    def vulnerable_directives(self) -> str:
        """Return every directive the marker's `vulnerable` scope carries, as the language spells them.

        The scope has three forms and a reference can carry more than one at a time, so this names the lot. Where
        one form alone is reported, that form's own directive is named instead, since the others may hold plenty
        back.
        """
        forms = (self.vulnerable_scope_directive, self.advisory_directives, self.vulnerable.directive)
        return " ".join(form for form in forms if form)

    def raw_directives(self, verb: Verb) -> str:
        """Return just the directives of one verb, as the user spelled them."""
        directives = (match for match in _DIRECTIVE.finditer(self.raw) if match.group("verb") == verb)
        return " ".join(match.group().strip() for match in directives)

    def merge(self, other: Marker) -> Marker:
        """Return this marker combined with another one.

        The boolean hold-backs and opt-ins combine as unions, so `ignore[update]` and `ignore[stale]` together hold
        back as much as a bare `ignore`, and so do the advisories named, so two `vulnerable=ID` items hold back the
        warnings about both advisories. A value that cannot combine — a version bound, an invalid item, and the
        thresholds `Threshold.merge` folds — is taken from the other marker only where this one leaves it unset, so
        this marker's wins; the `raw` texts concatenate in order, this marker's first. A default `Marker()` leaves
        every field unset, so it is the identity: merging it with any marker returns that marker's values. This lets
        markers fold at every level — each item into a bracket's marker, each directive into a text's, and the
        inline and comment-above texts into the line's.
        """
        return Marker(
            ignore_update=self.ignore_update or other.ignore_update,
            ignore_stale=self.ignore_stale or other.ignore_stale,
            ignore_yanked=self.ignore_yanked or other.ignore_yanked,
            ignore_vulnerable=self.ignore_vulnerable or other.ignore_vulnerable,
            ignored_advisories=self.ignored_advisories | other.ignored_advisories,
            allow_drift=self.allow_drift or other.allow_drift,
            version_bound=other.version_bound if self.version_bound == NO_BOUND else self.version_bound,
            stale=self.stale.merge(other.stale),
            cooldown=self.cooldown.merge(other.cooldown),
            vulnerable=self.vulnerable.merge(other.vulnerable),
            invalid_item=other.invalid_item if self.invalid_item is None else self.invalid_item,
            raw=" ".join(part for part in (self.raw, other.raw) if part),
        )


# The marker a bare `ignore` expresses: hold everything back.
_BARE_IGNORE = Marker(ignore_update=True, ignore_stale=True, ignore_yanked=True, ignore_vulnerable=True)

# The keyword bracket items each verb recognises, and the marker each expresses.
_KEYWORD_ITEMS = {
    (Verb.IGNORE, "update"): Marker(ignore_update=True),
    (Verb.IGNORE, _STALE): Marker(ignore_stale=True),
    (Verb.IGNORE, _YANKED): Marker(ignore_yanked=True),
    (Verb.IGNORE, _VULNERABLE): Marker(ignore_vulnerable=True),
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

# A day-count bracket item: a keyword, a comparison operator, and a number of days (`stale<90`, `cooldown<30`). The
# day count is captured loosely, so a malformed one is still recognised as such an item and reported as malformed.
_DAY_COUNT_ITEM = re.compile(rf"(?P<keyword>{_STALE}|{_COOLDOWN})(?P<operator><|>=)(?P<days>.*)")

_DAY_COUNT = re.compile(r"\d+")

# An advisory bracket item: the `vulnerable` scope narrowed to the advisory the identifier after the `=` names
# (`vulnerable=GHSA-2gwj-7jmv-h26r`). The identifier is opaque to Update-time, so whatever follows the `=` is taken
# as the user spelled it, and an item with nothing after the `=` names no advisory.
_ADVISORY_ITEM = re.compile(rf"{_VULNERABLE}=(?P<advisory>.+)")

# A risk level bracket item: the `vulnerable` scope, a comparison operator, and a risk level (`vulnerable<high`). The
# level is captured loosely, so a misspelled one is still recognised as such an item and reported as unreadable.
_RISK_LEVEL_ITEM = re.compile(rf"{_VULNERABLE}(?P<operator><|>=)(?P<level>.*)")

# The operator each verb names a threshold with. A threshold is sensible only when it names the values from the
# threshold upwards, so `ignore[stale<90]` and `allow[stale>=90]` set the same threshold, as do
# `ignore[cooldown<30]` and `allow[cooldown>=30]`, and `ignore[vulnerable<high]` and `allow[vulnerable>=high]`.
_THRESHOLD_OPERATOR = {Verb.IGNORE: "<", Verb.ALLOW: ">="}


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
        return Marker(invalid_item=f"[{unterminated}")
    if (bracket := directive.group("bracket")) is None:
        return _BARE_IGNORE
    return _parse_bracket(Verb(directive.group("verb")), bracket)


def _parse_bracket(verb: Verb, bracket: str) -> Marker:
    """Return the marker for a directive's bracket: its comma-separated items parsed for the verb and merged.

    An unrecognised item becomes an `invalid_item`, so a mistyped scope or bound (`ignore[updaet]`,
    `allow[patch-updates]`) can be warned about.
    """
    marker = Marker()
    for item in _bracket_items(bracket):
        item_marker = _parse_bracket_item(verb, item)
        marker = marker.merge(item_marker if item_marker is not None else Marker(invalid_item=item))
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
    if (advisory_marker := _parse_advisory_item(verb, item)) is not None:
        return advisory_marker
    try:
        if (risk_level_marker := _parse_risk_level_item(verb, item)) is not None:
            return risk_level_marker
        if (day_count_marker := _parse_day_count_item(verb, item)) is not None:
            return day_count_marker
        version_bound = parse_bound(verb, item)
    except InvalidSpecifier:
        # A bound's `update` names the item rather than the version it bounds, so the item reports its specifier.
        return Marker(invalid_item=item.removeprefix("update"))
    return Marker(version_bound=version_bound) if version_bound is not None else None


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


def _directive(verb: Verb, item: str) -> str:
    """Return a directive as the language spells it: a verb and the bracketed item that sets a scope."""
    return f"{verb}[{item}]"


def _threshold[T](verb: Verb, match: re.Match[str], value: T) -> Threshold[T]:
    """Return the threshold the matched comparison item sets, or the item itself when it compares the wrong way.

    Each verb names a threshold with one operator (see `_THRESHOLD_OPERATOR`), so the other operator sets nothing and
    leaves the item to be reported. Said once here, since every comparison item is read this way.
    """
    if match.group("operator") != _THRESHOLD_OPERATOR[verb]:
        return Threshold(inverted_item=match.group())
    return Threshold(value=value, directive=_directive(verb, match.group()))


def _parse_risk_level_item(verb: Verb, item: str) -> Marker | None:
    """Return the marker a risk level bracket item expresses, or None when the item names no risk level.

    Both `ignore[vulnerable<high]` and `allow[vulnerable>=high]` set a threshold of `high`, so the reference is
    warned about from that level up. The verb's other operator names the levels below the threshold rather than the
    ones from it up, which the item cannot express, so an inverted item is returned for the caller to report. A level
    outside `RISK_LEVELS` makes the item an invalid one, and is judged before the direction, so `vulnerable>=hgih` is
    reported as an unreadable level rather than as an inverted comparison.
    """
    match = _RISK_LEVEL_ITEM.fullmatch(item)
    if match is None:
        return None
    if (level := match.group("level")) not in RISK_LEVELS:
        return Marker(invalid_item=item)
    return Marker(vulnerable=_threshold(verb, match, level))


def _parse_day_count_item(verb: Verb, item: str) -> Marker | None:
    """Return the marker a day-count bracket item expresses, or None when the item is not a day-count.

    Both `ignore[stale<90]` and `allow[stale>=90]` set a staleness threshold of 90 days, and both
    `ignore[cooldown<30]` and `allow[cooldown>=30]` set a cooldown of 30 days. The verb's other operator names the
    fresh ages rather than the old ones, which neither item can express, so an inverted item of either keyword is
    returned for the caller to report. An unreadable day count makes the item an invalid one, and is judged before
    the direction, so `stale>=1.5` is reported as an unreadable count rather than as an inverted comparison.
    """
    match = _DAY_COUNT_ITEM.fullmatch(item)
    if match is None:
        return None
    if _DAY_COUNT.fullmatch(days := match.group("days")) is None:
        return Marker(invalid_item=item)
    day_count = _threshold(verb, match, int(days))
    return Marker(stale=day_count) if match.group("keyword") == _STALE else Marker(cooldown=day_count)
