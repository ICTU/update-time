"""Get the latest available image tags from OCI registries (Docker Hub and others).

This is a generic OCI distribution client: it resolves image references on any registry (Docker Hub, ghcr.io,
mcr.microsoft.com, quay.io, ...) by listing tag names, discovering the registry's auth via the OCI
`WWW-Authenticate` challenge, and reading the digest to pin from the tag's manifest. Docker-Hub-specific behavior
(the push date the cooldown and the staleness check rely on, and credentials that raise the rate limit) lives in
`docker_hub`.
"""

import re
from dataclasses import dataclass, replace
from datetime import date
from functools import cache, cached_property, partial, total_ordering
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

from packaging.version import InvalidVersion, Version

from update_time.domain.cooldown import CooldownWalk, first_eligible
from update_time.domain.dependency import (
    DASHED_SNAPSHOT,
    DATED_SNAPSHOT,
    LOWEST_VERSION,
    MAIN_VERSION,
    UNDATED,
    DependencyName,
    DependencyVersion,
    FloatingPin,
    ImageNotLookedUp,
    PinnedDependency,
    Project,
    Release,
    Unserved,
    VersionString,
    tag_of,
)
from update_time.domain.image_lookup import explaining_skipped_lookups
from update_time.domain.publication import publication_date_reporting, reports_publication_dates
from update_time.domain.reference import VARIABLE
from update_time.io.fetch import fetch, next_page_url
from update_time.io.log import get_logger
from update_time.primitives.lookup import DeferredLookup, LookedUp
from update_time.sources import docker_hub

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from update_time.domain.bound import VersionBound
    from update_time.io.fetch import Response
    from update_time.primitives.lookup import Lookup

_LOG = get_logger("oci")

# The tag Docker resolves a reference naming no tag to, which floats like any other channel.
_DEFAULT_TAG = "latest"

# The labels naming a channel the registry re-points rather than a variant of the image. A version tag can
# carry one — `24-lts` names whichever 24 release is the LTS one — so a pin keeping it would float on.
_CHANNEL_LABELS = frozenset({_DEFAULT_TAG, "lts", "stable", "edge"})

# Image references without a registry host are on Docker Hub, whose OCI registry host differs from the user-facing
# `docker.io` alias and whose images live under an implicit `library/` namespace.
_DOCKER_HUB_OCI_HOST = "registry-1.docker.io"

# The page size for the registry's tag listing. `_tag_names` follows the registry's pagination links, so this only
# sizes the pages, it does not cap how many tags are considered.
_TAGS_PAGE_SIZE = 1000

# What a registry's challenge says when it refuses the token rather than the repository the token is for.
_AUTHORIZATION_REFUSED = 'error="invalid_token"'

# Media types offered when resolving a manifest digest, so the registry returns the multi-arch index digest (the
# digest to pin) when the image is multi-arch, and the single image-manifest digest otherwise.
_MANIFEST_MEDIA_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)

# The structure of an image tag: an optional non-numeric label prefix, a version, and an optional suffix, e.g.
# `python3.12-bookworm-slim` -> prefix `python`, version `3.12`, suffix `bookworm-slim`. Prefix and version both
# exclude `-`, so the version ends where the suffix begins; numeric-leading tags (`3.12-slim`) get an empty prefix.
# Tags without a version (e.g. `bookworm-slim`) don't match at all; `version` still validates the match with packaging.
# The prefix class is possessive (`*+`) for the same reason `MAIN_VERSION` is: the engine never backtracks across
# it to salvage a failing match, which keeps matching linear.
_TAG = re.compile(rf"(?P<prefix>[^\d-]*+)(?P<version>{MAIN_VERSION})-?(?P<suffix>.*)$")

# A dated snapshot the repository labels. `_TAG` reads no version in one, its prefix admitting no dash, so the label
# is read as the prefix here and the date as the version, which puts such a snapshot on the line its label names.
_DATED_SNAPSHOT = re.compile(DATED_SNAPSHOT)

# A snapshot named by a date spelled with dashes. `_TAG` would read its year as the version and the rest as a suffix,
# so this is tried first.
_DASHED_SNAPSHOT = re.compile(DASHED_SNAPSHOT)

# A build number after the version, such as the `_10` of `26.0.2_10`. PEP 440 spells it as a local version, `+10`,
# which orders the builds of one version and leaves the version itself at `26.0.2`.
_BUILD_NUMBER = re.compile(r"_(\d+)$")

