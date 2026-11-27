"""Test helpers that simulate an OCI registry (plus Docker Hub) and the shared image-updater test suite."""

import unittest
from enum import Enum, auto
from http import HTTPStatus
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from mutation_time.mutation import kills
from update_time.domain import directive as directive_module
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion, FloatingPin, ImageNotLookedUp, Unserved
from update_time.domain.directive import Consequence, ImageNotLookedUpReason, Reason
from update_time.domain.reference import DriftedPin, DriftKind
from update_time.domain.source import Source
from update_time.primitives.location import Location
from update_time.references.resolve import _project_is_checked, _warn_about_redundant_update_directives, latest_version
from update_time.references.rewrite import _Rewriter
from update_time.sources import oci
from update_time.sources.docker_hub import _listing_page

from tests.file_mocks import mock_path
from tests.http_mocks import mock_response

from .base_test_cases import LoggingTestCase
from .domain_objects import days_ago
from .fixtures import DIGEST, DIGEST1, DIGEST2, FRESH_DATE, STALE_DATE
from .run_settings import staleness_disabled
from .sources.docker_hub_answers import docker_hub_version, docker_tag

if TYPE_CHECKING:
    from update_time.formats.json import JSON

    from tests.http_mocks import RequestStub


class RegistryRequestsMixin(unittest.TestCase):
    """Mix in to patch Session.get and Session.head with one shared mock, exposed as `self.requests`."""

    def setUp(self) -> None:
        """Patch Session.get and Session.head with a single shared mock for the duration of the test."""
        super().setUp()
        self.requests = Mock()
        for target in ("requests.Session.get", "requests.Session.head"):
            patcher = patch(target, self.requests)
            self.addCleanup(patcher.stop)
            patcher.start()


# An image on each kind of registry, named by a repository its registry does not serve, and whether the registry
# challenges for a token.
_UNSERVED_REPOSITORIES: Final[MappingProxyType[str, tuple[str, bool]]] = MappingProxyType(
    {
        "Docker Hub": ("spark-rm-base", True),
        "another registry": ("ghcr.io/acme/spark-rm-base", True),
        "an anonymous registry": ("mcr.microsoft.com/acme/spark-rm-base", False),
    }
)


