"""Parse the `# update-time:` marker language."""

import re
from dataclasses import dataclass, field, replace
from enum import Flag, auto
from typing import TYPE_CHECKING

from packaging.specifiers import InvalidSpecifier

from update_time.domain.bound import BLOCK_ALL_UPDATES, NO_BOUND, Verb
from update_time.domain.vulnerability import NO_RISK_LEVEL, RISK_LEVELS
from update_time.markers.bound import directive as _directive
from update_time.markers.bound import parse_bound, spell

if TYPE_CHECKING:
    from update_time.domain.bound import VersionBound
    from update_time.domain.line import Line
    from update_time.primitives.location import Location


class Scope(Flag):
    """What an item steers, rendering as the bracket item naming it.

    A flag rather than a plain enum, so the scopes a marker steers combine into one value: `Scope.STALE` is one
    scope and `Scope.STALE | Scope.YANKED` two. The items are spelled once here: the item regexps below match them,
    `_KEYWORD_ITEMS` recognises the ones a bare item names, and the warnings name them back, so the three cannot
    come to disagree. The spelling itself is fixed, being the language users write in their own repositories.
    """

    UPDATE = auto()
    COOLDOWN = auto()
    STALE = auto()
    YANKED = auto()
    VULNERABLE = auto()
    ARCHIVED = auto()
    HASH_DRIFT = auto()
    FLOATING_PIN = auto()

    def __str__(self) -> str:
        """Return the scope as the bracket item naming it, so parsing and rendering agree.

        The language hyphenates an item of two words, so `HASH_DRIFT` is spelled `hash-drift`.
        """
        return (self.name or "").lower().replace("_", "-")


# No scope at all: what a line without a marker names, and what the union in `Marker.merge` starts from.
_NO_SCOPE = Scope(0)

_IGNORABLE_SCOPES = Scope.UPDATE | Scope.YANKED | Scope.ARCHIVED

# The scopes that are off until an `allow` directive switches them on, whose `ignore` therefore spells the default.
_OPTIONAL_SCOPES = Scope.HASH_DRIFT | Scope.FLOATING_PIN

# The keys of the `set` items, each spelled as the command-line option it overrides.
_STALE_AFTER = "stale-after"
_COOLDOWN = "cooldown"
_VULNERABILITY_LEVEL = "vulnerability-level"


@dataclass(frozen=True)
class Threshold[T]:
    """What a `set` item sets for the reference carrying it: a staleness threshold, a cooldown, or a risk level.

    `value` is what the item sets for this reference alone, overriding the global setting: a number of days for a
    staleness threshold or a cooldown, a risk level for the vulnerability check; None when the reference sets none.
    `directive` is the directive that sets `value` (`set[cooldown=30]`), so a warning about this item alone never
    shows the directives beside it, which may hold plenty back. It is empty when the reference does not set a value.
    Comparisons leave it out, so two thresholds compare by the value they set.
    """

    value: T | None = None
    directive: str = field(compare=False, default="")

    def value_or(self, setting: T) -> T:
        """Return what the reference's own item set, or the run's setting when the reference sets none.

        A marker wins over the command line, whichever check asks.
        """
        return setting if self.value is None else self.value

    def merge(self, other: Threshold[T]) -> Threshold[T]:
        """Return this threshold combined with another one, this one winning where it sets a value."""
        return self if self.value is not None else other