# What orders two tags: each version axis, and the number of components it is spelled with.
type _SortKey = tuple[Version, Version, int, int]

# What orders the aliases of one digest: the version, how precisely it is spelled, the version of each variant
# label the floating tag asked for, and the shorter name last.
type _AliasKey = tuple[Version, int, tuple[Version, ...], int]


@total_ordering
@dataclass(frozen=True)
class Tag:
    """An image tag: name-only when listed, with a digest and (Docker Hub only) push date once resolved."""

    name: str
    digest: str = ""
    pushed: Lookup[datetime] = UNDATED  # When Docker Hub says the tag was pushed, or why it could not say

    @cached_property
    def _match(self) -> re.Match[str] | None:
        """Parse the tag name into its prefix, version, and suffix once, or None when the tag has no version."""
        return _DASHED_SNAPSHOT.match(self.name) or _TAG.match(self.name) or _DATED_SNAPSHOT.match(self.name)

    @property
    def prefix(self) -> str:
        """Return the label before the version, as `python` in `python3.12` or `x86_64-` in `x86_64-2023-10-20`."""
        return self._match.group("prefix") if self._match else ""

    @cached_property
    def version(self) -> Version | None:
        """Return the parsed version, or None if the tag does not contain a valid version.

        A leading label prefix (e.g. the `python` in `python3.12`) is stripped before parsing the version. A date drops
        its dashes, so `2023-10-20` reads as `20231020`, and `2026-02-12-23-06-03` as `20260212230603`. A build number
        reads as a local version, so `26.0.2_10` reads as `26.0.2+10`.
        """
        if not (match := self._match):
            return None
        try:
            return Version(_BUILD_NUMBER.sub(r"+\1", match.group("version").replace("-", "")))
        except InvalidVersion:
            return None

    @property
    def sortable_version(self) -> Version:
        """Return the tag's version, or the lowest sentinel when it has none, for ordering and comparison.

        Unlike `version` this is never None, so tags (and, via `_suffix_tag`, their suffixes) compare and sort
        uniformly; a tag without a valid version sorts and compares as the lowest.
        """
        return self.version or LOWEST_VERSION

    @property
    def suffix(self) -> str:
        """Return the non-version suffix of the tag (e.g., 'slim' in '3.12-slim'), or empty string if none.

        Only meaningful for a tag that has a version (it's compared between a candidate and the current tag, both of
        which have one); a tag without a version doesn't match and the suffix is reported as empty.
        """
        return self._match.group("suffix") if self._match else ""

    @cached_property
    def _suffix_tag(self) -> Tag:
        """Parse the suffix as a tag in its own right: it has the same shape (a label, a version, a remainder).

        This gives the suffix a second, independent version axis, e.g. the `3.23` in `alpine3.23`. Only the parsed
        parts (its `prefix`, `version`, and `suffix`) are used; the nested tag's digest and push date are never
        resolved, because a suffix is not a pullable image.
        """
        return Tag(name=self.suffix)

    @property
    def suffix_label(self) -> tuple[str, str]:
        """Return the non-version part of the suffix, which must match exactly to prevent variant drift.

        The two strings are the parts of the suffix around its embedded version: the label before it and the
        remainder after it (`alpine3.19-slim` -> `('alpine', 'slim')`, `alpine3.23` -> `('alpine', '')`). Returning
        them as a pair keeps them comparable without a separator that could collide. A suffix without an embedded
        version has no version to strip, so its whole string is the label (and the second string is empty), and the
        check reduces to an exact-suffix match (`slim` still only matches `slim`).
        """
        if self._suffix_tag.version is None:
            return (self.suffix, "")
        return (self._suffix_tag.prefix, self._suffix_tag.suffix)

    @property
    def sortable_suffix_version(self) -> Version:
        """Return the embedded suffix version, or the lowest sentinel when the suffix has none, for ordering.

        The suffix is a tag in its own right, so this is simply its `sortable_version`.
        """
        return self._suffix_tag.sortable_version

    @property
    def _sort_key(self) -> _SortKey:
        """Return what orders tags newest-first: both version axes, and how precisely each of them is spelled.

        Tags whose versions are equal are ordered by precision, so 1.3 sorts below 1.3.0. Tags differing in a label
        alone share a key, which is a tie the aliases of one digest are ordered on separately.
        """
        return (
            self.sortable_version,
            self.sortable_suffix_version,
            len(self.sortable_version.release),
            len(self.sortable_suffix_version.release),
        )

    def __lt__(self, other: Tag) -> bool:
        """Order tags by version axes (main version, then embedded suffix version), so candidates sort newest-first.

        This flat ordering picks the highest candidate; `is_newer_or_equal` is the separate axis-wise check that
        decides candidacy.
        """
        return self._sort_key < other._sort_key

    def is_newer_or_equal(self, tag: Tag) -> bool:
        """Return whether this tag is at least as new as `tag` on every version axis.

        A tag's version axes are its main version and, recursively, the version embedded in its suffix (`alpine3.23`),
        so this compares `sortable_version` at each level and descends into the suffix until neither side has one.
        Equal counts (a tag is always at least as new as itself). Labels are compared separately by `is_candidate_for`;
        matching labels already pin any deeper suffix remainder, so the levels below the embedded version are equal.
        """
        if tag.sortable_version > self.sortable_version:
            return False  # This version axis would go down.
        if not (self.suffix or tag.suffix):
            return True  # Neither side has a suffix, so there is no deeper version axis to compare.
        return self._suffix_tag.is_newer_or_equal(tag._suffix_tag)

    @property
    def _is_dated_snapshot(self) -> bool:
        """Return whether the tag's version is a date rather than a release.

        A repository can publish dated snapshots of a development branch beside its releases, in one tag namespace.
        Examples are `20260805`, `bookworm-20260803`, and `2026-02-12-23-06-03`.
        """
        if cast("re.Match[str]", self._match).re is _DASHED_SNAPSHOT:
            return True
        release = cast("Version", self.version).release
        if len(release) != 1:
            return False
        try:
            date.fromisoformat(str(release[0]))
        except ValueError:
            return False
        return True

    @property
    def _date_spelling(self) -> str | None:
        """Return how the snapshot spells its date, as `0000-00-00` for `2023-10-20`, or None for a release."""
        if not self._is_dated_snapshot:
            return None
        return re.sub(r"\d", "0", cast("re.Match[str]", self._match).group("version"))

    def is_candidate_for(self, current: Tag) -> bool:
        """Return whether this tag (known by name only) is a possible update of the current tag.

        These are the checks that can be made from the tag name alone, used to narrow the set of tags whose
        metadata (digest and push date) is worth fetching.
        """
        if self.version is None:
            return False  # Ignore tags if the version is not valid
        if self.version.is_prerelease:
            return False  # Ignore tags if the version is a prerelease
        if self._date_spelling != current._date_spelling:
            return False  # Ignore a snapshot for a release and vice versa, and a timestamp for a date and vice versa
        if self.prefix != current.prefix:
            return False  # Ignore tags with a different prefix so e.g. python3.x isn't replaced by pypy3.x
        if self.suffix_label != current.suffix_label:
            return False  # Ignore tags whose suffix label differs so we don't change e.g. fat to slim, or alpine to fat
        return self.is_newer_or_equal(current)


