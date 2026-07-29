"""Parse the `# update-time:` marker language.

Comments of the form `# update-time: <directive>…` let users steer what happens to an individual reference: hold it
back (`ignore`, optionally narrowed to the update or the staleness warning), bound how far it may update — to an
absolute version range (`allow[update<…>]` / `ignore[update<…>]`) or by update level (`allow[minor-update]` /
`ignore[major-update]`) — or opt it into adopting a re-pushed image digest (`allow[pin-drift]`).
A marker is read inline on the reference's own line or from a standalone comment on the line directly above it; one
prefix can carry several whitespace-separated directives, and a directive's bracket several comma-separated items
(`ignore[stale, update>=3.13]`). Parsing is pure — a `Line` in, a `Marker` out — so it lives in `domain`; acting on
the marker, and reporting about it, is left to the rewrite engine in `references`.
"""

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
    update, its staleness warning, and its yank warning: a bare `ignore` holds back all three, while
    `ignore[update]`, `ignore[stale]`, and `ignore[yanked]` each hold back just one.
    `allow_drift` is whether an `allow[pin-drift]` directive opts the reference into adopting a re-pushed digest.
    `version_bound` is the version bound from an `allow`/`ignore` directive (see `VersionBound`), defaulting to
    `NO_BOUND` (keep every candidate) when there is none.
    `invalid_specifier` is the raw text of a bracket item that could not be parsed — an invalid version specifier,
    or an unrecognised item in a comma list — so the caller can warn and leave the reference unchanged; None otherwise.
    `raw` is the marker's whole directive text exactly as it appears in the file, read back through `raw_marker` so
    the reference's marker can be echoed to the user verbatim.
    """

    ignore_update: bool = False
    ignore_stale: bool = False
    ignore_yanked: bool = False
    allow_drift: bool = False
    version_bound: VersionBound = NO_BOUND
    invalid_specifier: str | None = None
    raw: str = field(compare=False, default="")

    def raw_marker(self, verb: Verb | None = None) -> str:
        """Return the marker's verbatim directive text, or just the directives of one verb.

        A verb's directives are picked back out of `raw` with the same grammar that parsed it, so the text stays
        exactly as the user spelled it — a collapsed scope or a typo kept as written.
        """
        if verb is None:
            return self.raw
        directives = (match for match in _DIRECTIVE.finditer(self.raw) if match.group("verb") == verb)
        return " ".join(match.group().strip() for match in directives)

    def merge(self, other: Marker) -> Marker:
        """Return this marker combined with another one.

        The boolean hold-backs and opt-ins combine as unions, so `ignore[update]` and `ignore[stale]` together hold
        back as much as a bare `ignore`; of two values that cannot combine — a version bound, an invalid specifier —
        this marker's wins, and the `raw` texts concatenate in order, this marker's first. A default `Marker()`
        leaves every field unset, so it is the identity: merging it with any marker returns that marker's values.
        This lets markers fold at every level — each item into a bracket's marker, each directive into a text's, and
        the inline and comment-above texts into the line's.
        """
        return Marker(
            self.ignore_update or other.ignore_update,
            self.ignore_stale or other.ignore_stale,
            self.ignore_yanked or other.ignore_yanked,
            self.allow_drift or other.allow_drift,
            other.version_bound if self.version_bound == NO_BOUND else self.version_bound,
            self.invalid_specifier if self.invalid_specifier is not None else other.invalid_specifier,
            " ".join(part for part in (self.raw, other.raw) if part),
        )


# The marker a bare `ignore` (or a single unrecognised ignore bracket) expresses: hold everything back.
_BARE_IGNORE = Marker(ignore_update=True, ignore_stale=True, ignore_yanked=True)

# The keyword bracket items each verb recognises, and the marker each expresses. An `ignore` scope holds back just
# the update, just the staleness warning, or just the yank warning, and `allow[pin-drift]` opts into adopting a
# re-pushed digest, while a bare `allow[update]` allows every update, which is the default anyway, keeping the two
# verbs complements. Items outside this vocabulary are update bounds, parsed by `parse_bound`.
_KEYWORD_ITEMS = {
    (Verb.IGNORE, "update"): Marker(ignore_update=True),
    (Verb.IGNORE, "stale"): Marker(ignore_stale=True),
    (Verb.IGNORE, "yanked"): Marker(ignore_yanked=True),
    (Verb.ALLOW, "update"): Marker(),  # bare `allow[update]`: the default no-op
    (Verb.ALLOW, "pin-drift"): Marker(allow_drift=True),
}

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


def parse_marker(line: Line) -> Marker:
    """Return the `# update-time:` directives affecting the line as a `Marker`.

    A directive is read inline on the line, or from the line directly above it when that is a standalone comment
    (the form Dockerfiles need, since they reject inline comments); requiring the preceding line to start with a
    comment lead (`#`, or `//`) keeps an inline marker from also affecting the line below it. Directives combine by
    listing them after a single `# update-time:` prefix and across the two placements; where directives conflict,
    the first one wins, inline directives before those on the line above. Taking the whole `Line` is what keeps the
    two placements from being read apart: a caller cannot hand over one of them and silently lose the other. Parsing
    reports nothing itself, so an unparsable version specifier is carried out as `invalid_specifier` for the caller
    to report against the line's location.
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
    marker can later be echoed back to the user exactly as they spelled it (see `Marker.raw_marker`).
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

    The keyword vocabulary is per verb (see `_KEYWORD_ITEMS`). An update bound, a specifier after `update` or a
    `<level>-update` item, is shared between the verbs and delegated to `parse_bound`. A malformed `update` specifier
    surfaces from it as `InvalidSpecifier`, which tells a mistyped bound apart from an unrecognised item without
    re-testing the item's shape here.
    """
    if (keyword_marker := _KEYWORD_ITEMS.get((verb, item))) is not None:
        return keyword_marker
    # not a keyword item — try the shared update bounds
    try:
        version_bound = parse_bound(verb, item)
    except InvalidSpecifier:
        return Marker(invalid_specifier=item.removeprefix("update"))  # `update` with an unparsable specifier
    return Marker(version_bound=version_bound) if version_bound is not None else None