class ImageUpdaterTestMixin(RegistryRequestsMixin, LoggingTestCase):
    """Shared tests for the updaters that rewrite `image:tag[@digest]` references via `update_file`/`image_source`.

    A concrete suite supplies how its format writes a reference and how its files are found, through `reference` and
    `run_updater`.
    """

    def test_no_changes(self) -> None:
        """Test that an image already at the latest pinned tag is left unchanged."""
        self.requests.side_effect = mock_docker_registry()
        mock_file = self.updated_file(self.reference(f"python:3.14@{DIGEST}"))
        mock_file.write_text.assert_not_called()
        self.assert_path_logged(mock_file)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def updated_file(self, contents: str) -> Mock:
        """Run the updater on a file holding the contents, and return that file."""
        mock_file = mock_path(contents)
        self.run_updater(mock_file)
        return mock_file

    def run_updater(self, mock_file: Mock) -> None:
        """Discover `mock_file` and run the updater."""
        raise NotImplementedError

    def reference(self, image: str) -> str:
        """Return `image` embedded in the file format the updater rewrites (e.g. `FROM {image}` and a newline)."""
        raise NotImplementedError

    def test_tag_inside_the_cooldown_is_logged_as_held_back(self) -> None:
        """Test that a tag pushed inside the cooldown is logged as held back, naming the marker that sets a cooldown."""
        cases = {"the run's cooldown": ("", COOLDOWN.default, 1), "a marker's cooldown": ("set[cooldown=30]", 30, 2)}
        for case, (directive, days, line) in cases.items():
            with self.subTest(case):
                self.requests.side_effect = mock_docker_registry(
                    docker_tag("3.15", DIGEST2, tag_last_pushed=FRESH_DATE)
                )
                marker = self.marker_line(directive) if directive else ""
                mock_file = self.updated_file(marker + self.reference(f"python:3.14@{DIGEST1}"))
                mock_file.write_text.assert_not_called()
                location = Location(mock_file, line)
                self.assert_held_back_by_cooldown_logged("python", "3.15", location, days=days, directive=directive)
                self.assert_no_warnings_logged()

    def marker_line(self, directive: str) -> str:
        """Return the directive as a marker on a line of its own, the placement every format accepts."""
        return f"# update-time: {directive}\n"

    @kills.replace(
        oci,
        "declares_archival=_is_docker_hub_image",
        "declares_archival=bool",
        "an `ignore[archived]` on an image off Docker Hub decides nothing without being reported",
        case="ignore[archived]",
    )
    @kills.replace(
        directive_module,
        "Reason.NO_STALENESS_DATES,",
        "Reason.NO_COOLDOWN_DATES,",
        "a staleness threshold on an image off Docker Hub is reported as a cooldown that has no dates",
        case="set[stale-after=90]",
    )
    def test_marker_outside_docker_hub_is_reported_as_redundant(self) -> None:
        """Test that a staleness threshold or `ignore[archived]` on an image off Docker Hub is redundant."""
        cases = {"set[stale-after=90]": Reason.NO_STALENESS_DATES, "ignore[archived]": Reason.NO_ARCHIVAL_SIGNAL}
        for directive, reason in cases.items():
            with self.subTest(directive=directive):
                self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
                marker = self.marker_line(directive)
                mock_file = self.updated_file(marker + self.reference(f"ghcr.io/owner/python:3.14@{DIGEST1}"))
                updated = marker + self.reference(f"ghcr.io/owner/python:3.15@{DIGEST2}")
                mock_file.write_text.assert_called_once_with(updated)
                self.assert_redundant_directive_logged(
                    reason, "ghcr.io/owner/python", Location(mock_file, 2), directive
                )

    def test_floating_pin_marker_on_a_version_tag_is_reported_as_redundant(self) -> None:
        """Test that `allow[floating-pin]` on a tag naming a version is reported, since that tag does not float."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        marker = self.marker_line("allow[floating-pin]")
        mock_file = self.updated_file(marker + self.reference(f"python:3.14@{DIGEST1}"))
        mock_file.write_text.assert_called_once_with(marker + self.reference(f"python:3.15@{DIGEST2}"))
        self.assert_redundant_directive_logged(
            Reason.PIN_NOT_FLOATING, "python", Location(mock_file, 2), "allow[floating-pin]"
        )

    def test_stale_image_names_the_newest_tag(self) -> None:
        """Test that a stale image is warned about by its newest tag."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2, tag_last_pushed=STALE_DATE))
        mock_file = self.updated_file(self.marker_line("ignore[update]") + self.reference(f"python:3.14@{DIGEST1}"))
        mock_file.write_text.assert_not_called()
        self.assert_stale_dependency_logged("python", "3.15", Location(mock_file, 2))

    @kills.replace(
        _project_is_checked,
        "declares_archival(reference.dependency)",
        'declares_archival("ghcr.io/")',
        "with the staleness check switched off, the archival of an image is never checked",
    )
    @staleness_disabled()
    def test_archived_image_warned_with_the_staleness_check_switched_off(self) -> None:
        """Test that an image whose Docker Hub repository is archived is warned about and updated, staleness off."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("1.31", DIGEST2), repository=RepositoryState.ARCHIVED
        )
        image = "nginxinc/nginx-unprivileged"
        mock_file = self.updated_file(self.reference(f"{image}:1.30@{DIGEST1}"))
        mock_file.write_text.assert_called_once_with(self.reference(f"{image}:1.31@{DIGEST2}"))
        self.assert_archived_repository_logged(image, Location(mock_file, 1))

    @kills.replace(
        oci,
        "@partial(ProjectGetter, declares_archival=_is_docker_hub_image)",
        "@ProjectGetter",
        "an `ignore[archived]` on an active image is also reported as lacking an archival signal",
    )
    def test_ignore_archived_on_an_active_repository_is_reported_as_redundant(self) -> None:
        """Test that an `ignore[archived]` on an image whose repository is active is reported as redundant."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        marker = self.marker_line("ignore[archived]")
        mock_file = self.updated_file(marker + self.reference(f"python:3.14@{DIGEST1}"))
        mock_file.write_text.assert_called_once_with(marker + self.reference(f"python:3.15@{DIGEST2}"))
        self.assert_redundant_ignore_archived_logged("python", Location(mock_file, 2))

    def test_snapshot_on_a_living_image_is_not_stale(self) -> None:
        """Test that a snapshot pinned long ago is not reported as stale while its image keeps publishing.

        The snapshot is 512 days old and debian released 10 days ago, so the reference is dated by the release.
        """
        recent = days_ago(10)
        snapshot = "bookworm-20240110"
        self.requests.side_effect = mock_docker_registry(
            docker_tag("13.2", DIGEST2, tag_last_pushed=recent),
            docker_tag(snapshot, DIGEST1, tag_last_pushed=STALE_DATE),
        )
        self.updated_file(self.reference(f"debian:{snapshot}@{DIGEST1}")).write_text.assert_not_called()
        self.assert_no_warnings_logged()

    def test_bumped(self) -> None:
        """Test that the image tag and digest are bumped when a newer version is available."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        mock_file = self.updated_file(self.reference(f"python:3.14@{DIGEST1}"))
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.15@{DIGEST2}"))
        self.assert_new_version_logged("python", docker_hub_version("3.15"), Location(mock_file, 1))
        self.assert_no_warnings_logged()

    def test_digest_drift_warned_not_repinned(self) -> None:
        """Test that a pinned image whose tag was re-pushed inside the cooldown is warned about, not rewritten."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST2, tag_last_pushed=FRESH_DATE))
        mock_file = self.updated_file(self.reference(f"python:3.14@{DIGEST1}"))
        mock_file.write_text.assert_not_called()
        self.assert_drift_logged(self.drifted(mock_file, "python", "3.14"))
        self.assert_no_new_version_logged()

    @staticmethod
    def drifted(mock_file: Mock, image: str, tag: str, line: int = 1) -> DriftedPin:
        """Return the drifted pin of the image and tag at the line, re-pointed from `DIGEST1` to `DIGEST2`."""
        return DriftedPin(image, tag, Location(mock_file, line), DIGEST1, kind=DriftKind.DIGEST, new_sha=DIGEST2)

    @kills.delete(
        oci._resolved_tag,
        ", publication=tag.pushed",
        "a tag naming neither a version nor a channel adopts a digest re-pushed inside the cooldown",
    )
    def test_drift_inside_the_cooldown_is_held_back_silently(self) -> None:
        """Test that a tag naming neither a version nor a channel, re-pushed inside the cooldown, is left unreported."""
        self.requests.side_effect = mock_docker_registry(docker_tag("dev-2024", DIGEST2, tag_last_pushed=FRESH_DATE))
        mock_file = self.updated_file(self.reference_adopting_drift(self.reference(f"debian:dev-2024@{DIGEST1}")))
        mock_file.write_text.assert_not_called()
        self.assertTrue(self.requests.called)
        self.assert_no_warnings_logged()
        self.assert_no_info_logged()

    def reference_adopting_drift(self, reference: str) -> str:
        """Return the reference with a marker opting it into adopting hash drift on the line above it."""
        return self.marker_line("allow[hash-drift]") + reference

    def test_digest_drift_whose_push_date_cannot_be_fetched_is_held_back_with_the_reason(self) -> None:
        """Test that a re-pushed tag whose push date cannot be fetched is left as it is, the failure logged."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST2), unavailable=Endpoint.PUSH_DATE)
        mock_file = self.updated_file(self.reference_adopting_drift(self.reference(f"python:3.14@{DIGEST1}")))
        mock_file.write_text.assert_not_called()
        self.assert_could_not_fetch_logged(status=HTTPStatus.SERVICE_UNAVAILABLE)
        self.assert_undated_drift_logged(self.drifted(mock_file, "python", "3.14", 2), "HTTP 503")

    def test_digest_drift_under_a_zero_day_cooldown_is_adopted_without_its_push_date(self) -> None:
        """Test that a re-pushed tag opted into drift with a zero-day cooldown is re-pinned although it is undated."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST2), unavailable=Endpoint.PUSH_DATE)
        marker = self.marker_line("allow[hash-drift] set[cooldown=0]")
        mock_file = self.updated_file(marker + self.reference(f"python:3.14@{DIGEST1}"))
        mock_file.write_text.assert_called_once_with(marker + self.reference(f"python:3.14@{DIGEST2}"))
        self.assert_adopted_drift_logged(self.drifted(mock_file, "python", "3.14", 2))

    def test_digest_drift_adopted_with_marker(self) -> None:
        """Test that an `allow[hash-drift]` marker re-pins a re-pushed tag to the digest it now serves."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST2))
        mock_file = self.updated_file(self.reference_adopting_drift(self.reference(f"python:3.14@{DIGEST1}")))
        mock_file.write_text.assert_called_once_with(
            self.reference_adopting_drift(self.reference(f"python:3.14@{DIGEST2}"))
        )
        self.assert_adopted_drift_logged(self.drifted(mock_file, "python", "3.14", 2))
        self.assert_no_warnings_logged()

    def test_digest_drift_on_another_registry_adopted_with_marker(self) -> None:
        """Test that an `allow[hash-drift]` marker re-pins a re-pushed tag on a registry that does not date a push."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST2))
        image = "ghcr.io/owner/python"
        mock_file = self.updated_file(self.reference_adopting_drift(self.reference(f"{image}:3.14@{DIGEST1}")))
        mock_file.write_text.assert_called_once_with(
            self.reference_adopting_drift(self.reference(f"{image}:3.14@{DIGEST2}"))
        )
        self.assert_adopted_drift_logged(self.drifted(mock_file, image, "3.14", 2))
        self.assert_no_warnings_logged()

    def test_pinned_floating_tag_on_another_registry_drift_warned(self) -> None:
        """Test that a floating tag off Docker Hub serving another digest than it records is warned about."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST2), docker_tag("3.14.7", DIGEST2))
        image = "ghcr.io/owner/python"
        mock_file = self.updated_file(self.reference(f"{image}:latest@{DIGEST1}"))
        mock_file.write_text.assert_not_called()
        self.assert_drift_logged(self.drifted(mock_file, image, "latest"))

    def test_pinned_floating_tag_on_another_registry_serving_no_concrete_version_drift_warned(self) -> None:
        """Test that a drifted floating tag off Docker Hub is warned about when only floating tags serve its image."""
        self.requests.side_effect = mock_docker_registry(docker_tag("dev", DIGEST2), docker_tag("prod", DIGEST2))
        image = "ghcr.io/acme/api"
        mock_file = self.updated_file(self.reference(f"{image}:dev@{DIGEST1}"))
        mock_file.write_text.assert_not_called()
        self.assert_drift_logged(self.drifted(mock_file, image, "dev"))

    def test_reference_naming_no_version_is_pinned_to_the_version_latest_serves(self) -> None:
        """Test that a reference without a tag, or with a floating one, is pinned to the version `latest` serves."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        for case, image in {
            "no tag": "python",
            "a floating tag": "python:latest",
            "a digest but no tag": f"python@{DIGEST}",
            "a floating tag at its digest": f"python:latest@{DIGEST}",
        }.items():
            with self.subTest(case):
                mock_file = self.updated_file(self.reference(image))
                mock_file.write_text.assert_called_once_with(self.reference(f"python:3.14.7@{DIGEST}"))
                self.assert_pinned_logged("python", "3.14.7", DIGEST, Location(mock_file, 1))
                self.assert_no_warnings_logged()

    def test_drifted_reference_naming_a_digest_but_no_tag(self) -> None:
        """Test that a drifted reference that does not name a tag is warned about by the image's name alone."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST2), docker_tag("prod", DIGEST2))
        mock_file = self.updated_file(self.reference(f"acme/api@{DIGEST1}"))
        mock_file.write_text.assert_not_called()
        self.assert_drift_logged(self.drifted(mock_file, "acme/api", ""))

    def test_pin_unpinned_image(self) -> None:
        """Test that an image referenced by tag only is pinned with the latest tag and digest."""
        self.assert_update_written("3.15", self.reference("python:3.14"), self.reference(f"python:3.15@{DIGEST}"))

    def assert_update_written(
        self, new_tag: str, contents: str, expected: str, *, image: str = "python", line: int = 1
    ) -> Mock:
        """Assert that the registry's new tag is written as expected, and logged as a new version at the line."""
        self.requests.side_effect = mock_docker_registry(docker_tag(new_tag, DIGEST))
        mock_file = self.updated_file(contents)
        mock_file.write_text.assert_called_once_with(expected)
        self.assert_new_version_logged(image, docker_hub_version(new_tag), Location(mock_file, line))
        self.assert_no_warnings_logged()
        return mock_file

    def test_pin_version_tag_inside_the_cooldown(self) -> None:
        """Test that an image tag pushed inside the cooldown is pinned to the digest it serves."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST, tag_last_pushed=FRESH_DATE))
        mock_file = self.updated_file(self.reference("python:3.14"))
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.14@{DIGEST}"))
        self.assert_pinned_logged("python", "3.14", DIGEST, Location(mock_file, 1))

    def test_pin_a_snapshot_with_no_newer_sibling(self) -> None:
        """Test that a dated snapshot the registry lists no newer one of keeps its tag and is pinned to its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("bookworm-20260803", DIGEST))
        mock_file = self.updated_file(self.reference("debian:bookworm-20260803"))
        mock_file.write_text.assert_called_once_with(self.reference(f"debian:bookworm-20260803@{DIGEST}"))
        self.assert_pinned_logged("debian", "bookworm-20260803", DIGEST, Location(mock_file, 1))
        self.assert_no_warnings_logged()

    def test_floating_tag_serving_no_concrete_version(self) -> None:
        """Test that a floating tag without a concrete version is left alone and reported unless its digest drifted."""
        self.requests.side_effect = mock_docker_registry(docker_tag("dev", DIGEST), docker_tag("prod", DIGEST))
        reason = FloatingPin.NO_VERSION_TAG
        for case, image in {"no digest": "acme/api:dev", "the digest it serves": f"acme/api:dev@{DIGEST}"}.items():
            with self.subTest(case):
                mock_file = self.updated_file(self.reference(image))
                mock_file.write_text.assert_not_called()
                self.assert_unpinned_floating_tag_logged("acme/api", "dev", Location(mock_file, 1), reason)
                self.assert_no_warnings_logged()

    def test_pinned_floating_tag_the_registry_does_not_list(self) -> None:
        """Test that a floating tag the registry does not list keeps its digest, without a drift warning."""
        self.requests.side_effect = mock_docker_registry(docker_tag("prod", DIGEST2))
        mock_file = self.updated_file(self.reference(f"acme/api:dev@{DIGEST1}"))
        mock_file.write_text.assert_not_called()
        self.assert_unpinned_floating_tag_logged("acme/api", "dev", Location(mock_file, 1), FloatingPin.NOT_LISTED)
        self.assert_no_warnings_logged()

    @kills.delete(
        _listing_page,
        "HTTPStatus.NOT_FOUND,",
        "a repository Docker Hub does not serve is warned about, and its floating tag reported as not listed",
        case="[Docker Hub] (tag='latest')",
    )
    @kills.force_false(
        oci._walked_floating_tag,
        "names.absent",
        "a floating tag of a repository another registry does not serve is warned about as missing a manifest",
        case="[another registry] (tag='latest')",
    )
    @kills.force_false(
        _Rewriter._report_why_the_source_left_it,
        "latest.unserved is Unserved.REPOSITORY",
        "leaves a version tag of a repository the registry does not serve as it is without saying why",
        case="[an anonymous registry] (tag='3.5.1')",
    )
    @kills.append(
        oci._resolved_tag,
        "if names.absent",
        " and current.version is not None",
        "warns about the manifest of a dev-2024 tag of a repository the registry does not serve",
        case="[Docker Hub] (tag='dev-2024')",
    )
    @kills.append(
        oci._resolved_tag,
        "if names.absent",
        " and current.version is None",
        "looks for newer versions of a version tag of a repository the registry does not serve",
        case="[an anonymous registry] (tag='3.5.1')",
    )
    @kills.force_false(
        oci._refuses_the_repository,
        "response.status_code == HTTPStatus.NOT_FOUND",
        "the check takes the 404 of a registry without a challenge for a repository the registry serves",
        case="[an anonymous registry] (tag='3.5.1')",
    )
    def test_tag_whose_repository_the_registry_does_not_serve(self) -> None:
        """Test that a floating, versioned, or other tag of a repository not served is reported at DEBUG alone."""
        for case, tag in (
            ("Docker Hub", "latest"),
            ("another registry", "latest"),
            ("an anonymous registry", "3.5.1"),
            ("Docker Hub", "dev-2024"),
        ):
            image, challenge = _UNSERVED_REPOSITORIES[case]
            with self.subTest(case, tag=tag):
                self.requests.side_effect = mock_docker_registry(
                    repository=RepositoryState.UNSERVED, challenge=challenge
                )
                mock_file = self.updated_file(self.reference(f"{image}:{tag}"))
                mock_file.write_text.assert_not_called()
                self.assert_left_as_is_logged(image, tag, Location(mock_file, 1), Unserved.REPOSITORY)
                self.assert_no_warnings_logged()

    def test_pinned_floating_tag_that_drifted(self) -> None:
        """Test that a floating tag serving another digest than it is pinned to is warned about, not re-pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST2), docker_tag("3.14.7", DIGEST2))
        mock_file = self.updated_file(self.reference(f"python:latest@{DIGEST1}"))
        mock_file.write_text.assert_not_called()
        self.assert_drift_logged(self.drifted(mock_file, "python", "latest"))
        self.assert_no_new_version_logged()

    def test_pinned_floating_tag_serving_no_concrete_version_that_drifted(self) -> None:
        """Test that a pinned floating tag is warned about when it drifted to an image only floating tags serve."""
        self.requests.side_effect = mock_docker_registry(docker_tag("dev", DIGEST2), docker_tag("prod", DIGEST2))
        mock_file = self.updated_file(self.reference(f"acme/api:dev@{DIGEST1}"))
        mock_file.write_text.assert_not_called()
        self.assert_drift_logged(self.drifted(mock_file, "acme/api", "dev"))
        self.assert_no_reference_left_as_is_logged()

    def test_pinned_floating_tag_re_pointed_without_a_push_date_is_held_back_with_the_reason(self) -> None:
        """Test that a floating tag whose listing entry lacks a push date is left as it is, the reason logged."""
        cases = {
            ("python", "latest"): [docker_tag("latest", DIGEST2, tag_last_pushed=None), docker_tag("3.14.7", DIGEST2)],
            ("acme/api", "dev"): [docker_tag("dev", DIGEST2, tag_last_pushed=None), docker_tag("prod", DIGEST2)],
        }
        for (image, tag), tags in cases.items():
            with self.subTest(image=image):
                self.requests.side_effect = mock_docker_registry(*tags)
                mock_file = self.updated_file(self.reference_adopting_drift(self.reference(f"{image}:{tag}@{DIGEST1}")))
                mock_file.write_text.assert_not_called()
                self.assert_no_warnings_logged()
                reason = "Docker Hub did not report a push date"
                self.assert_undated_drift_logged(self.drifted(mock_file, image, tag, 2), reason)

    def test_pinned_floating_tag_serving_no_concrete_version_drift_adopted_with_marker(self) -> None:
        """Test that a drifted floating tag adopts its new digest under `allow[hash-drift]`, and keeps its tag."""
        self.requests.side_effect = mock_docker_registry(docker_tag("dev", DIGEST2), docker_tag("prod", DIGEST2))
        mock_file = self.updated_file(self.reference_adopting_drift(self.reference(f"acme/api:dev@{DIGEST1}")))
        mock_file.write_text.assert_called_once_with(
            self.reference_adopting_drift(self.reference(f"acme/api:dev@{DIGEST2}"))
        )
        self.assert_adopted_drift_logged(self.drifted(mock_file, "acme/api", "dev", 2))
        self.assert_no_warnings_logged()

    def test_pinned_floating_tag_serving_no_concrete_version_kept_floating_that_drifted(self) -> None:
        """Test that a drifted floating tag is warned about when a marker keeps it floating."""
        self.requests.side_effect = mock_docker_registry(docker_tag("dev", DIGEST2), docker_tag("prod", DIGEST2))
        mock_file = self.updated_file(
            self.marker_line("allow[floating-pin]") + self.reference(f"acme/api:dev@{DIGEST1}")
        )
        mock_file.write_text.assert_not_called()
        self.assert_drift_logged(self.drifted(mock_file, "acme/api", "dev", 2))
        self.assert_no_reference_left_as_is_logged()

    def test_pinned_floating_tag_drift_adopted_with_marker(self) -> None:
        """Test that `allow[hash-drift]` re-pins a re-pointed floating tag to the version and digest it now serves."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST2), docker_tag("3.14.7", DIGEST2))
        mock_file = self.updated_file(self.reference_adopting_drift(self.reference(f"python:latest@{DIGEST1}")))
        mock_file.write_text.assert_called_once_with(
            self.reference_adopting_drift(self.reference(f"python:3.14.7@{DIGEST2}"))
        )
        self.assert_adopted_drift_logged(self.drifted(mock_file, "python", "latest", 2), among_others=True)
        self.assert_pinned_logged("python", "3.14.7", DIGEST2, Location(mock_file, 2), among_others=True)
        self.assert_no_warnings_logged()

    def test_marker_keeps_the_tag_floating(self) -> None:
        """Test that a marker allowing the floating pin leaves the tag as it is, naming what it resolves to."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_file = self.updated_file(self.marker_line("allow[floating-pin]") + self.reference("python:latest"))
        mock_file.write_text.assert_not_called()
        resolved = DependencyVersion(version="3.14.7", sha=DIGEST)
        self.assert_kept_floating_logged("python", "latest", resolved, Location(mock_file, 2))
        self.assert_no_warnings_logged()

    def test_reference_through_a_variable_is_left_alone_and_reported(self) -> None:
        """Test that a reference whose image name or tag holds a variable is reported and not looked up."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        cases = {
            "a tag that is a variable": ("app", "${TAG}"),
            "a tag ending in $VAR": ("redis", "7.2-$VARIANT"),
            "an image name ending in ${VAR}": ("ghcr.io/org/app-${ENV}", "1.0"),
            "an image name ending in $VAR": ("registry/$APP", "1.0"),
        }
        for case, (image, tag) in cases.items():
            with self.subTest(case):
                mock_file = self.updated_file(self.reference(f"{image}:{tag}"))
                self.assert_left_alone_unqueried(mock_file, image, tag, 1, ImageNotLookedUp.THROUGH_A_VARIABLE)

    def assert_left_alone_unqueried(
        self, mock_file: Mock, image: str, tag: str, line: int, reason: ImageNotLookedUp
    ) -> None:
        """Assert that the run reported the reference at the line, and did not write, ask a registry, or report more."""
        mock_file.write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_left_as_is_logged(image, tag, Location(mock_file, line), reason)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @kills.replace(
        _warn_about_redundant_update_directives,
        "(marker.scope_directive(Scope.UPDATE), marker.version_bound_directive)",
        "(marker.version_bound_directive,)",
        "an `ignore[update]` on an image never looked up is not reported, though it holds nothing back",
        case="ignore[update]",
    )
    @kills.delete(
        latest_version,
        " and never_updated is None",
        "an `ignore[update]` on an image never looked up hides why the image is left as it is",
        case="ignore[update]",
    )
    @kills.delete(
        directive_module,
        "consequence=Consequence.ARCHIVAL_NEVER_CHECKED,",
        "an `ignore[archived]` on an image never looked up is blamed on its source rather than on the variable",
        case="ignore[archived]",
    )
    @kills.delete(
        Source.reports_archival,
        "and self.why_image_not_looked_up(pinned) is None",
        "an `ignore[archived]` on an image never looked up decides nothing without being reported",
        case="ignore[archived]",
    )
    @kills.delete(
        directive_module,
        "consequence=Consequence.NEVER_UPDATED,",
        "a cooldown on an image never looked up is blamed on its source rather than on the variable",
        case="set[cooldown=30]",
    )
    def test_markers_on_a_reference_through_a_variable_are_redundant_because_of_it(self) -> None:
        """Test that an `ignore[archived]`, a cooldown, or an `ignore[update]` is redundant.

        None of them holds anything back: the reference is reported as left alone, as it is without a marker.
        """
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        cases = {
            "ignore[archived]": Consequence.ARCHIVAL_NEVER_CHECKED,
            "set[cooldown=30]": Consequence.NEVER_UPDATED,
            "ignore[update]": Consequence.NEVER_UPDATED,
        }
        for directive, consequence in cases.items():
            with self.subTest(directive=directive):
                mock_file = self.updated_file(self.marker_line(directive) + self.reference("app:${TAG}"))
                mock_file.write_text.assert_not_called()
                reason = ImageNotLookedUpReason(ImageNotLookedUp.THROUGH_A_VARIABLE, consequence)
                self.assert_redundant_directive_logged(reason, "app", Location(mock_file, 2), directive)
                self.assert_no_ignored_logged()
                through_a_variable = ImageNotLookedUp.THROUGH_A_VARIABLE
                self.assert_left_as_is_logged("app", "${TAG}", Location(mock_file, 2), through_a_variable)

    def test_reference_through_syntax_not_read_as_a_variable_is_left_alone(self) -> None:
        """Test that a tag ending in syntax Update-time does not read as a variable, `$(VARIANT)`, is not queried."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_file = self.updated_file(self.reference("python:3.12-$(VARIANT)"))
        mock_file.write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_path_logged(mock_file)

    def test_pin_unpinned_image_already_at_latest(self) -> None:
        """Test that an unpinned image already at the latest version is still pinned with its digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14", DIGEST))
        mock_file = self.updated_file(self.reference("python:3.14"))
        mock_file.write_text.assert_called_once_with(self.reference(f"python:3.14@{DIGEST}"))
        self.assert_pinned_logged("python", "3.14", DIGEST, Location(mock_file, 1))
        self.assert_no_warnings_logged()