def _split_domain(image: str) -> tuple[str | None, str]:
    """Split an image reference into its registry host and the remaining repository path.

    Following the rule Docker's reference parser uses to split the registry domain from the image name
    (`splitDockerDomain` in https://github.com/distribution/reference/blob/main/normalize.go), the first path
    component is a registry host when it contains a `.` or `:`, equals `localhost`, or contains an uppercase
    character (repository namespaces must be lowercase). When there is no such host the image is on Docker Hub
    (under the implicit `library/` namespace) and the host is returned as None with the full reference as the path.
    So `registry.gitlab.com/group/image` -> (`registry.gitlab.com`, `group/image`) and `cimg/go` -> (None, `cimg/go`).
    """
    first, _, remainder = image.partition("/")
    if "." in first or ":" in first or first == "localhost" or first != first.lower():
        return first, remainder
    return None, image


def _is_docker_hub_host(host: str | None) -> bool:
    """Return whether an OCI registry host is Docker Hub's, including a hostless reference (host None)."""
    return host is None or host.endswith("docker.io")


def is_docker_hub_image(image: str) -> bool:
    """Return whether the image reference points to Docker Hub rather than another registry.

    A reference is on Docker Hub when it uses an explicit Docker Hub host (e.g. `docker.io/library/redis`) or no
    registry host at all. So `registry.gitlab.com/...`, `gcr.io/...` and `localhost:5000/...` are not on Docker
    Hub; `python`, `cimg/go` and `docker.io/library/redis` are.
    """
    host, _ = _split_domain(image)
    return _is_docker_hub_host(host)


