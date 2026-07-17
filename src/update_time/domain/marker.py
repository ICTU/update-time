"""Parse the `# update-time:` marker language.

Comments of the form `# update-time: <directive>…` let users steer what happens to an individual reference: hold it
back (`ignore`, optionally narrowed to the update or the staleness warning), bound how far it may update — to an
absolute version range (`allow[update<…>]` / `ignore[update<…>]`) or by update level (`allow[minor-update]` /
`ignore[major-update]`) — or opt it into adopting a re-pushed image digest (`allow[digest-drift]`).
A marker is read inline on the reference's own line or from a standalone comment on the line directly above it; one
prefix can carry several whitespace-separated directives, and a directive's bracket several comma-separated items
(`ignore[stale, update>=3.13]`). Parsing is pure — text in, `Marker` out — so it lives in `domain`; acting on the
marker, and reporting about it, is left to the rewrite engine in `io`.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from packaging.specifiers import InvalidSpecifier

from update_time.domain.version import NO_BOUND, Verb, parse_bound

if TYPE_CHECKING:
    from update_time.domain.version import VersionFilter


@dataclass(frozen=True)
class Marker:
    """The `# update-time:` directives affecting a line (see `parse_marker`).

    `ignore_update` and `ignore_stale` are whether an `ignore` directive holds back the reference's update and its
    staleness warning: a bare `ignore` holds back both, `ignore[update]` and `ignore[stale]` each just one.
    `allow_drift` is whether an `allow[digest-drift]` directive opts the reference into adopting a re-pushed digest.
    `version_filter` is the version bound from an `allow`/`ignore` directive (see `VersionFilter`), defaulting to
    `NO_BOUND` (keep every candidate) when there is none.
    `invalid_specifier` is the raw text of a bracket item that could not be parsed — an invalid version specifier,
    or an unrecognised item in a comma list — so the caller can warn and leave the reference unchanged; None otherwise.
    """

    ignore_update: bool = False
    ignore_stale: bool = False
    allow_drift: bool = False
    version_filter: VersionFilter = NO_BOUND
    invalid_specifier: str | None = None

    @property
    def ignore_directive(self) -> str:
        """Return the marker's `ignore` directive in its normalised form, or an empty string when it holds nothing back.

        Combined scopes collapse to a bare `ignore`; a single scope renders as `ignore[update]` or `ignore[stale]`.
        """
        if self.ignore_update and self.ignore_stale:
            return "ignore"
        if self.ignore_update:
            return "ignore[update]"
        if self.ignore_stale:
            return "ignore[stale]"
        return ""

    @property
    def drift_directive(self) -> str:
        """Return the marker's digest-drift opt-in directive, or an empty string when it opts nothing in."""
        return "allow[digest-drift]" if self.allow_drift else ""

    def __str__(self) -> str:
        """Return the marker as the directive list it expresses, or an empty string when it expresses nothing.

        The directive list is normalised rather than the text the user wrote: combined `ignore` scopes collapse to
        a bare `ignore`, comma-combined bracket items render as separate directives, and an invalid item is not a
        directive, so it is not rendered. A bound's specifier, however, renders as the user wrote it (see
        `VersionFilter.__str__`).
        """
        directives = []
        if directive := self.ignore_directive:
            directives.append(directive)
        if self.version_filter != NO_BOUND:
            directives.append(str(self.version_filter))
        if directive := self.drift_directive:
            directives.append(directive)
        return " ".join(directives)

    def merge(self, other: Marker) -> Marker:
        """Return this marker combined with another one.

        The boolean hold-backs and opt-ins combine as unions, so `ignore[update]` and `ignore[stale]` together hold
        back as much as a bare `ignore`; of two values that cannot combine — a version bound, an invalid specifier —
        this marker's wins. A default `Marker()` leaves every field unset, so it is the identity: merging it with
        any marker returns that marker's values. This lets markers fold at every level — each item into a bracket's
        marker, each directive into a text's, and the inline and comment-above texts into the line's.
        """
        return Marker(
            self.ignore_update or other.ignore_update,
            self.ignore_stale or other.ignore_stale,
            self.allow_drift or other.allow_drift,
            other.version_filter if self.version_filter == NO_BOUND else self.version_filter,
            self.invalid_specifier if self.invalid_specifier is not None else other.invalid_specifier,
        )


# The marker a bare `ignore` (or a single unrecognised ignore bracket) expresses: hold everything back.
_BARE_IGNORE = Marker(ignore_update=True, ignore_stale=True)

# The comment leads that can carry a marker: `#` in most formats we update, `//` in devcontainer.json (which is
# JSONC). Shared by the marker prefix and the standalone-comment check in `parse_marker`, so the two always agree
# on what counts as a comment.
_COMMENT_LEADS = ("#", "//")

# An `# update-time:` comment steers what happens to the reference on its line. It works inline on the reference's
# own line (valid in YAML and requirements) or as a standalone comment on the line directly above it (the form
# Dockerfiles need, as they reject inline comments). The prefix introduces a whitespace-separated list of one or
# more directives (see `_DIRECTIVE`); trailing text after the last directive (a reason) is allowed.
_MARKER_PREFIX = re.compile(rf"(?:{'|'.join(re.escape(lead) for lead in _COMMENT_LEADS)})\s*update-time:\s*")