class Endpoint(Enum):
    """An endpoint of the registry a test can make unavailable, so the code under test meets that failure."""

    TAG_NAMES = auto()  # The OCI listing of tag names.
    TAG_DIGESTS = auto()  # Docker Hub's listing of every tag with its digest.
    PUSH_DATE = auto()  # Docker Hub's per-tag metadata, which carries the push date.


class RepositoryState(Enum):
    """A state of the image's repository that a test can have the registry report: active, archived, or not served."""

    ACTIVE = auto()  # Served, and active on Docker Hub.
    ARCHIVED = auto()  # Served, and archived on Docker Hub.
    UNSERVED = auto()  # Not served at all.


def mock_docker_registry(
    *tags: dict[str, JSON],
    names: list[str] | None = None,
    unavailable: Endpoint | None = None,
    repository: RepositoryState = RepositoryState.ACTIVE,
    challenge: bool = True,
    page_size: int | None = None,
) -> RequestStub:
    """Return a Session.get/.head side effect that mimics an OCI registry plus Docker Hub's proprietary API."""
    by_name = {cast("str", tag["name"]): tag for tag in tags}
    tag_names = list(by_name) if names is None else names

    def get(url: str, *_args: object, **_kwargs: object) -> Mock:
        if url.endswith("/v2/"):  # OCI auth challenge probe.
            return probe_response(url, challenge=challenge)
        if "/token" in url and "/v2/" not in url:  # Token endpoint discovered from the challenge.
            return mock_response({"token": "token"})  # nosec
        if "/tags/list" in url:
            names_ok = unavailable is not Endpoint.TAG_NAMES
            return _tags_list_response(url, tag_names, list_ok=names_ok, page_size=page_size)
        if "/manifests/" in url:
            return _manifest_response(url, by_name)
        return _docker_hub_response(url, tags, by_name, unavailable, repository)

    def unserved(url: str, *_args: object, **_kwargs: object) -> Mock:
        if url.endswith("/v2/") or url.startswith("https://auth.docker.io/"):  # Docker Hub still hands out a token.
            return get(url)
        if "/token" in url:
            return _forbidden(url)
        return _not_found(url) if "/tags?" in url or not challenge else _refusal(url)

    return unserved if repository is RepositoryState.UNSERVED else get


