"""Unit tests for the OCI registry module."""

import unittest
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import requests

from update_time.domain import dependency
from update_time.domain.bound import NO_BOUND, Verb
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion, FloatingPin, ImageNotLookedUp, PinnedDependency, Release
from update_time.domain.publication import reports_publication_dates
from update_time.io.log import Logger
from update_time.sources import docker_hub, oci
from update_time.sources.docker_hub import _MAX_TAG_LISTING_PAGES, _TAG_LISTING_PAGE_SIZE
from update_time.sources.oci import (
    _MAX_FLOATING_TAG_PROBES,
    ImagesNotPulled,
    Tag,
    TagGetter,
    _registry_token,
    is_docker_hub_image,
)

from tests.helpers import patch_environ, requested_urls
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2, DIGEST3, FRESH_DATE, PAST_COOLDOWN_DATE, STALE_DATE
from tests.update_time.helpers import LoggingTestCase, bound, days_ago, docker_tag, message_of
from tests.update_time.registry import (
    Endpoint,
    RegistryRequestsMixin,
    mock_docker_registry,
    probe_response,
    service_unavailable,
    token_endpoint,
    unauthorized,
)

if TYPE_CHECKING:
    from datetime import datetime

    from update_time.domain.bound import NewVersionGetter, VersionBound


def get_latest_tag(
    image: str, current_tag: str, version_bound: VersionBound = NO_BOUND, cooldown_days: int = COOLDOWN.default
) -> DependencyVersion:
    """Return what the source resolves for the image and tag, as a test writes them."""
    pinned = PinnedDependency(image, current_tag)
    return oci.get_latest_tag(pinned, version_bound, cooldown_days, check_archival=True)


class IsDockerHubImageTest(unittest.TestCase):
    """Unit tests for distinguishing Docker Hub images from other-registry references."""

    def test_docker_hub_images(self):
        """Test that images without a registry host, or with an explicit Docker Hub host, are Docker Hub images."""
        images = ("python", "library/ubuntu", "cimg/go", "docker.io/library/redis", "index.docker.io/org/app")
        for image in images:
            with self.subTest(image=image):
                self.assertTrue(is_docker_hub_image(image))

    def test_other_registry_images(self):
        """Test that images with a registry host as their first path component are not Docker Hub images."""
        for image in ("registry.gitlab.com/group/image", "gcr.io/proj/image", "localhost:5000/i", "Host/image"):
            with self.subTest(image=image):
                self.assertFalse(is_docker_hub_image(image))


class TagTest(unittest.TestCase):
    """Unit tests for tags."""

    @kills(
        Mutation(
            oci.Tag.version,
            r'r"+\1"',
            r'r".\1"',
            "a build number is read as a version component, so 26.0.2_10 sorts above 26.0.2.1_1",
        )
    )
    def test_sort_tag(self):
        """Test that a tag sorts below one with a higher version, or a more precise version of the same release."""
        lower_and_higher = (
            ("1", "2"),
            ("1.3", "1.4"),
            ("1.3.1", "1.3.2"),
            ("1.3", "1.3.1"),
            ("1.3", "1.3.0"),
            ("python1.3", "python1.3.0"),
            ("python1.3-alpine2.3", "python1.3.0-alpine2.3"),  # the main version decides
            ("python1.3.0-alpine2.3", "python1.3.0-alpine2.3.0"),  # ...and the suffix's when the main ones are equal
            ("26.0.2_10-jre", "26.0.2.1_1-jre"),  # a build number orders the builds within one version
        )
        for lower, higher in lower_and_higher:
            with self.subTest(lower=lower, higher=higher):
                self.assertLess(Tag(lower), Tag(higher))


@patch_environ()
class ImagesNotPulledTest(RegistryRequestsMixin, unittest.TestCase):
    """Unit tests for the getter that leaves the references to an image the file does not pull as they are."""

    # How a file writes a reference to an image it does not pull: one naming a tag and one naming none.
    NOT_PULLED = ("ubuntu-2204:2024.01.1", "default")
    REASON = ImageNotLookedUp.MACHINE_EXECUTOR_IMAGE

    def getter(self) -> NewVersionGetter:
        """Return a getter excluding those references, as an updater builds it from what its file declares."""
        return TagGetter(ImagesNotPulled(set(self.NOT_PULLED), self.REASON))

    def resolve(self, image: str, tag: str) -> DependencyVersion:
        """Return what the getter resolves for the reference, with the registry offering a newer tag."""
        self.requests.side_effect = mock_docker_registry(docker_tag("9.9", DIGEST))
        return self.getter()(PinnedDependency(image, tag), NO_BOUND, COOLDOWN.default, check_archival=True)

    def test_a_reference_not_pulled_keeps_its_version_where_another_resolves(self):
        """Test that a reference not pulled is left as it is, saying why, where one the getter resolves moves on."""
        not_pulled = self.resolve("ubuntu-2204", "2024.01.1")
        self.assertEqual(not_pulled, DependencyVersion(version="2024.01.1", image_not_looked_up=self.REASON))
        self.requests.assert_not_called()
        self.assertEqual(self.resolve("python", "3.14").version, "9.9")

    def test_a_reference_not_pulled_naming_no_tag_is_left_as_it_is(self):
        """Test that an entry listed without a tag matches the reference that names none."""
        resolved = self.resolve("default", "")
        self.assertEqual(resolved, DependencyVersion(version="", image_not_looked_up=self.REASON))
        self.requests.assert_not_called()

    def test_a_reference_not_pulled_is_dated_by_nothing(self):
        """Test that the getter does not date the versions of a reference not pulled."""
        not_pulled = PinnedDependency("ubuntu-2204", "2024.01.1")
        self.assertFalse(reports_publication_dates(self.getter(), not_pulled))

    def test_a_reference_pulled_is_dated_by_docker_hub(self):
        """Test that a reference the getter resolves on Docker Hub is dated, since Docker Hub dates its tags."""
        served = PinnedDependency("python", "3.14")
        self.assertTrue(reports_publication_dates(self.getter(), served))


