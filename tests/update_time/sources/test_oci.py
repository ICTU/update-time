"""Unit tests for the OCI registry module."""

import unittest
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from unittest.mock import Mock, patch

import requests

from update_time.domain.bound import NO_BOUND, Verb
from update_time.sources.oci import Tag, _registry_token, get_latest_tag, is_docker_hub_image

from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2, DIGEST3
from tests.update_time.helpers import LoggingTestCase, bound, docker_tag, mock_response, patch_environ
from tests.update_time.registry import RegistryRequestsMixin, mock_docker_registry


class IsDockerHubImageTest(unittest.TestCase):
    """Unit tests for distinguishing Docker Hub images from other-registry references."""

    def test_docker_hub_images(self):
        """Test that images without a registry host, or with an explicit Docker Hub host, are Docker Hub images."""
        images = ("python", "library/ubuntu", "cimg/go", "docker.io/library/redis", "index.docker.io/org/app")
        for image in images:
            self.assertTrue(is_docker_hub_image(image), image)

    def test_other_registry_images(self):
        """Test that images with a registry host as their first path component are not Docker Hub images.

        A host is recognised by a dot, a colon, the name `localhost`, or an uppercase character.
        """
        for image in ("registry.gitlab.com/group/image", "gcr.io/proj/image", "localhost:5000/i", "Host/image"):
            self.assertFalse(is_docker_hub_image(image), image)


class TagTest(unittest.TestCase):
    """Unit tests for tags."""

    def test_sort_tag(self):
        """Test tag sorting."""
        self.assertLess(Tag("1"), Tag("2"))
        self.assertLess(Tag("1.3"), Tag("1.4"))
        self.assertLess(Tag("1.3.1"), Tag("1.3.2"))
        self.assertLess(Tag("1.3"), Tag("1.3.1"))
        self.assertLess(Tag("1.3"), Tag("1.3.0"))
        self.assertLess(Tag("python1.3"), Tag("python1.3.0"))
        self.assertLess(Tag("python1.3-alpine2.3"), Tag("python1.3.0-alpine2.3"))
        self.assertLess(Tag("python1.3.0-alpine2.3"), Tag("python1.3.0-alpine2.3.0"))