def _is_on_docker_hub(pinned: PinnedDependency) -> bool:
    """Return whether the pinned image is on Docker Hub, the one registry that dates the tags it serves."""
    return is_docker_hub_image(pinned.name)


@partial(publication_date_reporting, when=_is_on_docker_hub)
def get_latest_tag(pinned: PinnedDependency, version_bound: VersionBound, cooldown_days: int) -> DependencyVersion:
    """Return the tag to pin the reference to, carrying the image's newest release where one dates it.

    Resolves images on any OCI registry (Docker Hub, ghcr.io, mcr.microsoft.com, quay.io, ...). The digest comes
    back even when the current version is already the latest, so an unpinned reference can be pinned without
    bumping its version. A reference whose tag the registry does not serve names no image in the repository, so
    the image's newest release does not date the reference.
    """
    image = pinned.name
    resolved = _resolved_tag(image, pinned.version, version_bound, cooldown_days)
    if resolved.unserved is not None:
        return resolved
    return replace(resolved, project=Project(newest=_newest_release(image)))


def _resolved_tag(
    image: DependencyName, current_tag: VersionString, version_bound: VersionBound, cooldown_days: int
) -> DependencyVersion:
    """Return the tag the run leaves the reference on, with the digest that pins it.

    Keeps the tag's non-numerical parts while upgrading its version. A tag naming a channel rather than a version
    is floating, and resolves to the concrete tag that channel currently serves instead of to a newer one. A
    reference naming no tag equals `latest`, so it resolves the way a floating tag does. One request lists the tag
    names, and each candidate examined costs one more, for its digest. On Docker Hub, the cooldown costs another for
    the candidate's push date. A bound narrows the candidates by their main version alone, leaving the labels to
    match as they otherwise would.
    """
    current = Tag(name=current_tag or _DEFAULT_TAG)
    if _is_floating(current):
        return _resolved_floating_tag(image, current)
    names = _tag_names(image)
    if names.absent:
        return _repository_not_served(current)
    if current.version is None:
        # A tag Update-time can read as neither a version nor a channel, such as `debian:dev-2024`: it names no
        # version to advance, so the tag stands and the digest it serves pins it.
        if (tag := _get_tag(image, current.name)) is None:
            return DependencyVersion(version=current.name, unserved=Unserved.VERSION)
        return DependencyVersion(version=current.name, sha=tag.digest, publication=tag.pushed)
    candidates = [
        tag
        for tag in (Tag(name=name) for name in names.value or [])
        if tag.is_candidate_for(current)
        and version_bound.keeps(cast("Version", tag.version), current_tag)
        and not _is_another_spelling(tag, current)
    ]
    return first_eligible(
        candidates,
        lambda candidate, walk: _eligible_tag(image, current, candidate, walk),
        lambda candidate: candidate.name == current.name,
        current_tag,
        cooldown_days,
    )


def _is_another_spelling(candidate: Tag, current: Tag) -> bool:
    """Return whether the candidate names the current version under another spelling, such as `3.12.0` for `3.12`.

    `is_newer_or_equal` tells equality here, since a candidate is never older than the current tag (see
    `is_candidate_for`).
    """
    return candidate.name != current.name and current.is_newer_or_equal(candidate)


def _pulled(_pinned: PinnedDependency) -> ImageNotLookedUp | None:
    """Return no reason, for a file format that pulls every image it names."""
    return None


@dataclass(eq=False)
class TagGetter:
    """A getter that resolves image tags, and says which images it does not look up."""

    image_not_pulled: Callable[[PinnedDependency], ImageNotLookedUp | None] = _pulled

    def __post_init__(self) -> None:
        """Register which references the getter dates the versions of, and which images it does not look up."""
        publication_date_reporting(self, when=self._dates_the_versions_of)
        explaining_skipped_lookups(self, self._image_not_looked_up)

    def __call__(
        self, pinned: PinnedDependency, version_bound: VersionBound, cooldown_days: int, /
    ) -> DependencyVersion:
        """Return the tag to pin the reference to, or the reference as it is where its image is not looked up."""
        if (reason := self._image_not_looked_up(pinned)) is not None:
            return DependencyVersion(version=pinned.version, image_not_looked_up=reason)
        return get_latest_tag(pinned, version_bound, cooldown_days)

    def _image_not_looked_up(self, pinned: PinnedDependency) -> ImageNotLookedUp | None:
        """Return why no registry is asked about the pinned image, or None when one is."""
        if re.search(VARIABLE, f"{pinned.name}:{pinned.version}"):
            return ImageNotLookedUp.THROUGH_A_VARIABLE
        return self.image_not_pulled(pinned)

    def _dates_the_versions_of(self, pinned: PinnedDependency) -> bool:
        """Return whether the pinned image's versions carry a date, which they do not when no registry is asked."""
        return self._image_not_looked_up(pinned) is None and reports_publication_dates(get_latest_tag, pinned)


