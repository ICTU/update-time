"""Test helpers that simulate an OCI registry (plus Docker Hub) and the shared image-updater test suite.

This is the machinery only the image tests need: a fake registry that answers the OCI auth/listing/manifest flow
and Docker Hub's per-tag metadata (`mock_docker_registry` and its `RegistryRequestsMixin`), and the parametrized
`ImageUpdaterTestMixin` that every `image:tag[@digest]` updater inherits. It builds on the generic primitives in
`helpers` (`mock_response`, `mock_path`, `docker_tag`, `LoggingTestCase`), which import nothing back from here.
"""

import unittest
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from update_time.domain.drift import ALLOW_HASH_DRIFT, DriftedPin
from update_time.domain.version import Reference
from update_time.primitives.location import Location

from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2, DIGEST3
from tests.update_time.helpers import LoggingTestCase, docker_tag, mock_path, mock_response, patch_environ

if TYPE_CHECKING:
    from collections.abc import Callable


class RegistryRequestsMixin(unittest.TestCase):
    """Mix in to patch requests.get and requests.head with one shared mock, exposed as `self.requests`.

    The OCI client resolves a digest with HEAD and does everything else (auth probe, token, tag listing, push date)
    with GET. Routing both verbs through a single mock lets a test install one `mock_docker_registry` dispatcher via
    `self.requests.side_effect` and assert against one `self.requests.call_args_list`.
    """

    def setUp(self) -> None:
        """Patch requests.get and requests.head with a single shared mock for the duration of the test."""
        super().setUp()
        self.requests = Mock()
        for target in ("requests.get", "requests.head"):
            patcher = patch(target, self.requests)
            self.addCleanup(patcher.stop)
            patcher.start()


def _probe_response(url: str, *, challenge: bool) -> Mock:
    """Answer the OCI `/v2/` auth probe: a `401` challenge pointing at the token endpoint, or `200` when anonymous."""
    if not challenge:  # Anonymous registry: no challenge, so the client proceeds without a token.
        return mock_response({}, ok=True, status_code=200, headers={})
    host = url.removeprefix("https://").split("/", maxsplit=1)[0]
    realm = "https://auth.docker.io/token" if host == "registry-1.docker.io" else f"https://{host}/token"
    bearer = f'Bearer realm="{realm}",service="{host}"'
    return mock_response({}, ok=False, status_code=401, headers={"WWW-Authenticate": bearer})


def _tags_list_response(url: str, tag_names: list[str], *, list_ok: bool, page_size: int | None) -> Mock:
    """Answer `tags/list`: a 404 when `list_ok` is false, else the names — one `page_size` page (with a next link)."""
    if not list_ok:
        return mock_response({"tags": tag_names}, ok=False, status_code=404, url=url, headers={})
    if page_size is None:
        return mock_response({"tags": tag_names}, ok=True, status_code=200, url=url, headers={})
    last = parse_qs(urlparse(url).query).get("last", [None])[0]  # The name the previous page ended on, if any.
    start = tag_names.index(last) + 1 if last and last in tag_names else 0
    page = tag_names[start : start + page_size]
    links = {"next": {"url": f"?last={page[-1]}", "rel": "next"}} if start + page_size < len(tag_names) else {}
    return mock_response({"tags": page}, ok=True, status_code=200, url=url, links=links, headers={})


def _manifest_response(url: str, by_name: dict[str, dict[str, object]]) -> Mock:
    """Answer a manifest `HEAD`: the tag's digest in the `Docker-Content-Digest` header, or a 404 if unknown."""
    tag = by_name.get(url.rsplit("/manifests/", maxsplit=1)[-1])
    if tag is None:
        return mock_response({}, ok=False, status_code=404, url=url)
    digest = cast("str", tag.get("digest", ""))
    return mock_response({}, headers={"Docker-Content-Digest": digest} if digest else {})


