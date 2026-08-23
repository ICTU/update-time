"""Unit tests for the OCI registry module."""

import unittest
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from unittest.mock import Mock, patch

import requests

from update_time.domain.bound import NO_BOUND, Verb
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import FloatingPin, Release
from update_time.sources import docker_hub, oci
from update_time.sources.docker_hub import _MAX_TAG_LISTING_PAGES
from update_time.sources.oci import (
    _MAX_FLOATING_TAG_PROBES,
    Tag,
    _registry_token,
    get_latest_tag,
    is_docker_hub_image,
)

from tests.helpers import mock_response, patch_environ
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2, DIGEST3
from tests.update_time.helpers import LoggingTestCase, bound, docker_tag
from tests.update_time.registry import Endpoint, RegistryRequestsMixin, mock_docker_registry


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
        )
        for lower, higher in lower_and_higher:
            with self.subTest(lower=lower, higher=higher):
                self.assertLess(Tag(lower), Tag(higher))


@patch_environ()
class GetLatestTagTest(RegistryRequestsMixin, LoggingTestCase):
    """Unit tests for getting the latest tag."""

    def test_no_tags(self):
        """Test that the current tag is returned if the image has no tags."""
        self.requests.side_effect = mock_docker_registry()
        self.assertEqual(get_latest_tag("no_tags", "1.0", NO_BOUND, COOLDOWN.default).version, "1.0")

    def test_image_not_resolvable(self):
        """Test that a reference that doesn't resolve (e.g. a CircleCI machine image) is left unchanged, not crash."""
        self.requests.side_effect = mock_docker_registry(unavailable=Endpoint.TAG_NAMES)
        self.assertEqual(get_latest_tag("ubuntu-2204", "2025.09.1", NO_BOUND, COOLDOWN.default).version, "2025.09.1")
        url = "https://registry-1.docker.io/v2/library/ubuntu-2204/tags/list?n=1000"
        self.assert_could_not_fetch_logged(url, HTTPStatus.NOT_FOUND)

    def test_other_registry_image_resolved(self):
        """Test that an image on a registry other than Docker Hub is resolved against that registry's host."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        latest = get_latest_tag("registry.gitlab.com/group/image", "1.0", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "1.1")
        self.assertEqual(DIGEST, latest.sha)
        self.assertIsNone(latest.published)  # No push date (and so no cooldown) outside Docker Hub.
        self.assertTrue(any("registry.gitlab.com" in call.args[0] for call in self.requests.call_args_list))

    def test_other_registry_repository_path_excludes_host(self):
        """Test that the API repository path drops the registry host (a strict registry 404s on a host-prefixed one)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        get_latest_tag("mcr.microsoft.com/devcontainers/typescript-node", "1.0", NO_BOUND, COOLDOWN.default)
        tags_list_urls = [call.args[0] for call in self.requests.call_args_list if "/tags/list" in call.args[0]]
        self.assertTrue(tags_list_urls)
        for url in tags_list_urls:
            with self.subTest(url=url):
                self.assertIn("https://mcr.microsoft.com/v2/devcontainers/typescript-node/tags/list", url)
                self.assertNotIn("/v2/mcr.microsoft.com/", url)  # The host must not leak into the repository path.

    def test_explicit_docker_hub_host(self):
        """Test that an image with an explicit docker.io host is updated, querying the host-less Docker Hub URL."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        self.assertEqual(get_latest_tag("docker.io/library/redis", "1.0", NO_BOUND, COOLDOWN.default).version, "1.1")
        self.assertIn("/namespaces/library/repositories/redis/", self.requests.call_args.args[0])

    def test_up_to_date(self):
        """Test that the current tag and its digest are returned if it's up to date, so it can be pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.0", DIGEST))
        latest = get_latest_tag("up_to_date", "1.0", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "1.0")
        self.assertEqual(DIGEST, latest.sha)

    def test_newer(self):
        """Test that the current tag is returned if it's newer than the newest tag available."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.0", DIGEST))
        self.assertEqual(get_latest_tag("newer", "1.1", NO_BOUND, COOLDOWN.default).version, "1.1")

    def test_new_version_available(self):
        """Test that the new tag is returned if it's newer, without a publication date when the push date is unknown."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST))
        latest = get_latest_tag("new_version_available", "1.2", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "2.1")
        self.assertIsNone(latest.published)

    def test_multiple_new_versions_available(self):
        """Test that the newest tag is returned if multiple newer tags are available."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("2.2", DIGEST2), docker_tag("2.1", DIGEST1), docker_tag("2.3", DIGEST3)
        )
        self.assertEqual(get_latest_tag("new_versions_available", "1.2", NO_BOUND, COOLDOWN.default).version, "2.3")

    def test_ignore_tags_without_digest(self):
        """Test that tags without digests are ignored, falling back to the next-highest version."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.2", DIGEST), docker_tag("2.3"))
        self.assertEqual(get_latest_tag("ignore_tags_without_digest", "1.2", NO_BOUND, COOLDOWN.default).version, "2.2")

    def test_equal_version_alias_tag_keeps_current_spelling(self):
        """Test that an alias tag spelling the current version differently (`22.15` for `22.15.0`) is not adopted."""
        self.requests.side_effect = mock_docker_registry(docker_tag("22.15", DIGEST), docker_tag("22.15.0", DIGEST))
        latest = get_latest_tag("alias", "22.15.0", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "22.15.0")
        self.assertEqual(latest.sha, DIGEST)

    def test_equal_version_alias_tag_keeps_current_spelling_under_exact_bound(self):
        """Test that a bound pinning the current release exactly (`ignore[patch-update]`) adopts no alias spelling."""
        self.requests.side_effect = mock_docker_registry(docker_tag("22.15", DIGEST), docker_tag("22.15.0", DIGEST))
        version_bound = bound(Verb.ALLOW, "update==22.15.0")
        self.assertEqual(get_latest_tag("alias-bounded", "22.15.0", version_bound, COOLDOWN.default).version, "22.15.0")

    def test_equal_version_alias_tag_does_not_lend_its_digest(self):
        """Test that the current spelling keeps its own digest, not a co-listed alias tag's differing digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("22.15", DIGEST2), docker_tag("22.15.0", DIGEST1))
        latest = get_latest_tag("alias", "22.15.0", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "22.15.0")
        self.assertEqual(latest.sha, DIGEST1)

    def test_update_to_a_version_listed_under_two_spellings_keeps_the_precise_one(self):
        """Test that updating to a version the registry lists twice adopts the precise spelling, not the alias."""
        self.requests.side_effect = mock_docker_registry(docker_tag("22.16", DIGEST1), docker_tag("22.16.0", DIGEST2))
        latest = get_latest_tag("two-spellings", "22.15.0", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "22.16.0")
        self.assertEqual(latest.sha, DIGEST2)

    def test_level_bound_anchors_to_the_current_tag(self):
        """Test that a level bound is anchored to the current tag, keeping updates within the pinned minor line."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.12.9", DIGEST1), docker_tag("3.13.0", DIGEST2))
        version_bound = bound(Verb.IGNORE, "minor-update")
        self.assertEqual(get_latest_tag("level-bounded", "3.12.1", version_bound, COOLDOWN.default).version, "3.12.9")

    def test_bound_narrows_candidates(self):
        """Test that a version bound drops out-of-bound tags so a bounded tag wins over a higher one."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("2.2", DIGEST2), docker_tag("2.1", DIGEST1), docker_tag("2.3", DIGEST3)
        )
        version_bound = bound(Verb.ALLOW, "update<2.3")
        self.assertEqual(get_latest_tag("bounded", "1.2", version_bound, COOLDOWN.default).version, "2.2")

    def test_tag_names_paginated(self):
        """Test that the newest tag is returned even if the tag names listing is paginated."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("2.1", DIGEST1), docker_tag("2.2", DIGEST2), page_size=1
        )
        self.assertEqual(get_latest_tag("pagination", "1.2", NO_BOUND, COOLDOWN.default).version, "2.2")

    def test_tag_manifest_not_found(self):
        """Test that a listed tag whose manifest can't be fetched is skipped, leaving the current tag unchanged."""
        self.requests.side_effect = mock_docker_registry(names=["2.2"])
        self.assertEqual(get_latest_tag("manifest_not_found", "1.2", NO_BOUND, COOLDOWN.default).version, "1.2")
        url = "https://registry-1.docker.io/v2/library/manifest_not_found/manifests/2.2"
        self.assert_could_not_fetch_logged(url, HTTPStatus.NOT_FOUND)

    def test_invalid_new_tag(self):
        """Test that a tag whose version part can't be parsed (e.g. 1.2.invalid) is ignored."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.2.invalid", DIGEST))
        self.assertEqual(get_latest_tag("invalid_new_tag", "1.3", NO_BOUND, COOLDOWN.default).version, "1.3")

    def test_prerelease(self):
        """Test that prerelease tags are ignored."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4a1", DIGEST))
        self.assertEqual(get_latest_tag("prerelease", "1.3", NO_BOUND, COOLDOWN.default).version, "1.3")

    @kills(
        Mutation(
            oci,
            "        if self._is_dated_snapshot and not current._is_dated_snapshot:",
            "        if self._is_dated_snapshot and current._is_dated_snapshot:",
            "a dated snapshot of a development branch is adopted as an update for a tag naming a release",
        )
    )
    def test_dated_snapshot_tag_is_no_candidate_for_a_release(self):
        """Test that a tag naming a date, such as `20260805`, loses to a release tag it sorts above."""
        self.requests.side_effect = mock_docker_registry(docker_tag("20260805", DIGEST1), docker_tag("3.25.0", DIGEST2))
        latest = get_latest_tag("dated_snapshot", "3.24.1", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "3.25.0")

    @kills(
        Mutation(
            oci,
            "        try:\n            date.fromisoformat(str(release[0]))\n        except ValueError:\n"
            "            return False\n        return True",
            "        return True",
            "a version of eight digits naming no calendar date is passed over as if it were a snapshot",
        )
    )
    def test_eight_digit_version_that_is_no_date_is_a_candidate(self):
        """Test that an eight-digit version reading as no calendar date, such as `20261332`, still updates a release."""
        self.requests.side_effect = mock_docker_registry(docker_tag("20261332", DIGEST))
        latest = get_latest_tag("no_calendar_date", "3.24.1", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "20261332")

    def test_different_suffix(self):
        """Test that tags for different suffixes are ignored."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4-windows", DIGEST))
        self.assertEqual(get_latest_tag("different_suffix", "1.3", NO_BOUND, COOLDOWN.default).version, "1.3")

    def test_suffix_embedded_version_bumped(self):
        """Test that a version embedded in the suffix is upgraded while its label is kept (alpine3.23 -> alpine3.24)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.6-alpine3.24", DIGEST))
        latest = get_latest_tag("python", "3.14.6-alpine3.23", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "3.14.6-alpine3.24")
        self.assertEqual(DIGEST, latest.sha)

    def test_suffix_and_main_version_bumped_together(self):
        """Test that both the main version and the embedded suffix version advance together to the newest tag."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("3.14.6-alpine3.23", DIGEST1),
            docker_tag("3.14.6-alpine3.24", DIGEST2),
            docker_tag("3.15.0-alpine3.24", DIGEST3),
        )
        latest = get_latest_tag("python", "3.14.6-alpine3.23", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "3.15.0-alpine3.24")
        self.assertEqual(DIGEST3, latest.sha)

    def test_suffix_version_not_downgraded(self):
        """Test that a newer main version is not adopted when it would downgrade the embedded suffix version."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15.0-alpine3.22", DIGEST))
        self.assertEqual(
            get_latest_tag("python", "3.14.6-alpine3.23", NO_BOUND, COOLDOWN.default).version, "3.14.6-alpine3.23"
        )

    def test_suffix_label_not_crossed(self):
        """Test that a versioned suffix label is never crossed (alpine is not replaced by a newer debian)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15.0-debian12", DIGEST))
        self.assertEqual(
            get_latest_tag("python", "3.14.6-alpine3.23", NO_BOUND, COOLDOWN.default).version, "3.14.6-alpine3.23"
        )

    def test_invalid_suffix_version(self):
        """Test that a suffix with an unparsable embedded version is treated as an unversioned (whole) label."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15.0-alpine3.2.invalid", DIGEST))
        self.assertEqual(
            get_latest_tag("python", "3.14.6-alpine3.23", NO_BOUND, COOLDOWN.default).version, "3.14.6-alpine3.23"
        )

    def test_label_prefixed_version(self):
        """Test that a label-prefixed tag (e.g. python3.12-...) is bumped with the prefix and suffix kept."""
        self.requests.side_effect = mock_docker_registry(docker_tag("python3.13-bookworm-slim", DIGEST))
        latest = get_latest_tag("ghcr.io/astral-sh/uv", "python3.12-bookworm-slim", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "python3.13-bookworm-slim")
        self.assertEqual(DIGEST, latest.sha)

    def test_label_prefix_not_crossed(self):
        """Test that a python-prefixed tag is not replaced by a higher pypy-prefixed tag."""
        self.requests.side_effect = mock_docker_registry(docker_tag("pypy3.99-bookworm-slim", DIGEST))
        self.assertEqual(
            get_latest_tag("ghcr.io/astral-sh/uv", "python3.12-bookworm-slim", NO_BOUND, COOLDOWN.default).version,
            "python3.12-bookworm-slim",
        )

    def test_version_prefix_preserved(self):
        """Test that a 'v'-prefixed tag keeps its 'v' when bumped (v3.12 -> v3.13, not 3.13)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("v3.13", DIGEST))
        self.assertEqual(get_latest_tag("prefixed", "v3.12", NO_BOUND, COOLDOWN.default).version, "v3.13")

    def test_outside_cooldown(self):
        """Test that tags pushed before the cooldown are considered, with the push date as publication date."""
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4", DIGEST, tag_last_pushed=old))
        latest = get_latest_tag("outside_cooldown", "1.3", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "1.4")
        self.assertEqual(datetime.fromisoformat(old), latest.published)

    def test_cooldown_decides_eligibility(self):
        """Test that a tag is held back or adopted according to the cooldown the getter is passed."""
        pushed = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4", DIGEST, tag_last_pushed=pushed))
        for cooldown_days, expected in ((30, "1.3"), (5, "1.4")):
            with self.subTest(cooldown_days=cooldown_days):
                self.assertEqual(get_latest_tag("cooldown_argument", "1.3", NO_BOUND, cooldown_days).version, expected)

    def test_newest_release_ignores_labels(self):
        """Test that the newest release is the image's, whatever labels its tag carries.

        The update keeps the reference's `slim` label, while the release that dates the image carries none.
        """
        recent = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
        self.requests.side_effect = mock_docker_registry(  # Docker Hub lists the most recently pushed tag first
            docker_tag("3.14.7", DIGEST2, tag_last_pushed=recent),
            docker_tag("3.13-slim", DIGEST1, tag_last_pushed=old),
        )
        latest = get_latest_tag("python", "3.12-slim", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "3.13-slim")  # The update keeps the slim line...
        self.assertEqual(Release("3.14.7", datetime.fromisoformat(recent)), latest.newest)  # ...staleness does not.

    @kills(
        Mutation(
            oci,
            "    if latest is None or not latest.is_eligible(cooldown_days):",
            "    if latest is None or not (latest.is_eligible(cooldown_days) or latest._is_dated_snapshot):",
            "a dated snapshot is adopted however freshly it was pushed",
        )
    )
    def test_dated_snapshot_within_the_cooldown_is_held_back(self):
        """Test that a snapshot pushed too recently is held back, and the sibling below it adopted instead."""
        fresh = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        eligible = (datetime.now(UTC) - timedelta(days=200)).isoformat()
        current = (datetime.now(UTC) - timedelta(days=955)).isoformat()
        self.requests.side_effect = mock_docker_registry(
            docker_tag("bookworm-20260803", DIGEST3, tag_last_pushed=fresh),
            docker_tag("bookworm-20250101", DIGEST2, tag_last_pushed=eligible),
            docker_tag("bookworm-20240110", DIGEST1, tag_last_pushed=current),
        )
        latest = get_latest_tag("debian", "bookworm-20240110", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "bookworm-20250101")
        self.assertEqual(latest.sha, DIGEST2)

    @kills(
        Mutation(
            oci,
            '_DATED_SNAPSHOT = re.compile(r"(?P<prefix>.+-)(?P<version>\\d{8})(?P<suffix>)$")',
            '_DATED_SNAPSHOT = re.compile(r"(?P<prefix>).+-(?P<version>\\d{8})(?P<suffix>)$")',
            "a snapshot's label is read as no part of its tag, so the pin loses it and crosses to another line",
        )
    )
    def test_dated_snapshot_stays_on_its_own_line(self):
        """Test that a dated snapshot is not moved onto another label's line, however much newer that line is.

        `trixie-20260803` is newer than every bookworm snapshot, and the reference stays on the bookworm line.
        """
        current = (datetime.now(UTC) - timedelta(days=955)).isoformat()
        sibling = (datetime.now(UTC) - timedelta(days=200)).isoformat()
        other_line = (datetime.now(UTC) - timedelta(days=20)).isoformat()
        self.requests.side_effect = mock_docker_registry(
            docker_tag("trixie-20260803", DIGEST3, tag_last_pushed=other_line),
            docker_tag("bookworm-20250101", DIGEST2, tag_last_pushed=sibling),
            docker_tag("bookworm-20240110", DIGEST1, tag_last_pushed=current),
        )
        latest = get_latest_tag("debian", "bookworm-20240110", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "bookworm-20250101")
        self.assertEqual(latest.sha, DIGEST2)

    @kills(
        Mutation(
            docker_hub,
            "@cache\ndef _listing_page(url: str) -> tuple[tuple[_TagJSON, ...], str]:",
            "def _listing_page(url: str) -> tuple[tuple[_TagJSON, ...], str]:",
            "every reference to an image reads the listing that dates it again",
        )
    )
    def test_listing_read_once_for_two_references(self):
        """Test that a second reference to one image is dated by the listing the first one already read."""
        pushed = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        self.requests.side_effect = mock_docker_registry(
            docker_tag("3.14.7", DIGEST, tag_last_pushed=pushed),
            docker_tag("3.13.5", DIGEST2, tag_last_pushed=pushed),
        )
        get_latest_tag("python", "3.14.7", NO_BOUND, COOLDOWN.default)
        get_latest_tag("python", "3.13.5", NO_BOUND, COOLDOWN.default)
        listings = [call for call in self.requests.call_args_list if "/tags?" in call.args[0]]
        self.assertEqual(len(listings), 1)

    def test_newest_release_names_the_most_precise_tag(self):
        """Test that the release is named by the most precise of the tags pushed at that moment.

        Docker Hub pushes a tag together with the tags serving the same image, so `latest`, `3`, `3.14` and
        `3.14.7` carry one push date between them.
        """
        pushed = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        names = ("latest", "3", "3.14", "3.14.7")
        self.requests.side_effect = mock_docker_registry(
            *(docker_tag(name, DIGEST, tag_last_pushed=pushed) for name in names)
        )
        latest = get_latest_tag("python", "3.12", NO_BOUND, COOLDOWN.default)
        self.assertEqual(Release("3.14.7", datetime.fromisoformat(pushed)), latest.newest)

    def test_newest_release_ignores_cooldown(self):
        """Test that the newest release is the newest tag even when that tag is held back by the cooldown."""
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4", DIGEST, tag_last_pushed=recent))
        latest = get_latest_tag("newest_release", "1.3", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "1.3")  # 1.4 is held back by the cooldown...
        self.assertEqual(Release("1.4", datetime.fromisoformat(recent)), latest.newest)

    def test_newest_release_ignores_version_bound(self):
        """Test that the newest release is the newest tag even when a bound excludes it from the update."""
        old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
        newest = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        self.requests.side_effect = mock_docker_registry(
            docker_tag("1.4", DIGEST1, tag_last_pushed=old), docker_tag("2.0", DIGEST2, tag_last_pushed=newest)
        )
        latest = get_latest_tag("bounded_staleness", "1.3", bound(Verb.ALLOW, "update<2"), COOLDOWN.default)
        self.assertEqual(latest.version, "1.4")  # The bound keeps the update below 2.0...
        # ...but 2.0 still defines staleness:
        self.assertEqual(Release("2.0", datetime.fromisoformat(newest)), latest.newest)

    def test_no_newest_release_for_other_registry(self):
        """Test that no newest release is reported for non-Docker-Hub registries, which expose no push date."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST), challenge=False)
        self.assertIsNone(get_latest_tag("mcr.microsoft.com/dotnet/sdk", "1.0", NO_BOUND, COOLDOWN.default).newest)

    def test_no_newest_release_without_tags(self):
        """Test that no newest release is set when the registry lists no tags at all."""
        self.requests.side_effect = mock_docker_registry()
        self.assertIsNone(get_latest_tag("no_tags", "1.0", NO_BOUND, COOLDOWN.default).newest)

    @patch_environ({"DOCKER_HUB_USERNAME": "joe_doe", "DOCKER_HUB_TOKEN": "pat123"})  # nosec
    @patch("requests.post")
    def test_user_bearer_token(self, mock_post: Mock):
        """Test that the credentials are used for both the per-tag metadata and the OCI registry token requests."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST))
        self.assertEqual(
            get_latest_tag("new_version_available_with_credentials", "1.2", NO_BOUND, COOLDOWN.default).version, "2.1"
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
        get_latest_tag("new_version_available", "1.2", NO_BOUND, COOLDOWN.default)
        token_call = next(call for call in self.requests.call_args_list if "auth.docker.io" in call.args[0])
        self.assertIsNone(token_call.kwargs["auth"])

    def test_registry_without_auth_challenge(self):
        """Test that a registry that doesn't challenge for auth (e.g. mcr.microsoft.com) is queried without a token."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST), challenge=False)
        latest = get_latest_tag("mcr.microsoft.com/dotnet/sdk", "1.0", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "1.1")
        self.assertEqual(DIGEST, latest.sha)
        tags_call = next(call for call in self.requests.call_args_list if "/tags/list" in call.args[0])
        self.assertEqual(tags_call.kwargs["headers"], {})  # No Authorization header for an anonymous registry.

    def test_push_date_unavailable(self):
        """Test that a Docker Hub tag whose push date can't be fetched is still usable, just without a cooldown."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST), unavailable=Endpoint.PUSH_DATE)
        latest = get_latest_tag("push_date_unavailable", "1.0", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "1.1")
        self.assertEqual(DIGEST, latest.sha)
        self.assertIsNone(latest.published)
        # The unavailable push date is logged as a could-not-fetch warning for the Docker Hub tags API:
        url = "https://registry.hub.docker.com/v2/namespaces/library/repositories/push_date_unavailable/tags/1.1"
        self.assert_could_not_fetch_logged(url, HTTPStatus.NOT_FOUND)


@patch_environ()
class GetLatestTagForFloatingTagTest(RegistryRequestsMixin, LoggingTestCase):
    """Unit tests for resolving a tag that names no version: a floating tag, a snapshot, or a tag that is neither."""

    def test_highest_concrete_alias(self):
        """Test that a floating tag resolves to the highest-versioned tag sharing its digest, and to that digest."""
        aliases = ("latest", "3.14.7", "3.14", "3")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(alias, DIGEST) for alias in aliases))
        latest = get_latest_tag("python", "latest", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "3.14.7")
        self.assertEqual(latest.sha, DIGEST)

    def test_alias_carrying_the_floating_tag_labels(self):
        """Test that a floating tag lands on an alias carrying its words, not on a suffixless one it ties with."""
        aliases = ("trixie", "26.7.0-trixie", "26.7-trixie", "26-trixie", "26.7.0", "26.7", "26")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in aliases))
        self.assertEqual(get_latest_tag("node", "trixie", NO_BOUND, COOLDOWN.default).version, "26.7.0-trixie")

    def test_alias_carrying_the_variant_of_the_floating_tag_but_not_its_channel(self):
        """Test that a floating tag lands on the alias carrying its variant, not on one carrying its channel.

        An alias carrying the channel floats on, `24-lts` naming whichever 24 release is the LTS one, so the pin
        keeps the variant word and drops the channel word even where a version tag carries it.
        """
        aliases = ("lts-alpine", "24-alpine", "24-lts")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in aliases))
        latest = get_latest_tag("node", "lts-alpine", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "24-alpine")
        self.assertEqual(latest.floating, FloatingPin.RESOLVED)

    def test_alias_with_the_most_precise_suffix_version(self):
        """Test that a floating tag lands on the alias whose variant word carries the most precise version."""
        aliases = ("lts-alpine", "lts-alpine3.24", "24-alpine", "24-alpine3.24", "24.19.0-alpine", "24.19.0-alpine3.24")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in aliases))
        latest = get_latest_tag("node", "lts-alpine", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "24.19.0-alpine3.24")

    def test_alias_on_a_later_page(self):
        """Test that a floating tag whose aliases the listing pages past the first is resolved all the same."""
        first_page = [docker_tag(f"1.{index}", DIGEST2) for index in range(100)]
        aliases = [docker_tag("lts-alpine", DIGEST), docker_tag("24.19.0-alpine3.24", DIGEST)]
        self.requests.side_effect = mock_docker_registry(*first_page, *aliases)
        latest = get_latest_tag("node", "lts-alpine", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "24.19.0-alpine3.24")

    def test_alias_on_a_page_after_one_holding_other_digests(self):
        """Test that an alias is found on a later page, the aliases of one digest being interleaved with other tags."""
        listed = [docker_tag("latest", DIGEST), docker_tag("3", DIGEST)]
        listed += [docker_tag(f"1.{index}", DIGEST2) for index in range(98)]  # The page ends on another digest.
        listed.append(docker_tag("3.14.7", DIGEST))  # The most precise alias, on the page after it.
        self.requests.side_effect = mock_docker_registry(*listed, page_size=100)
        latest = get_latest_tag("python", "latest", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "3.14.7")

    def test_tag_listed_below_the_page_cap(self):
        """Test that a floating tag the listing pages past the cap is left as it is, at a bounded cost."""
        listed = [docker_tag(f"1.{index}", DIGEST2) for index in range(_MAX_TAG_LISTING_PAGES * 100 + 1)]
        self.requests.side_effect = mock_docker_registry(*listed)
        latest = get_latest_tag("node", "lts-alpine", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "lts-alpine")
        listings = [call for call in self.requests.call_args_list if "/tags?" in call.args[0]]
        self.assertEqual(len(listings), _MAX_TAG_LISTING_PAGES)

    def test_listing_read_one_page_past_the_aliases(self):
        """Test that the listing is read until a page holds no alias, which is what says the aliases are exhausted."""
        aliases = [docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST)]
        listed_below = [docker_tag(f"1.{index}", DIGEST2) for index in range(198)]
        self.requests.side_effect = mock_docker_registry(*aliases, *listed_below)
        latest = get_latest_tag("python", "latest", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "3.14.7")
        listings = [call for call in self.requests.call_args_list if "/tags?" in call.args[0]]
        self.assertEqual(len(listings), 2)  # The aliases sit on the first page, and the second holds none of them.

    def test_alias_carrying_another_label(self):
        """Test that an alias labelled for a component it bundles, such as `php8.3`, is not read as a version."""
        aliases = ("latest", "php8.3", "php8.3-apache", "7.1.0", "7.1.0-apache", "7.1", "7")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in aliases))
        self.assertEqual(get_latest_tag("wordpress", "latest", NO_BOUND, COOLDOWN.default).version, "7.1.0")

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
        self.requests.side_effect = mock_docker_registry(docker_tag("12.15", DIGEST))
        get_latest_tag("debian", "12.15", NO_BOUND, COOLDOWN.default)
        self.requests.reset_mock()
        get_latest_tag("debian", "12.15", NO_BOUND, COOLDOWN.default)
        self.requests.assert_not_called()

    def test_dated_snapshot_tag_without_a_manifest(self):
        """Test that a snapshot tag the registry serves no manifest for keeps its tag, with no digest to pin it.

        The image dates it all the same, one tag serving no manifest saying nothing about the image behind it.
        """
        pushed = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        self.requests.side_effect = mock_docker_registry(
            docker_tag("12.15", DIGEST, tag_last_pushed=pushed), names=["bookworm-20260803"]
        )
        latest = get_latest_tag("debian", "bookworm-20260803", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "bookworm-20260803")
        self.assertEqual(latest.sha, "")
        self.assertEqual(Release("12.15", datetime.fromisoformat(pushed)), latest.newest)

    def test_newest_release_for_a_floating_tag(self):
        """Test that a reference on a floating tag is measured against the image's newest release.

        The run pins the tag to the version it serves, and that version names the same dependency as any other.
        """
        pushed = (datetime.now(UTC) - timedelta(days=512)).isoformat()
        names = ("latest", "3.14.7")
        self.requests.side_effect = mock_docker_registry(
            *(docker_tag(name, DIGEST, tag_last_pushed=pushed) for name in names)
        )
        latest = get_latest_tag("python", "latest", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "3.14.7")
        self.assertEqual(Release("3.14.7", datetime.fromisoformat(pushed)), latest.newest)

    @kills(
        Mutation(
            oci,
            "        return DependencyVersion(version=current.name, sha=_manifest_digest(image, current.name))",
            "        return DependencyVersion(version=current.name)",
            "a tag naming neither a version nor a channel is left without the digest that would pin it",
        )
    )
    def test_tag_naming_neither_a_version_nor_a_channel(self):
        """Test that a tag such as `dev-2024`, which names neither, keeps its tag and is pinned to its digest."""
        aliases = ("dev-2024", "dev", "12.15", "12")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in aliases))
        latest = get_latest_tag("debian", "dev-2024", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "dev-2024")
        self.assertEqual(latest.sha, DIGEST)
        self.assertIsNone(latest.floating)  # The tag names no channel, so no pin of its floats.

    def test_shortest_alias_wins_when_the_tag_names_no_variant(self):
        """Test that a floating tag naming no variant lands on the alias that adds none, versioned or not."""
        cases = (
            ("unversioned variant", "node", ("latest", "26.7.0", "26.7", "26", "26.7.0-trixie", "26-trixie"), "26.7.0"),
            ("versioned variant", "amazoncorretto", ("latest", "8u504", "8-al2023", "8-al2023-jdk", "8-jdk", "8"), "8"),
        )
        for case, image, aliases, expected in cases:
            with self.subTest(case=case):
                self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in aliases))
                self.assertEqual(get_latest_tag(image, "latest", NO_BOUND, COOLDOWN.default).version, expected)

    def test_listing_read_once_for_two_floating_tags(self):
        """Test that a second floating tag of one repository reads the listing the first one already read."""
        listed = (docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        listed += (docker_tag("slim", DIGEST2), docker_tag("3.14.7-slim", DIGEST2))
        self.requests.side_effect = mock_docker_registry(*listed)
        self.assertEqual(get_latest_tag("python", "latest", NO_BOUND, COOLDOWN.default).version, "3.14.7")
        self.assertEqual(get_latest_tag("python", "slim", NO_BOUND, COOLDOWN.default).version, "3.14.7-slim")
        listings = [call for call in self.requests.call_args_list if "/tags?" in call.args[0]]
        self.assertEqual(len(listings), 1)

    def test_tag_not_listed(self):
        """Test that a floating tag the listing doesn't know is left as it is."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.7", DIGEST))
        latest = get_latest_tag("python", "latest", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "latest")
        self.assertEqual(latest.floating, FloatingPin.NOT_LISTED)

    def test_listing_unavailable(self):
        """Test that a floating tag is left as it is when the tag listing can't be fetched, and the failure logged."""
        self.requests.side_effect = mock_docker_registry(unavailable=Endpoint.TAG_DIGESTS)
        latest = get_latest_tag("python", "latest", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "latest")
        url = "https://registry.hub.docker.com/v2/namespaces/library/repositories/python/tags?page_size=100"
        self.assert_could_not_fetch_logged(url, HTTPStatus.NOT_FOUND)

    @kills(
        Mutation(
            oci,
            """    for candidate in ordered[:_MAX_FLOATING_TAG_PROBES]:
        if _manifest_digest(image, candidate.name) == digest:
            return DependencyVersion(version=candidate.name, sha=digest, floating=FloatingPin.RESOLVED)""",
            """    probes = ordered[:_MAX_FLOATING_TAG_PROBES]
    for candidate in [tag for tag in probes if _manifest_digest(image, tag.name) == digest]:
        return DependencyVersion(version=candidate.name, sha=digest, floating=FloatingPin.RESOLVED)""",
            "the walk asks every tag for its manifest rather than stopping at the tag it pins",
        )
    )
    def test_walk_stops_at_the_first_tag_serving_the_digest(self):
        """Test that the walk asks for one manifest per candidate down to the match, and for none below it."""
        served = ("latest", "3.14.7", "3.14", "3")  # `3.15.0` is newer than the image the floating tag serves.
        tags = [docker_tag(name, DIGEST) for name in served] + [docker_tag("3.15.0", DIGEST2)]
        self.requests.side_effect = mock_docker_registry(*tags)
        latest = get_latest_tag("ghcr.io/owner/python", "latest", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "3.14.7")
        manifests = [
            call.args[0].rsplit("/", maxsplit=1)[-1]
            for call in self.requests.call_args_list
            if "/manifests/" in call.args[0]
        ]
        self.assertEqual(manifests, ["latest", "3.15.0", "3.14.7"])  # `3.14` and `3` sort below the match.

    @kills(
        Mutation(
            oci,
            "    carried = sum(_carries(alias, label) for label in labels)",
            "    carried = 0",
            "the walk ignores the floating tag's words, so it lands on the shorter alias it ties with",
        )
    )
    def test_walk_keeps_the_words_of_the_floating_tag(self):
        """Test that the walk lands on an alias carrying the floating tag's words, not on a shorter one it ties with."""
        names = ("trixie", "26.7.0-trixie", "26.7-trixie", "26.7.0", "26.7", "26")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in names))
        latest = get_latest_tag("ghcr.io/owner/node", "trixie", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "26.7.0-trixie")

    def test_walk_gives_up_before_examining_every_tag(self):
        """Test that a floating tag no version tag near the top of the listing serves is left as it is."""
        listed = [docker_tag(f"3.0.{index}", DIGEST2) for index in range(100)]
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), *listed)
        latest = get_latest_tag("ghcr.io/owner/python", "latest", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "latest")
        self.assertEqual(latest.floating, FloatingPin.NO_VERSION_TAG_EXAMINED)
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
            get_latest_tag("ghcr.io/owner/python", "latest", NO_BOUND, COOLDOWN.default)
        manifests = [call for call in self.requests.call_args_list if "/manifests/" in call.args[0]]
        self.assertEqual(len(manifests), 2)  # `latest` and the candidate serving its digest, once each.

    def test_floating_tag_on_another_registry_the_walk_finds_no_match_for(self):
        """Test that a floating tag no version tag of the registry serves is left as it is."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST2))
        latest = get_latest_tag("ghcr.io/owner/python", "latest", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "latest")
        self.assertEqual(latest.floating, FloatingPin.NO_VERSION_TAG)

    def test_floating_tag_on_another_registry_without_a_manifest(self):
        """Test that a floating tag the registry serves no manifest for is left as it is, its digest unknown."""
        names = ["latest", "3.14.7"]
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.7", DIGEST), names=names)
        latest = get_latest_tag("ghcr.io/owner/python", "latest", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "latest")
        self.assertEqual(latest.floating, FloatingPin.NO_MANIFEST)

    def test_digest_without_concrete_tag(self):
        """Test that a floating tag whose digest no concrete tag serves is left as it is."""
        self.requests.side_effect = mock_docker_registry(docker_tag("dev", DIGEST), docker_tag("prod", DIGEST))
        latest = get_latest_tag("acme/api", "dev", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "dev")
        self.assertEqual(latest.floating, FloatingPin.NO_VERSION_TAG)

    def test_floating_tag_on_another_registry(self):
        """Test that a floating tag on a registry other than Docker Hub resolves to the tag serving its digest."""
        names = ("latest", "3.14.7", "3.14", "3")
        self.requests.side_effect = mock_docker_registry(*(docker_tag(name, DIGEST) for name in names))
        latest = get_latest_tag("ghcr.io/owner/python", "latest", NO_BOUND, COOLDOWN.default)
        self.assertEqual(latest.version, "3.14.7")
        self.assertEqual(latest.sha, DIGEST)
        self.assertEqual(latest.floating, FloatingPin.RESOLVED)


class RegistryTokenTest(LoggingTestCase):
    """Unit tests for discovering and fetching a registry pull token."""

    @patch("requests.get", Mock(side_effect=requests.exceptions.ConnectionError))
    def test_probe_network_error(self):
        """Test that a network error probing the registry yields no token (anonymous) instead of crashing."""
        self.assertIsNone(_registry_token("ghcr.io", "owner/repo"))

    @patch("requests.get")
    def test_token_request_failure(self, mock_get: Mock):
        """Test that a failed token request (after a valid auth challenge) yields no token instead of crashing."""
        challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'
        mock_get.side_effect = [
            mock_response({}, ok=False, status_code=401, headers={"WWW-Authenticate": challenge}),  # auth probe
            mock_response({}, ok=False),  # token endpoint fails
        ]
        self.assertIsNone(_registry_token("ghcr.io", "owner/repo"))