@dataclass(frozen=True)
class ImagesNotPulled:
    """Which images a file does not pull, and why."""

    images: set[str]
    reason: ImageNotLookedUp

    @cached_property
    def _without_digests(self) -> frozenset[str]:
        """Return the images without their digests, since a reference is matched by name and tag."""
        return frozenset(image.partition("@")[0] for image in self.images)

    def __call__(self, pinned: PinnedDependency) -> ImageNotLookedUp | None:
        """Return the reason when the reference is among the images, or None when it is not."""
        as_written = f"{pinned.name}{tag_of(pinned.version)}"
        return self.reason if as_written in self._without_digests else None


def _is_floating(tag: Tag) -> bool:
    """Return whether the tag names a channel the registry re-points, rather than a version or a dated snapshot.

    A channel names no version and carries no digit. `latest` and `bookworm` are channels. `dev-2024` is not,
    because its digits name a build.
    """
    return tag.version is None and not any(character.isdigit() for character in tag.name)


def _resolved_floating_tag(image: DependencyName, current: Tag) -> DependencyVersion:
    """Return the concrete tag a floating tag serves, with its digest, or the floating tag unchanged.

    What a floating tag serves is decided by its digest, so its aliases — the tags sharing that digest — are the
    candidates `_pinned_alias` picks between. Docker Hub lists the digest of every tag, so one listing gives them
    all; another registry lists names only, so `_walked_floating_tag` asks it tag by tag instead.
    """
    if not is_docker_hub_image(image):
        return _walked_floating_tag(image, current)
    if (listing := docker_hub.tag_listing(_repository(image), current.name).value) is None:
        return _repository_not_served(current)
    if not (digest := listing.digests.get(current.name)):
        reason_no_version_was_pinned = (
            FloatingPin.NOT_LISTED if listing.examined_all else FloatingPin.NOT_AMONG_EXAMINED
        )
        return _unpinned_floating_tag(current, reason_no_version_was_pinned)
    pushed = listing.pushed(current.name)
    aliases = [Tag(name=name) for name, tag_digest in listing.digests.items() if tag_digest == digest]
    if (alias := _pinned_alias(current, aliases)) is None:
        return _unpinned_floating_tag(current, FloatingPin.NO_VERSION_TAG, digest, pushed)
    return DependencyVersion(version=alias.name, sha=digest, publication=pushed, floating=FloatingPin.RESOLVED)


# How many tags the walk asks for a manifest at most, so that a repository whose tags the floating tag's image
# sorts below cannot turn one reference into dozens of requests. A floating tag serves a recent image, so its
# concrete tag sorts high among the versions; a walk that reaches this many without a match gives up.
_MAX_FLOATING_TAG_PROBES = 25


def _walked_floating_tag(image: DependencyName, current: Tag) -> DependencyVersion:
    """Return the concrete tag serving the floating tag's digest, found by asking the registry tag by tag.

    A registry other than Docker Hub lists tag names without their digests, so each candidate costs a manifest
    request. The candidates are walked in the order `_pinned_alias` would pick between them, and the first one
    serving the same digest is the tag to pin, which keeps the common case to a handful of requests. The walk stops
    at `_MAX_FLOATING_TAG_PROBES` candidates, and says whether it examined them all or gave up. The tag names are
    listed first, since the registry refuses that listing for a repository it does not serve.
    """
    names = _tag_names(image)
    if names.absent:
        return _repository_not_served(current)
    digest = _manifest_digest(image, current.name)
    if not digest:
        return _unpinned_floating_tag(current, FloatingPin.NO_MANIFEST)
    candidates = _concrete_tags(current, [Tag(name=name) for name in names.value or []])
    labels = _variant_labels(current, candidates)
    ordered = sorted(candidates, key=lambda tag: _alias_rank(tag, labels), reverse=True)
    for candidate in ordered[:_MAX_FLOATING_TAG_PROBES]:
        if _manifest_digest(image, candidate.name) == digest:
            return DependencyVersion(version=candidate.name, sha=digest, floating=FloatingPin.RESOLVED)
    examined_all = len(ordered) <= _MAX_FLOATING_TAG_PROBES
    return _unpinned_floating_tag(
        current, FloatingPin.NO_VERSION_TAG if examined_all else FloatingPin.NO_VERSION_TAG_EXAMINED, digest
    )


