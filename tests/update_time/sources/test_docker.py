"""Unit tests for the Docker module."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from update_time.sources.docker import get_latest_tag, is_docker_hub_image

from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2, DIGEST3
from tests.update_time.helpers import CacheClearingTestCase, docker_tag, mock_docker_registry, mock_response


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
@patch("requests.get")
class GetLatestTagTest(CacheClearingTestCase):
    """Unit tests for getting the latest tag."""

    def test_invalid_current_tag(self, mock_get: Mock):
        """Test that the current tag is returned without querying Docker Hub if it's not a valid version."""
        self.assertEqual("invalid version", get_latest_tag("image", "invalid version").version)
        mock_get.assert_not_called()

    def test_no_tags(self, mock_get: Mock):
        """Test that the current tag is returned if the image has no tags."""
        mock_get.side_effect = mock_docker_registry()
        self.assertEqual("1.0", get_latest_tag("no_tags", "1.0").version)

    @patch("logging.Logger.warning")
    def test_image_not_on_docker_hub(self, mock_warning: Mock, mock_get: Mock):
        """Test that an image not on Docker Hub (e.g. a CircleCI machine image) is left unchanged, not crashing."""
        mock_get.side_effect = mock_docker_registry(list_ok=False)
        self.assertEqual("2025.09.1", get_latest_tag("ubuntu-2204", "2025.09.1").version)
        mock_warning.assert_called_once()

    def test_other_registry_image_skipped(self, mock_get: Mock):
        """Test that an image on another registry is left unchanged without querying Docker Hub."""
        self.assertEqual("1.0", get_latest_tag("registry.gitlab.com/group/image", "1.0").version)
        mock_get.assert_not_called()

    def test_explicit_docker_hub_host(self, mock_get: Mock):
        """Test that an image with an explicit docker.io host is updated, querying the host-less Docker Hub URL."""
        mock_get.side_effect = mock_docker_registry(docker_tag("1.1", DIGEST))
        self.assertEqual("1.1", get_latest_tag("docker.io/library/redis", "1.0").version)
        self.assertIn("/namespaces/library/repositories/redis/", mock_get.call_args.args[0])

    def test_up_to_date(self, mock_get: Mock):
        """Test that the current tag and its digest are returned if it's up to date, so it can be pinned."""
        mock_get.side_effect = mock_docker_registry(docker_tag("1.0", DIGEST))
        latest = get_latest_tag("up_to_date", "1.0")
        self.assertEqual("1.0", latest.version)
        self.assertEqual(DIGEST, latest.sha)

    def test_newer(self, mock_get: Mock):
        """Test that the current tag is returned if it's newer than the newest tag available."""
        mock_get.side_effect = mock_docker_registry(docker_tag("1.0", DIGEST))
        self.assertEqual("1.1", get_latest_tag("newer", "1.1").version)

    def test_new_version_available(self, mock_get: Mock):
        """Test that the new tag is returned if it's newer, without a publication date when the push date is unknown."""
        mock_get.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST))
        latest = get_latest_tag("new_version_available", "1.2")
        self.assertEqual("2.1", latest.version)
        self.assertIsNone(latest.published)

    def test_multiple_new_versions_available(self, mock_get: Mock):
        """Test that the newest tag is returned if multiple newer tags are available."""
        mock_get.side_effect = mock_docker_registry(
            docker_tag("2.2", DIGEST2), docker_tag("2.1", DIGEST1), docker_tag("2.3", DIGEST3)
        )
        self.assertEqual("2.3", get_latest_tag("new_versions_available", "1.2").version)

    def test_ignore_tags_without_digest(self, mock_get: Mock):
        """Test that tags without digests are ignored, falling back to the next-highest version."""
        mock_get.side_effect = mock_docker_registry(docker_tag("2.2", DIGEST), docker_tag("2.3"))
        self.assertEqual("2.2", get_latest_tag("ignore_tags_without_digest", "1.2").version)

    def test_tag_names_paginated(self, mock_get: Mock):
        """Test that the newest tag is returned even if the tag names listing is paginated."""
        token = mock_response({"token": "token"})  # nosec[B105]
        next_link = '</v2/library/pagination/tags/list?last=2.1>; rel="next"'
        page1 = mock_response({"tags": ["2.1"]}, headers={"Link": next_link})
        page2 = mock_response({"tags": ["2.2"]}, headers={})
        mock_get.side_effect = [token, page1, page2, mock_response(docker_tag("2.2", DIGEST2))]
        self.assertEqual("2.2", get_latest_tag("pagination", "1.2").version)

    @patch("logging.Logger.warning")
    def test_tag_metadata_not_found(self, mock_warning: Mock, mock_get: Mock):
        """Test that a listed tag whose metadata can't be fetched is skipped, leaving the current tag unchanged."""
        mock_get.side_effect = mock_docker_registry(names=["2.2"])
        self.assertEqual("1.2", get_latest_tag("metadata_not_found", "1.2").version)
        mock_warning.assert_called_once()

    def test_invalid_new_tag(self, mock_get: Mock):
        """Test that invalid new tags are ignored."""
        mock_get.side_effect = mock_docker_registry(docker_tag("invalid", DIGEST))
        self.assertEqual("1.3", get_latest_tag("invalid_new_tag", "1.3").version)

    def test_prerelease(self, mock_get: Mock):
        """Test that prerelease tags are ignored."""
        mock_get.side_effect = mock_docker_registry(docker_tag("1.4a1", DIGEST))
        self.assertEqual("1.3", get_latest_tag("prerelease", "1.3").version)

    def test_different_suffix(self, mock_get: Mock):
        """Test that tags for different suffixes are ignored."""
        mock_get.side_effect = mock_docker_registry(docker_tag("1.4-windows", DIGEST))
        self.assertEqual("1.3", get_latest_tag("different_suffix", "1.3").version)

    def test_within_cooldown(self, mock_get: Mock):
        """Test that tags pushed within the cooldown period are ignored."""
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        mock_get.side_effect = mock_docker_registry(docker_tag("1.4", DIGEST, tag_last_pushed=recent))
        self.assertEqual("1.3", get_latest_tag("within_cooldown", "1.3").version)

    def test_outside_cooldown(self, mock_get: Mock):
        """Test that tags pushed before the cooldown are considered, with the push date as publication date."""
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        mock_get.side_effect = mock_docker_registry(docker_tag("1.4", DIGEST, tag_last_pushed=old))
        latest = get_latest_tag("outside_cooldown", "1.3")
        self.assertEqual("1.4", latest.version)
        self.assertEqual(datetime.fromisoformat(old), latest.published)

    @patch.dict("os.environ", {"DOCKER_HUB_USERNAME": "joe_doe", "DOCKER_HUB_TOKEN": "pat123"})  # nosec
    @patch("requests.post")
    def test_user_bearer_token(self, mock_post: Mock, mock_get: Mock):
        """Test that the credentials are used for both the per-tag metadata and the OCI tag-listing requests."""
        mock_get.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST))
        self.assertEqual("2.1", get_latest_tag("new_version_available_with_credentials", "1.2").version)
        mock_post.assert_called_once_with(
            "https://hub.docker.com/v2/auth/token",
            timeout=10,
            json={"identifier": "joe_doe", "secret": "pat123"},  # nosec
        )
        oci_token_call = next(call for call in mock_get.call_args_list if "auth.docker.io" in call.args[0])
        self.assertEqual(("joe_doe", "pat123"), oci_token_call.kwargs["auth"])  # nosec

    def test_anonymous_oci_token_without_credentials(self, mock_get: Mock):
        """Test that an anonymous OCI token is requested (no basic auth) when no credentials are set."""
        mock_get.side_effect = mock_docker_registry(docker_tag("2.1", DIGEST))
        get_latest_tag("new_version_available", "1.2")
        oci_token_call = next(call for call in mock_get.call_args_list if "auth.docker.io" in call.args[0])
        self.assertIsNone(oci_token_call.kwargs["auth"])
