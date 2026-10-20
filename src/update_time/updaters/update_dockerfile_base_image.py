"""Update the base images of Dockerfiles, writing a version that a build argument holds onto that argument's line."""

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from update_time.domain.dependency import VERSION_NUMBERS, DependencyVersion, ImageNotLookedUp
from update_time.domain.file_type import DOCKERFILES
from update_time.domain.image_lookup import explaining_skipped_lookups
from update_time.domain.line import Line
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.dockerfile import (
    ARGUMENT_VARIABLE_RE,
    BASE_IMAGE_REFERENCE,
    DIGESTLESS_BASE_IMAGE_REFERENCE,
    SCRATCH,
    ReferencePart,
    base_image_match,
    build_arguments,
    stage_names,
)
from update_time.markers.marker import Marker, parse_marker
from update_time.references.file import rewrite_file
from update_time.references.match import matched_reference
from update_time.references.rewrite import update_references_in_lines
from update_time.sources.oci import tag_getter

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from update_time.domain.bound import NewVersionGetter, VersionBound
    from update_time.domain.dependency import PinnedDependency


_LOG = get_logger("dockerfile")
# A version axis of a tag: a run of digits and the dots between them, such as the `3.14` and `3.20` of
# `3.14-alpine3.20`.
_VERSION_AXIS_RE = re.compile(VERSION_NUMBERS)


def _refusal(reason: ImageNotLookedUp) -> NewVersionGetter:
    """Return a getter that leaves every reference as it is for the reason, whatever the reference names."""

    def get_new_version(
        pinned: PinnedDependency, _version_bound: VersionBound, _cooldown_days: int, /, *, check_archival: bool
    ) -> DependencyVersion:
        del check_archival
        return DependencyVersion(version=pinned.version, image_not_looked_up=reason)

    return explaining_skipped_lookups(get_new_version, lambda _pinned: reason)


@dataclass(frozen=True)
class _SubstitutedLine(Line):
    """A `FROM` line with its reference's build arguments substituted, and where each axis of its tag is written.

    The axis parts are empty where an axis is written across more than one line.
    """

    axis_parts: tuple[ReferencePart, ...]

    @property
    def writes_version_across_lines(self) -> bool:
        """Return whether a version axis of the tag stretches over more than one line."""
        return not self.axis_parts

    @property
    def substituted_reference(self) -> str:
        """Return the image reference, its build arguments substituted."""
        reference = base_image_match(self.text)
        return self.text[reference.start("dependency") : reference.end("version")]

    def axis_rewrites(self, updated_line: str) -> Iterator[tuple[ReferencePart, str]]:
        """Return where each version axis of the tag is written, with the axis the updated line has in its place."""
        return zip(self.axis_parts, _new_axes(self.text, updated_line), strict=True)


def _axis_parts(line: str, placed_parts: list[tuple[int, ReferencePart]]) -> tuple[ReferencePart, ...]:
    """Return where each version axis of the line's tag is written, or nothing where one stretches over two parts.

    Each part comes with its offset in the line. The last part holding an axis writes it, so the `FROM` line gets the
    tag of a tagless reference.
    """
    axis_parts = []
    for axis_start, axis_end in _axes(line):
        holders = [
            (offset, part)
            for offset, part in placed_parts
            if offset <= axis_start <= axis_end <= offset + len(part.text)
        ]
        if not holders:
            return ()
        offset, part = holders[-1]
        start = part.span[0] + axis_start - offset
        axis_parts.append(ReferencePart(part.index, part.line, (start, start + axis_end - axis_start)))
    return tuple(axis_parts)


def _new_axes(line: str, updated_line: str) -> list[str]:
    """Return the version axes of the updated line's tag, in the order of the axes of the line's tag.

    A tag without a version axis, such as `latest`, is one axis, which the whole new tag replaces. A tagless reference
    has one empty axis, which the whole tag replaces, with the `:` attaching it to the image.
    """
    tag = base_image_match(line).group("version")
    updated_reference = base_image_match(updated_line)
    if not tag:
        return [updated_line[updated_reference.end("dependency") : updated_reference.end("version")]]
    if _VERSION_AXIS_RE.search(tag) is None:
        return [updated_reference.group("version")]
    return [updated_line[start:end] for start, end in _axes(updated_line)]


def _axes(line: str) -> list[tuple[int, int]]:
    """Return the spans of the version axes of the tag on the `FROM` line, the whole tag where it holds none."""
    start, end = base_image_match(line).span("version")
    return [axis.span() for axis in _VERSION_AXIS_RE.finditer(line, start, end)] or [(start, end)]


def _substituted_line(index: int, line: Line, arguments: dict[str, ReferencePart]) -> _SubstitutedLine | None:
    """Return the line with its reference's build arguments substituted, or None where they cannot be substituted.

    They cannot be where the reference does not name a build argument, or names one without a default. Neither can
    they be where a default does not fit where its variable stands, such as a `/` in a tag.
    """
    reference = re.search(DIGESTLESS_BASE_IMAGE_REFERENCE, line.text)
    if reference is None:
        return None
    start, end = reference.start("dependency"), reference.end("version")
    variables = list(ARGUMENT_VARIABLE_RE.finditer(line.text, start, end))
    names = [variable.group("name") for variable in variables]
    if not variables or any(name not in arguments for name in names):
        return None
    reference_parts, position = [], start
    for variable, name in zip(variables, names, strict=True):
        reference_parts += [ReferencePart(index, line.text, (position, variable.start())), arguments[name]]
        position = variable.end()
    reference_parts.append(ReferencePart(index, line.text, (position, end)))
    text, placed_parts = line.text[:start], []
    for part in reference_parts:
        placed_parts.append((len(text), part))
        text += part.text
    text += line.text[end:]
    if re.search(DIGESTLESS_BASE_IMAGE_REFERENCE, text) is None:
        return None
    return _SubstitutedLine(text, line.previous_text, line.location, _axis_parts(text, placed_parts))