def mock_docker_registry(
    *tags: dict[str, object],
    names: list[str] | None = None,
    list_ok: bool = True,
    challenge: bool = True,
    push_date_ok: bool = True,
    page_size: int | None = None,
) -> Callable[..., Mock]:
    """Return a requests.get/.head side effect that mimics an OCI registry plus Docker Hub's per-tag metadata.

    It models the full flow. The `/v2/` probe answers the OCI auth challenge pointing at the registry's token
    endpoint, the token request returns a token, and `tags/list` returns the names of the given tags unless
    overridden. A manifest `HEAD` returns the tag's digest in the `Docker-Content-Digest` header, and Docker Hub's
    proprietary per-tag request returns that tag's push date. Each of the last two answers 404 for a tag it
    doesn't know.
    The same callable is assigned to both `requests.get` and `requests.head`; it routes purely on the URL.

    Knobs for the less common flows:
    - `list_ok=False` makes the tag listing 404 (an unresolvable reference, e.g. a CircleCI machine image).
    - `challenge=False` makes the `/v2/` probe answer `200` without a `WWW-Authenticate` header, modelling an
      anonymous registry that isn't queried with a token (e.g. mcr.microsoft.com).
    - `push_date_ok=False` makes Docker Hub's per-tag push-date request 404, so the tag resolves without a cooldown.
    - `page_size` splits the tag listing into pages of that many names, each linking to the next via the `Link`
      header (as the OCI spec and `next_page_url` expect), to model a paginated listing.
    """
    by_name = {cast("str", tag["name"]): tag for tag in tags}
    tag_names = list(by_name) if names is None else names

    def get(url: str, *_args: object, **_kwargs: object) -> Mock:
        if url.endswith("/v2/"):  # OCI auth challenge probe.
            return _probe_response(url, challenge=challenge)
        if "/token" in url and "/v2/" not in url:  # Token endpoint discovered from the challenge.
            return mock_response({"token": "token"})  # nosec[B105]
        if "/tags/list" in url:
            return _tags_list_response(url, tag_names, list_ok=list_ok, page_size=page_size)
        if "/manifests/" in url:
            return _manifest_response(url, by_name)
        # Docker Hub proprietary per-tag metadata (the push date); only reached for a tag that resolved a digest.
        if not push_date_ok:
            return mock_response({}, ok=False, status_code=404, url=url)
        return mock_response(by_name.get(url.rsplit("/tags/", maxsplit=1)[-1], {}))

    return get


class ImageUpdaterTestMixin(RegistryRequestsMixin, LoggingTestCase):
    """Shared tests for the updaters that rewrite `image:tag[@digest]` references via `update_file`/`get_latest_tag`.

    All these updaters do the same thing to a reference (leave it when already latest, bump the tag and digest, pin a
    tag-only reference); they differ only in how a reference is written in their file format and how the file is
    discovered. A concrete suite supplies those two through `reference` and `run_updater`, inherits the common cases
    below, and adds its own format-specific tests (stage aliases, machine images, features, variable substitution).
    """

    def reference(self, image: str) -> str:
        """Return `image` embedded in the file format the updater rewrites (e.g. `FROM {image}` and a newline)."""
        raise NotImplementedError

    def run_updater(self, mock_file: Mock) -> None:
        """Discover `mock_file` and run the updater."""
        raise NotImplementedError

    @staticmethod
    def drifted(mock_file: Mock) -> DriftedPin:
        """Return the drifted pin the re-pushed `python:3.14` reference these tests use produces."""
        return DriftedPin(Reference("python", "3.14", DIGEST1), DIGEST2, Location(mock_file, 1))

    def test_no_changes(self) -> None:
        """Test that an image already at the latest pinned tag is left unchanged."""
        self.requests.side_effect = mock_docker_registry()
        mock_file = mock_path(self.reference(f"python:3.14@{DIGEST}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_not_called()
        self.assert_path_logged(mock_file)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_stale_image_warned(self) -> None:
        """Test that an image whose newest tag was pushed long ago is warned about as stale, without being rewritten."""
        old = (datetime.now(UTC) - timedelta(days=512)).isoformat()
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST, tag_last_pushed=old))
        mock_file = mock_path(self.reference(f"python:3.14@{DIGEST}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_not_called()
        self.assert_stale_dependency_logged("python", "3.14", Location(mock_file, 1))

    def test_bumped(self) -> None:
        """Test that the image tag and digest are bumped when a newer version is available."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        mock_file = mock_path(self.reference(f"python:3.14@{DIGEST1}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.15@{DIGEST2}"))
        self.assert_new_version_logged("python", "3.15", Location(mock_file, 1))
        self.assert_no_warnings_logged()

    def test_digest_drift_warned_not_repinned(self) -> None:
        """Test that a pinned image whose tag was re-pushed with a different digest is warned about, not rewritten."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST2))
        mock_file = mock_path(self.reference(f"python:3.14@{DIGEST1}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_not_called()
        self.assert_digest_drift_logged(self.drifted(mock_file))
        self.assert_no_new_version_logged()

    def test_digest_drift_adopted_with_flag(self) -> None:
        """Test that --allow-hash-drift re-pins a re-pushed tag's digest instead of only warning about it."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST2))
        mock_file = mock_path(self.reference(f"python:3.14@{DIGEST1}"))
        with patch_environ({ALLOW_HASH_DRIFT.name: "1"}):
            self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.14@{DIGEST2}"))
        self.assert_adopted_digest_drift_logged(self.drifted(mock_file))
        self.assert_no_warnings_logged()

    def test_pin_unpinned_image(self) -> None:
        """Test that an image referenced by tag only is pinned with the latest tag and digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        mock_file = mock_path(self.reference("python:3.14"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.15@{DIGEST2}"))
        self.assert_new_version_logged("python", "3.15", Location(mock_file, 1))
        self.assert_no_warnings_logged()

    def test_pin_unpinned_image_already_at_latest(self) -> None:
        """Test that an unpinned image already at the latest version is still pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST3))
        mock_file = mock_path(self.reference("python:3.14"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.14@{DIGEST3}"))
        self.assert_pinned_logged("python", "3.14", DIGEST3, Location(mock_file, 1))
        self.assert_no_warnings_logged()