class GetLatestTagTest(RegistryRequestsMixin, LoggingTestCase):
    """Unit tests for getting the latest tag."""

    def test_no_tags(self):
        """Test that the current tag is returned if the image has no tags."""
        self.requests.side_effect = mock_docker_registry()
        self.assertEqual(get_latest_tag("no_tags", "1.0").version, "1.0")

    def test_tag_names_unavailable(self):
        """Test that a reference whose tag names can't be fetched is left unchanged, and the failure logged."""
        self.requests.side_effect = mock_docker_registry(unavailable=Endpoint.TAG_NAMES)
        self.assertEqual(
            get_latest_tag("python", "3.14.6").version,
            "3.14.6",
        )
        url = "https://registry-1.docker.io/v2/library/python/tags/list?n=1000"
        self.assert_could_not_fetch_logged(url, status=HTTPStatus.SERVICE_UNAVAILABLE)

    @kills(
        Mutation(
            oci._tag_names,
            "if response is None:\n            break",
            "if response is None:\n            return LookedUp(None, absent=True)",
            "a tag names request that fails in transport reads as a repository the registry does not serve",
        )
    )
    def test_tag_names_request_that_fails_takes_the_repository_as_served(self):
        """Test that a tag names request failing in transport leaves the reference on a repository taken as served."""
        self.requests.side_effect = requests.exceptions.Timeout
        latest = get_latest_tag("python", "3.14.6")
        self.assertEqual(latest.version, "3.14.6")
        self.assertIsNone(latest.unserved)

    @kills(
        Mutation(
            oci._refuses_the_repository,
            "response.status_code == HTTPStatus.UNAUTHORIZED and _AUTHORIZATION_REFUSED not in challenge",
            "response.status_code == HTTPStatus.UNAUTHORIZED",
            "an expired or invalid token reads as a repository the registry does not serve, and goes unwarned",
        )
    )
    def test_tag_names_refused_for_an_invalid_token_are_warned_about(self):
        """Test that tag names refused for an invalid token are warned about, on a repository taken as served."""
        challenge = 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",error="invalid_token"'
        registry = mock_docker_registry()

        def get(url: str, *args: object, **kwargs: object) -> Mock:
            if "/tags/list" in url:
                return unauthorized(url, headers={"WWW-Authenticate": challenge})
            return registry(url, *args, **kwargs)

        self.requests.side_effect = get
        self.assertIsNone(get_latest_tag("python", "3.14.6").unserved)
        url = "https://registry-1.docker.io/v2/library/python/tags/list?n=1000"
        self.assert_could_not_fetch_logged(url, status=HTTPStatus.UNAUTHORIZED)

    def test_tag_of_a_repository_the_registry_does_not_serve_costs_no_request_to_date_it(self):
        """Test that a version tag of a repository the registry does not serve is left undated, without asking."""
        self.requests.side_effect = mock_docker_registry(repository_served=False)
        latest = get_latest_tag("spark-rm-base", "3.5.1")
        self.assertIsNone(latest.project.newest)
        requested = requested_urls(self.requests)
        self.assertIn("https://registry-1.docker.io/v2/library/spark-rm-base/tags/list?n=1000", requested)
        self.assertFalse([url for url in requested if url.startswith("https://registry.hub.docker.com/")])

    def test_other_registry_image_resolved(self):
        """Test that an image on a registry other than Docker Hub is resolved against that registry's host."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        latest = get_latest_tag("registry.gitlab.com/group/image", "1.0")
        self.assertEqual(latest.version, "1.1")
        self.assertEqual(DIGEST, latest.sha)
        self.assertIsNone(latest.publication.value)  # No push date (and so no cooldown) outside Docker Hub.
        self.assertTrue(any("registry.gitlab.com" in call.args[0] for call in self.requests.call_args_list))

    def test_other_registry_repository_path_excludes_host(self):
        """Test that the API repository path drops the registry host (a strict registry 404s on a host-prefixed one)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        get_latest_tag("mcr.microsoft.com/devcontainers/typescript-node", "1.0")
        tags_list_urls = [call.args[0] for call in self.requests.call_args_list if "/tags/list" in call.args[0]]
        self.assertTrue(tags_list_urls)
        for url in tags_list_urls:
            with self.subTest(url=url):
                self.assertIn("https://mcr.microsoft.com/v2/devcontainers/typescript-node/tags/list", url)
                self.assertNotIn("/v2/mcr.microsoft.com/", url)  # The host must not leak into the repository path.

    def test_explicit_docker_hub_host(self):
        """Test that an image with an explicit docker.io host is updated, querying the host-less Docker Hub URL."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        self.assertEqual(
            get_latest_tag("docker.io/library/redis", "1.0").version,
            "1.1",
        )
        self.assertIn("/namespaces/library/repositories/redis/", self.requests.call_args.args[0])

    def test_up_to_date(self):
        """Test that the current tag and its digest are returned if it's up to date, so it can be pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.0", DIGEST))
        latest = get_latest_tag("up_to_date", "1.0")
        self.assertEqual(latest.version, "1.0")
        self.assertEqual(DIGEST, latest.sha)

    @kills(
        Mutation(
            oci.Tag.suffix_label,
            "return (self._suffix_tag.prefix, self._suffix_tag.suffix)",
            'return (self._suffix_tag.prefix, "")',
            "a suffix's remainder is dropped from its label, so a slim reference drifts onto the fat image",
        ),
        Mutation(
            oci.Tag._date_spelling,
            're.sub(r"\\d", "0", cast("re.Match[str]", self._match).group("version"))',
            '""',
            "every snapshot spells its date the same way, so a reference on a date moves onto a timestamp",
        ),
        Mutation(
            oci.Tag._date_spelling,
            'cast("re.Match[str]", self._match).group("version")',
            "str(self.version)",
            "a snapshot's date spelling is read from its parsed version, so an undashed date moves onto a dashed one",
        ),
        Mutation(
            dependency,
            r'r"(?:-|$)(?P<suffix>',
            r'r"-?(?P<suffix>',
            "a date is read from the first eight of a longer run of digits, so a tag naming an hour moves to another",
        ),
        Mutation(
            oci,
            r'r"_(\d+)$"',
            r'r"_(\w+)$"',
            "a prerelease label after an underscore is read as a build number, so a reference moves onto a prerelease",
        ),
    )
    def test_which_listed_tags_leave_the_reference_as_it_is(self):
        """Test which listed tags leave the reference on the tag it already names."""
        cases = {
            "a lower version": ("lower", "1.1", "1.0"),
            "an unparsable version": ("invalid-version", "1.3", "1.2.invalid"),
            "a prerelease": ("prerelease", "1.3", "1.4a1"),
            "a prerelease after an underscore": ("underscored-prerelease", "1.3", "1.4_rc1"),
            "another suffix": ("other-suffix", "1.3", "1.4-windows"),
            "a downgraded suffix version": ("suffix-downgrade", "3.14.6-alpine3.23", "3.15.0-alpine3.22"),
            "another suffix label": ("other-suffix-label", "3.14.6-alpine3.23", "3.15.0-debian12"),
            "a suffix that drops the remainder": ("suffix-remainder", "3.14.6-alpine3.19-slim", "3.15.0-alpine3.19"),
            "another label prefix": ("ghcr.io/astral-sh/uv", "python3.12-bookworm-slim", "pypy3.99-bookworm-slim"),
            "a timestamp for a date": ("date", "2023-10-20", "2023-10-21-08-00-00"),
            "a release for a snapshot": ("release-for-snapshot", "2023-10-19", "20240101.1"),
            "a dashed date for an undashed one": ("debian", "bookworm-20240110", "bookworm-2024-01-11"),
            "digits running past a date": ("nightly", "nightly-2026080312", "nightly-2026080412"),
        }
        for case, (image, current_tag, listed_tag) in cases.items():
            with self.subTest(case=case):
                self.requests.side_effect = mock_docker_registry(docker_tag(listed_tag, DIGEST))
                self.assertEqual(get_latest_tag(image, current_tag).version, current_tag)

    def test_new_version_available(self):
        """Test that the new tag is returned if it's newer, its push date as its publication date."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST))
        latest = get_latest_tag("new_version_available", "1.2")
        self.assertEqual(latest.version, "2.1")
        self.assertEqual(latest.publication.value, PAST_COOLDOWN_DATE)

    def test_multiple_new_versions_available(self):
        """Test that the newest tag is returned if multiple newer tags are available."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("2.2", DIGEST2), docker_tag("2.1", DIGEST1), docker_tag("2.3", DIGEST3)
        )
        self.assertEqual(
            get_latest_tag("new_versions_available", "1.2").version,
            "2.3",
        )

    def test_ignore_tags_without_digest(self):
        """Test that tags without digests are ignored, falling back to the next-highest version."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.2", DIGEST), docker_tag("2.3"))
        self.assertEqual(
            get_latest_tag("ignore_tags_without_digest", "1.2").version,
            "2.2",
        )

    def test_equal_version_alias_tag_keeps_current_spelling(self):
        """Test that an alias tag spelling the current version differently (`22.15` for `22.15.0`) is not adopted."""
        self.requests.side_effect = mock_docker_registry(docker_tag("22.15", DIGEST), docker_tag("22.15.0", DIGEST))
        latest = get_latest_tag("alias", "22.15.0")
        self.assertEqual(latest.version, "22.15.0")
        self.assertEqual(latest.sha, DIGEST)

    def test_equal_version_alias_tag_keeps_current_spelling_under_exact_bound(self):
        """Test that a bound pinning the current release exactly (`ignore[patch-update]`) adopts no alias spelling."""
        self.requests.side_effect = mock_docker_registry(docker_tag("22.15", DIGEST), docker_tag("22.15.0", DIGEST))
        version_bound = bound(Verb.ALLOW, "update==22.15.0")
        self.assertEqual(
            get_latest_tag("alias-bounded", "22.15.0", version_bound).version,
            "22.15.0",
        )

    def test_equal_version_alias_tag_does_not_lend_its_digest(self):
        """Test that the current spelling keeps its own digest, not a co-listed alias tag's differing digest.

        The alias is spelled less precisely in one case, and more precisely in the other.
        """
        self.requests.side_effect = mock_docker_registry(docker_tag("22.15", DIGEST2), docker_tag("22.15.0", DIGEST1))
        cases = {"alias": ("22.15.0", DIGEST1), "precise-alias": ("22.15", DIGEST2)}
        for image, (current, digest) in cases.items():
            with self.subTest(current):
                latest = get_latest_tag(image, current)
                self.assertEqual(latest.version, current)
                self.assertEqual(latest.sha, digest)

    def test_update_to_a_version_listed_under_two_spellings_keeps_the_precise_one(self):
        """Test that updating to a version the registry lists twice adopts the precise spelling, not the alias."""
        self.requests.side_effect = mock_docker_registry(docker_tag("22.16", DIGEST1), docker_tag("22.16.0", DIGEST2))
        latest = get_latest_tag("two-spellings", "22.15.0")
        self.assertEqual(latest.version, "22.16.0")
        self.assertEqual(latest.sha, DIGEST2)

    def test_level_bound_anchors_to_the_current_tag(self):
        """Test that a level bound is anchored to the current tag, keeping updates within the pinned minor line."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.12.9", DIGEST1), docker_tag("3.13.0", DIGEST2))
        version_bound = bound(Verb.IGNORE, "minor-update")
        self.assertEqual(
            get_latest_tag("level-bounded", "3.12.1", version_bound).version,
            "3.12.9",
        )

    def test_bound_narrows_candidates(self):
        """Test that a version bound drops out-of-bound tags so a bounded tag wins over a higher one."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("2.2", DIGEST2), docker_tag("2.1", DIGEST1), docker_tag("2.3", DIGEST3)
        )
        version_bound = bound(Verb.ALLOW, "update<2.3")
        self.assertEqual(get_latest_tag("bounded", "1.2", version_bound).version, "2.2")

    def test_tag_names_paginated(self):
        """Test that the newest tag is returned even if the tag names listing is paginated."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("2.1", DIGEST1), docker_tag("2.2", DIGEST2), page_size=1
        )
        self.assertEqual(get_latest_tag("pagination", "1.2").version, "2.2")

    def test_tag_manifest_not_found(self):
        """Test that a listed tag whose manifest can't be fetched is skipped, leaving the current tag unchanged."""
        self.requests.side_effect = mock_docker_registry(names=["2.2"])
        self.assertEqual(get_latest_tag("manifest_not_found", "1.2").version, "1.2")
        url = "https://registry-1.docker.io/v2/library/manifest_not_found/manifests/2.2"
        self.assert_could_not_fetch_logged(url, status=HTTPStatus.NOT_FOUND)

    @kills(
        Mutation(
            oci.Tag._is_dated_snapshot,
            ".re is _DASHED_SNAPSHOT",
            ".re is None",
            "a timestamp is read as a release, so a release pin moves onto it",
        )
    )
    def test_dated_snapshot_tag_is_no_candidate_for_a_release(self):
        """Test that a tag naming a date, or a date and a time, loses to a release tag it sorts above."""
        cases = {"a date": ("dated_snapshot", "20260805"), "a timestamp": ("timestamped", "2026-02-12-23-06-03")}
        for case, (image, snapshot) in cases.items():
            with self.subTest(case=case):
                self.requests.side_effect = mock_docker_registry(
                    docker_tag(snapshot, DIGEST1), docker_tag("3.25.0", DIGEST2)
                )
                self.assertEqual(get_latest_tag(image, "3.24.1").version, "3.25.0")

    def test_eight_digit_version_that_is_no_date_is_a_candidate(self):
        """Test that an eight-digit version reading as no calendar date, such as `20261332`, still updates a release."""
        self.requests.side_effect = mock_docker_registry(docker_tag("20261332", DIGEST))
        latest = get_latest_tag("no_calendar_date", "3.24.1")
        self.assertEqual(latest.version, "20261332")

    def test_suffix_embedded_version_bumped(self):
        """Test that a version embedded in the suffix is upgraded while its label is kept (alpine3.23 -> alpine3.24)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.6-alpine3.24", DIGEST))
        latest = get_latest_tag("python", "3.14.6-alpine3.23")
        self.assertEqual(latest.version, "3.14.6-alpine3.24")
        self.assertEqual(DIGEST, latest.sha)

    def test_suffix_and_main_version_bumped_together(self):
        """Test that both the main version and the embedded suffix version advance together to the newest tag."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("3.14.6-alpine3.23", DIGEST1),
            docker_tag("3.14.6-alpine3.24", DIGEST2),
            docker_tag("3.15.0-alpine3.24", DIGEST3),
        )
        latest = get_latest_tag("python", "3.14.6-alpine3.23")
        self.assertEqual(latest.version, "3.15.0-alpine3.24")
        self.assertEqual(DIGEST3, latest.sha)

    def test_label_prefixed_version(self):
        """Test that a label-prefixed tag (e.g. python3.12-...) is bumped with the prefix and suffix kept."""
        self.requests.side_effect = mock_docker_registry(docker_tag("python3.13-bookworm-slim", DIGEST))
        latest = get_latest_tag("ghcr.io/astral-sh/uv", "python3.12-bookworm-slim")
        self.assertEqual(latest.version, "python3.13-bookworm-slim")
        self.assertEqual(DIGEST, latest.sha)

    def test_update_writes_the_listed_spelling(self):
        """Test that an update writes the tag as the registry lists it, such as the leading zero in `2024.01`."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2024.01", DIGEST))
        self.assertEqual(get_latest_tag("cimg/base", "2023.12").version, "2024.01")

    @kills(
        Mutation(
            oci.Tag.version,
            r'r"+\1"',
            r'r"+b\1"',
            "a build number is compared as text, so build 10 sorts below build 9",
        )
    )
    def test_tag_with_a_build_number_is_a_candidate(self):
        """Test that a tag spelling its version with a build number, such as `26.0.2_10-jre`, updates a reference."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("26-jre", DIGEST1, tag_last_pushed=FRESH_DATE),
            docker_tag("26.0.2.1_1-jre", DIGEST1, tag_last_pushed=FRESH_DATE),
            docker_tag("26.0.2_10-jre", DIGEST2),
            docker_tag("26.0.2_9-jre", DIGEST3),
        )
        cases = {"a version without a build number": "25-jre", "a lower build number": "26.0.2_9-jre"}
        for case, current_tag in cases.items():
            with self.subTest(case=case):
                latest = get_latest_tag("eclipse-temurin", current_tag)
                self.assertEqual(latest.version, "26.0.2_10-jre")
                self.assertEqual(latest.sha, DIGEST2)

    def test_outside_cooldown(self):
        """Test that tags pushed before the cooldown are considered, with the push date as publication date."""
        old = days_ago(10)
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4", DIGEST, tag_last_pushed=old))
        latest = get_latest_tag("outside_cooldown", "1.3")
        self.assertEqual(latest.version, "1.4")
        self.assertEqual(old, latest.publication.value)

    def test_cooldown_decides_eligibility(self):
        """Test that a tag is held back or adopted according to the cooldown the getter is passed."""
        pushed = days_ago(10)
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4", DIGEST, tag_last_pushed=pushed))
        for cooldown_days, expected in ((30, "1.3"), (5, "1.4")):
            with self.subTest(cooldown_days=cooldown_days):
                self.assertEqual(
                    get_latest_tag("cooldown_argument", "1.3", cooldown_days=cooldown_days).version,
                    expected,
                )

    def test_newest_release_ignores_labels(self):
        """Test that the newest release is the image's, whatever labels its tag carries.

        The update keeps the reference's `slim` label, while the release that dates the image carries none.
        """
        recent = days_ago(10)
        old = days_ago(400)
        self.requests.side_effect = mock_docker_registry(  # Docker Hub lists the most recently pushed tag first
            docker_tag("3.14.7", DIGEST2, tag_last_pushed=recent),
            docker_tag("3.13-slim", DIGEST1, tag_last_pushed=old),
        )
        latest = get_latest_tag("python", "3.12-slim")
        self.assertEqual(latest.version, "3.13-slim")  # The update keeps the slim line...
        newest = latest.project.newest
        self.assertEqual(Release("3.14.7", recent), newest)  # ...staleness does not.

    def test_dated_snapshot_within_the_cooldown_is_held_back(self):
        """Test that a snapshot pushed too recently is held back, and the sibling below it adopted instead."""
        fresh = days_ago(2)
        eligible = days_ago(200)
        current = days_ago(955)
        self.requests.side_effect = mock_docker_registry(
            docker_tag("bookworm-20260803", DIGEST3, tag_last_pushed=fresh),
            docker_tag("bookworm-20250101", DIGEST2, tag_last_pushed=eligible),
            docker_tag("bookworm-20240110", DIGEST1, tag_last_pushed=current),
        )
        latest = get_latest_tag("debian", "bookworm-20240110")
        self.assertEqual(latest.version, "bookworm-20250101")
        self.assertEqual(latest.sha, DIGEST2)

    @kills(
        Mutation(
            dependency,
            r'r"(?P<prefix>.+-)(?P<version>\d{8})',
            r'r"(?P<prefix>).+-(?P<version>\d{8})',
            "a snapshot's prefix leaves out its label, so the reference crosses to another line",
        ),
        Mutation(
            dependency,
            r'r"(?P<prefix>(?:.+-)?)(?P<version>\d{4}-',
            r'r"(?P<prefix>)(?:.+-)?(?P<version>\d{4}-',
            "a dashed date's prefix leaves out its label, so the reference crosses to another line",
        ),
        Mutation(
            dependency,
            r'(?:-\d{2}-\d{2}-\d{2})?)" + _LABEL_AFTER_A_DATE',
            r'(?:-\d{2}-\d{2}-\d{2})?)(?P<suffix>)"',
            "a dashed date is read wherever it starts the tag, so a slim reference drifts onto the fat image",
        ),
    )
    def test_dated_snapshot_moves_to_the_newest_snapshot_under_its_own_labels(self):
        """Test that a dated snapshot moves to the newest snapshot under its own labels, whatever other labels hold."""
        cases = {
            "a date": ("debian", "bookworm-20240110", "bookworm-20250101", "trixie-20260803"),
            "a date with a label after it": (
                "debian",
                "trixie-20260803-slim",
                "trixie-20261005-slim",
                "trixie-20261020",
            ),
            "a labelled dashed date": ("jupyter/base-notebook", "x86_64-2023-10-19", "x86_64-2023-10-20", "2023-10-21"),
            "a dashed date with a label after it": (
                "jupyter/base-notebook",
                "2023-10-19-slim",
                "2023-10-20-slim",
                "2023-10-21",
            ),
            "a timestamp": ("gitpod/workspace-full", "2025-11-14-10-05-32", "2026-02-12-23-06-03", "x86_64-2026-03-01"),
        }
        for case, (image, current_tag, sibling_tag, other_labels_tag) in cases.items():
            with self.subTest(case=case):
                self.requests.side_effect = mock_docker_registry(
                    docker_tag(other_labels_tag, DIGEST3),
                    docker_tag(sibling_tag, DIGEST2),
                    docker_tag(current_tag, DIGEST1),
                )
                latest = get_latest_tag(image, current_tag)
                self.assertEqual(latest.version, sibling_tag)
                self.assertEqual(latest.sha, DIGEST2)

    def test_listing_read_once_for_two_references(self):
        """Test that a second reference to one image is dated by the listing the first one already read."""
        pushed = days_ago(10)
        self.requests.side_effect = mock_docker_registry(
            docker_tag("3.14.7", DIGEST1, tag_last_pushed=pushed),
            docker_tag("3.13.5", DIGEST2, tag_last_pushed=pushed),
        )
        get_latest_tag("python", "3.14.7")
        get_latest_tag("python", "3.13.5")
        listings = [call for call in self.requests.call_args_list if "/tags?" in call.args[0]]
        self.assertEqual(len(listings), 1)

    def test_newest_release_names_the_most_precise_tag(self):
        """Test that the release is named by the most precise of the tags pushed at that moment.

        Docker Hub pushes a tag together with the tags serving the same image, so `latest`, `3`, `3.14` and
        `3.14.7` carry one push date between them.
        """
        pushed = days_ago(10)
        names = ("latest", "3", "3.14", "3.14.7")
        self.requests.side_effect = mock_docker_registry(
            *(docker_tag(name, DIGEST, tag_last_pushed=pushed) for name in names)
        )
        latest = get_latest_tag("python", "3.12")
        self.assertEqual(Release("3.14.7", pushed), latest.project.newest)

    def test_newest_release_ignores_cooldown(self):
        """Test that the newest release is the newest tag even when that tag is held back by the cooldown."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4", DIGEST, tag_last_pushed=FRESH_DATE))
        latest = get_latest_tag("newest_release", "1.3")
        self.assertEqual(latest.version, "1.3")  # 1.4 is held back by the cooldown...
        self.assertEqual(Release("1.4", FRESH_DATE), latest.project.newest)

    def test_newest_release_ignores_version_bound(self):
        """Test that the newest release is the newest tag even when a bound excludes it from the update."""
        old = days_ago(400)
        newest = days_ago(10)
        self.requests.side_effect = mock_docker_registry(
            docker_tag("1.4", DIGEST1, tag_last_pushed=old), docker_tag("2.0", DIGEST2, tag_last_pushed=newest)
        )
        latest = get_latest_tag("bounded_staleness", "1.3", bound(Verb.ALLOW, "update<2"))
        self.assertEqual(latest.version, "1.4")  # The bound keeps the update below 2.0...
        # ...but 2.0 still defines staleness:
        self.assertEqual(Release("2.0", newest), latest.project.newest)

    def test_no_newest_release_for_other_registry(self):
        """Test that no newest release is reported for non-Docker-Hub registries, which expose no push date."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST), challenge=False)
        latest_tag = get_latest_tag("mcr.microsoft.com/dotnet/sdk", "1.0")
        self.assertIsNone(latest_tag.project.newest)

    def test_no_newest_release_without_tags(self):
        """Test that no newest release is set when the registry lists no tags at all."""
        self.requests.side_effect = mock_docker_registry()
        self.assertIsNone(get_latest_tag("no_tags", "1.0").project.newest)

    @patch_environ({"DOCKER_HUB_USERNAME": "joe_doe", "DOCKER_HUB_TOKEN": "pat123"})  # nosec
    @patch("requests.Session.post")
    def test_user_bearer_token(self, mock_post: Mock):
        """Test that the credentials are used for both the per-tag metadata and the OCI registry token requests."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST))
        self.assertEqual(
            get_latest_tag("new_version_available_with_credentials", "1.2").version,
            "2.1",
        )
        mock_post.assert_called_once_with(
            "https://hub.docker.com/v2/auth/token",
            timeout=10,
            json={"identifier": "joe_doe", "secret": "pat123"},  # nosec
        )
        token_call = next(call for call in self.requests.call_args_list if "auth.docker.io" in call.args[0])
        self.assertEqual(token_call.kwargs["auth"], ("joe_doe", "pat123"))  # nosec

    def test_anonymous_registry_token_without_credentials(self):
        """Test that an anonymous registry token is requested (no basic auth) when no credentials are set."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST))
        get_latest_tag("new_version_available", "1.2")
        token_call = next(call for call in self.requests.call_args_list if "auth.docker.io" in call.args[0])
        self.assertIsNone(token_call.kwargs["auth"])

    def test_registry_without_auth_challenge(self):
        """Test that a registry that doesn't challenge for auth (e.g. mcr.microsoft.com) is queried without a token."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST), challenge=False)
        latest = get_latest_tag("mcr.microsoft.com/dotnet/sdk", "1.0")
        self.assertEqual(latest.version, "1.1")
        self.assertEqual(DIGEST, latest.sha)
        tags_call = next(call for call in self.requests.call_args_list if "/tags/list" in call.args[0])
        self.assertEqual(tags_call.kwargs["headers"], {})  # No Authorization header for an anonymous registry.

    @kills(
        Mutation(
            oci._resolved_tag,
            "lambda candidate: candidate.name == current.name,",
            "lambda candidate: False,",
            "a rate-limited push date leaves the reference unpinned, holding back its own tag as well",
        )
    )
    def test_tag_whose_push_date_cannot_be_fetched_holds_back_the_tags_in_between(self):
        """Test that a Docker Hub tag whose push date can't be fetched holds back the tags in between, logging why."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("1.0", DIGEST1),
            docker_tag("1.1", DIGEST2),
            docker_tag("1.2", DIGEST3),
            unavailable=Endpoint.PUSH_DATE,
        )
        latest = get_latest_tag("push_date_unavailable", "1.0")
        self.assertEqual(latest.version, "1.0")
        self.assertEqual(DIGEST1, latest.sha)
        tags = "https://registry.hub.docker.com/v2/namespaces/library/repositories/push_date_unavailable/tags/"
        requested = requested_urls(self.requests)
        self.assertEqual([url for url in requested if url.startswith(tags)], [f"{tags}1.2"])
        self.assert_error_logged(
            message_of(Logger.undated_push),
            dependency="push_date_unavailable",
            candidate="1.2",
            current="1.0",
            reason="HTTP 503",
        )


