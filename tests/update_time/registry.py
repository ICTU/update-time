"""Test helpers that simulate an OCI registry (plus Docker Hub) and the shared image-updater test suite.

This is the machinery only the image tests need: a fake registry that answers the OCI auth/listing/manifest flow
and Docker Hub's per-tag metadata (`mock_docker_registry` and its `RegistryRequestsMixin`), and the parametrized
`ImageUpdaterTestMixin` that every `image:tag[@digest]` updater inherits. It builds on the generic primitives in
`helpers` (`mock_response`, `mock_path`, `docker_tag`, `LoggingTestCase`), which import nothing back from here.
"""

import unittest
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from update_time.domain.dependency import DependencyVersion, FloatingPin
from update_time.domain.reference import DriftedPin
from update_time.markers.directive import Reason
from update_time.markers.drift import ALLOW_HASH_DRIFT
from update_time.markers.floating import ALLOW_FLOATING_PIN
from update_time.primitives.location import Location

from tests.helpers import mock_path, mock_response, patch_environ
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2
from tests.update_time.helpers import LoggingTestCase, docker_tag

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


class Endpoint(Enum):
    """An endpoint of the registry a test can make unavailable, so the code under test meets that failure."""

    TAG_NAMES = auto()  # The OCI listing of tag names.
    TAG_DIGESTS = auto()  # Docker Hub's listing of every tag with its digest.
    PUSH_DATE = auto()  # Docker Hub's per-tag metadata, which carries the push date.


def _not_found(url: str) -> Mock:
    """Answer a request with a 404, as the registry does for an endpoint or a tag it does not serve."""
    return mock_response({}, ok=False, status_code=404, url=url)


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
        return _not_found(url)
    digest = cast("str", tag.get("digest", ""))
    return mock_response({}, headers={"Docker-Content-Digest": digest} if digest else {})


def _tag_digests_response(url: str, tags: tuple[dict[str, object], ...], *, ok: bool) -> Mock:
    """Answer Docker Hub's tag listing: the requested page of tags with their digests, or a 404 when unavailable.

    The page and its size are read from the URL, as Docker Hub reads them, and a page that leaves tags unlisted
    carries the `next` URL of the page after it.
    """
    if not ok:
        return _not_found(url)
    query = parse_qs(urlparse(url).query)
    page, page_size = int(query.get("page", ["1"])[0]), int(query["page_size"][0])
    start = (page - 1) * page_size
    listed = list(tags)[start : start + page_size]
    more = (
        f"{url.split('?', maxsplit=1)[0]}?page={page + 1}&page_size={page_size}"
        if start + page_size < len(tags)
        else None
    )
    return mock_response({"count": len(tags), "next": more, "results": listed})


def _push_date_response(url: str, by_name: dict[str, dict[str, object]], *, ok: bool) -> Mock:
    """Answer Docker Hub's per-tag request with that tag's push date, or a 404 when it is unavailable."""
    return mock_response(by_name.get(url.rsplit("/tags/", maxsplit=1)[-1], {})) if ok else _not_found(url)


