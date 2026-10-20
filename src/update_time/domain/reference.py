"""What a file records about a dependency it pins, what a source resolved for it, and whether the two agree."""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from update_time.domain.dependency import PinnedDependency
from update_time.primitives.digest import SHA256_DIGEST

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from update_time.domain.dependency import DependencyName, DependencyVersion, VersionString
    from update_time.primitives.location import Location

# A variable or a template standing in for part of a reference, whose value Update-time does not know: a `$VAR` or
# `${VAR}`, a Helm `{{ .Values.tag }}`, or a CircleCI `<< parameters.tag >>`. A `$VAR` takes every word character
# after its `$` and gives none back. Otherwise the regex tries every split between a variable and the characters
# around it, which takes seconds on a line that holds several variables and does not match.
VARIABLE = r"\$\w++|\$\{[^}]*\}|\{\{[^}]*\}\}|<<[^>]*>>"

# A Docker image reference as it appears in files: `dependency:version` with an optional `@sha256:digest`. A file
# format prefixes it with the keyword that introduces the reference (`FROM `, `image: `). The image name and the tag
# may hold a variable.
_IMAGE_NAME = rf"(?P<dependency>(?:[\w\d\./-]|{VARIABLE})+)"
_TAG_CHARACTER = rf"(?:[\d\w\.\-]|{VARIABLE})"
_IMAGE_DIGEST = rf"(?:@(?P<sha>{SHA256_DIGEST}))?"

# The reference as most file formats write it, its digest optional but its tag required.
IMAGE_REFERENCE = rf"{_IMAGE_NAME}:(?P<version>{_TAG_CHARACTER}+){_IMAGE_DIGEST}"

# The reference for a file format that writes it without a tag as well, such as a Dockerfile's `FROM python`. The
# tag then matches empty, at the position pinning writes it to. A colon is consumed only when a tag follows it. The
# reference ends at whitespace, a double quote, the end of the line, or a YAML flow collection's `,`, `]`, or `}`, so
# a reference holding syntax Update-time does not read as a variable does not match.
_OPTIONALLY_TAGGED_IMAGE = rf"{_IMAGE_NAME}(?::(?={_TAG_CHARACTER}))?(?P<version>{_TAG_CHARACTER}*)"
_REFERENCE_END = r'(?=[\s",\]}]|$)'
OPTIONALLY_TAGGED_IMAGE_REFERENCE = rf"{_OPTIONALLY_TAGGED_IMAGE}{_IMAGE_DIGEST}{_REFERENCE_END}"

# The same reference without a digest.
DIGESTLESS_IMAGE_REFERENCE = rf"{_OPTIONALLY_TAGGED_IMAGE}{_REFERENCE_END}"

# The same reference under a YAML `image:` key, as CircleCI, GitLab CI, Docker Compose, and Helm files write it. Its
# tag is optional, an `image:` naming none as readable a reference as one that names a tag.
YAML_IMAGE_REFERENCE = rf"image: {OPTIONALLY_TAGGED_IMAGE_REFERENCE}"


@dataclass(frozen=True)
class Reference:
    """A pinned reference as a file records it: a dependency, the version it is pinned to, and the line it sits on.

    The line is part of the reference because every report about one names where to edit it, so the two are never
    apart for long.

    `current_sha` is the commit SHA a GitHub Action `uses:` or pre-commit hook `rev:` is pinned to, in a `<sha> # <ref>`
    form whose comment names a version, a branch, or a tag. It is empty for a reference that does not record a SHA of
    its own. A subclass declares its own fields keyword-only, because `current_sha` carries a default and Python
    refuses the class otherwise.
    """

    dependency: DependencyName
    current_version: VersionString
    location: Location
    current_sha: str = ""

    @property
    def pinned(self) -> PinnedDependency:
        """Return the dependency and the version this reference pins it to."""
        return PinnedDependency(self.dependency, self.current_version)

    @classmethod
    def from_reference(cls, reference: Reference, **resolved: object) -> Self:
        """Return this kind of reference, carrying what the given reference records and what a source resolved."""
        return cls(
            reference.dependency, reference.current_version, reference.location, reference.current_sha, **resolved
        )


@dataclass(frozen=True, kw_only=True)
class ResolvedReference(Reference):
    """A reference, and the release a source resolved for that reference.

    The release is the version the run leaves the reference on. That is the new version when the run updated
    the reference, and the current version otherwise.
    """

    release: DependencyVersion


@dataclass(frozen=True, kw_only=True)
class DriftedPin(Reference):
    """A hash pin that no longer matches what it points at, and the hash it now resolves to."""

    new_sha: str


class RefKind(StrEnum):
    """A kind of git ref: a tag, a branch, an abbreviated commit SHA, or a ref of unknown kind."""

    TAG = "tag"
    BRANCH = "branch"
    SHORT_COMMIT_SHA = "short commit SHA"
    REF = "ref"


# What a delegating updater hands its checks: a function asking its source about the references of one file.
type ReferenceResolver[ReferenceT: Reference, ResolvedT: ResolvedReference] = Callable[
    [Iterable[ReferenceT]], Iterable[ResolvedT]
]


def resolved_references[ReferenceT: Reference, ResolvedT: ResolvedReference](
    declared: Iterable[Iterable[ReferenceT]], resolve: ReferenceResolver[ReferenceT, ResolvedT]
) -> Iterator[ResolvedT]:
    """Yield what the resolver answers for the references of each file."""
    for references in declared:
        yield from resolve(references)


def hash_drifted(resolved: str, pinned: str) -> bool:
    """Return whether a hash (resolved digest, commit SHA, or integrity hash) differs from the one already pinned."""
    return bool(resolved) and resolved != pinned
