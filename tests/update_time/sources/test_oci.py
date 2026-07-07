"""Unit tests for the OCI registry module."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import requests

from update_time.sources.oci import _registry_token, get_latest_tag, is_docker_hub_image

from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2, DIGEST3
from tests.update_time.helpers import (
    CacheClearingTestCase,
    RegistryRequestsMixin,
    docker_tag,
    mock_docker_registry,
    mock_response,
)


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


@patch.dict("os.environ", {}, clear=True)
class GetLatestTagTest(RegistryRequestsMixin, CacheClearingTestCase):
    """Unit tests for getting the latest tag."""

    def test_invalid_current_tag(self):
        """Test that the current tag is returned without querying a registry if it's not a valid version."""
        self.assertEqual("invalid version", get_latest_tag("image", "invalid version").version)
        self.requests.assert_not_called()

    def test_no_tags(self):
        """Test that the current tag is returned if the image has no tags."""
        self.requests.side_effect = mock_docker_registry()
        self.assertEqual("1.0", get_latest_tag("no_tags", "1.0").version)

    @patch("logging.Logger.warning")
    def test_image_not_resolvable(self, mock_warning: Mock):
        """Test that a reference that doesn't resolve (e.g. a CircleCI machine image) is left unchanged, not crash."""
        self.requests.side_effect = mock_docker_registry(list_ok=False)
        self.assertEqual("2025.09.1", get_latest_tag("ubuntu-2204", "2025.09.1").version)
        mock_warning.assert_called_once()

    def test_other_registry_image_resolved(self):
        """Test that an image on a registry other than Docker Hub is resolved against that registry's host."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        latest = get_latest_tag("registry.gitlab.com/group/image", "1.0")
        self.assertEqual("1.1", latest.version)
        self.assertEqual(DIGEST, latest.sha)
        self.assertIsNone(latest.published)  # No push date (and so no cooldown) outside Docker Hub.
        self.assertTrue(any("registry.gitlab.com" in call.args[0] for call in self.requests.call_args_list))

    def test_other_registry_repository_path_excludes_host(self):
        """Test that the API repository path drops the registry host (a strict registry 404s on a host-prefixed one)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        get_latest_tag("mcr.microsoft.com/devcontainers/typescript-node", "1.0")
        tags_list_urls = [call.args[0] for call in self.requests.call_args_list if "/tags/list" in call.args[0]]
        self.assertTrue(tags_list_urls)
        for url in tags_list_urls:
            self.assertIn("https://mcr.microsoft.com/v2/devcontainers/typescript-node/tags/list", url)
            self.assertNotIn("/v2/mcr.microsoft.com/", url)  # The host must not leak into the repository path.

    def test_explicit_docker_hub_host(self):
        """Test that an image with an explicit docker.io host is updated, querying the host-less Docker Hub URL."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        self.assertEqual("1.1", get_latest_tag("docker.io/library/redis", "1.0").version)
        self.assertIn("/namespaces/library/repositories/redis/", self.requests.call_args.args[0])

    def test_up_to_date(self):
        """Test that the current tag and its digest are returned if it's up to date, so it can be pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.0", DIGEST))
        latest = get_latest_tag("up_to_date", "1.0")
        self.assertEqual("1.0", latest.version)
        self.assertEqual(DIGEST, latest.sha)

    def test_newer(self):
        """Test that the current tag is returned if it's newer than the newest tag available."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.0", DIGEST))
        self.assertEqual("1.1", get_latest_tag("newer", "1.1").version)

    def test_new_version_available(self):
        """Test that the new tag is returned if it's newer, without a publication date when the push date is unknown."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST))
        latest = get_latest_tag("new_version_available", "1.2")
        self.assertEqual("2.1", latest.version)
        self.assertIsNone(latest.published)

    def test_multiple_new_versions_available(self):
        """Test that the newest tag is returned if multiple newer tags are available."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("2.2", DIGEST2), docker_tag("2.1", DIGEST1), docker_tag("2.3", DIGEST3)
        )
        self.assertEqual("2.3", get_latest_tag("new_versions_available", "1.2").version)

    def test_ignore_tags_without_digest(self):
        """Test that tags without digests are ignored, falling back to the next-highest version."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.2", DIGEST), docker_tag("2.3"))
        self.assertEqual("2.2", get_latest_tag("ignore_tags_without_digest", "1.2").version)

    def test_tag_names_paginated(self):
        """Test that the newest tag is returned even if the tag names listing is paginated."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("2.1", DIGEST1), docker_tag("2.2", DIGEST2), page_size=1
        )
        self.assertEqual("2.2", get_latest_tag("pagination", "1.2").version)

    @patch("logging.Logger.warning")
    def test_tag_manifest_not_found(self, mock_warning: Mock):
        """Test that a listed tag whose manifest can't be fetched is skipped, leaving the current tag unchanged."""
        self.requests.side_effect = mock_docker_registry(names=["2.2"])
        self.assertEqual("1.2", get_latest_tag("manifest_not_found", "1.2").version)
        mock_warning.assert_called_once()

    def test_invalid_new_tag(self):
        """Test that a tag whose version part can't be parsed (e.g. 1.2.invalid) is ignored."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.2.invalid", DIGEST))
        self.assertEqual("1.3", get_latest_tag("invalid_new_tag", "1.3").version)

    def test_prerelease(self):
        """Test that prerelease tags are ignored."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4a1", DIGEST))
        self.assertEqual("1.3", get_latest_tag("prerelease", "1.3").version)

    def test_different_suffix(self):
        """Test that tags for different suffixes are ignored."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4-windows", DIGEST))
        self.assertEqual("1.3", get_latest_tag("different_suffix", "1.3").version)

    def test_suffix_embedded_version_bumped(self):
        """Test that a version embedded in the suffix is upgraded while its label is kept (alpine3.23 -> alpine3.24)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.6-alpine3.24", DIGEST))
        latest = get_latest_tag("python", "3.14.6-alpine3.23")
        self.assertEqual("3.14.6-alpine3.24", latest.version)
        self.assertEqual(DIGEST, latest.sha)

    def test_suffix_and_main_version_bumped_together(self):
        """Test that both the main version and the embedded suffix version advance together to the newest tag."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("3.14.6-alpine3.23", DIGEST1),
            docker_tag("3.14.6-alpine3.24", DIGEST2),
            docker_tag("3.15.0-alpine3.24", DIGEST3),
        )
        latest = get_latest_tag("python", "3.14.6-alpine3.23")
        self.assertEqual("3.15.0-alpine3.24", latest.version)
        self.assertEqual(DIGEST3, latest.sha)

    def test_suffix_version_not_downgraded(self):
        """Test that a newer main version is not adopted when it would downgrade the embedded suffix version."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15.0-alpine3.22", DIGEST))
        self.assertEqual("3.14.6-alpine3.23", get_latest_tag("python", "3.14.6-alpine3.23").version)

    def test_suffix_label_not_crossed(self):
        """Test that a versioned suffix label is never crossed (alpine is not replaced by a newer debian)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15.0-debian12", DIGEST))
        self.assertEqual("3.14.6-alpine3.23", get_latest_tag("python", "3.14.6-alpine3.23").version)

    def test_invalid_suffix_version(self):
        """Test that a suffix with an unparsable embedded version is treated as an unversioned (whole) label."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15.0-alpine3.2.invalid", DIGEST))
        self.assertEqual("3.14.6-alpine3.23", get_latest_tag("python", "3.14.6-alpine3.23").version)

    def test_label_prefixed_version(self):
        """Test that a label-prefixed tag (e.g. python3.12-...) is bumped with the prefix and suffix kept."""
        self.requests.side_effect = mock_docker_registry(docker_tag("python3.13-bookworm-slim", DIGEST))
        latest = get_latest_tag("ghcr.io/astral-sh/uv", "python3.12-bookworm-slim")
        self.assertEqual("python3.13-bookworm-slim", latest.version)
        self.assertEqual(DIGEST, latest.sha)

    def test_label_prefix_not_crossed(self):
        """Test that a python-prefixed tag is not replaced by a higher pypy-prefixed tag."""
        self.requests.side_effect = mock_docker_registry(docker_tag("pypy3.99-bookworm-slim", DIGEST))
        self.assertEqual(
            "python3.12-bookworm-slim", get_latest_tag("ghcr.io/astral-sh/uv", "python3.12-bookworm-slim").version
        )

    def test_version_prefix_preserved(self):
        """Test that a 'v'-prefixed tag keeps its 'v' when bumped (v3.12 -> v3.13, not 3.13)."""
        self.requests.side_effect = mock_docker_registry(docker_tag("v3.13", DIGEST))
        self.assertEqual("v3.13", get_latest_tag("prefixed", "v3.12").version)

    def test_rolling_tag_without_version(self):
        """Test that a rolling tag without a version (e.g. bookworm-slim) is left unchanged, querying nothing."""
        self.assertEqual("bookworm-slim", get_latest_tag("rolling", "bookworm-slim").version)
        self.requests.assert_not_called()

    def test_within_cooldown(self):
        """Test that tags pushed within the cooldown period are ignored."""
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4", DIGEST, tag_last_pushed=recent))
        self.assertEqual("1.3", get_latest_tag("within_cooldown", "1.3").version)

    def test_outside_cooldown(self):
        """Test that tags pushed before the cooldown are considered, with the push date as publication date."""
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        self.requests.side_effect = mock_docker_registry(docker_tag("1.4", DIGEST, tag_last_pushed=old))
        latest = get_latest_tag("outside_cooldown", "1.3")
        self.assertEqual("1.4", latest.version)
        self.assertEqual(datetime.fromisoformat(old), latest.published)

    @patch.dict("os.environ", {"DOCKER_HUB_USERNAME": "joe_doe", "DOCKER_HUB_TOKEN": "pat123"})  # nosec
    @patch("requests.post")
    def test_user_bearer_token(self, mock_post: Mock):
        """Test that the credentials are used for both the per-tag metadata and the OCI registry token requests."""
        self.requests.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST))
        self.assertEqual("2.1", get_latest_tag("new_version_available_with_credentials", "1.2").version)
        mock_post.assert_called_once_with(
            "https://hub.docker.com/v2/auth/token",
            timeout=10,
            json={"identifier": "joe_doe", "secret": "pat123"},  # nosec
        )
        token_call = next(call for call in self.requests.call_args_list if "auth.docker.io" in call.args[0])
        self.assertEqual(("joe_doe", "pat123"), token_call.kwargs["auth"])  # nosec

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
        self.assertEqual("1.1", latest.version)
        self.assertEqual(DIGEST, latest.sha)
        tags_call = next(call for call in self.requests.call_args_list if "/tags/list" in call.args[0])
        self.assertEqual({}, tags_call.kwargs["headers"])  # No Authorization header for an anonymous registry.

    @patch("logging.Logger.warning")
    def test_push_date_unavailable(self, mock_warning: Mock):
        """Test that a Docker Hub tag whose push date can't be fetched is still usable, just without a cooldown."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST), push_date_ok=False)
        latest = get_latest_tag("push_date_unavailable", "1.0")
        self.assertEqual("1.1", latest.version)
        self.assertEqual(DIGEST, latest.sha)
        self.assertIsNone(latest.published)
        mock_warning.assert_called_once()  # The unavailable push date is logged.


class RegistryTokenTest(CacheClearingTestCase):
    """Unit tests for discovering and fetching a registry pull token."""

    @patch("logging.Logger.warning", Mock())
    @patch("requests.get", Mock(side_effect=requests.exceptions.ConnectionError))
    def test_probe_network_error(self):
        """Test that a network error probing the registry yields no token (anonymous) instead of crashing."""
        self.assertIsNone(_registry_token("ghcr.io", "owner/repo"))

    @patch("logging.Logger.warning", Mock())
    @patch("requests.get")
    def test_token_request_failure(self, mock_get: Mock):
        """Test that a failed token request (after a valid auth challenge) yields no token instead of crashing."""
        challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'
        mock_get.side_effect = [
            mock_response({}, ok=False, status_code=401, headers={"WWW-Authenticate": challenge}),  # auth probe
            mock_response({}, ok=False),  # token endpoint fails
        ]
        self.assertIsNone(_registry_token("ghcr.io", "owner/repo"))
