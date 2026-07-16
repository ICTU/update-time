"""Parse the `# update-time:` marker language.

Comments of the form `# update-time: <directive>…` let users steer what happens to an individual reference: hold it
back (`ignore`, optionally narrowed to the update or the staleness warning), bound how far it may update
(`allow[update<…>]` / `ignore[update<…>]`), or opt it into adopting a re-pushed image digest (`allow[digest-drift]`).
A marker is read inline on the reference's own line or from a standalone comment on the line directly above it; one
prefix can carry several whitespace-separated directives, and a directive's bracket several comma-separated items
(`ignore[stale, update>=3.13]`). Parsing is pure — text in, `Marker` out — so it lives in `domain`; acting on the
marker, and reporting about it, is left to the rewrite engine in `io`.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from update_time.domain.version import NO_BOUND, VersionFilter, parse_version_filter

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class Marker:
    """The `# update-time:` directives affecting a line (see `parse_marker`).

    `ignore_update` and `ignore_stale` are whether an `ignore` directive holds back the reference's update and its
    staleness warning: a bare `ignore` holds back both, `ignore[update]` and `ignore[stale]` each just one.
    `allow_drift` is whether an `allow[digest-drift]` directive opts the reference into adopting a re-pushed digest.
    `version_filter` is the bound from an `allow[update…]` / `ignore[update…]` directive that carries a specifier
    (see `VersionFilter`), defaulting to `NO_BOUND` (keep every candidate) when there is none. `invalid_specifier`
    is the raw text of a bracket item that could not be parsed — an invalid version specifier, or an unrecognised
    item in a comma list — so the caller can warn and leave the reference unchanged; None otherwise.
    """

    ignore_update: bool = False
    ignore_stale: bool = False
    allow_drift: bool = False
    version_filter: VersionFilter = NO_BOUND
    invalid_specifier: str | None = None

    def __str__(self) -> str:
        """Return the marker as the directive list it expresses, or an empty string when it expresses nothing.

        The rendering is normalised rather than the text the user wrote: combined `ignore` scopes collapse to a
        bare `ignore`, a bound's specifier is in PEP 440's clause order, and an invalid item is not a directive,
        so it is not rendered.
        """
        directives = []
        if self.ignore_update and self.ignore_stale:
            directives.append("ignore")
        elif self.ignore_update:
            directives.append("ignore[update]")
        elif self.ignore_stale:
            directives.append("ignore[stale]")
        if self.version_filter != NO_BOUND:
            verb = "allow" if self.version_filter.allow else "ignore"
            directives.append(f"{verb}[update{self.version_filter.specifier}]")
        if self.allow_drift:
            directives.append("allow[digest-drift]")
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

# A single directive in a marker's directive list: an `ignore` verb with an optional bracket, or an `allow` verb
# with a required one. `ignore` holds a reference back: bare `ignore` skips both the update and the staleness
# warning, `ignore[update]` only the update (a PEP 440 specifier after `update` bounds which updates are dropped
# instead of dropping them all), and `ignore[stale]` only the staleness warning. `allow` opts a reference into
# behaviour that is off by default: adopting a re-pushed image digest (`allow[digest-drift]`), or keeping only the
# updates whose version satisfies a specifier (`allow[update<…>]`). A bracket carries one item or a comma-separated
# list of them (`ignore[stale, update>=3.13]`); what the items mean — including the fallbacks for unrecognised
# ones, such as `ignore[stale<…>]`, since `stale` takes no specifier — is decided by the per-verb item parsers. The
# trailing whitespace lets consecutive directives be matched one after another; the first token that is not a
# directive ends the list.
_DIRECTIVE = re.compile(
    r"(?:(?P<ignore>ignore)\b(?:\[(?P<ignore_bracket>[^\]]*)\])?|allow\[(?P<allow_bracket>[^\]]*)\])\s*"
)


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
    """Return the marker expressed by a single parsed directive."""
    if directive.group("ignore"):
        return _parse_ignore_directive(directive.group("ignore_bracket"))
    return _parse_allow_directive(directive.group("allow_bracket"))


def _parse_ignore_directive(bracket: str | None) -> Marker:
    """Return the marker for an `ignore` directive: hold-backs, or a bound dropping matching updates.

    No bracket is a bare `ignore` (hold back both the update and the staleness warning); the bracket items narrow
    that (see `_parse_ignore_item`). A single unrecognised item (a typo, or `stale` with a specifier) falls back to
    a bare `ignore`.
    """
    if bracket is None:
        return _BARE_IGNORE  # bare `ignore`
    return _parse_bracket(bracket, _parse_ignore_item, fallback=_BARE_IGNORE)


def _parse_allow_directive(bracket: str) -> Marker:
    """Return the marker for an `allow` directive: a digest-drift opt-in, or a bound keeping matching updates.

    The bracket items opt the reference into behaviour that is off by default (see `_parse_allow_item`). A single
    unrecognised item (a typo) expresses nothing.
    """
    return _parse_bracket(bracket, _parse_allow_item, fallback=Marker())


def _parse_bracket(bracket: str, parse_item: Callable[[str], Marker | None], fallback: Marker) -> Marker:
    """Return the marker for a directive's bracket: its comma-separated items parsed by `parse_item` and merged.

    A single unrecognised item degrades to the verb's `fallback` (a bare `ignore`, or the `allow` no-op); in a
    comma list an unrecognised item is instead carried out as `invalid_specifier`, so a mistyped combination warns
    rather than silently doing the wrong thing.
    """
    items = _bracket_items(bracket)
    markers = [parse_item(item) for item in items]
    if markers == [None]:
        return fallback
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


def _parse_ignore_item(item: str) -> Marker | None:
    """Return the marker for one `ignore` bracket item, or None when the item is unrecognised.

    `update` and `stale` hold back just the update or just the staleness warning, and a specifier after `update`
    bounds which updates are dropped instead of dropping them all.
    """
    if item == "update":
        return Marker(ignore_update=True)
    if item == "stale":
        return Marker(ignore_stale=True)
    if item.startswith("update"):
        return _parse_version_bound(item.removeprefix("update"), allow=False)  # `ignore[update<…>]`
    return None


def _parse_allow_item(item: str) -> Marker | None:
    """Return the marker for one `allow` bracket item, or None when the item is unrecognised.

    `digest-drift` opts into adopting a re-pushed digest, and a specifier after `update` keeps only the updates
    that satisfy it (a bare `update` keeps them all, expressing nothing).
    """
    if item == "digest-drift":
        return Marker(allow_drift=True)
    if item == "update":
        return Marker()  # A bare `allow[update]` allows every update, which is the default anyway.
    if item.startswith("update"):
        return _parse_version_bound(item.removeprefix("update"), allow=True)  # `allow[update<…>]`
    return None


def _parse_version_bound(specifier: str, *, allow: bool) -> Marker:
    """Return the marker for a version bound: the parsed filter, or the invalid specifier for the caller to report."""
    parsed = parse_version_filter(specifier, allow=allow)
    return Marker(invalid_specifier=specifier) if parsed is None else Marker(version_filter=parsed)