def probe_response(url: str, *, challenge: bool) -> Mock:
    """Answer the OCI `/v2/` auth probe: a `401` challenge pointing at the token endpoint, or `200` when anonymous."""
    if not challenge:  # Anonymous registry: no challenge, so the client proceeds without a token.
        return mock_response({}, ok=True, status_code=200, headers={})
    return unauthorized(url, headers={"WWW-Authenticate": _bearer(url)})


def unauthorized(url: str, headers: dict[str, str] | None) -> Mock:
    """Answer a request with a 401, as a registry does for a repository it does not serve or a probe it challenges."""
    return mock_response({}, ok=False, status_code=401, url=url, headers=headers or {})


def _bearer(url: str) -> str:
    """Return the challenge of the registry the URL is on, pointing at the registry's token endpoint."""
    return f'Bearer realm="{token_endpoint(url)}",service="{_host(url)}"'


def token_endpoint(url: str) -> str:
    """Return the token endpoint of the registry the URL is on, which its challenge names as the realm."""
    host = _host(url)
    return "https://auth.docker.io/token" if host == oci._DOCKER_HUB_OCI_HOST else f"https://{host}/token"


def _host(url: str) -> str:
    """Return the host the URL is on."""
    return url.removeprefix("https://").split("/", maxsplit=1)[0]


