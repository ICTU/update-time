"""The reference kinds that carry their own marker: one a source has answered for, and one it has not."""

from dataclasses import dataclass, field
from typing import Self

from update_time.domain.reference import Reference, ResolvedReference
from update_time.markers.marker import Marker


@dataclass(frozen=True, kw_only=True)
class SteeredReference(Reference):
    """A reference, and the `# update-time:` directives steering what happens to it.

    A check that walks a collection of references reads the marker off each of them, where a check that reports
    one reference at a time is handed the marker beside it. The marker defaults to one that holds nothing back,
    which is what a reference with no line of its own to write a marker on carries.
    """

    marker: Marker = field(default_factory=Marker)

    @classmethod
    def from_reference(cls, reference: Reference, **resolved: object) -> Self:
        """Return this kind of reference, carrying the marker of the reference it is built from.

        A caller whose marker comes from elsewhere than the reference passes it, and that one wins.
        """
        if isinstance(reference, SteeredReference):
            resolved.setdefault("marker", reference.marker)
        return super().from_reference(reference, **resolved)


@dataclass(frozen=True, kw_only=True)
class SteeredResolvedReference(ResolvedReference, SteeredReference):
    """A reference a source has answered for, and the marker steering what is reported about the answer."""