def _repository_not_served(current: Tag) -> DependencyVersion:
    """Return the tag as it is, on a repository the registry does not serve, so nothing pins or dates it."""
    return DependencyVersion(version=current.name, unserved=Unserved.REPOSITORY)


def _unpinned_floating_tag(
    current: Tag, reason: FloatingPin, digest: str = "", pushed: Lookup[datetime] = UNDATED
) -> DependencyVersion:
    """Return the floating tag as it is, carrying why no version was pinned in its place.

    It carries the digest the tag serves, and when the tag was pushed, where those are known. Two of the reasons say
    the registry does not serve the tag, so nothing dates the reference.
    """
    unserved = Unserved.VERSION if reason in (FloatingPin.NOT_LISTED, FloatingPin.NO_MANIFEST) else None
    return DependencyVersion(version=current.name, sha=digest, publication=pushed, floating=reason, unserved=unserved)


def _pinned_alias(current: Tag, aliases: list[Tag]) -> Tag | None:
    """Return the alias to pin the reference to, or None when none of them names a version of it.

    What is left after `_concrete_tags` is ranked by `_alias_rank`, and the highest of them is the tag to pin.
    """
    concrete = _concrete_tags(current, aliases)
    if not concrete:
        return None
    labels = _variant_labels(current, concrete)
    return max(concrete, key=lambda alias: _alias_rank(alias, labels))


def _concrete_tags(current: Tag, tags: list[Tag]) -> list[Tag]:
    """Return the tags naming a version of the image the floating tag names.

    A tag whose label prefix differs names a version of something the image bundles rather than of the image —
    `php8.3` and `jdk25` are the PHP and JDK the build carries — so it is passed over, as `is_candidate_for` passes
    over such a tag when updating a version pin.
    """
    return [tag for tag in tags if tag.version is not None and tag.prefix == current.prefix]


def _alias_rank(alias: Tag, labels: list[str]) -> tuple[int, _AliasKey]:
    """Return what ranks the aliases of one digest: the variant labels the alias carries, then `_alias_key`.

    A floating tag's labels are kept where an alias carries them, so an alias carrying more of them outranks one
    carrying fewer, and a tag whose labels no single alias carries together still lands on one carrying some. Both
    ways of resolving a floating tag rank their candidates this way, so the two cannot come to disagree: Docker Hub
    picks the highest of the aliases its listing gives, and another registry walks them in this order until one
    serves the digest.
    """
    carried = sum(_carries(alias, label) for label in labels)
    return carried, _alias_key(alias, labels)


def _alias_key(alias: Tag, labels: list[str]) -> _AliasKey:
    """Return what orders the aliases of one digest, so the highest is the one to pin the reference to.

    The key is the version, then how precisely it is spelled, then the version of each label the floating tag asked
    for, and last the shorter name.
    """
    variants = tuple(_variant_version(alias, label) for label in labels)
    return (alias.sortable_version, len(alias.sortable_version.release), variants, -len(alias.name))


def _variant_version(tag: Tag, label: str) -> Version:
    """Return the version the tag attaches to the label, or the lowest sentinel when it attaches none."""
    versions = [part.version for part in _labels(tag) if part.prefix == label and part.version is not None]
    return max(versions, default=LOWEST_VERSION)


def _variant_labels(current: Tag, concrete: list[Tag]) -> list[str]:
    """Return the labels of a floating tag that name a variant of the image, rather than a channel it follows.

    A label no alias carries names a channel, and requiring it would leave no alias to pick from, so it is left
    out. A label in `_CHANNEL_LABELS` is left out even where an alias carries it: keeping it would pin the
    reference to a tag that floats on, `node:lts-alpine` landing on the `24-lts` that follows whichever 24 release
    is the LTS one instead of on `24-alpine`.
    """
    return [
        label.name
        for label in _labels(current)
        if label.name not in _CHANNEL_LABELS and any(_carries(alias, label.name) for alias in concrete)
    ]