def _tags_past_the_page_cap(pushed: datetime | None = None) -> list[dict[str, object]]:
    """Return more tags than the page cap reaches, so the listing is cut short before it has been read out."""
    listed = _MAX_TAG_LISTING_PAGES * _TAG_LISTING_PAGE_SIZE + 1
    return [docker_tag(f"1.{index}", DIGEST, tag_last_pushed=pushed) for index in range(listed)]


@patch_environ()
class GetLatestTagForFloatingTagTest(RegistryRequestsMixin, LoggingTestCase):
    """Unit tests for resolving a tag that names no version: a floating tag, a snapshot, or a tag that is neither."""

    def test_highest_concrete_alias(self):
        """Test that a floating tag resolves to the highest-versioned tag sharing its digest, and to that digest."""
        aliases = ("latest", "3.14.7", "3.14", "3")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(alias, DIGEST) for alias in aliases))
        latest = get_latest_tag("python", "latest")
        self.assertEqual(latest.version, "3.14.7")
        self.assertEqual(latest.sha, DIGEST)

    def test_alias_carrying_the_floating_tag_labels(self):
        """Test that a floating tag lands on an alias carrying its words, not on a suffixless one it ties with."""
        aliases = ("trixie", "26.7.0-trixie", "26.7-trixie", "26-trixie", "26.7.0", "26.7", "26")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in aliases))
        self.assertEqual(get_latest_tag("node", "trixie").version, "26.7.0-trixie")

    def test_alias_carrying_the_variant_of_the_floating_tag_but_not_its_channel(self):
        """Test that a floating tag lands on the alias carrying its variant, not on one carrying its channel.

        An alias carrying the channel floats on, `24-lts` naming whichever 24 release is the LTS one, so the pin
        keeps the variant word and drops the channel word even where a version tag carries it.
        """
        aliases = ("lts-alpine", "24-alpine", "24-lts")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in aliases))
        latest = get_latest_tag("node", "lts-alpine")
        self.assertEqual(latest.version, "24-alpine")
        self.assertEqual(latest.floating, FloatingPin.RESOLVED)

    def test_alias_with_the_most_precise_suffix_version(self):
        """Test that a floating tag lands on the alias whose variant word carries the most precise version."""
        aliases = ("lts-alpine", "lts-alpine3.24", "24-alpine", "24-alpine3.24", "24.19.0-alpine", "24.19.0-alpine3.24")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in aliases))
        latest = get_latest_tag("node", "lts-alpine")
        self.assertEqual(latest.version, "24.19.0-alpine3.24")

    def test_alias_on_a_later_page(self):
        """Test that a floating tag whose aliases the listing pages past the first is resolved all the same."""
        first_page = [docker_tag(f"1.{index}", DIGEST2) for index in range(100)]
        aliases = [docker_tag("lts-alpine", DIGEST1), docker_tag("24.19.0-alpine3.24", DIGEST1)]
        self.requests.side_effect = mock_docker_registry(*first_page, *aliases)
        latest = get_latest_tag("node", "lts-alpine")
        self.assertEqual(latest.version, "24.19.0-alpine3.24")

    def test_alias_on_a_page_after_one_holding_other_digests(self):
        """Test that an alias is found on a later page, the aliases of one digest being interleaved with other tags."""
        listed = [docker_tag("latest", DIGEST1), docker_tag("3", DIGEST1)]
        listed += [docker_tag(f"1.{index}", DIGEST2) for index in range(98)]  # The page ends on another digest.
        listed.append(docker_tag("3.14.7", DIGEST1))  # The most precise alias, on the page after it.
        self.requests.side_effect = mock_docker_registry(*listed, page_size=100)
        latest = get_latest_tag("python", "latest")
        self.assertEqual(latest.version, "3.14.7")

    def test_tag_listed_below_the_page_cap(self):
        """Test that a floating tag the listing pages past the cap is left as it is, at a bounded cost."""
        self.requests.side_effect = mock_docker_registry(*_tags_past_the_page_cap())
        latest = get_latest_tag("node", "lts-alpine")
        self.assertEqual(latest.version, "lts-alpine")
        self.assertEqual(latest.floating, FloatingPin.NOT_AMONG_EXAMINED)
        listings = [call for call in self.requests.call_args_list if "/tags?" in call.args[0]]
        self.assertEqual(len(listings), _MAX_TAG_LISTING_PAGES)

    @kills(
        Mutation(
            oci._unpinned_floating_tag,
            "if reason in (FloatingPin.NOT_LISTED, FloatingPin.NO_MANIFEST)",
            "if reason in (FloatingPin.NOT_LISTED, FloatingPin.NO_MANIFEST, FloatingPin.NOT_AMONG_EXAMINED)",
            "a listing cut short before reaching the tag leaves the image's newest release off the reference",
        )
    )
    def test_newest_release_when_the_listing_was_cut_short(self):
        """Test that a floating tag the listing may hold below the page cap is dated by the image's newest release."""
        self.requests.side_effect = mock_docker_registry(*_tags_past_the_page_cap(STALE_DATE))
        latest = get_latest_tag("node", "lts-alpine")
        newest_on_the_first_page = f"1.{_TAG_LISTING_PAGE_SIZE - 1}"
        self.assertEqual(Release(newest_on_the_first_page, STALE_DATE), latest.project.newest)

    def test_listing_read_one_page_past_the_aliases(self):
        """Test that the listing is read until a page holds no alias, which is what says the aliases are exhausted."""
        aliases = [docker_tag("latest", DIGEST1), docker_tag("3.14.7", DIGEST1)]
        listed_below = [docker_tag(f"1.{index}", DIGEST2) for index in range(198)]
        self.requests.side_effect = mock_docker_registry(*aliases, *listed_below)
        latest = get_latest_tag("python", "latest")
        self.assertEqual(latest.version, "3.14.7")
        listings = [call for call in self.requests.call_args_list if "/tags?" in call.args[0]]
        self.assertEqual(len(listings), 2)  # The aliases sit on the first page, and the second holds none of them.

    def test_alias_carrying_another_label(self):
        """Test that an alias labelled for a component it bundles, such as `php8.3`, is not read as a version."""
        aliases = ("latest", "php8.3", "php8.3-apache", "7.1.0", "7.1.0-apache", "7.1", "7")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in aliases))
        self.assertEqual(get_latest_tag("wordpress", "latest").version, "7.1.0")

    @kills(
        Mutation(
            oci,
            "@cache\ndef _get_tag(image: str, name: str) -> Tag | None:",
            "def _get_tag(image: str, name: str) -> Tag | None:",
            "a tag named more than once in a scan is resolved once per reference",
        )
    )
    def test_repeating_a_reference_costs_no_request(self):
        """Test that a tag resolved a second time asks the registry for nothing, everything it reads being cached."""
        self.requests.side_effect = mock_docker_registry(docker_tag("12.14", DIGEST), docker_tag("12.15", DIGEST))
        get_latest_tag("debian", "12.14")
        self.requests.reset_mock()
        get_latest_tag("debian", "12.14")
        self.requests.assert_not_called()

    def test_an_up_to_date_tag_costs_no_push_date_request(self):
        """Test that a tag with no newer one is resolved without asking Docker Hub when it was pushed."""
        self.requests.side_effect = mock_docker_registry(docker_tag("12.15", DIGEST))
        self.assertEqual(get_latest_tag("debian", "12.15").sha, DIGEST)
        requested = requested_urls(self.requests)
        self.assertNotIn(
            "https://registry.hub.docker.com/v2/namespaces/library/repositories/debian/tags/12.15", requested
        )

    def test_dated_snapshot_tag_without_a_manifest(self):
        """Test that a snapshot tag the registry serves no manifest for keeps its tag, with no digest to pin it.

        The tag names a version, so the image's newest release dates it whether or not the registry serves it.
        """
        pushed = days_ago(10)
        self.requests.side_effect = mock_docker_registry(
            docker_tag("12.15", DIGEST, tag_last_pushed=pushed), names=["bookworm-20260803"]
        )
        latest = get_latest_tag("debian", "bookworm-20260803")
        self.assertEqual(latest.version, "bookworm-20260803")
        self.assertEqual(latest.sha, "")
        self.assertEqual(Release("12.15", pushed), latest.project.newest)

    def test_newest_release_for_a_floating_tag(self):
        """Test that a reference on a floating tag is measured against the image's newest release.

        The run pins the tag to the version it serves, and that version names the same dependency as any other.
        """
        names = ("latest", "3.14.7")
        self.requests.side_effect = mock_docker_registry(
            *(docker_tag(name, DIGEST, tag_last_pushed=STALE_DATE) for name in names)
        )
        latest = get_latest_tag("python", "latest")
        self.assertEqual(latest.version, "3.14.7")
        self.assertEqual(Release("3.14.7", STALE_DATE), latest.project.newest)

    @kills(
        Mutation(
            oci._resolved_tag,
            "return DependencyVersion(version=current.name, sha=tag.digest, publication=tag.pushed)",
            "return DependencyVersion(version=current.name, publication=tag.pushed)",
            "a tag naming neither a version nor a channel is left without the digest that would pin it",
        ),
        Mutation(
            oci._resolved_tag,
            "return DependencyVersion(version=current.name, sha=tag.digest, publication=tag.pushed)",
            "return DependencyVersion(version=current.name, sha=tag.digest, publication=tag.pushed, "
            "unserved=Unserved.VERSION)",
            "a tag naming neither a version nor a channel is dated by no release, whatever the registry serves",
        ),
    )
    def test_tag_naming_neither_a_version_nor_a_channel(self):
        """Test that a tag such as `dev-2024` keeps its tag, is pinned to the digest it serves, and is dated."""
        aliases = ("dev-2024", "dev", "12.15", "12")
        self.requests.side_effect = mock_docker_registry(
            *(docker_tag(name, DIGEST, tag_last_pushed=STALE_DATE) for name in aliases)
        )
        latest = get_latest_tag("debian", "dev-2024")
        self.assertEqual(latest.version, "dev-2024")
        self.assertEqual(latest.sha, DIGEST)
        self.assertIsNone(latest.floating)  # The tag names no channel, so no pin of its floats.
        self.assertEqual(Release("12.15", STALE_DATE), latest.project.newest)

    @kills(
        Mutation(
            oci._resolved_tag,
            "return DependencyVersion(version=current.name, unserved=Unserved.VERSION)",
            "return DependencyVersion(version=current.name)",
            "a tag the registry serves no manifest for is dated by the image's newest release",
        )
    )
    def test_no_newest_release_when_the_registry_serves_no_manifest(self):
        """Test that a tag such as `dev-2024` the registry serves no manifest for is dated by no release."""
        self.requests.side_effect = mock_docker_registry(docker_tag("4.7.0", DIGEST, tag_last_pushed=STALE_DATE))
        latest = get_latest_tag("acme/api", "dev-2024")
        self.assertEqual(latest.version, "dev-2024")
        self.assertEqual(latest.sha, "")  # The registry serves no manifest, so there is no digest to pin.
        self.assertIsNone(latest.project.newest)

    def test_shortest_alias_wins_when_the_tag_names_no_variant(self):
        """Test that a floating tag naming no variant lands on the alias that adds none, versioned or not."""
        cases = (
            ("unversioned variant", "node", ("latest", "26.7.0", "26.7", "26", "26.7.0-trixie", "26-trixie"), "26.7.0"),
            ("versioned variant", "amazoncorretto", ("latest", "8u504", "8-al2023", "8-al2023-jdk", "8-jdk", "8"), "8"),
        )
        for case, image, aliases, expected in cases:
            with self.subTest(case=case):
                self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in aliases))
                self.assertEqual(get_latest_tag(image, "latest").version, expected)

    def test_listing_read_once_for_two_floating_tags(self):
        """Test that a second floating tag of one repository reads the listing the first one already read."""
        listed = (docker_tag("latest", DIGEST1), docker_tag("3.14.7", DIGEST1))
        listed += (docker_tag("slim", DIGEST2), docker_tag("3.14.7-slim", DIGEST2))
        self.requests.side_effect = mock_docker_registry(*listed)
        self.assertEqual(get_latest_tag("python", "latest").version, "3.14.7")
        self.assertEqual(get_latest_tag("python", "slim").version, "3.14.7-slim")
        listings = [call for call in self.requests.call_args_list if "/tags?" in call.args[0]]
        self.assertEqual(len(listings), 1)

    def test_tag_not_listed(self):
        """Test that a floating tag the registry does not list is left as it is, and dated by no release."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.7", DIGEST, tag_last_pushed=STALE_DATE))
        latest = get_latest_tag("python", "latest")
        self.assertEqual(latest.version, "latest")
        self.assertEqual(latest.floating, FloatingPin.NOT_LISTED)
        self.assertIsNone(latest.project.newest)

    @kills(
        Mutation(
            docker_hub._listing_page,
            "_LOG.response(response)",
            "pass",
            "a tag listing Docker Hub fails to serve leaves the floating tag as it is without a warning",
        )
    )
    def test_listing_unavailable(self):
        """Test that a floating tag is left as it is when the tag listing can't be fetched, and the failure logged."""
        self.requests.side_effect = mock_docker_registry(unavailable=Endpoint.TAG_DIGESTS)
        latest = get_latest_tag("python", "latest")
        self.assertEqual(latest.version, "latest")
        url = "https://registry.hub.docker.com/v2/namespaces/library/repositories/python/tags?page_size=100"
        self.assert_could_not_fetch_logged(url, status=HTTPStatus.SERVICE_UNAVAILABLE)

    @kills(
        Mutation(
            oci._walked_floating_tag,
            """for candidate in ordered[:_MAX_FLOATING_TAG_PROBES]:
        if _manifest_digest(image, candidate.name) == digest:
            return DependencyVersion(version=candidate.name, sha=digest, floating=FloatingPin.RESOLVED)""",
            """probes = ordered[:_MAX_FLOATING_TAG_PROBES]
    for candidate in [tag for tag in probes if _manifest_digest(image, tag.name) == digest]:
        return DependencyVersion(version=candidate.name, sha=digest, floating=FloatingPin.RESOLVED)""",
            "the walk asks every tag for its manifest rather than stopping at the tag it pins",
        )
    )
    def test_walk_stops_at_the_first_tag_serving_the_digest(self):
        """Test that the walk asks for one manifest per candidate down to the match, and for none below it."""
        served = ("latest", "3.14.7", "3.14", "3")  # `3.15.0` is newer than the image the floating tag serves.
        tags = [docker_tag(name, DIGEST1) for name in served] + [docker_tag("3.15.0", DIGEST2)]
        self.requests.side_effect = mock_docker_registry(*tags)
        latest = get_latest_tag("ghcr.io/owner/python", "latest")
        self.assertEqual(latest.version, "3.14.7")
        manifests = [
            call.args[0].rsplit("/", maxsplit=1)[-1]
            for call in self.requests.call_args_list
            if "/manifests/" in call.args[0]
        ]
        self.assertEqual(manifests, ["latest", "3.15.0", "3.14.7"])  # `3.14` and `3` sort below the match.

    def test_walk_keeps_the_words_of_the_floating_tag(self):
        """Test that the walk lands on an alias carrying the floating tag's words, not on a shorter one it ties with."""
        names = ("trixie", "26.7.0-trixie", "26.7-trixie", "26.7.0", "26.7", "26")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in names))
        latest = get_latest_tag("ghcr.io/owner/node", "trixie")
        self.assertEqual(latest.version, "26.7.0-trixie")

    def test_walk_gives_up_before_examining_every_tag(self):
        """Test that a floating tag no version tag near the top of the listing serves is left as it is."""
        listed = [docker_tag(f"3.0.{index}", DIGEST2) for index in range(100)]
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST1), *listed)
        latest = get_latest_tag("ghcr.io/owner/python", "latest")
        self.assertEqual(latest.version, "latest")
        self.assertEqual(latest.floating, FloatingPin.NO_VERSION_TAG_EXAMINED)
        self.assertEqual(latest.sha, DIGEST1)
        manifests = [call for call in self.requests.call_args_list if "/manifests/" in call.args[0]]
        # The walk asked the floating tag for its digest, then spent its budget on the newest version tags:
        self.assertEqual(len(manifests), _MAX_FLOATING_TAG_PROBES + 1)

    @kills(
        Mutation(
            oci,
            "@cache\ndef _manifest_digest(image: str, tag: str) -> str:",
            "def _manifest_digest(image: str, tag: str) -> str:",
            "a walked candidate has its manifest read once per reference naming the floating tag",
        )
    )
    def test_the_digest_of_a_walked_candidate_is_read_once(self):
        """Test that a second reference to one floating tag off Docker Hub re-reads none of the walk's manifests."""
        names = ("latest", "3.14.7")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in names))
        for _ in range(2):
            get_latest_tag("ghcr.io/owner/python", "latest")
        manifests = [call for call in self.requests.call_args_list if "/manifests/" in call.args[0]]
        self.assertEqual(len(manifests), 2)  # `latest` and the candidate serving its digest, once each.

    def test_floating_tag_on_another_registry_the_walk_finds_no_match_for(self):
        """Test that a floating tag no version tag of the registry serves is left as it is."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST1), docker_tag("3.14.7", DIGEST2))
        latest = get_latest_tag("ghcr.io/owner/python", "latest")
        self.assertEqual(latest.version, "latest")
        self.assertEqual(latest.floating, FloatingPin.NO_VERSION_TAG)

    def test_floating_tag_on_another_registry_without_a_manifest(self):
        """Test that a floating tag the registry serves no manifest for is left as it is, its digest unknown."""
        names = ["latest", "3.14.7"]
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.7", DIGEST), names=names)
        latest = get_latest_tag("ghcr.io/owner/python", "latest")
        self.assertEqual(latest.version, "latest")
        self.assertEqual(latest.floating, FloatingPin.NO_MANIFEST)

    def test_digest_without_concrete_tag(self):
        """Test that a floating tag whose digest no concrete tag serves is left as it is."""
        self.requests.side_effect = mock_docker_registry(docker_tag("dev", DIGEST), docker_tag("prod", DIGEST))
        latest = get_latest_tag("acme/api", "dev")
        self.assertEqual(latest.version, "dev")
        self.assertEqual(latest.floating, FloatingPin.NO_VERSION_TAG)

    def test_floating_tag_on_another_registry(self):
        """Test that a floating tag on a registry other than Docker Hub resolves to the tag serving its digest."""
        names = ("latest", "3.14.7", "3.14", "3")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in names))
        latest = get_latest_tag("ghcr.io/owner/python", "latest")
        self.assertEqual(latest.version, "3.14.7")
        self.assertEqual(latest.sha, DIGEST)
        self.assertEqual(latest.floating, FloatingPin.RESOLVED)


class RegistryTokenTest(LoggingTestCase):
    """Unit tests for discovering and fetching a registry pull token."""

    @patch("requests.Session.get", Mock(side_effect=requests.exceptions.ConnectionError))
    def test_probe_network_error(self):
        """Test that a network error probing the registry yields no token (anonymous) instead of crashing."""
        self.assertIsNone(_registry_token("ghcr.io", "owner/repo"))

    @kills(
        Mutation(
            oci._registry_token,
            "_LOG.response(response)",
            "pass",
            "drops the warning about a token request the registry fails to answer",
        )
    )
    @patch("requests.Session.get")
    def test_token_request_failure(self, mock_get: Mock):
        """Test that a failed token request (after a valid auth challenge) yields no token instead of crashing."""
        probe = "https://ghcr.io/v2/"
        mock_get.side_effect = [probe_response(probe, challenge=True), service_unavailable(token_endpoint(probe))]
        self.assertIsNone(_registry_token("ghcr.io", "owner/repo"))
        self.assert_could_not_fetch_logged(token_endpoint(probe), status=HTTPStatus.SERVICE_UNAVAILABLE)