@dataclass(frozen=True)
class Marker:
    """The `# update-time:` directives affecting a line (see `parse_marker`).

    `ignored_scopes` are the scopes an `ignore` holds back outright, which `_IGNORABLE_SCOPES` names: the update,
    the yank warning, and the archival warning. Each `ignore`, such as `ignore[yanked]`, holds back its scope alone.
    `ignored_advisories` are the advisories an `ignore[vulnerable=ID]` directive silences the warning for, each as
    the identifier the user spelled it by; empty when the reference names none.
    `allowed_scopes` are the scopes an `allow` directive opts the reference into, each off by default:
    `allow[hash-drift]` adopts a drifted hash pin, and `allow[floating-pin]` keeps a tag naming no version as it is
    rather than pinning it to the version it serves.
    `version_bound` is the version bound from an `allow`/`ignore` directive (see `VersionBound`), defaulting to
    `NO_BOUND` (keep every candidate) when there is none.
    `stale`, `cooldown`, and `vulnerable` are what the reference's `set` items set (see `Threshold`).
    `set[stale-after=90]` sets a staleness threshold in days, `set[cooldown=30]` sets a cooldown in days, and
    `set[vulnerability-level=high]` sets the risk level to warn from.
    `invalid_item` is the raw text of a bracket item that could not be parsed — an invalid version specifier,
    or an unrecognised item in a comma list — so the caller can warn and leave the reference unchanged; None otherwise.
    `default_directives` are the directives that ask for what Update-time does without them, such as
    `ignore[hash-drift]` or a bare `allow[update]`, as the language spells them; left out of comparisons, so a marker
    asking for the default compares equal to one that does not.
    `raw` is the marker's whole directive text exactly as it appears in the file, so the reference's marker can be
    echoed to the user verbatim.
    """

    ignored_scopes: Scope = _NO_SCOPE
    ignored_advisories: frozenset[str] = frozenset()
    allowed_scopes: Scope = _NO_SCOPE
    version_bound: VersionBound = NO_BOUND
    stale: Threshold[int] = Threshold()
    cooldown: Threshold[int] = Threshold()
    vulnerable: Threshold[str] = Threshold()
    invalid_item: str | None = None
    default_directives: tuple[str, ...] = field(compare=False, default=())
    raw: str = field(compare=False, default="")

    def __str__(self) -> str:
        """Render the marker as its verbatim directive text, exactly as the user spelled it."""
        return self.raw

    def directive_for(self, scope: Scope) -> str:
        """Return the directive the marker carries for this scope, as the language spells it, or nothing for none.

        A scope taking more than one form is asked for the forms it takes: `vulnerable` names every one the
        reference wrote, while `stale` names the threshold it sets.
        It is empty exactly when the marker decides nothing for the scope, so a caller reads it as both.
        """
        if scope is Scope.VULNERABLE:
            return self._vulnerable_directives
        if scope is Scope.STALE:
            return self.stale.directive
        if scope is Scope.COOLDOWN:
            return self.cooldown_directive
        return self.scope_directive(scope)

    @property
    def frozen(self) -> Marker:
        """Return this marker with the update held back as well, keeping everything else it says."""
        return replace(self, ignored_scopes=self.ignored_scopes | Scope.UPDATE)

    def ignores(self, scope: Scope) -> bool:
        """Return whether the marker holds the scope back outright.

        The outright form of a scope is one that a bound cannot express: `ignore[update]` blocks every version, and a
        bound narrows the versions an update may take. A check whose forms fold into one decision reads this alongside
        them (see `directive_for`).
        """
        return scope in self.ignored_scopes

    @property
    def cooldown_directive(self) -> str:
        """Return the directive that sets the cooldown, as the language spells it."""
        return self.cooldown.directive

    @property
    def version_bound_directive(self) -> str:
        """Return the directive narrowing which versions an update may take, or nothing when the marker sets none.

        The bound is named as the item the user wrote, since `ignore` and `allow` can each set one. An
        `ignore[update]` beside it leaves it named, so a caller judging the two apart can name each.
        """
        return "" if self.version_bound == NO_BOUND else spell(self.version_bound)

    @property
    def bound_directive(self) -> str:
        """Return the directive bounding the update, as the language spells it, or nothing when the marker sets none.

        An `ignore[update]` has one spelling, so it is spelled out. Where a reference carries both, the `ignore[update]`
        is named, since it holds every update back whatever the bound would admit.
        """
        if self.ignores(Scope.UPDATE):
            return spell(BLOCK_ALL_UPDATES)
        return self.version_bound_directive

    def scope_directive(self, scope: Scope) -> str:
        """Return the directive steering the scope, as the language spells it, or nothing when it steers it not.

        A scope has one spelling, so the directive is spelled out rather than read back from the text the user wrote:
        an `ignore` naming the scope. Spelling it out leaves out a directive beside it that does hold something back,
        and names the scope alone when it was written in a bracket it shares with other items.
        """
        return _directive(Verb.IGNORE, str(scope)) if self.ignores(scope) else ""

    def allows(self, scope: Scope) -> bool:
        """Return whether an `allow` directive opts the reference into the scope, which is off without one."""
        return scope in self.allowed_scopes

    def allow_directive(self, scope: Scope) -> str:
        """Return the directive opting the reference into the scope, or nothing when the marker opts into it not.

        Spelled out rather than read back from the text the user wrote, for the reason `scope_directive` gives:
        spelling it out leaves out an `allow` directive beside it that opts into something else.
        """
        return _directive(Verb.ALLOW, str(scope)) if self.allows(scope) else ""

    @property
    def advisory_directives(self) -> str:
        """Return the directive silencing the advisories the marker names, as the language spells it.

        A marker naming several advisories is judged as one, so they share a bracket, which is how the language
        spells a second advisory. They are sorted, since a `frozenset` keeps no order, and a marker split over two
        placements reaches them in the order they merged rather than the order they were written.
        """
        items = ", ".join(f"{Scope.VULNERABLE}={advisory}" for advisory in sorted(self.ignored_advisories))
        return _directive(Verb.IGNORE, items) if items else ""

    @property
    def _vulnerable_directives(self) -> str:
        """Return every directive the marker's `vulnerable` scope carries, as the language spells them.

        The scope has two forms and a reference can carry both at a time, so this names both. Where one form alone is
        reported, that form's own directive is named instead, since the other may hold plenty back.
        """
        forms = (self.advisory_directives, self.vulnerable.directive)
        return " ".join(form for form in forms if form)

    def merge(self, other: Marker) -> Marker:
        """Return this marker combined with another one.

        The scopes held back and the opt-ins combine as unions, so `ignore[update]` and `ignore[yanked]` together
        hold back both scopes, and so do the advisories named, so two `vulnerable=ID` items silence the warnings about
        both advisories. A value that cannot combine — a version bound, an invalid item, and the thresholds
        `Threshold.merge` folds — is taken from the other marker only where this one leaves it unset, so this marker's
        wins. The `raw` texts and the directives asking for the default concatenate in order, this marker's first. A
        default `Marker()` leaves every field unset, so it is the identity: merging it with any marker returns that
        marker's values. This lets markers fold at every level — each item into a bracket's marker, each directive into
        a text's, and the inline and comment-above texts into the line's.
        """
        return Marker(
            ignored_scopes=self.ignored_scopes | other.ignored_scopes,
            ignored_advisories=self.ignored_advisories | other.ignored_advisories,
            allowed_scopes=self.allowed_scopes | other.allowed_scopes,
            version_bound=other.version_bound if self.version_bound == NO_BOUND else self.version_bound,
            stale=self.stale.merge(other.stale),
            cooldown=self.cooldown.merge(other.cooldown),
            vulnerable=self.vulnerable.merge(other.vulnerable),
            invalid_item=other.invalid_item if self.invalid_item is None else self.invalid_item,
            default_directives=self.default_directives + other.default_directives,
            raw=" ".join(part for part in (self.raw, other.raw) if part),
        )


@dataclass(frozen=True)
class ReferenceMarker:
    """A marker for one reference, and the location of that reference.

    A format that holds no comment beside a reference names the marker elsewhere, and names the reference the
    marker steers along with it. `reference_location` is where that reference sits, down to the column, so a line
    declaring two references is read from the one this marker steers.
    """

    reference_location: Location
    marker: Marker


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