def _carries(tag: Tag, label: str) -> bool:
    """Return whether the tag's name holds the label as a label of its own, with or without a version attached.

    A label carries a version of its own where the variant it names is versioned — `alpine3.24` is the `alpine`
    variant on Alpine 3.24 — and such a label names `alpine` as much as the bare label does.
    """
    return any(part.name == label or (part.version is not None and part.prefix == label) for part in _labels(tag))


@cache
def _labels(tag: Tag) -> tuple[Tag, ...]:
    """Return the tag's dash-separated labels, each parsed as a tag in its own right.

    Cached because picking an alias asks the same tag for the same labels repeatedly: once to decide which of the
    floating tag's labels name a variant, and again to keep the aliases carrying them.
    """
    return tuple(Tag(name=part) for part in tag.name.split("-"))


def _newest_release(image: str) -> Release | None:
    """Return the image's newest release: the tag its registry pushed most recently, or None when it dates none.

    The release is the whole image's, whatever labels its tag carries. Only Docker Hub exposes a push date (the OCI
    protocol doesn't), so an image on another registry is never flagged as stale.

    A tag is pushed together with the tags serving the same image, so one push date is shared by a tag naming a
    version and its aliases. `_alias_key` ranks them by version, then by how precisely it is spelled, so the
    release is named `3.14.7` rather than the `latest` or `3` beside it. Where no tag pushed at that moment names
    a version, the release is named by the tag itself, such as the `dev` of an image tagged `dev` and `prod`.
    """
    if not is_docker_hub_image(image):
        return None
    if not (pushes := docker_hub.newest_pushes(_repository(image))):
        return None
    published = max(pushes.values())
    pushed_together = [Tag(name=name) for name, pushed in pushes.items() if pushed == published]
    named_by = max(pushed_together, key=lambda tag: _alias_key(tag, labels=[]))
    return Release(version=named_by.name, published=published)


def _eligible_tag(image: str, current: Tag, candidate: Tag, walk: CooldownWalk) -> DependencyVersion | None:
    """Return the candidate, resolved to its digest, when it is eligible, or None when it isn't.

    The cooldown does not hold the current tag back, since the reference uses it already. A cooldown that applies
    holds back a candidate whose push date Docker Hub failed to report, and every tag between it and the current one.
    """
    is_current = candidate.name == current.name
    latest = _get_tag(image, candidate.name)
    if latest is None:
        return None
    report_undated = partial(_LOG.undated_push, image, candidate.name, current.name)
    if not is_current and not walk.past(candidate.name, latest.pushed, report_undated):
        return None
    return DependencyVersion(version=candidate.name, sha=latest.digest, publication=latest.pushed)


def _registry_host(image: str) -> str:
    """Return the OCI registry API host for an image reference, defaulting to Docker Hub's registry."""
    host, _ = _split_domain(image)
    # In the else branch `_is_docker_hub_host` was False, so `host` is a real (non-None) registry host.
    return _DOCKER_HUB_OCI_HOST if _is_docker_hub_host(host) else cast("str", host)


def _repository(image: str) -> str:
    """Return the `namespace/repository` path for an image, relative to its registry host.

    The repository path in the registry API is host-relative, so an explicit registry host is dropped, e.g.
    `ghcr.io/devcontainers/features/node` -> `devcontainers/features/node` and `docker.io/library/redis` ->
    `library/redis`. Dropping it for every registry (not just Docker Hub) matters because strict registries such as
    `mcr.microsoft.com` return a 404 for a host-prefixed path. A hostless reference is on Docker Hub, where a
    single-segment name lives under the implicit `library/` namespace (`node` -> `library/node`).
    """
    host, remainder = _split_domain(image)
    path = image if host is None else remainder
    if _is_docker_hub_host(host):
        return path if "/" in path else f"library/{path}"
    return path


def _credentials(image: str) -> tuple[str, str] | None:
    """Return Docker Hub credentials (for a higher rate limit) when the image is on Docker Hub, else None.

    Docker Hub is currently the only registry we hold credentials for; all other registries are queried anonymously.
    """
    return docker_hub.credentials() if is_docker_hub_image(image) else None