def _tags_list_response(url: str, tag_names: list[str], *, list_ok: bool, page_size: int | None) -> Mock:
    """Answer `tags/list`: a 503 when `list_ok` is false, else the names — one `page_size` page (with a next link)."""
    if not list_ok:
        return service_unavailable(url)
    if page_size is None:
        return mock_response({"tags": tag_names}, ok=True, status_code=200, url=url, headers={})
    last = parse_qs(urlparse(url).query).get("last", [None])[0]  # The name the previous page ended on, if any.
    start = tag_names.index(last) + 1 if last and last in tag_names else 0
    page = tag_names[start : start + page_size]
    links = {"next": {"url": f"?last={page[-1]}", "rel": "next"}} if start + page_size < len(tag_names) else {}
    return mock_response({"tags": page}, ok=True, status_code=200, url=url, links=links, headers={})


def service_unavailable(url: str) -> Mock:
    """Answer a request with a 503, as a registry does that cannot serve the request for now."""
    return mock_response({}, ok=False, status_code=503, url=url)


def _manifest_response(url: str, by_name: dict[str, dict[str, JSON]]) -> Mock:
    """Answer a manifest `HEAD`: the tag's digest in the `Docker-Content-Digest` header, or a 404 if unknown."""
    tag = by_name.get(url.rsplit("/manifests/", maxsplit=1)[-1])
    if tag is None:
        return _not_found(url)
    digest = cast("str", tag.get("digest", ""))
    return mock_response({}, headers={"Docker-Content-Digest": digest} if digest else {})