def _pins_a_digest_beside_a_variable(line: str) -> bool:
    """Return whether the `FROM` line pins a digest beside a reference naming a variable, which the digest overrules."""
    reference = re.search(BASE_IMAGE_REFERENCE, line)
    if reference is None or reference.group("sha") is None:
        return False
    return ARGUMENT_VARIABLE_RE.search(line, reference.start("dependency"), reference.end("version")) is not None


def _shared_lines(substituted_lines: list[_SubstitutedLine | None]) -> set[int]:
    """Return the indexes of the lines that write a version axis for references or markers that differ.

    Each `FROM` line reads its own marker, so two lines sharing a reference can still ask for different versions.
    """
    writers: dict[int, set[tuple[str, Marker]]] = defaultdict(set)
    for substituted_line in filter(None, substituted_lines):
        for part in substituted_line.axis_parts:
            writers[part.index].add((substituted_line.substituted_reference, parse_marker(substituted_line)))
    return {index for index, line_writers in writers.items() if len(line_writers) > 1}


def _refusal_reason(substituted_line: _SubstitutedLine, shared_lines: set[int]) -> ImageNotLookedUp | None:
    """Return why the reference's new version cannot be written, or None where it can."""
    if substituted_line.writes_version_across_lines:
        return ImageNotLookedUp.VERSION_ACROSS_LINES
    if any(part.index in shared_lines for part in substituted_line.axis_parts):
        return ImageNotLookedUp.ARGUMENT_SHARED_WITH_ANOTHER_FROM_LINE
    return None


def _updated_lines(lines: list[Line], get_new_version: NewVersionGetter) -> list[str]:
    """Return the Dockerfile's lines with each base image updated, each version axis on the line that writes it."""
    arguments = build_arguments(lines)
    substituted_lines = [_substituted_line(index, line, arguments) for index, line in enumerate(lines)]
    shared_lines = _shared_lines(substituted_lines)
    result = [line.text for line in lines]
    # The new version axis of each part. A part that several `FROM` lines write gets its new axis once.
    rewrites: dict[ReferencePart, str] = {}
    for index, (line, substituted_line) in enumerate(zip(lines, substituted_lines, strict=True)):
        if substituted_line is None:
            digest_overrules = _pins_a_digest_beside_a_variable(line.text)
            getter = (
                _refusal(ImageNotLookedUp.DIGEST_OVERRULES_BUILD_ARGUMENTS) if digest_overrules else get_new_version
            )
            [result[index]] = update_references_in_lines(
                [line], BASE_IMAGE_REFERENCE, get_new_version=getter, logger=_LOG
            )
            continue
        reference = matched_reference(base_image_match(line.text), line.location)
        _LOG.substituted_reference(reference, substituted_line.substituted_reference)
        reason = _refusal_reason(substituted_line, shared_lines)
        getter = get_new_version if reason is None else _refusal(reason)
        [updated_line] = update_references_in_lines(
            [substituted_line], DIGESTLESS_BASE_IMAGE_REFERENCE, get_new_version=getter, logger=_LOG
        )
        if reason is None:
            rewrites.update(substituted_line.axis_rewrites(updated_line))
    # Rewrite right to left, so a rewrite leaves the span of each part still to come on its line where it was.
    for part, new_axis in sorted(rewrites.items(), key=lambda rewrite: rewrite[0].span, reverse=True):
        start, end = part.span
        result[part.index] = result[part.index][:start] + new_axis + result[part.index][end:]
    return result


def _update_dockerfile(dockerfile: Path) -> None:
    """Update the base images in one Dockerfile, leaving the references to an image it does not pull unchanged."""
    stages = stage_names(dockerfile)

    def image_not_pulled(pinned: PinnedDependency) -> ImageNotLookedUp | None:
        """Return why the Dockerfile does not pull the image the reference names, or None when it does.

        Neither `scratch` nor a build stage carries a tag to tell one from another, so the version decides nothing.
        """
        if pinned.name == SCRATCH:
            return ImageNotLookedUp.EMPTY_BASE_IMAGE
        return ImageNotLookedUp.BUILD_STAGE if pinned.name.lower() in stages else None

    get_new_version = tag_getter(image_not_pulled)
    rewrite_file(dockerfile, lambda lines: _updated_lines(lines, get_new_version), _LOG)


def update_dockerfiles() -> None:
    """Update the base image of Dockerfiles."""
    for dockerfile in glob_for(DOCKERFILES):
        _update_dockerfile(dockerfile)


def main() -> None:  # pragma: no cover
    """Update the base images in the repository's Dockerfiles."""
    update_dockerfiles()


if __name__ == "__main__":  # pragma: no cover
    main()