def mock_docker_registry(
    *tags: dict[str, object],
    names: list[str] | None = None,
    unavailable: Endpoint | None = None,
    challenge: bool = True,
    page_size: int | None = None,
) -> Callable[..., Mock]:
    """Return a requests.get/.head side effect that mimics an OCI registry plus Docker Hub's per-tag metadata.

    It models the full flow. The `/v2/` probe answers the OCI auth challenge pointing at the registry's token
    endpoint, the token request returns a token, and `tags/list` returns the names of the given tags unless
    overridden. A manifest `HEAD` returns the tag's digest in the `Docker-Content-Digest` header, and Docker Hub's
    proprietary per-tag request returns that tag's push date. Each of the last two answers 404 for a tag it
    doesn't know. Docker Hub's proprietary tag listing returns the given tags with their digests, a page at a
    time, reading the page and its size off the URL as Docker Hub does.
    The same callable is assigned to both `requests.get` and `requests.head`; it routes purely on the URL.

    Knobs for the less common flows:
    - `unavailable` makes the named endpoint answer 404: the tag names for a reference that doesn't resolve (e.g. a
      CircleCI machine image), the tag digests for a floating tag whose digest stays unknown, the push date for a
      tag that resolves without a cooldown.
    - `challenge=False` makes the `/v2/` probe answer `200` without a `WWW-Authenticate` header, modelling an
      anonymous registry that isn't queried with a token (e.g. mcr.microsoft.com).
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
            names_ok = unavailable is not Endpoint.TAG_NAMES
            return _tags_list_response(url, tag_names, list_ok=names_ok, page_size=page_size)
        if "/manifests/" in url:
            return _manifest_response(url, by_name)
        if "/tags?" in url:  # Docker Hub's proprietary tag listing: every tag with its digest.
            return _tag_digests_response(url, tags, ok=unavailable is not Endpoint.TAG_DIGESTS)
        # Docker Hub proprietary per-tag metadata (the push date); only reached for a tag that resolved a digest.
        return _push_date_response(url, by_name, ok=unavailable is not Endpoint.PUSH_DATE)

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

    def marker_line(self, directive: str) -> str:
        """Return the directive as a marker on a line of its own, in the comment lead the file format takes.

        The line-above placement is the one every format accepts, so a shared test can mark a reference whatever
        format it is written in. A suite whose format is JSONC overrides this.
        """
        return f"# update-time: {directive}\n"

    @staticmethod
    def drifted(mock_file: Mock) -> DriftedPin:
        """Return the drifted pin the re-pushed `python:3.14` reference these tests use produces."""
        return DriftedPin("python", "3.14", Location(mock_file, 1), DIGEST1, new_sha=DIGEST2)

    def test_no_changes(self) -> None:
        """Test that an image already at the latest pinned tag is left unchanged."""
        self.requests.side_effect = mock_docker_registry()
        mock_file = mock_path(self.reference(f"python:3.14@{DIGEST}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_not_called()
        self.assert_path_logged(mock_file)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_cooldown_marker_is_not_reported_as_redundant(self) -> None:
        """Test that a `cooldown` marker on a Docker Hub image holds a freshly pushed tag back, unreported."""
        pushed_today = datetime.now(UTC).isoformat()
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2, tag_last_pushed=pushed_today))
        marked = self.marker_line("ignore[cooldown<30]") + self.reference(f"python:3.14@{DIGEST1}")
        mock_file = mock_path(marked)
        self.run_updater(mock_file)
        mock_file.write_text.assert_not_called()  # 3.15 was pushed inside the marker's 30-day window
        self.assert_no_warnings_logged()

    def test_cooldown_marker_outside_docker_hub_is_reported_as_redundant(self) -> None:
        """Test that a `cooldown` marker on an image off Docker Hub is reported, since no tag there carries a date."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        marker = self.marker_line("ignore[cooldown<30]")
        mock_file = mock_path(marker + self.reference(f"ghcr.io/owner/python:3.14@{DIGEST1}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(marker + self.reference(f"ghcr.io/owner/python:3.15@{DIGEST2}"))
        self.assert_redundant_directive_logged(
            Reason.NO_COOLDOWN_DATES, "ghcr.io/owner/python", Location(mock_file, 2), "ignore[cooldown<30]"
        )

    def test_stale_marker_outside_docker_hub_is_reported_as_redundant(self) -> None:
        """Test that a `stale` marker on an image off Docker Hub is reported, since no tag there carries a date."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        marker = self.marker_line("ignore[stale<90]")
        mock_file = mock_path(marker + self.reference(f"ghcr.io/owner/python:3.14@{DIGEST1}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(marker + self.reference(f"ghcr.io/owner/python:3.15@{DIGEST2}"))
        self.assert_redundant_directive_logged(
            Reason.NO_STALENESS_DATES, "ghcr.io/owner/python", Location(mock_file, 2), "ignore[stale<90]"
        )

    def test_floating_pin_marker_on_a_version_tag_is_reported_as_redundant(self) -> None:
        """Test that `allow[floating-pin]` on a tag naming a version is reported, since that tag does not float."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        marker = self.marker_line("allow[floating-pin]")
        mock_file = mock_path(marker + self.reference(f"python:3.14@{DIGEST1}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(marker + self.reference(f"python:3.15@{DIGEST2}"))
        self.assert_redundant_directive_logged(
            Reason.NOTHING_FLOATING, "python", Location(mock_file, 2), "allow[floating-pin]"
        )

    def test_floating_pin_marker_on_a_frozen_reference_is_reported_as_redundant(self) -> None:
        """Test that `allow[floating-pin]` on a frozen reference is reported, since the freeze keeps its tag too."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        marker = self.marker_line("ignore[update] allow[floating-pin]")
        mock_file = mock_path(marker + self.reference(f"python:latest@{DIGEST1}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_not_called()
        self.assert_redundant_directive_logged(
            Reason.UPDATE_HELD_BACK, "python", Location(mock_file, 2), "allow[floating-pin]"
        )

    def test_stale_image_names_the_newest_tag(self) -> None:
        """Test that a stale image is warned about by its newest tag."""
        old = (datetime.now(UTC) - timedelta(days=512)).isoformat()
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2, tag_last_pushed=old))
        marker = self.marker_line("ignore[update]")
        mock_file = mock_path(marker + self.reference(f"python:3.14@{DIGEST1}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_not_called()
        self.assert_stale_dependency_logged("python", "3.15", Location(mock_file, 2))

    def test_snapshot_on_a_living_image_is_not_stale(self) -> None:
        """Test that a snapshot pinned long ago is not reported as stale while its image keeps publishing.

        The snapshot is 512 days old and debian released 10 days ago, so the reference is dated by the release.
        """
        old = (datetime.now(UTC) - timedelta(days=512)).isoformat()
        recent = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        snapshot = "bookworm-20240110"
        self.requests.side_effect = mock_docker_registry(
            docker_tag("13.2", DIGEST2, tag_last_pushed=recent),
            docker_tag(snapshot, DIGEST1, tag_last_pushed=old),
        )
        mock_file = mock_path(self.reference(f"debian:{snapshot}@{DIGEST1}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_not_called()
        self.assert_no_warnings_logged()

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

    def test_pinned_floating_tag_on_another_registry_drift_warned(self) -> None:
        """Test that a floating tag off Docker Hub serving another digest than it records is warned about."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST2), docker_tag("3.14.7", DIGEST2))
        image = "ghcr.io/owner/python"
        mock_file = mock_path(self.reference(f"{image}:latest@{DIGEST1}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_not_called()
        drifted = DriftedPin(image, "latest", Location(mock_file, 1), DIGEST1, new_sha=DIGEST2)
        self.assert_digest_drift_logged(drifted)

    def test_pin_reference_naming_a_digest_but_no_tag(self) -> None:
        """Test that a reference naming a digest but no tag gains the version tag that digest serves."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_file = mock_path(self.reference(f"python@{DIGEST}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.14.7@{DIGEST}"))
        self.assert_pinned_logged("python", "3.14.7", DIGEST, Location(mock_file, 1))
        self.assert_no_warnings_logged()

    def test_pin_unpinned_image(self) -> None:
        """Test that an image referenced by tag only is pinned with the latest tag and digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        mock_file = mock_path(self.reference("python:3.14"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.15@{DIGEST}"))
        self.assert_new_version_logged("python", "3.15", Location(mock_file, 1))
        self.assert_no_warnings_logged()

    def test_pin_floating_tag(self) -> None:
        """Test that a floating tag is pinned to the concrete version and digest it currently serves."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_file = mock_path(self.reference("python:latest"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.14.7@{DIGEST}"))
        self.assert_pinned_logged("python", "3.14.7", DIGEST, Location(mock_file, 1))
        self.assert_no_warnings_logged()

    def test_pin_a_snapshot_with_no_newer_sibling(self) -> None:
        """Test that a dated snapshot the registry lists no newer one of keeps its tag and is pinned to its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("bookworm-20260803", DIGEST))
        mock_file = mock_path(self.reference("debian:bookworm-20260803"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(self.reference(f"debian:bookworm-20260803@{DIGEST}"))
        self.assert_pinned_logged("debian", "bookworm-20260803", DIGEST, Location(mock_file, 1))
        self.assert_no_warnings_logged()

    def test_floating_tag_serving_no_concrete_version(self) -> None:
        """Test that a floating tag serving no concrete version is left unchanged and reported."""
        self.requests.side_effect = mock_docker_registry(docker_tag("dev", DIGEST), docker_tag("prod", DIGEST))
        mock_file = mock_path(self.reference("acme/api:dev"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_not_called()
        reason = FloatingPin.NO_VERSION_TAG
        self.assert_unpinned_floating_tag_logged("acme/api", "dev", Location(mock_file, 1), reason)
        self.assert_no_warnings_logged()

    def test_pinned_floating_tag(self) -> None:
        """Test that a floating tag pinned to the digest it still serves keeps it and gains the version."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_file = mock_path(self.reference(f"python:latest@{DIGEST}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.14.7@{DIGEST}"))
        self.assert_pinned_logged("python", "3.14.7", DIGEST, Location(mock_file, 1))
        self.assert_no_warnings_logged()

    def test_pinned_floating_tag_that_drifted(self) -> None:
        """Test that a floating tag serving another digest than it is pinned to is warned about, not re-pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST2), docker_tag("3.14.7", DIGEST2))
        mock_file = mock_path(self.reference(f"python:latest@{DIGEST1}"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_not_called()
        drifted = DriftedPin("python", "latest", Location(mock_file, 1), DIGEST1, new_sha=DIGEST2)
        self.assert_digest_drift_logged(drifted)
        self.assert_no_new_version_logged()

    def test_pinned_floating_tag_drift_adopted_with_flag(self) -> None:
        """Test that --allow-hash-drift re-pins a re-pointed floating tag to the version and digest it now serves."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST2), docker_tag("3.14.7", DIGEST2))
        mock_file = mock_path(self.reference(f"python:latest@{DIGEST1}"))
        with patch_environ({ALLOW_HASH_DRIFT.name: "1"}):
            self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.14.7@{DIGEST2}"))
        drifted = DriftedPin("python", "latest", Location(mock_file, 1), DIGEST1, new_sha=DIGEST2)
        self.assert_adopted_digest_drift_logged_among_others(drifted)
        self.assert_pinned_logged("python", "3.14.7", DIGEST2, Location(mock_file, 1))
        self.assert_no_warnings_logged()

    def test_marker_keeps_the_tag_floating(self) -> None:
        """Test that a marker allowing the floating pin leaves the tag as it is, naming what it resolves to."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        marker = self.marker_line("allow[floating-pin]")
        mock_file = mock_path(marker + self.reference("python:latest"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_not_called()
        resolved = DependencyVersion(version="3.14.7", sha=DIGEST)
        self.assert_kept_floating_logged("python", "latest", resolved, Location(mock_file, 2))
        self.assert_no_warnings_logged()

    def test_flag_keeps_every_tag_floating(self) -> None:
        """Test that --allow-floating-pin leaves a floating tag as it is, naming the flag as what kept it."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_file = mock_path(self.reference("python:latest"))
        with patch_environ({ALLOW_FLOATING_PIN.name: "1"}):
            self.run_updater(mock_file)
        mock_file.write_text.assert_not_called()
        resolved = DependencyVersion(version="3.14.7", sha=DIGEST)
        self.assert_kept_floating_logged("python", "latest", resolved, Location(mock_file, 1), "--allow-floating-pin")
        self.assert_no_warnings_logged()

    def test_pin_unpinned_image_already_at_latest(self) -> None:
        """Test that an unpinned image already at the latest version is still pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST))
        mock_file = mock_path(self.reference("python:3.14"))
        self.run_updater(mock_file)
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.14@{DIGEST}"))
        self.assert_pinned_logged("python", "3.14", DIGEST, Location(mock_file, 1))
        self.assert_no_warnings_logged()