def _docker_hub_response(
    url: str,
    tags: tuple[dict[str, JSON], ...],
    by_name: dict[str, dict[str, JSON]],
    unavailable: Endpoint | None,
    repository: RepositoryState,
) -> Mock:
    """Answer a request to Docker Hub's proprietary API: the tag listing, a repository, or a tag's push date."""
    if "/tags?" in url:  # The tag listing: every tag with its digest.
        return _tag_digests_response(url, tags, ok=unavailable is not Endpoint.TAG_DIGESTS)
    if "/namespaces/" in url and "/tags" not in url:  # A request about the repository, rather than about its tags.
        return mock_response({"status_description": "archived" if repository is RepositoryState.ARCHIVED else "active"})
    # A tag's metadata, which carries its push date. Only a tag that resolved a digest gets this far.
    ok = unavailable is not Endpoint.PUSH_DATE
    return mock_response(by_name.get(url.rsplit("/tags/", maxsplit=1)[-1], {})) if ok else service_unavailable(url)


def _tag_digests_response(url: str, tags: tuple[dict[str, JSON], ...], *, ok: bool) -> Mock:
    """Answer Docker Hub's tag listing: the requested page of tags with their digests, or a 503 when unavailable.

    The page and its size are read from the URL, as Docker Hub reads them, and a page that leaves tags unlisted
    carries the `next` URL of the page after it.
    """
    if not ok:
        return service_unavailable(url)
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


def _forbidden(url: str) -> Mock:
    """Answer a request with a 403, as a registry's token endpoint does for a repository the registry does not serve."""
    return mock_response({}, ok=False, status_code=403, url=url)


def _not_found(url: str) -> Mock:
    """Answer a request with a 404, as the registry does for an endpoint or a tag it does not serve."""
    return mock_response({}, ok=False, status_code=404, url=url)


def _refusal(url: str) -> Mock:
    """Answer a request for a repository the registry does not serve, with the challenge the registry sends.

    Docker Hub says the token it handed out does not reach the repository, and ghcr.io adds nothing to its challenge.
    """
    challenge = _bearer(url)
    if _host(url) == oci._DOCKER_HUB_OCI_HOST:
        challenge += ',error="insufficient_scope"'
    return unauthorized(url, headers={"WWW-Authenticate": challenge})
