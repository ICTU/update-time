"""What a file records about a dependency it pins, and what a source resolved for it."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyName, DependencyVersion, VersionString
    from update_time.primitives.location import Location


@dataclass(frozen=True)
class Reference:
    """A pinned reference as a file records it: a dependency, the version it is pinned to, and the line it sits on.

    The line is part of the reference because every report about one names where to edit it, so the two are never
    apart for long. A source asked about a reference reads its dependency and version alone.

    `current_sha` is the commit SHA the reference is pinned to when it carries one in a `<sha> # <version>` form (a
    GitHub Action `uses:` or pre-commit hook `rev:`); it is empty for a reference that is still an unpinned version
    tag or that records no SHA of its own.
    """

    dependency: DependencyName
    current_version: VersionString
    location: Location
    current_sha: str = ""


@dataclass(frozen=True, kw_only=True)
class ResolvedReference(Reference):
    """A reference and the release a source resolved for it, which the staleness and yank checks report on.

    The release is the one the run leaves the reference on: the version it moved to, or the version it stayed on.
    Every field a subclass of `Reference` adds is keyword-only, since `current_sha` above it carries a default.
    """

    release: DependencyVersion