@cache
def _tag_names(image: str) -> LookedUp[list[str]]:
    """Fetch all tag names for an image from its OCI registry's names-only listing.

    Where the registry refuses the repository, the names are absent. Any other failure is logged, and leaves the
    names listed until then.
    """
    host = _registry_host(image)
    repository = _repository(image)
    headers = _auth_headers(host, repository, _credentials(image))
    url: str | None = f"https://{host}/v2/{repository}/tags/list?n={_TAGS_PAGE_SIZE}"
    names: list[str] = []
    while url:
        response = fetch(url, _LOG, headers=headers, require_ok=False)
        if response is None:
            break
        if _refuses_the_repository(response):
            return LookedUp(None, absent=True)
        if not response.ok:
            _LOG.response(response)
            break
        names.extend(response.json().get("tags") or [])
        url = next_page_url(response)
    return LookedUp(names)


def _refuses_the_repository(response: Response) -> bool:
    """Return whether the registry answered that it does not serve the repository, rather than refusing the token."""
    if response.status_code == HTTPStatus.NOT_FOUND:
        return True
    challenge = response.headers.get("WWW-Authenticate", "")
    return response.status_code == HTTPStatus.UNAUTHORIZED and _AUTHORIZATION_REFUSED not in challenge


@cache
def _get_tag(image: str, name: str) -> Tag | None:
    """Return the tag with the digest its OCI manifest holds, or None when the registry does not serve one.

    The digest comes from the OCI manifest, which works on every registry. Docker Hub's proprietary API reports a push
    date, which the OCI protocol does not expose; see `docker_hub`. So the cooldown applies to a Docker Hub tag alone,
    and the push date is read when the cooldown needs it.
    """
    digest = _manifest_digest(image, name)
    if not digest:
        return None
    if not is_docker_hub_image(image):
        return Tag(name=name, digest=digest)
    pushed = DeferredLookup(partial(docker_hub.last_pushed, _repository(image), name))
    return Tag(name=name, digest=digest, pushed=pushed)


@cache
def _manifest_digest(image: str, tag: str) -> str:
    """Return the digest to pin for an image tag, read from its OCI manifest, or empty string if unavailable.

    A `HEAD` on the manifest returns the canonical digest in the `Docker-Content-Digest` header (the multi-arch
    index digest when the Accept header offers the index/list media types). This works on every OCI registry.
    Cached, so a tag named more than once by the same run is read once.
    """
    host = _registry_host(image)
    repository = _repository(image)
    headers = {"Accept": ", ".join(_MANIFEST_MEDIA_TYPES), **_auth_headers(host, repository, _credentials(image))}
    response = fetch(f"https://{host}/v2/{repository}/manifests/{tag}", _LOG, method="head", headers=headers)
    if response is None:
        return ""
    return response.headers.get("Docker-Content-Digest", "")


def _auth_headers(host: str, repository: str, credentials: tuple[str, str] | None) -> dict[str, str]:
    """Return the Authorization header for pulling from the registry, or no header for anonymous access."""
    token = _registry_token(host, repository, credentials)
    return {"Authorization": f"Bearer {token}"} if token else {}


@cache
def _registry_token(host: str, repository: str, credentials: tuple[str, str] | None = None) -> str | None:
    """Return a pull token for the repository, from the token endpoint the registry's challenge names.

    A registry that does not challenge, such as mcr.microsoft.com, is asked without a token. The credentials, if any,
    authenticate the token request. A 403, as ghcr.io answers for a repository it does not serve, is not logged,
    since the registry refuses the tag names next. Any other failure is logged.
    """
    probe = fetch(f"https://{host}/v2/", _LOG, require_ok=False)
    if probe is None:
        return None
    challenge = probe.headers.get("WWW-Authenticate", "")
    if probe.status_code != HTTPStatus.UNAUTHORIZED or not challenge.lower().startswith("bearer "):
        return None  # Anonymous registry (or an unexpected response): proceed without a token.
    # Pull out each `key="value"` pair. The word boundary keeps the scan linear. It rejects a start position inside
    # a run of word characters at once. No match can start there anyway: a match starting at the run's first
    # character spans the same run. The `++` and the `[^"]*` match each pair without giving characters back.
    params = dict(re.findall(r'\b(\w++)="([^"]*)"', challenge))
    realm = params.pop("realm", "")
    params["scope"] = f"repository:{repository}:pull"
    response = fetch(realm, _LOG, params=params, auth=credentials, require_ok=False)
    if response is None or response.status_code == HTTPStatus.FORBIDDEN:
        return None
    if not response.ok:
        _LOG.response(response)
        return None
    token = response.json()
    return token.get("token") or token.get("access_token")
