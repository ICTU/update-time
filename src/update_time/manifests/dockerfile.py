"""Read the `FROM` instructions, the build stages, and the build arguments of Dockerfiles."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from update_time.domain.reference import DIGESTLESS_IMAGE_REFERENCE, OPTIONALLY_TAGGED_IMAGE_REFERENCE

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.line import Line

# The `FROM` instruction up to the image reference, with an optional `--platform=…` flag between the two (common in
# multi-arch builds). The flag is matched but not captured, so a rewrite of the reference leaves it untouched.
_FROM_INSTRUCTION = r"^\s*(?i:FROM)\s+(?:--platform=\S+\s+)?"

# The image reference a `FROM` builds on.
BASE_IMAGE_REFERENCE = rf"{_FROM_INSTRUCTION}{OPTIONALLY_TAGGED_IMAGE_REFERENCE}"
# The same reference without a digest, for a reference that names build arguments. Docker pulls a reference by its
# digest and ignores its tag, so a digest would overrule a `--build-arg` that overrides the tag.
DIGESTLESS_BASE_IMAGE_REFERENCE = rf"{_FROM_INSTRUCTION}{DIGESTLESS_IMAGE_REFERENCE}"

# The empty base image Docker builds from. Registries do not serve it.
SCRATCH = "scratch"

# A build argument declared with a default, which may be quoted. The value holds the characters of an image name, such
# as `acme/app`, or of a tag. Docker strips the quotes, so the value omits them.
_ARGUMENT_RE = re.compile(r"""^\s*(?i:ARG)\s+(?P<name>\w+)=(?P<quote>["']?)(?P<value>[\w./-]+)(?P=quote)(?=\s|$)""")

# A variable naming a build argument, with or without braces.
ARGUMENT_VARIABLE_RE = re.compile(r"\$(\{)?(?P<name>\w+)(?(1)\})")

# The name a `FROM ... AS name` gives a build stage, which a later `FROM` in the same file names to build on it.
# Read case-insensitively, since Dockerfile keywords are, and a lower-case `as` is as common as an upper-case one.
_STAGE_NAME_RE = re.compile(rf"{_FROM_INSTRUCTION}.*\sAS\s+(?P<stage>\S+)", re.IGNORECASE | re.MULTILINE)


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


def stage_names(dockerfile: Path) -> frozenset[str]:
    """Return the names the Dockerfile gives its build stages, in the lower case Docker matches them in."""
    return frozenset(match.group("stage").lower() for match in _STAGE_NAME_RE.finditer(dockerfile.read_text()))


def build_arguments(lines: list[Line]) -> dict[str, ReferencePart]:
    """Return the defaults of the build arguments the Dockerfile declares with one before its first `FROM`, by name.

    An `ARG` after the first `FROM` belongs to the stage declaring it, and a `FROM` does not read it.
    """
    first_from = next((index for index, line in enumerate(lines) if re.match(_FROM_INSTRUCTION, line.text)), len(lines))
    return {
        declaration.group("name"): ReferencePart(index, line.text, declaration.span("value"))
        for index, line in enumerate(lines[:first_from])
        if (declaration := _ARGUMENT_RE.match(line.text))
    }


def base_image_match(line: str) -> re.Match[str]:
    """Return the match of the digestless base image reference on a `FROM` line that holds one."""
    return cast("re.Match[str]", re.search(DIGESTLESS_BASE_IMAGE_REFERENCE, line))
