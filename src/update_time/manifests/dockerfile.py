"""Read Dockerfiles: their `FROM` lines, build stages, and build arguments, and the lines with the arguments put in."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from update_time.domain.dependency import VERSION_NUMBERS, ImageNotLookedUp
from update_time.domain.line import Line
from update_time.manifests.image_reference import DIGESTLESS_IMAGE_REFERENCE, OPTIONALLY_TAGGED_IMAGE_REFERENCE

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from update_time.domain.dependency import PinnedDependency

# The `FROM` instruction up to the image reference, with an optional `--platform=…` flag between the two (common in
# multi-arch builds). The flag is matched but not captured, so a rewrite of the reference leaves it untouched.
_FROM_INSTRUCTION = r"^\s*(?i:FROM)\s+(?:--platform=\S+\s+)?"

# The image reference a `FROM` builds on.
BASE_IMAGE_REFERENCE = re.compile(rf"{_FROM_INSTRUCTION}{OPTIONALLY_TAGGED_IMAGE_REFERENCE}")
# The same reference without a digest, for a reference that names build arguments. Docker pulls a reference by its
# digest and ignores its tag, so a digest would overrule a `--build-arg` that overrides the tag.
DIGESTLESS_BASE_IMAGE_REFERENCE = re.compile(rf"{_FROM_INSTRUCTION}{DIGESTLESS_IMAGE_REFERENCE}")

# The empty base image Docker builds from. Registries do not serve it.
_SCRATCH = "scratch"

# A build argument declared with a default, which may be quoted. The value holds the characters of an image name, such
# as `acme/app`, or of a tag. Docker strips the quotes, so the value omits them.
_ARGUMENT_RE = re.compile(r"""^\s*(?i:ARG)\s+(?P<name>\w+)=(?P<quote>["']?)(?P<value>[\w./-]+)(?P=quote)(?=\s|$)""")

# A variable naming a build argument, with or without braces.
_ARGUMENT_VARIABLE_RE = re.compile(r"\$(\{)?(?P<name>\w+)(?(1)\})")

# The name a `FROM ... AS name` gives a build stage, which a later `FROM` in the same file names to build on it.
# Read case-insensitively, since Dockerfile keywords are, and a lower-case `as` is as common as an upper-case one.
_STAGE_NAME_RE = re.compile(rf"{_FROM_INSTRUCTION}.*\sAS\s+(?P<stage>\S+)", re.IGNORECASE | re.MULTILINE)

# A version axis of a tag: a run of digits and the dots between them, such as the `3.14` and `3.20` of
# `3.14-alpine3.20`.
_VERSION_AXIS_RE = re.compile(VERSION_NUMBERS)


@dataclass(frozen=True)
class ReferencePart:
    """A part of an image reference as a Dockerfile line writes it: the line's index and text, and the part's span."""

    index: int
    line: str
    span: tuple[int, int]

    @property
    def text(self) -> str:
        """Return the part as the line writes it."""
        start, end = self.span
        return self.line[start:end]


@dataclass(frozen=True)
class _BuildStages:
    """Which images a Dockerfile does not pull, and why: the empty base image, and its own build stages."""

    stages: frozenset[str]

    def __call__(self, pinned: PinnedDependency) -> ImageNotLookedUp | None:
        """Return why the Dockerfile does not pull the image the reference names, or None when it does.

        Neither `scratch` nor a build stage carries a tag to tell one from another, so the version decides nothing.
        """
        if pinned.name == _SCRATCH:
            return ImageNotLookedUp.EMPTY_BASE_IMAGE
        return ImageNotLookedUp.BUILD_STAGE if pinned.name.lower() in self.stages else None


@dataclass(frozen=True)
class Dockerfile:
    """A Dockerfile's lines, and what Docker reads from them: its build stages and its build arguments."""

    lines: Sequence[Line]

    def images_not_pulled(self) -> Callable[[PinnedDependency], ImageNotLookedUp | None]:
        """Return what says why the Dockerfile does not pull an image, or None where it does pull it."""
        return _BuildStages(self._stage_names())

    def substituted_lines(self) -> list[SubstitutedLine | None]:
        """Return each line with its reference's build arguments substituted, or None where they cannot be."""
        arguments = self._build_arguments()
        return [_substituted_line(index, line, arguments) for index, line in enumerate(self.lines)]

    def _stage_names(self) -> frozenset[str]:
        """Return the names the Dockerfile gives its build stages, in the lower case Docker matches them in."""
        text = "".join(line.text for line in self.lines)
        return frozenset(match.group("stage").lower() for match in _STAGE_NAME_RE.finditer(text))

    def _build_arguments(self) -> dict[str, ReferencePart]:
        """Return the defaults of the build arguments declared with one before the first `FROM`, by name.

        An `ARG` after the first `FROM` belongs to the stage declaring it, and a `FROM` does not read it.
        """
        lines = self.lines
        first_from = next(
            (index for index, line in enumerate(lines) if re.match(_FROM_INSTRUCTION, line.text)), len(lines)
        )
        return {
            declaration.group("name"): ReferencePart(index, line.text, declaration.span("value"))
            for index, line in enumerate(lines[:first_from])
            if (declaration := _ARGUMENT_RE.match(line.text))
        }


def base_image_match(line: str) -> re.Match[str]:
    """Return the match of the digestless base image reference on a `FROM` line that holds one."""
    return cast("re.Match[str]", re.search(DIGESTLESS_BASE_IMAGE_REFERENCE, line))


def pins_a_digest_beside_a_variable(line: str) -> bool:
    """Return whether the `FROM` line pins a digest beside a reference naming a variable, which the digest overrules."""
    reference = re.search(BASE_IMAGE_REFERENCE, line)
    if reference is None or reference.group("sha") is None:
        return False
    return _ARGUMENT_VARIABLE_RE.search(line, reference.start("dependency"), reference.end("version")) is not None


@dataclass(frozen=True)
class SubstitutedLine(Line):
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


def _substituted_line(index: int, line: Line, arguments: dict[str, ReferencePart]) -> SubstitutedLine | None:
    """Return the line with its reference's build arguments substituted, or None where they cannot be substituted.

    They cannot be where the reference does not name a build argument, or names one without a default. Neither can
    they be where a default does not fit where its variable stands, such as a `/` in a tag.
    """
    reference = re.search(DIGESTLESS_BASE_IMAGE_REFERENCE, line.text)
    if reference is None:
        return None
    start, end = reference.start("dependency"), reference.end("version")
    variables = list(_ARGUMENT_VARIABLE_RE.finditer(line.text, start, end))
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
    return SubstitutedLine(text, line.previous_text, line.location, _axis_parts(text, placed_parts))