@patch_environ()
class GetLatestTagTest(RegistryRequestsMixin, LoggingTestCase):
    """Unit tests for getting the latest tag."""

    def test_invalid_current_tag(self):
        """Test that the current tag is returned without querying a registry if it's not a valid version."""
        self.assertEqual(get_latest_tag("image", "invalid version", NO_BOUND).version, "invalid version")
        self.requests.assert_not_called()

    def test_no_tags(self):
        """Test that the current tag is returned if the image has no tags."""
        self.requests.side_effect = mock_docker_registry()
        self.assertEqual(get_latest_tag("no_tags", "1.0", NO_BOUND).version, "1.0")

    def test_image_not_resolvable(self):
        """Test that a reference that doesn't resolve (e.g. a CircleCI machine image) is left unchanged, not crash."""
        self.requests.side_effect = mock_docker_registry(list_ok=False)
        self.assertEqual(get_latest_tag("ubuntu-2204", "2025.09.1", NO_BOUND).version, "2025.09.1")
        url = "https://registry-1.docker.io/v2/library/ubuntu-2204/tags/list?n=1000"
        self.assert_could_not_fetch_logged(url, HTTPStatus.NOT_FOUND)

    def test_other_registry_image_resolved(self):
        """Test that an image on a registry other than Docker Hub is resolved against that registry's host."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        latest = get_latest_tag("registry.gitlab.com/group/image", "1.0", NO_BOUND)
        self.assertEqual(latest.version, "1.1")
        self.assertEqual(DIGEST, latest.sha)
        self.assertIsNone(latest.published)  # No push date (and so no cooldown) outside Docker Hub.
        self.assertTrue(any("registry.gitlab.com" in call.args[0] for call in self.requests.call_args_list))

    def test_other_registry_repository_path_excludes_host(self):
        """Test that the API repository path drops the registry host (a strict registry 404s on a host-prefixed one)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        get_latest_tag("mcr.microsoft.com/devcontainers/typescript-node", "1.0", NO_BOUND)
        tags_list_urls = [call.args[0] for call in self.requests.call_args_list if "/tags/list" in call.args[0]]
        self.assertTrue(tags_list_urls)
        for url in tags_list_urls:
            self.assertIn("https://mcr.microsoft.com/v2/devcontainers/typescript-node/tags/list", url)
            self.assertNotIn("/v2/mcr.microsoft.com/", url)  # The host must not leak into the repository path.

    def test_explicit_docker_hub_host(self):
        """Test that an image with an explicit docker.io host is updated, querying the host-less Docker Hub URL."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        self.assertEqual(get_latest_tag("docker.io/library/redis", "1.0", NO_BOUND).version, "1.1")
        self.assertIn("/namespaces/library/repositories/redis/", self.requests.call_args.args[0])

    def test_up_to_date(self):
        """Test that the current tag and its digest are returned if it's up to date, so it can be pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.0", DIGEST))
        latest = get_latest_tag("up_to_date", "1.0", NO_BOUND)
        self.assertEqual(latest.version, "1.0")
        self.assertEqual(DIGEST, latest.sha)

    def test_newer(self):
        """Test that the current tag is returned if it's newer than the newest tag available."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.0", DIGEST))
        self.assertEqual(get_latest_tag("newer", "1.1", NO_BOUND).version, "1.1")

    def test_new_version_available(self):
        """Test that the new tag is returned if it's newer, without a publication date when the push date is unknown."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST))
        latest = get_latest_tag("new_version_available", "1.2", NO_BOUND)
        self.assertEqual(latest.version, "2.1")
        self.assertIsNone(latest.published)

    def test_multiple_new_versions_available(self):
        """Test that the newest tag is returned if multiple newer tags are available."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("2.2", DIGEST2), docker_tag("2.1", DIGEST1), docker_tag("2.3", DIGEST3)
        )
        self.assertEqual(get_latest_tag("new_versions_available", "1.2", NO_BOUND).version, "2.3")

    def test_ignore_tags_without_digest(self):
        """Test that tags without digests are ignored, falling back to the next-highest version."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.2", DIGEST), docker_tag("2.3"))
        self.assertEqual(get_latest_tag("ignore_tags_without_digest", "1.2", NO_BOUND).version, "2.2")

    def test_equal_version_alias_tag_keeps_current_spelling(self):
        """Test that an alias tag spelling the current version differently (`22.15` for `22.15.0`) is not adopted."""
        self.requests.side_effect = mock_docker_registry(docker_tag("22.15", DIGEST), docker_tag("22.15.0", DIGEST))
        latest = get_latest_tag("alias", "22.15.0", NO_BOUND)
        self.assertEqual(latest.version, "22.15.0")
        self.assertEqual(latest.sha, DIGEST)

    def test_equal_version_alias_tag_keeps_current_spelling_under_exact_bound(self):
        """Test that a bound pinning the current release exactly (`ignore[patch-update]`) adopts no alias spelling."""
        self.requests.side_effect = mock_docker_registry(docker_tag("22.15", DIGEST), docker_tag("22.15.0", DIGEST))
        version_bound = bound(Verb.ALLOW, "update==22.15.0")
        self.assertEqual(get_latest_tag("alias-bounded", "22.15.0", version_bound).version, "22.15.0")

    def test_equal_version_alias_tag_does_not_lend_its_digest(self):
        """Test that the current spelling keeps its own digest, not a co-listed alias tag's differing digest.

        The alias `22.15` (digest DIGEST2) is listed before the exact `22.15.0` (digest DIGEST1), so were the tie
        broken by listing order the alias would resolve first and lend its digest to the `22.15.0` name; the tag
        ordering prefers the more precise spelling instead, so its own digest is pinned.
        """
        self.requests.side_effect = mock_docker_registry(docker_tag("22.15", DIGEST2), docker_tag("22.15.0", DIGEST1))
        latest = get_latest_tag("alias", "22.15.0", NO_BOUND)
        self.assertEqual(latest.version, "22.15.0")
        self.assertEqual(latest.sha, DIGEST1)

    def test_update_to_a_version_listed_under_two_spellings_keeps_the_precise_one(self):
        """Test that updating to a version the registry lists twice adopts the precise spelling, not the alias.

        The registry lists both `22.16` and `22.16.0` for the new version, with the shorter alias first; the tag
        ordering prefers the more precise spelling, so a `22.15.0` pin advances to `22.16.0` without losing a
        component.
        """
        self.requests.side_effect = mock_docker_registry(docker_tag("22.16", DIGEST1), docker_tag("22.16.0", DIGEST2))
        latest = get_latest_tag("two-spellings", "22.15.0", NO_BOUND)
        self.assertEqual(latest.version, "22.16.0")
        self.assertEqual(latest.sha, DIGEST2)

    def test_level_bound_anchors_to_the_current_tag(self):
        """Test that a level bound is anchored to the current tag, keeping updates within the pinned minor line."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.12.9", DIGEST1), docker_tag("3.13.0", DIGEST2))
        version_bound = bound(Verb.IGNORE, "minor-update")
        self.assertEqual(get_latest_tag("level-bounded", "3.12.1", version_bound).version, "3.12.9")

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
        self.assertEqual(get_latest_tag("pagination", "1.2", NO_BOUND).version, "2.2")

    def test_tag_manifest_not_found(self):
        """Test that a listed tag whose manifest can't be fetched is skipped, leaving the current tag unchanged."""
        self.requests.side_effect = mock_docker_registry(names=["2.2"])
        self.assertEqual(get_latest_tag("manifest_not_found", "1.2", NO_BOUND).version, "1.2")
        url = "https://registry-1.docker.io/v2/library/manifest_not_found/manifests/2.2"
        self.assert_could_not_fetch_logged(url, HTTPStatus.NOT_FOUND)

    def test_invalid_new_tag(self):
        """Test that a tag whose version part can't be parsed (e.g. 1.2.invalid) is ignored."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.2.invalid", DIGEST))
        self.assertEqual(get_latest_tag("invalid_new_tag", "1.3", NO_BOUND).version, "1.3")

    def test_prerelease(self):
        """Test that prerelease tags are ignored."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4a1", DIGEST))
        self.assertEqual(get_latest_tag("prerelease", "1.3", NO_BOUND).version, "1.3")

    def test_different_suffix(self):
        """Test that tags for different suffixes are ignored."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4-windows", DIGEST))
        self.assertEqual(get_latest_tag("different_suffix", "1.3", NO_BOUND).version, "1.3")

    def test_suffix_embedded_version_bumped(self):
        """Test that a version embedded in the suffix is upgraded while its label is kept (alpine3.23 -> alpine3.24)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.6-alpine3.24", DIGEST))
        latest = get_latest_tag("python", "3.14.6-alpine3.23", NO_BOUND)
        self.assertEqual(latest.version, "3.14.6-alpine3.24")
        self.assertEqual(DIGEST, latest.sha)

    def test_suffix_and_main_version_bumped_together(self):
        """Test that both the main version and the embedded suffix version advance together to the newest tag."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("3.14.6-alpine3.23", DIGEST1),
            docker_tag("3.14.6-alpine3.24", DIGEST2),
            docker_tag("3.15.0-alpine3.24", DIGEST3),
        )
        latest = get_latest_tag("python", "3.14.6-alpine3.23", NO_BOUND)
        self.assertEqual(latest.version, "3.15.0-alpine3.24")
        self.assertEqual(DIGEST3, latest.sha)

    def test_suffix_version_not_downgraded(self):
        """Test that a newer main version is not adopted when it would downgrade the embedded suffix version."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15.0-alpine3.22", DIGEST))
        self.assertEqual(get_latest_tag("python", "3.14.6-alpine3.23", NO_BOUND).version, "3.14.6-alpine3.23")

    def test_suffix_label_not_crossed(self):
        """Test that a versioned suffix label is never crossed (alpine is not replaced by a newer debian)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15.0-debian12", DIGEST))
        self.assertEqual(get_latest_tag("python", "3.14.6-alpine3.23", NO_BOUND).version, "3.14.6-alpine3.23")

    def test_invalid_suffix_version(self):
        """Test that a suffix with an unparsable embedded version is treated as an unversioned (whole) label."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15.0-alpine3.2.invalid", DIGEST))
        self.assertEqual(get_latest_tag("python", "3.14.6-alpine3.23", NO_BOUND).version, "3.14.6-alpine3.23")

    def test_label_prefixed_version(self):
        """Test that a label-prefixed tag (e.g. python3.12-...) is bumped with the prefix and suffix kept."""
        self.requests.side_effect = mock_docker_registry(docker_tag("python3.13-bookworm-slim", DIGEST))
        latest = get_latest_tag("ghcr.io/astral-sh/uv", "python3.12-bookworm-slim", NO_BOUND)
        self.assertEqual(latest.version, "python3.13-bookworm-slim")
        self.assertEqual(DIGEST, latest.sha)

    def test_label_prefix_not_crossed(self):
        """Test that a python-prefixed tag is not replaced by a higher pypy-prefixed tag."""
        self.requests.side_effect = mock_docker_registry(docker_tag("pypy3.99-bookworm-slim", DIGEST))
        self.assertEqual(
            get_latest_tag("ghcr.io/astral-sh/uv", "python3.12-bookworm-slim", NO_BOUND).version,
            "python3.12-bookworm-slim",
        )

    def test_version_prefix_preserved(self):
        """Test that a 'v'-prefixed tag keeps its 'v' when bumped (v3.12 -> v3.13, not 3.13)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("v3.13", DIGEST))
        self.assertEqual(get_latest_tag("prefixed", "v3.12", NO_BOUND).version, "v3.13")

    def test_rolling_tag_without_version(self):
        """Test that a rolling tag without a version (e.g. bookworm-slim) is left unchanged, querying nothing."""
        self.assertEqual(get_latest_tag("rolling", "bookworm-slim", NO_BOUND).version, "bookworm-slim")
        self.requests.assert_not_called()

    def test_within_cooldown(self):
        """Test that tags pushed within the cooldown period are ignored."""
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4", DIGEST, tag_last_pushed=recent))
        self.assertEqual(get_latest_tag("within_cooldown", "1.3", NO_BOUND).version, "1.3")

    def test_outside_cooldown(self):
        """Test that tags pushed before the cooldown are considered, with the push date as publication date."""
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4", DIGEST, tag_last_pushed=old))
        latest = get_latest_tag("outside_cooldown", "1.3", NO_BOUND)
        self.assertEqual(latest.version, "1.4")
        self.assertEqual(datetime.fromisoformat(old), latest.published)

    def test_newest_published_ignores_cooldown(self):
        """Test that newest_published is the newest tag's push date even when that tag is held back by the cooldown."""
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4", DIGEST, tag_last_pushed=recent))
        latest = get_latest_tag("newest_published", "1.3", NO_BOUND)
        self.assertEqual(latest.version, "1.3")  # 1.4 is held back by the cooldown...
        self.assertEqual(datetime.fromisoformat(recent), latest.newest_published)  # ...but still defines staleness.

    def test_newest_published_ignores_version_bound(self):
        """Test that newest_published is the newest compatible tag's push date even when a bound excludes that tag.

        A version bound narrows the update only, not the staleness check, so a reference held on an old line by a
        bound is still measured against the image's newest push overall.
        """
        old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
        newest = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        self.requests.side_effect = mock_docker_registry(
            docker_tag("1.4", DIGEST1, tag_last_pushed=old), docker_tag("2.0", DIGEST2, tag_last_pushed=newest)
        )
        latest = get_latest_tag("bounded_staleness", "1.3", bound(Verb.ALLOW, "update<2"))
        self.assertEqual(latest.version, "1.4")  # The bound keeps the update below 2.0...
        self.assertEqual(datetime.fromisoformat(newest), latest.newest_published)  # ...but 2.0 still defines staleness.

    def test_newest_published_none_for_other_registry(self):
        """Test that no newest_published is set for non-Docker-Hub registries, which expose no push date."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST), challenge=False)
        self.assertIsNone(get_latest_tag("mcr.microsoft.com/dotnet/sdk", "1.0", NO_BOUND).newest_published)

    def test_newest_published_none_without_candidates(self):
        """Test that no newest_published is set when the registry lists no compatible tags to date."""
        self.requests.side_effect = mock_docker_registry()
        self.assertIsNone(get_latest_tag("no_tags", "1.0", NO_BOUND).newest_published)

    @patch_environ({"DOCKER_HUB_USERNAME": "joe_doe", "DOCKER_HUB_TOKEN": "pat123"})  # nosec
    @patch("requests.post")
    def test_user_bearer_token(self, mock_post: Mock):
        """Test that the credentials are used for both the per-tag metadata and the OCI registry token requests."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST))
        self.assertEqual(get_latest_tag("new_version_available_with_credentials", "1.2", NO_BOUND).version, "2.1")
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
        get_latest_tag("new_version_available", "1.2", NO_BOUND)
        token_call = next(call for call in self.requests.call_args_list if "auth.docker.io" in call.args[0])
        self.assertIsNone(token_call.kwargs["auth"])

    def test_registry_without_auth_challenge(self):
        """Test that a registry that doesn't challenge for auth (e.g. mcr.microsoft.com) is queried without a token."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST), challenge=False)
        latest = get_latest_tag("mcr.microsoft.com/dotnet/sdk", "1.0", NO_BOUND)
        self.assertEqual(latest.version, "1.1")
        self.assertEqual(DIGEST, latest.sha)
        tags_call = next(call for call in self.requests.call_args_list if "/tags/list" in call.args[0])
        self.assertEqual(tags_call.kwargs["headers"], {})  # No Authorization header for an anonymous registry.

    def test_push_date_unavailable(self):
        """Test that a Docker Hub tag whose push date can't be fetched is still usable, just without a cooldown."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST), push_date_ok=False)
        latest = get_latest_tag("push_date_unavailable", "1.0", NO_BOUND)
        self.assertEqual(latest.version, "1.1")
        self.assertEqual(DIGEST, latest.sha)
        self.assertIsNone(latest.published)
        # The unavailable push date is logged as a could-not-fetch warning for the Docker Hub tags API:
        url = "https://registry.hub.docker.com/v2/namespaces/library/repositories/push_date_unavailable/tags/1.1"
        self.assert_could_not_fetch_logged(url, HTTPStatus.NOT_FOUND)


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
