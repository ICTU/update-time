"""Update the base images of Dockerfiles, writing a version that a build argument holds onto that argument's line."""

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from update_time.domain.dependency import DependencyVersion, ImageNotLookedUp
from update_time.domain.file_type import DOCKERFILES
from update_time.domain.image_lookup import explaining_skipped_lookups
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.dockerfile import (
    BASE_IMAGE_REFERENCE,
    DIGESTLESS_BASE_IMAGE_REFERENCE,
    Dockerfile,
    SubstitutedLine,
    base_image_match,
    pins_a_digest_beside_a_variable,
)
from update_time.markers.marker import Marker, parse_marker
from update_time.references.file import rewrite_file
from update_time.references.match import matched_reference
from update_time.references.rewrite import update_references_in_lines
from update_time.sources.oci import TagGetter

if TYPE_CHECKING:
    from update_time.domain.bound import VersionBound
    from update_time.domain.dependency import PinnedDependency
    from update_time.domain.line import Line
    from update_time.manifests.dockerfile import ReferencePart


_LOG = get_logger("dockerfile")


@dataclass(eq=False)
class _Refusal:
    """A getter that leaves every reference as it is for the reason, whatever the reference names."""

    reason: ImageNotLookedUp

    def __post_init__(self) -> None:
        """Register that the getter does not look any image up, and why."""
        explaining_skipped_lookups(self, lambda _pinned: self.reason)

    def __call__(
        self, pinned: PinnedDependency, _version_bound: VersionBound, _cooldown_days: int, /, *, check_archival: bool
    ) -> DependencyVersion:
        """Return the reference as it is, saying why its image is not looked up."""
        del check_archival
        return DependencyVersion(version=pinned.version, image_not_looked_up=self.reason)


def _shared_lines(substituted_lines: list[SubstitutedLine | None]) -> set[int]:
    """Return the indexes of the lines that write a version axis for references or markers that differ.

    Each `FROM` line reads its own marker, so two lines sharing a reference can still ask for different versions.
    """
    writers: dict[int, set[tuple[str, Marker]]] = defaultdict(set)
    for substituted in filter(None, substituted_lines):
        for part in substituted.axis_parts:
            writers[part.index].add((substituted.substituted_reference, parse_marker(substituted)))
    return {index for index, line_writers in writers.items() if len(line_writers) > 1}


def _refusal_reason(substituted: SubstitutedLine, shared_lines: set[int]) -> ImageNotLookedUp | None:
    """Return why the reference's new version cannot be written, or None where it can."""
    if substituted.writes_version_across_lines:
        return ImageNotLookedUp.VERSION_ACROSS_LINES
    if any(part.index in shared_lines for part in substituted.axis_parts):
        return ImageNotLookedUp.ARGUMENT_SHARED_WITH_ANOTHER_FROM_LINE
    return None


def _updated_lines(lines: list[Line]) -> list[str]:
    """Return the Dockerfile's lines with each base image it pulls updated, each version axis where it is written."""
    dockerfile = Dockerfile(lines)
    get_new_version = TagGetter(dockerfile.images_not_pulled())
    substituted_lines = dockerfile.substituted_lines()
    shared_lines = _shared_lines(substituted_lines)
    result = [line.text for line in lines]
    # The new version axis of each part. A part that several `FROM` lines write gets its new axis once.
    rewrites: dict[ReferencePart, str] = {}
    for index, (line, substituted) in enumerate(zip(lines, substituted_lines, strict=True)):
        if substituted is None:
            digest_overrules = pins_a_digest_beside_a_variable(line.text)
            getter = (
                _Refusal(ImageNotLookedUp.DIGEST_OVERRULES_BUILD_ARGUMENTS) if digest_overrules else get_new_version
            )
            [result[index]] = update_references_in_lines(
                [line], BASE_IMAGE_REFERENCE, get_new_version=getter, logger=_LOG
            )
            continue
        reference = matched_reference(base_image_match(line.text), line.location)
        _LOG.substituted_reference(reference, substituted.substituted_reference)
        reason = _refusal_reason(substituted, shared_lines)
        getter = get_new_version if reason is None else _Refusal(reason)
        [updated_line] = update_references_in_lines(
            [substituted], DIGESTLESS_BASE_IMAGE_REFERENCE, get_new_version=getter, logger=_LOG
        )
        if reason is None:
            rewrites.update(substituted.axis_rewrites(updated_line))
    # Rewrite right to left, so a rewrite leaves the span of each part still to come on its line where it was.
    for part, new_axis in sorted(rewrites.items(), key=lambda rewrite: rewrite[0].span, reverse=True):
        start, end = part.span
        result[part.index] = result[part.index][:start] + new_axis + result[part.index][end:]
    return result


def update_dockerfiles() -> None:
    """Update the base image of Dockerfiles."""
    for dockerfile in glob_for(DOCKERFILES):
        rewrite_file(dockerfile, _updated_lines, _LOG)


def main() -> None:  # pragma: no cover
    """Update the base images in the repository's Dockerfiles."""
    update_dockerfiles()


if __name__ == "__main__":  # pragma: no cover
    main()