# A single directive in a marker's directive list: a verb, optionally followed by a bracket. The `verb` group's
# text is a `Verb` member value; the `\b` keeps a word merely starting with a verb (`ignores`) from matching. The
# `bracket` group captures everything between the square brackets. It is optional only after `ignore`: the
# lookahead requires a bracket after `allow`, so a bare `allow` is not a directive. The trailing whitespace lets
# consecutive directives be matched one after another.
_DIRECTIVE = re.compile(r"(?P<verb>ignore\b|allow(?=\[))(?:\[(?P<bracket>[^\]]*)\])?\s*")


def parse_marker(line: str, previous_line: str) -> Marker:
    """Return the `# update-time:` directives affecting the line as a `Marker`.

    A directive is read inline on the line, or from the line directly above it when that is a standalone comment
    (the form Dockerfiles need, since they reject inline comments); requiring the preceding line to start with a
    comment lead (`#`, or `//`) keeps an inline marker from also affecting the line below it. Directives combine by
    listing them after a single `# update-time:` prefix and across the two placements; where directives conflict,
    the first one wins, inline directives before those on the line above. Parsing is pure (no logger or path), so
    an unparsable version specifier is carried out as `invalid_specifier` for the caller to report.
    """
    marker = _parse_marker_contents(line)
    if previous_line.lstrip().startswith(_COMMENT_LEADS):
        marker = marker.merge(_parse_marker_contents(previous_line))  # The inline directives win over those above.
    return marker


def _parse_marker_contents(text: str) -> Marker:
    """Return the marker expressed by the `# update-time:` directives in one line of text.

    Each `# update-time:` prefix introduces a whitespace-separated list of directives, so directives combine behind
    a single prefix (`# update-time: ignore[stale] allow[update>=3.13]`); the first token that is not a directive
    ends the list, so a trailing reason is allowed. Each directive folds into the marker with `Marker.merge`, so
    earlier directives win over later ones.
    """
    marker = Marker()
    for prefix in _MARKER_PREFIX.finditer(text):
        position = prefix.end()
        while directive := _DIRECTIVE.match(text, position):
            position = directive.end()
            marker = marker.merge(_parse_directive(directive))
    return marker


def _parse_directive(directive: re.Match[str]) -> Marker:
    """Return the marker expressed by a single parsed directive.

    A directive without a bracket degrades to its verb's default: a bare `ignore`, holding back everything, or the
    `allow` no-op, which expresses nothing. For `ignore` the bracketless form is the documented bare `ignore`; for
    `allow` it is an `allow[` whose bracket was never closed, since the lookahead in `_DIRECTIVE` guarantees a `[`
    follows the verb, but not that a bracket was consumed — nothing was captured, so there is no item to report as
    invalid, and the malformed directive is left as a no-op reason. A closed bracket, even one holding only an
    unrecognised item, is handled by `_parse_bracket` instead.
    """
    verb = Verb(directive.group("verb"))
    if (bracket := directive.group("bracket")) is None:
        return _BARE_IGNORE if verb is Verb.IGNORE else Marker()  # A bare `ignore`, or an unterminated `allow[`.
    return _parse_bracket(verb, bracket)


def _parse_bracket(verb: Verb, bracket: str) -> Marker:
    """Return the marker for a directive's bracket: its comma-separated items parsed for the verb and merged.

    A single unrecognised item under `ignore` degrades to a bare `ignore`, so a typo can't accidentally un-hold a
    held-back reference. Every other unrecognised item — one under `allow`, or any in a comma list — is carried out
    as `invalid_specifier`, so a mistyped bound (`allow[patch-updates]`, `ignore[stale, updaet]`) warns and leaves
    the reference unchanged rather than silently dropping the bound; only `ignore`'s hold-back has a safe direction
    to fail towards, so only it falls back rather than warning.
    """
    items = _bracket_items(bracket)
    markers = [_parse_bracket_item(verb, item) for item in items]
    if markers == [None] and verb is Verb.IGNORE:
        return _BARE_IGNORE
    marker = Marker()
    for item, item_marker in zip(items, markers, strict=True):
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
    """Return the marker for one bracket item of the given verb, or None when the item is unrecognised.

    Most of the vocabulary is per verb: `ignore[update]` and `ignore[stale]` hold back just the update or just the
    staleness warning, and `allow[digest-drift]` opts into adopting a re-pushed digest, while a bare `allow[update]`
    allows every update, which is the default anyway, keeping the two verbs complements. The update bounds — a
    specifier after `update`, or a `<level>-update` item — are shared between the verbs and delegated to
    `parse_bound`; a malformed `update` specifier surfaces from it as `InvalidSpecifier`, telling a mistyped bound
    (reported as an invalid item) apart from an unrecognised item (None) without re-testing the item's shape here.
    """
    match verb, item:
        case Verb.IGNORE, "update":
            return Marker(ignore_update=True)
        case Verb.ALLOW, "update":
            return Marker()  # bare `allow[update]`: the default no-op
        case Verb.IGNORE, "stale":
            return Marker(ignore_stale=True)
        case Verb.ALLOW, "digest-drift":
            return Marker(allow_drift=True)
    # not a keyword item — try the shared update bounds
    try:
        version_filter = parse_bound(verb, item)
    except InvalidSpecifier:
        return Marker(invalid_specifier=item.removeprefix("update"))  # `update` with an unparsable specifier
    return Marker(version_filter=version_filter) if version_filter is not None else None
