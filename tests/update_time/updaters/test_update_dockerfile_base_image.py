"""Unit tests for the Dockerfile base image update script."""

from unittest.mock import Mock, patch

from update_time.domain import floating
from update_time.domain.dependency import DependencyVersion
from update_time.domain.directive import Reason
from update_time.domain.file_type import DOCKERFILES
from update_time.primitives.location import Location
from update_time.sources import oci
from update_time.updaters import update_dockerfile_base_image
from update_time.updaters.update_dockerfile_base_image import update_dockerfiles

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time import registry
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2
from tests.update_time.helpers import docker_tag, mock_docker_hub_auth
from tests.update_time.registry import mock_docker_registry


@mock_docker_hub_auth
class UpdateDockerfileTest(registry.ImageUpdaterTestMixin):
    """Unit tests for the update Dockerfile function."""

    def reference(self, image: str) -> str:
        """Return a Dockerfile `FROM` line for the image."""
        return f"FROM {image}\n"

    def run_updater(self, mock_file: Mock) -> None:
        """Run the Dockerfile updater with the mock file as the only discovered Dockerfile.

        The updater globs several Dockerfile patterns; returning the file for the exact `Dockerfile` one only
        processes it exactly once.
        """

        def rglob(pattern: str, *, case_sensitive: bool | None = None) -> list[Mock]:  # noqa: ARG001
            return [mock_file] if pattern == "Dockerfile" else []

        with patch("pathlib.Path.rglob", side_effect=rglob):
            update_dockerfiles()

    def test_alternate_filenames_are_scanned(self):
        """Test that `*.Dockerfile` and `Dockerfile.*` files are scanned, not only an exact `Dockerfile`."""
        with patch("update_time.updaters.update_dockerfile_base_image.glob_for", return_value=[]) as glob_for:
            update_dockerfiles()
        glob_for.assert_called_once_with(DOCKERFILES)
        self.assertEqual(DOCKERFILES.patterns, ("Dockerfile", "*.Dockerfile", "Dockerfile.*"))
        self.assertFalse(DOCKERFILES.case_sensitive)  # A `dockerfile` is found as well

    @kills(
        Mutation(
            oci,
            r'    rf"{_IMAGE_NAME}(?::(?=[\d\w\.\-]))?(?P<version>[\d\w\.\-]*){_IMAGE_DIGEST}(?![\w\d\./:@-])"',
            r'    rf"{_IMAGE_NAME}:(?P<version>[\d\w\.\-]+){_IMAGE_DIGEST}(?![\w\d\./:@-])"',
            "a reference naming no tag is not read as a reference at all",
        )
    )
    def test_pin_tagless_base_image(self):
        """Test that a `FROM image` naming no tag is pinned to the version and digest `latest` serves."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_dockerfile = mock_path("FROM python\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_once_with(f"FROM python:3.14.7@{DIGEST}\n")
        self.assert_pinned_logged("python", "3.14.7", DIGEST, Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_dockerfile_base_image,
            "        return image != _SCRATCH and image.lower() not in stages",
            "        return image != _SCRATCH.upper() and image.lower() not in stages",
            "a `FROM scratch` is resolved as though a registry served the empty base",
        )
    )
    def test_scratch_base_image_is_left_alone_and_not_queried(self):
        """Test that `FROM scratch` names Docker's empty base rather than an image, so no registry is queried."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_dockerfile = mock_path("FROM scratch AS base\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_dockerfile_base_image,
            "        return image != _SCRATCH and image.lower() not in stages",
            "        return image != _SCRATCH and image.lower() not in frozenset()",
            "a `FROM` naming a build stage is resolved as though a registry served an image of that name",
        )
    )
    def test_build_stage_reference_is_left_alone_and_not_queried(self):
        """Test that a `FROM` naming one of the file's own build stages is left alone, no registry serving it."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_dockerfile = mock_path("FROM python:3.14 AS deps\nFROM deps\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_once_with(f"FROM python:3.14.7@{DIGEST} AS deps\nFROM deps\n")
        requested = "".join(call.args[0] for call in self.requests.call_args_list)
        self.assertNotIn("deps", requested)
        self.assert_new_version_logged("python", "3.14.7", Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_dockerfile_base_image,
            'rf"^\\s*(?i:FROM)\\s+(?:--platform=\\S+\\s+)?{OPTIONALLY_TAGGED_IMAGE_REFERENCE}"',
            'rf"^\\s*(?i:FROM)\\s+(?:--platform=\\S+\\s+)?\\$?\\{{?{OPTIONALLY_TAGGED_IMAGE_REFERENCE}"',
            "a variable's name is read as an image, so its own name is queried as a registry host",
        )
    )
    def test_image_name_from_a_variable_is_left_alone_and_not_queried(self):
        """Test that a `FROM` taking its image name from a variable names no image, so no registry is queried.

        One case per spelling a Dockerfile takes, the braces being optional.
        """
        # A name of its own per case: the source caches what it resolved, so a repeated name would let the
        # second case pass on the first one's cache rather than on its own run.
        for reference in ("$BASE_IMAGE", "${OTHER_IMAGE}"):
            with self.subTest(reference=reference):
                self.requests.reset_mock()
                self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST))
                mock_dockerfile = mock_path(f"FROM {reference}\n")
                self.run_updater(mock_dockerfile)
                mock_dockerfile.write_text.assert_not_called()
                self.requests.assert_not_called()

    @kills(
        Mutation(
            floating,
            "    return _FLOATING_PIN.cause(marker, allowed=marker.allow_floating_pin)",
            "    return _FLOATING_PIN.cause(marker, allowed=False)",
            "a marker allowing the floating pin pins the reference anyway",
        )
    )
    def test_marker_keeps_a_tagless_base_image_as_it_is(self):
        """Test that `allow[floating-pin]` on a `FROM` naming no tag leaves it, naming what it resolves to."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_dockerfile = mock_path("# update-time: allow[floating-pin]\nFROM python\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_not_called()
        resolved = DependencyVersion(version="3.14.7", sha=DIGEST)
        self.assert_kept_floating_logged("python", "", resolved, Location(mock_dockerfile, 2))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            oci,
            r'    rf"{_IMAGE_NAME}(?::(?=[\d\w\.\-]))?(?P<version>[\d\w\.\-]*){_IMAGE_DIGEST}(?![\w\d\./:@-])"',
            r'    rf"{_IMAGE_NAME}:?(?P<version>[\d\w\.\-]*){_IMAGE_DIGEST}"',
            "a tag written as a variable substitution is read as no tag at all, so the reference is rewritten",
        )
    )
    def test_variable_substitution_in_the_tag_is_left_alone(self):
        """Test that a `FROM image:${TAG}` names a tag Update-time cannot read, so no registry is queried."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_dockerfile = mock_path("FROM myrepo/app:${TAG}\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_dockerfile_base_image,
            r'_STAGE_NAME_RE = re.compile(r"^FROM\s.*\sAS\s+(?P<stage>\S+)", re.IGNORECASE | re.MULTILINE)',
            r'_STAGE_NAME_RE = re.compile(r"^FROM\s.*\sAS\s+(?P<stage>\S+)", re.MULTILINE)',
            "a stage introduced with a lower-case `as` is not recognised as a stage",
        )
    )
    def test_lower_case_stage_keyword_introduces_a_stage_too(self):
        """Test that a stage introduced with a lower-case `as` is one too, so a `FROM` naming it is left alone."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_dockerfile = mock_path("FROM python:3.14 as deps\nFROM deps\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_once_with(f"FROM python:3.14.7@{DIGEST} as deps\nFROM deps\n")
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_dockerfile_base_image,
            r'_IMAGE_RE = rf"^\s*(?i:FROM)\s+(?:--platform=\S+\s+)?{OPTIONALLY_TAGGED_IMAGE_REFERENCE}"',
            r'_IMAGE_RE = rf"^\s*FROM\s+(?:--platform=\S+\s+)?{OPTIONALLY_TAGGED_IMAGE_REFERENCE}"',
            "a lower-case `from` introduces no base image, so its line is left as it is",
        )
    )
    def test_lower_case_from_introduces_a_base_image_too(self):
        """Test that a lower-case `from` is updated too, Dockerfile keywords being case-insensitive."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        mock_dockerfile = mock_path("from python:3.14\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_once_with(f"from python:3.15@{DIGEST2}\n")
        self.assert_new_version_logged("python", "3.15", Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_dockerfile_base_image,
            r'_IMAGE_RE = rf"^\s*(?i:FROM)\s+(?:--platform=\S+\s+)?{OPTIONALLY_TAGGED_IMAGE_REFERENCE}"',
            r'_IMAGE_RE = rf"(?i:FROM)\s+(?:--platform=\S+\s+)?{OPTIONALLY_TAGGED_IMAGE_REFERENCE}"',
            "a `FROM` anywhere on a line introduces a base image, so prose mentioning one is rewritten",
        )
    )
    def test_from_inside_a_comment_is_left_alone(self):
        """Test that a `FROM` that does not open its line introduces no base image, so prose is left as it is."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        mock_dockerfile = mock_path("# built FROM python:3.14\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            update_dockerfile_base_image,
            '    return frozenset(match.group("stage").lower() for match in _STAGE_NAME_RE.finditer'
            "(dockerfile.read_text()))",
            '    return frozenset(match.group("stage") for match in _STAGE_NAME_RE.finditer(dockerfile.read_text()))',
            "a stage whose name is written in another case than the `FROM` naming it is not matched",
        )
    )
    def test_a_stage_is_matched_whatever_the_case_of_its_name(self):
        """Test that a `FROM` naming a stage in another case names that stage, so it is left alone."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_dockerfile = mock_path("FROM python:3.14 AS Deps\nFROM deps\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_once_with(f"FROM python:3.14.7@{DIGEST} AS Deps\nFROM deps\n")
        self.assert_no_warnings_logged()

    def test_stage_alias_is_preserved_when_pinning(self):
        """Test that a multi-stage `FROM image:tag AS name` alias is kept intact when the image is pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.4", DIGEST2))
        mock_dockerfile = mock_path("FROM ruby:3.3 AS build\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(f"FROM ruby:3.4@{DIGEST2} AS build\n")
        self.assert_new_version_logged("ruby", "3.4", Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()

    def test_platform_flag_with_build_arg_is_preserved(self):
        """Test that a `FROM --platform=$BUILDPLATFORM image:tag` line is updated with the flag left untouched."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        mock_dockerfile = mock_path("FROM --platform=$BUILDPLATFORM python:3.14\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(f"FROM --platform=$BUILDPLATFORM python:3.15@{DIGEST2}\n")
        self.assert_new_version_logged("python", "3.15", Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()

    def test_platform_flag_with_literal_value_and_stage_alias_is_preserved(self):
        """Test that a `FROM --platform=linux/amd64 image:tag AS name` line keeps both the flag and the stage alias."""
        self.requests.side_effect = mock_docker_registry(docker_tag("20", DIGEST2))
        mock_dockerfile = mock_path("FROM --platform=linux/amd64 node:18 AS build\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(f"FROM --platform=linux/amd64 node:20@{DIGEST2} AS build\n")
        self.assert_new_version_logged("node", "20", Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()

    def test_ignore_marker_leaves_base_image_untouched(self):
        """Test that a FROM line pinned by a preceding `# update-time: ignore` comment is not updated or queried."""
        mock_dockerfile = mock_path("# update-time: ignore\nFROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_ignored_logged("ghcr.io/astral-sh/uv", Location(mock_dockerfile, 2))
        self.assert_no_new_version_logged()
        self.assert_no_redundant_suppression_logged()
        self.assert_no_warnings_logged()

    def test_ignore_yanked_marker_is_reported_as_redundant(self):
        """Test that an `ignore[yanked]` marker on a base image is reported when an image has no yank to hold back."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        marker = self.marker_line("ignore[yanked]")
        mock_dockerfile = mock_path(marker + self.reference("python:3.14"))
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(marker + self.reference(f"python:3.15@{DIGEST2}"))
        self.assert_redundant_directive_logged(
            Reason.NO_YANK_CONCEPT, "python", Location(mock_dockerfile, 2), "ignore[yanked]"
        )

    def test_redundant_yank_scope_in_a_combined_bracket_names_the_scope_alone(self):
        """Test that a yank scope sharing a bracket is named on its own, without the other item in that bracket."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        marker = self.marker_line("ignore[update,yanked]")
        mock_dockerfile = mock_path(marker + self.reference("python:3.14"))
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_not_called()  # `ignore[update]` shares the bracket and freezes the image
        self.assert_redundant_directive_logged(
            Reason.NO_YANK_CONCEPT, "python", Location(mock_dockerfile, 2), "ignore[yanked]"
        )

    def test_redundant_cooldown_names_the_cooldown_directive_alone(self):
        """Test that the warning names the `cooldown` directive, leaving out one on the same line that does apply."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
        marker = self.marker_line("ignore[cooldown<30] allow[hash-drift]")
        mock_dockerfile = mock_path(marker + self.reference(f"ghcr.io/owner/python:3.14@{DIGEST1}"))
        self.run_updater(mock_dockerfile)
        self.assert_redundant_directive_logged(
            Reason.NO_COOLDOWN_DATES, "ghcr.io/owner/python", Location(mock_dockerfile, 2), "ignore[cooldown<30]"
        )

    def test_vulnerable_scope_is_reported_as_redundant(self):
        """Test that each `vulnerable` marker is reported when an image has no vulnerability to hold back."""
        directives = (
            "ignore[vulnerable]",
            "ignore[vulnerable=GHSA-2gwj-7jmv-h26r]",
            "ignore[vulnerable<high]",
            "allow[vulnerable>=high]",  # `allow` sets a level too, so the warning must name it rather than nothing
        )
        for directive in directives:
            with self.subTest(directive=directive):
                self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
                mock_dockerfile = mock_path(f"# update-time: {directive}\nFROM python:3.14\n")
                self.run_updater(mock_dockerfile)
                mock_dockerfile.write_text.assert_called_with(
                    f"# update-time: {directive}\nFROM python:3.15@{DIGEST2}\n"
                )
                self.assert_redundant_directive_logged(
                    Reason.NO_VULNERABILITY_REPORTS, "python", Location(mock_dockerfile, 2), directive
                )
                self.mock_log.reset_mock()  # Judge each case on the records of its own run.

    def test_redundant_vulnerable_scope_beside_another_ignore_names_the_scope_alone(self):
        """Test that a `vulnerable` scope is named on its own, without an `ignore` beside it that freezes the image."""
        for directive in ("ignore[vulnerable]", "ignore[vulnerable=GHSA-2gwj-7jmv-h26r]", "allow[vulnerable>=high]"):
            with self.subTest(directive=directive):
                self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST2))
                mock_dockerfile = mock_path(f"# update-time: ignore[update] {directive}\nFROM python:3.14\n")
                self.run_updater(mock_dockerfile)
                mock_dockerfile.write_text.assert_not_called()  # `ignore[update]` holds the update back
                self.assert_redundant_directive_logged(
                    Reason.NO_VULNERABILITY_REPORTS, "python", Location(mock_dockerfile, 2), directive
                )
                self.mock_log.reset_mock()  # Judge each case on the records of its own run.

    def test_label_prefixed_base_image_bumped_and_pinned(self):
        """Test that a label-prefixed base image (ghcr.io/astral-sh/uv:python3.12-...) is bumped and pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("python3.13-bookworm-slim", DIGEST2))
        mock_dockerfile = mock_path("FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(f"FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@{DIGEST2}\n")
        self.assert_new_version_logged("ghcr.io/astral-sh/uv", "python3.13-bookworm-slim", Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()
