"""A reference's `# update-time:` marker, its scopes and thresholds, and the spelling of its directives."""

from dataclasses import dataclass, field, replace
from enum import Flag, auto
from functools import reduce
from typing import TYPE_CHECKING, Final

from packaging.version import Version

from .bound import BLOCK_ALL_UPDATES, NO_BOUND, Verb, keeping_each

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .bound import BoundRedundancy, VersionBound
    from .dependency import VersionString


class Scope(Flag):
    """What an item steers, rendering as the bracket item naming it.

    A flag rather than a plain enum, so the scopes a marker steers combine into one value: `Scope.STALE` is one
    scope and `Scope.STALE | Scope.YANKED` two. The items are spelled once here, for reading a marker and for naming
    a scope back in a warning, so the two cannot come to disagree. The spelling itself is fixed, being the language
    users write in their own repositories.
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
_NO_SCOPE: Final[Scope] = Scope(0)


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
    """The `# update-time:` directives affecting a line.

    `ignored_scopes` are the scopes an `ignore` holds back, such as the yank warning for `ignore[yanked]`.
    `ignored_advisories` are the advisories an `ignore[vulnerable=ID]` silences, as the user spelled them.
    `allowed_scopes` are the scopes an `allow` opts into, such as `allow[hash-drift]` and `allow[floating-pin]`.
    `version_bound` is the bound an `allow` or `ignore` directive sets (see `VersionBound`).
    `stale`, `cooldown`, and `vulnerable` are what `set` items set, such as `set[stale-after=90]` (see `Threshold`).

    `invalid_item` is the text of a bracket item that does not parse, such as an invalid version specifier.
    `default_directives` are the directives asking for what Update-time does by default, such as `ignore[hash-drift]`.
    Comparisons leave them out.
    `raw` is the marker's directive text as the file writes it.
    `spelling` is the marker as the file spells it where that is more than `raw`, such as the JSON of a package.json's
    `update-time` field.
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
    spelling: str = field(compare=False, default="")

    def __str__(self) -> str:
        """Render the marker as the file spells it (see `spelling`)."""
        return self.spelling or self.raw

    @property
    def bound_directive(self) -> str:
        """Return the directive bounding the update, as the language spells it, or nothing when the marker sets none.

        An `ignore[update]` has one spelling, so it is spelled out. Where a reference carries both, the `ignore[update]`
        is named, since it holds every update back whatever the bound would admit.
        """
        if self.ignores(Scope.UPDATE):
            return spell(BLOCK_ALL_UPDATES)
        return self.version_bound_directive

    def ignores(self, scope: Scope) -> bool:
        """Return whether the marker holds the scope back outright.

        The outright form of a scope is one that a bound cannot express: `ignore[update]` blocks every version, and a
        bound narrows the versions an update may take. A check whose forms fold into one decision reads this alongside
        them (see `directive_for`).
        """
        return scope in self.ignored_scopes

    @property
    def version_bound_directive(self) -> str:
        """Return the directive narrowing which versions an update may take, or nothing when the marker sets none.

        The bound is named as the item the user wrote, since `ignore` and `allow` can each set one. An
        `ignore[update]` beside it leaves it named.
        """
        return "" if self.version_bound == NO_BOUND else spell(self.version_bound)

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
    def _vulnerable_directives(self) -> str:
        """Return every directive the marker's `vulnerable` scope carries, as the language spells them.

        The scope has two forms and a reference can carry both at a time, so this names both. Where one form alone is
        reported, that form's own directive is named instead, since the other may hold plenty back.
        """
        forms = (self.advisory_directives, self.vulnerable.directive)
        return " ".join(form for form in forms if form)

    @property
    def advisory_directives(self) -> str:
        """Return the directive silencing the advisories the marker names, as the language spells it.

        A marker naming several advisories is judged as one, so they share a bracket, which is how the language
        spells a second advisory. They are sorted, since a `frozenset` keeps no order, and a marker split over two
        placements reaches them in the order they merged rather than the order they were written.
        """
        items = ", ".join(f"{Scope.VULNERABLE}={advisory}" for advisory in sorted(self.ignored_advisories))
        return directive(Verb.IGNORE, items) if items else ""

    @property
    def cooldown_directive(self) -> str:
        """Return the directive that sets the cooldown, as the language spells it."""
        return self.cooldown.directive

    def scope_directive(self, scope: Scope) -> str:
        """Return the directive steering the scope, as the language spells it, or nothing when it steers it not.

        A scope has one spelling, so the directive is spelled out rather than read back from the text the user wrote:
        an `ignore` naming the scope. Spelling it out leaves out a directive beside it that does hold something back,
        and names the scope alone when it was written in a bracket it shares with other items.
        """
        return directive(Verb.IGNORE, str(scope)) if self.ignores(scope) else ""

    def update_specifier(self, current_version: VersionString) -> str | None:
        """Return the specifier keeping an update within the marker's bound, or None where the marker holds it back.

        A bound that leaves the current version out holds the update back, since the update would move into the bound.
        So does a bound that a specifier cannot carry.
        """
        bound = self.version_bound
        if self.ignores(Scope.UPDATE) or not bound.keeps(Version(current_version), current_version):
            return None
        return bound.specifier_in_allow_form(current_version)

    def bound_redundancy_at(self, current_version: VersionString) -> BoundRedundancy | None:
        """Return how the marker's bound is redundant at the current version, or None where it sets none or is live."""
        return None if self.version_bound == NO_BOUND else self.version_bound.redundancy(current_version)

    def allow_directive(self, scope: Scope) -> str:
        """Return the directive opting the reference into the scope, or nothing when the marker opts into it not.

        Spelled out rather than read back from the text the user wrote, for the reason `scope_directive` gives:
        spelling it out leaves out an `allow` directive beside it that opts into something else.
        """
        return directive(Verb.ALLOW, str(scope)) if self.allows(scope) else ""

    def allows(self, scope: Scope) -> bool:
        """Return whether an `allow` directive opts the reference into the scope, which is off without one."""
        return scope in self.allowed_scopes

    @staticmethod
    def combined(markers: Sequence[Marker], current_version: VersionString) -> Marker:
        """Return one marker holding back what each of the markers holds back, for a reference at the current version.

        The markers combine as `merge` combines them, except for the bound. The bound keeps the versions each marker's
        bound keeps, and where the bounds do not combine, the update is held back.
        """
        merged = reduce(Marker.merge, markers, Marker())
        bound = keeping_each((marker.version_bound for marker in markers), current_version)
        held_back = Scope.UPDATE if bound == BLOCK_ALL_UPDATES else _NO_SCOPE
        return replace(merged, ignored_scopes=merged.ignored_scopes | held_back, version_bound=bound)

    def merge(self, other: Marker) -> Marker:
        """Return this marker combined with another one.

        `Marker()` is the identity. The result leaves `spelling` out, since a marker is spelled only once it is whole.
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


# The marker of a reference whose marker is not read, which holds nothing back.
NO_MARKER: Final[Marker] = Marker()


def directive(verb: Verb, item: str) -> str:
    """Return a directive as the language spells it: a verb and the bracketed item that sets a scope."""
    return f"{verb}[{item}]"


def spell(version_bound: VersionBound) -> str:
    """Return the bound as the directive that expresses it, e.g. `allow[update<3.13]`."""
    return directive(version_bound.verb, version_bound.item)
