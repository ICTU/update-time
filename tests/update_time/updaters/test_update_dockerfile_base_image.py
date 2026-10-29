"""Unit tests for the Dockerfile base image update script."""

import unittest
from pathlib import Path, PurePath
from unittest.mock import Mock, patch

from update_time.domain import file_type
from update_time.domain.dependency import DependencyVersion, ImageNotLookedUp
from update_time.io import log
from update_time.io.filesystem import glob_for
from update_time.manifests import dockerfile as dockerfile_manifest
from update_time.markers.directive import Consequence, ImageNotLookedUpReason, Reason
from update_time.primitives.location import Location
from update_time.updaters import update_dockerfile_base_image
from update_time.updaters.update_dockerfile_base_image import update_dockerfiles

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time import registry
from tests.update_time.fixtures import DIGEST, DIGEST1, DIGEST2
from tests.update_time.helpers import docker_hub_version, docker_tag, message_of
from tests.update_time.registry import mock_docker_registry
from tests.update_time.updaters.helpers import mock_docker_hub_auth


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

    def assert_update_written(
        self, new_tag: str, dockerfile: str, expected: str, *, image: str = "python", line: int = 2
    ) -> Mock:
        """Assert that the Dockerfile, whose line pulls the image, is written as expected once the tag is out."""
        self.requests.side_effect = mock_docker_registry(docker_tag(new_tag, DIGEST))
        mock_dockerfile = mock_path(dockerfile)
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_once_with(expected)
        self.assert_new_version_logged(image, docker_hub_version(new_tag), Location(mock_dockerfile, line))
        self.assert_no_warnings_logged()
        return mock_dockerfile

    def test_pin_tagless_base_image(self):
        """Test that a `FROM image` naming no tag is pinned to the version and digest `latest` serves."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_dockerfile = mock_path("FROM python\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_once_with(f"FROM python:3.14.7@{DIGEST}\n")
        self.assert_pinned_logged("python", "3.14.7", DIGEST, Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()

    def test_scratch_base_image_is_left_alone_and_not_queried(self):
        """Test that `FROM scratch` names Docker's empty base rather than an image, so no registry is queried."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_dockerfile = mock_path("FROM scratch AS base\n")
        self.run_updater(mock_dockerfile)
        self.assert_left_alone_unqueried(mock_dockerfile, "scratch", "", 1, ImageNotLookedUp.EMPTY_BASE_IMAGE)

    def test_build_stage_reference_is_left_alone_and_not_queried(self):
        """Test that a `FROM` naming one of the file's own build stages is left alone, no registry serving it."""
        for indentation in ("", "  "):
            with self.subTest(indentation=indentation):
                self.requests.side_effect = mock_docker_registry(
                    docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST)
                )
                mock_dockerfile = mock_path(f"{indentation}FROM python:3.14 AS deps\nFROM deps\n")
                self.run_updater(mock_dockerfile)
                expected = f"{indentation}FROM python:3.14.7@{DIGEST} AS deps\nFROM deps\n"
                mock_dockerfile.write_text.assert_called_once_with(expected)
                requested = "".join(call.args[0] for call in self.requests.call_args_list)
                self.assertNotIn("deps", requested)
                location = Location(mock_dockerfile, 2)
                self.assert_image_not_looked_up_logged("deps", "", location, ImageNotLookedUp.BUILD_STAGE)
                self.assert_new_version_logged("python", docker_hub_version("3.14.7"), Location(mock_dockerfile, 1))
                self.assert_no_warnings_logged()

    @kills(
        Mutation(
            dockerfile_manifest,
            'rf"{_FROM_INSTRUCTION}{OPTIONALLY_TAGGED_IMAGE_REFERENCE}"',
            'rf"{_FROM_INSTRUCTION}\\$?\\{{?{OPTIONALLY_TAGGED_IMAGE_REFERENCE}"',
            "a variable's name is read as an image, so its own name is queried as a registry host",
        )
    )
    def test_image_name_from_a_variable_is_left_alone_and_reported(self):
        """Test that a `FROM` taking its image name from a variable is reported and not looked up.

        One case per spelling a Dockerfile takes, the braces being optional.
        """
        for reference in ("$BASE_IMAGE", "${OTHER_IMAGE}"):
            with self.subTest(reference=reference):
                self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST))
                mock_dockerfile = mock_path(f"FROM {reference}\n")
                self.run_updater(mock_dockerfile)
                self.assert_left_alone_unqueried(mock_dockerfile, reference, "", 1, ImageNotLookedUp.THROUGH_A_VARIABLE)

    def test_tag_from_a_build_argument_is_updated_on_its_arg_line(self):
        """Test that a tag taken from a global `ARG` gets its new version there, reported as Docker builds on it."""
        dockerfile = "ARG V={}\nFROM python:$V\n"
        mock_dockerfile = self.assert_update_written("3.15", dockerfile.format("3.14"), dockerfile.format("3.15"))
        self.assert_logged_among_others(
            message_of(log.Logger.substituted_reference),
            dependency="python",
            tag=":$V",
            location=Location(mock_dockerfile, 2),
            substituted="python:3.14",
        )

    def test_quoted_build_argument_keeps_its_quotes(self):
        """Test that a quoted `ARG` default gets its new version inside the quotes, which Docker strips."""
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                dockerfile = f"ARG VERSION={quote}{{}}{quote}\nFROM python:$VERSION\n"
                self.assert_update_written("3.15", dockerfile.format("3.14"), dockerfile.format("3.15"))

    @kills(
        Mutation(
            dockerfile_manifest.Dockerfile._build_arguments,
            "in enumerate(lines[:first_from])",
            "in [*enumerate(lines[:first_from])][::-1]",
            "the first declaration of a build argument holds, so the new version is written onto one Docker ignores",
        )
    )
    def test_last_declaration_of_a_build_argument_gets_the_new_version(self):
        """Test that the last of two global `ARG` declarations of a name holds, so it gets the new version."""
        dockerfile = "ARG V=3.12\nARG V={}\nFROM python:$V\n"
        self.assert_update_written("3.15", dockerfile.format("3.13"), dockerfile.format("3.15"), line=3)

    @kills(
        Mutation(
            dockerfile_manifest,
            r"(?(1)\})",
            r"(?(1)[^}]*\})",
            "a variable with a fallback is read as the variable alone, so its `ARG` is rewritten",
        )
    )
    def test_variable_with_a_fallback_is_left_alone_and_reported(self):
        """Test that a `FROM` naming a build argument with a fallback, as in `${V:-3.14}`, is left alone."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        mock_dockerfile = mock_path("ARG V=3.14\nFROM python:${V:-3.14}\n")
        self.run_updater(mock_dockerfile)
        self.assert_left_alone_unqueried(
            mock_dockerfile, "python", "${V:-3.14}", 2, ImageNotLookedUp.THROUGH_A_VARIABLE
        )

    def test_reference_naming_a_variable_without_a_default_beside_one_with_is_left_alone_and_reported(self):
        """Test that a `FROM` naming a build argument without a default is left alone, though another has one."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        mock_dockerfile = mock_path("ARG IMAGE=python\nFROM $IMAGE:$TAG\n")
        self.run_updater(mock_dockerfile)
        self.assert_left_alone_unqueried(mock_dockerfile, "$IMAGE", "$TAG", 2, ImageNotLookedUp.THROUGH_A_VARIABLE)

    @kills(
        Mutation(
            dockerfile_manifest,
            r"(?P=quote)(?=\s|$)",
            r"(?P=quote)",
            "a default is read up to where its characters stop, so `3.14:x` is taken as the tag `3.14`",
        )
    )
    def test_build_argument_whose_default_runs_on_past_a_reference_is_left_alone_and_reported(self):
        """Test that a default holding more than an image name or tag, such as `3.14:x`, is not read as one."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        mock_dockerfile = mock_path("ARG V=3.14:x\nFROM python:$V\n")
        self.run_updater(mock_dockerfile)
        self.assert_left_alone_unqueried(mock_dockerfile, "python", "$V", 2, ImageNotLookedUp.THROUGH_A_VARIABLE)

    def test_build_argument_a_stage_declares_does_not_reach_a_later_from(self):
        """Test that an `ARG` declared after the first `FROM` belongs to its stage, so a later `FROM` is left alone."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        mock_dockerfile = mock_path("FROM scratch AS base\nARG VERSION=3.14\nFROM python:$VERSION\n")
        self.run_updater(mock_dockerfile)
        self.assert_left_alone_unqueried(mock_dockerfile, "python", "$VERSION", 3, ImageNotLookedUp.THROUGH_A_VARIABLE)

    def test_build_argument_written_into_different_references_is_left_alone_and_reported(self):
        """Test that an `ARG` two `FROM` lines write into different references is left alone, whatever is served."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST), docker_tag("3.15-slim", DIGEST))
        mock_dockerfile = mock_path("ARG PY=3.14\nFROM python:$PY-slim AS build\nFROM python:$PY\n")
        self.run_updater(mock_dockerfile)
        reason = ImageNotLookedUp.ARGUMENT_SHARED_WITH_ANOTHER_FROM_LINE
        self.assert_left_alone_unqueried(mock_dockerfile, "python", "3.14", 3, reason)
        self.assert_image_not_looked_up_logged("python", "3.14-slim", Location(mock_dockerfile, 2), reason)

    @kills(
        Mutation(
            update_dockerfile_base_image._refusal_reason,
            "if any(part.index in shared_lines",
            "if all(part.index in shared_lines",
            "a `FROM` sharing one build argument but not another is looked up, so it moves the shared one",
        )
    )
    def test_reference_writing_one_shared_and_one_unshared_build_argument_is_left_alone_and_reported(self):
        """Test that a `FROM` is left alone when one of its build arguments is shared, though another is not."""
        self.requests.side_effect = mock_docker_registry(
            docker_tag("3.15-alpine3.21", DIGEST), docker_tag("3.15", DIGEST)
        )
        dockerfile = "ARG PY=3.14\nARG ALP=3.20\nFROM python:$PY-alpine$ALP AS build\nFROM python:$PY\n"
        mock_dockerfile = mock_path(dockerfile)
        self.run_updater(mock_dockerfile)
        reason = ImageNotLookedUp.ARGUMENT_SHARED_WITH_ANOTHER_FROM_LINE
        self.assert_left_alone_unqueried(mock_dockerfile, "python", "3.14", 4, reason)
        self.assert_image_not_looked_up_logged("python", "3.14-alpine3.20", Location(mock_dockerfile, 3), reason)

    @kills(
        Mutation(
            update_dockerfile_base_image._shared_lines,
            "(substituted.substituted_reference, parse_marker(substituted))",
            "(substituted.substituted_reference, None)",
            "`FROM` lines sharing an `ARG` are compared without their markers, so one overwrites the other's update",
        )
    )
    def test_build_argument_written_under_different_markers_is_left_alone_and_reported(self):
        """Test that an `ARG` two `FROM` lines with different markers write into is left alone, whatever is served."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        mock_dockerfile = mock_path(
            "ARG PY=3.14\nFROM python:$PY AS build\n# update-time: ignore[update]\nFROM python:${PY}\n"
        )
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_not_called()
        self.requests.assert_not_called()
        reason = ImageNotLookedUp.ARGUMENT_SHARED_WITH_ANOTHER_FROM_LINE
        self.assert_image_not_looked_up_logged("python", "3.14", Location(mock_dockerfile, 2), reason)
        self.assert_ignored_logged("python", Location(mock_dockerfile, 4))
        self.assert_no_new_version_logged()

    @kills(
        Mutation(
            update_dockerfile_base_image._shared_lines,
            "(substituted.substituted_reference, parse_marker",
            "(substituted.text, parse_marker",
            "two `FROM` lines are compared by their text, so spelling the same reference differently refuses it",
        )
    )
    def test_build_argument_written_into_the_same_reference_twice_is_updated_once(self):
        """Test that an `ARG` two `FROM` lines write into the same reference is updated once, however they spell it.

        The new version is longer than the old one, so writing it twice shows.
        """
        self.requests.side_effect = mock_docker_registry(docker_tag("3.14.1", DIGEST))
        dockerfile = "ARG PY={}\nFROM python:$PY AS build\nFROM python:${{PY}}\n"
        mock_dockerfile = mock_path(dockerfile.format("3.14"))
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_once_with(dockerfile.format("3.14.1"))
        version = docker_hub_version("3.14.1")
        self.assert_new_version_logged_among_others("python", version, Location(mock_dockerfile, 2))
        location = Location(mock_dockerfile, 3)
        self.assert_new_version_logged_among_others("python", version, location, log.Logger._SUPPRESSING_CHANGELOG)
        self.assert_no_warnings_logged()

    def test_floating_reference_through_a_build_argument_is_pinned_to_its_version_without_a_digest(self):
        """Test that a floating reference through a build argument is pinned to the version it serves, sans digest."""
        cases = {
            "a floating tag with a label": (
                "ARG VERSION=slim\nFROM python:$VERSION\n",
                "ARG VERSION=3.14.7-slim\nFROM python:$VERSION\n",
                ("python", "slim", "3.14.7-slim"),
            ),
            "a floating tag that resolves to two version axes": (
                "ARG VERSION=lts-alpine\nFROM node:$VERSION\n",
                "ARG VERSION=24.19.0-alpine3.24\nFROM node:$VERSION\n",
                ("node", "lts-alpine", "24.19.0-alpine3.24"),
            ),
            "no tag": (
                "ARG IMAGE=python\nFROM $IMAGE\n",
                "ARG IMAGE=python\nFROM $IMAGE:3.14.7\n",
                ("python", "latest", "3.14.7"),
            ),
        }
        for case, (dockerfile, expected, (image, floating_tag, version)) in cases.items():
            with self.subTest(case):
                self.requests.side_effect = mock_docker_registry(
                    docker_tag(floating_tag, DIGEST), docker_tag(version, DIGEST)
                )
                mock_dockerfile = mock_path(dockerfile)
                self.run_updater(mock_dockerfile)
                mock_dockerfile.write_text.assert_called_once_with(expected)
                location = Location(mock_dockerfile, 2)
                self.assert_logged(
                    message_of(log.Logger.pinned_version), dependency=image, location=location, version=version
                )
                self.assert_no_warnings_logged()

    def test_version_on_the_from_line_is_updated_there_when_the_image_is_a_build_argument(self):
        """Test that a `FROM` naming its image through a build argument gets each version axis updated there.

        The first axis grows longer, so a rewrite that shifts the second one shows.
        """
        dockerfile = "ARG IMAGE=python\nFROM $IMAGE:{}\n"
        new_tag = "3.10-alpine3.21"
        self.assert_update_written(new_tag, dockerfile.format("3.9-alpine3.20"), dockerfile.format(new_tag))

    def test_image_from_a_build_argument_holding_a_repository_path_is_updated(self):
        """Test that a build argument naming an image by its repository path, such as `acme/app`, is read."""
        dockerfile = "ARG IMAGE=acme/app\nFROM $IMAGE:{}\n"
        self.assert_update_written("1.3", dockerfile.format("1.2"), dockerfile.format("1.3"), image="acme/app")

    def test_build_argument_a_tag_cannot_hold_is_left_alone_and_reported(self):
        """Test that a `FROM` whose build argument holds a `/` where the tag stands is left alone, as unreadable."""
        self.requests.side_effect = mock_docker_registry(docker_tag("1.3", DIGEST))
        mock_dockerfile = mock_path("ARG VERSION=acme/app\nFROM python:$VERSION\n")
        self.run_updater(mock_dockerfile)
        self.assert_left_alone_unqueried(mock_dockerfile, "python", "$VERSION", 2, ImageNotLookedUp.THROUGH_A_VARIABLE)

    def test_version_from_a_build_argument_is_updated_on_its_arg_line_beside_a_label_from_another(self):
        """Test that a tag taking its version from one build argument and its label from another is updated."""
        arguments = "ARG IMAGE_NAME=sonarqube\nARG IMAGE_VERSION={}\nARG IMAGE_EDITION=developer\n\n"
        reference = "FROM $IMAGE_NAME:$IMAGE_VERSION-$IMAGE_EDITION\n"
        dockerfile, expected = arguments.format("2026.3.1") + reference, arguments.format("2026.4.1") + reference
        self.assert_update_written("2026.4.1-developer", dockerfile, expected, image="sonarqube", line=5)

    def test_version_written_partly_by_a_build_argument_is_left_alone_and_reported(self):
        """Test that a version the `FROM` line writes in part, and a build argument in part, is not looked up."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        mock_dockerfile = mock_path("ARG MINOR=14\nFROM python:3.$MINOR\n")
        self.run_updater(mock_dockerfile)
        self.assert_left_alone_unqueried(mock_dockerfile, "python", "3.14", 2, ImageNotLookedUp.VERSION_ACROSS_LINES)

    @kills(
        Mutation(
            update_dockerfile_base_image._Refusal.__post_init__,
            "explaining_skipped_lookups(self, lambda _pinned: self.reason)",
            "pass",
            "a refused reference does not say why it is never looked up, so a redundant marker names another reason",
        )
    )
    def test_stale_scope_on_a_refused_reference_is_reported_as_redundant_because_of_the_refusal(self):
        """Test that `set[stale-after=0]` is redundant for the reason a reference is left alone: its build arguments."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        mock_dockerfile = mock_path("ARG MINOR=14\n# update-time: set[stale-after=0]\nFROM python:3.$MINOR\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_not_called()
        self.requests.assert_not_called()
        reason = ImageNotLookedUpReason(ImageNotLookedUp.VERSION_ACROSS_LINES, Consequence.STALENESS_NEVER_CHECKED)
        self.assert_redundant_directive_logged(reason, "python", Location(mock_dockerfile, 3), "set[stale-after=0]")

    def test_digest_beside_a_build_argument_is_left_alone_and_reported(self):
        """Test that a `FROM` pinning a digest beside a build argument is left alone, as the digest overrules it."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        mock_dockerfile = mock_path(f"ARG PY=3.14\nFROM python:$PY@{DIGEST1}\n")
        self.run_updater(mock_dockerfile)
        reason = ImageNotLookedUp.DIGEST_OVERRULES_BUILD_ARGUMENTS
        self.assert_left_alone_unqueried(mock_dockerfile, "python", "$PY", 2, reason)

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
            dockerfile_manifest,
            "re.IGNORECASE | re.MULTILINE",
            "re.MULTILINE",
            "a stage introduced with a lower-case `as` is not recognised as a stage",
        ),
        Mutation(
            dockerfile_manifest,
            r"(?i:FROM)",
            r"FROM",
            "a lower-case `from` introduces no base image, so its line is left as it is",
        ),
        Mutation(
            dockerfile_manifest,
            r"(?i:ARG)",
            r"ARG",
            "a lower-case `arg` does not declare a build argument, so a `FROM` naming it is left as it is",
        ),
    )
    def test_lower_case_keyword_is_read_as_the_upper_case_one(self):
        """Test that a lower-case `from`, `as`, and `arg` are read as Dockerfile keywords, which are case-insensitive.

        A stage that a lower-case `as` names is one too, so the `FROM` naming it is left alone.
        """
        cases = {
            "a lower-case from": ("from python:3.14\n", f"from python:3.15@{DIGEST}\n", 1),
            "a lower-case as": (
                "FROM python:3.14 as deps\nFROM deps\n",
                f"FROM python:3.15@{DIGEST} as deps\nFROM deps\n",
                1,
            ),
            "a lower-case arg": ("arg V=3.14\nFROM python:$V\n", "arg V=3.15\nFROM python:$V\n", 2),
        }
        for case, (dockerfile, expected, line) in cases.items():
            with self.subTest(case):
                self.requests.side_effect = mock_docker_registry(
                    docker_tag("latest", DIGEST), docker_tag("3.15", DIGEST)
                )
                mock_dockerfile = mock_path(dockerfile)
                self.run_updater(mock_dockerfile)
                mock_dockerfile.write_text.assert_called_once_with(expected)
                self.assert_new_version_logged("python", docker_hub_version("3.15"), Location(mock_dockerfile, line))
                self.assert_no_warnings_logged()

    @kills(
        Mutation(
            dockerfile_manifest,
            r"^\s*(?i:FROM)",
            r"(?i:FROM)",
            "a `FROM` anywhere on a line introduces a base image, so prose mentioning one is rewritten",
        )
    )
    def test_from_inside_a_comment_is_left_alone(self):
        """Test that a `FROM` that does not open its line introduces no base image, so prose is left as it is."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        mock_dockerfile = mock_path("# built FROM python:3.14\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_not_called()
        self.requests.assert_not_called()
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            dockerfile_manifest.Dockerfile._stage_names,
            'match.group("stage").lower() for match in',
            'match.group("stage") for match in',
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

    @kills(
        Mutation(
            dockerfile_manifest.Dockerfile._stage_names,
            'text = "".join(line.text for line in self.lines)\n'
            '        return frozenset(match.group("stage").lower() for match in _STAGE_NAME_RE.finditer(text))\n',
            'return frozenset(m["stage"].lower() for line in self.lines for m in _STAGE_NAME_RE.finditer(line.text))\n',
            "a stage is read per line, so one whose `AS` sits on a continuation line is looked up as an image",
        )
    )
    def test_a_stage_named_on_a_continuation_line_is_matched(self):
        """Test that a `FROM` naming a stage whose `AS` sits on the continuation line names that stage."""
        self.requests.side_effect = mock_docker_registry(docker_tag("latest", DIGEST), docker_tag("3.14.7", DIGEST))
        mock_dockerfile = mock_path("FROM python:3.14 \\\nAS deps\nFROM deps\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_once_with(f"FROM python:3.14.7@{DIGEST} \\\nAS deps\nFROM deps\n")
        self.assert_no_warnings_logged()

    def test_stage_alias_is_preserved_when_pinning(self):
        """Test that a multi-stage `FROM image:tag AS name` alias is kept intact when the image is pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.4", DIGEST))
        mock_dockerfile = mock_path("FROM ruby:3.3 AS build\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(f"FROM ruby:3.4@{DIGEST} AS build\n")
        self.assert_new_version_logged("ruby", docker_hub_version("3.4"), Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            dockerfile_manifest.pins_a_digest_beside_a_variable,
            '_ARGUMENT_VARIABLE_RE.search(line, reference.start("dependency")',
            "_ARGUMENT_VARIABLE_RE.search(line, 0",
            "a digest is read as overruling the `--platform` flag's variable, so the image is left alone",
        )
    )
    def test_platform_flag_with_build_arg_is_preserved(self):
        """Test that a `FROM --platform=$BUILDPLATFORM image:tag@digest` line is updated, the flag left untouched."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        mock_dockerfile = mock_path(f"FROM --platform=$BUILDPLATFORM python:3.14@{DIGEST1}\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(f"FROM --platform=$BUILDPLATFORM python:3.15@{DIGEST}\n")
        self.assert_new_version_logged("python", docker_hub_version("3.15"), Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            dockerfile_manifest._substituted_line,
            "_ARGUMENT_VARIABLE_RE.finditer(line.text, start, end)",
            "_ARGUMENT_VARIABLE_RE.finditer(line.text, 0, end)",
            "the `--platform` flag's variable is substituted too, so the reference is reported as through a variable",
        )
    )
    def test_platform_flag_beside_a_reference_through_a_build_argument_is_preserved(self):
        """Test that a `FROM --platform=$BUILDPLATFORM` naming a build argument gets its `ARG` line updated."""
        dockerfile = "ARG PY={}\nFROM --platform=$BUILDPLATFORM python:$PY\n"
        self.assert_update_written("3.15", dockerfile.format("3.14"), dockerfile.format("3.15"))

    def test_platform_flag_with_literal_value_and_stage_alias_is_preserved(self):
        """Test that a `FROM --platform=linux/amd64 image:tag AS name` line keeps both the flag and the stage alias."""
        self.requests.side_effect = mock_docker_registry(docker_tag("20", DIGEST))
        mock_dockerfile = mock_path("FROM --platform=linux/amd64 node:18 AS build\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(f"FROM --platform=linux/amd64 node:20@{DIGEST} AS build\n")
        self.assert_new_version_logged("node", docker_hub_version("20"), Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()

    def test_ignore_marker_leaves_base_image_untouched(self):
        """Test that a base image a preceding `# update-time: ignore[update]` comment holds back is not updated."""
        cases = {
            "a reference": ("# update-time: ignore[update]\nFROM python:3.14\n", "python", 2),
            "a reference through a build argument": (
                "ARG V=3.14\n# update-time: ignore[update]\nFROM python:$V\n",
                "python",
                3,
            ),
        }
        for case, (dockerfile, image, line) in cases.items():
            with self.subTest(case):
                self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
                mock_dockerfile = mock_path(dockerfile)
                self.run_updater(mock_dockerfile)
                mock_dockerfile.write_text.assert_not_called()
                self.assert_ignored_logged(image, Location(mock_dockerfile, line))
                self.assert_no_new_version_logged()
                self.assert_no_redundant_suppression_logged()
                self.assert_no_warnings_logged()

    def test_the_yanked_and_archived_scopes_are_reported_as_redundant(self):
        """Test that the `yanked` and `archived` scopes hold nothing back for an image, which still updates."""
        for directive, reason in (
            ("ignore[yanked]", Reason.NO_YANK_CONCEPT),
            ("ignore[archived]", Reason.NO_ARCHIVAL_SIGNAL),
        ):
            with self.subTest(directive=directive):
                self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
                marker = self.marker_line(directive)
                mock_dockerfile = mock_path(marker + self.reference("python:3.14"))
                self.run_updater(mock_dockerfile)
                mock_dockerfile.write_text.assert_called_once_with(marker + self.reference(f"python:3.15@{DIGEST}"))
                self.assert_redundant_directive_logged(reason, "python", Location(mock_dockerfile, 2), directive)

    def test_hash_drift_scope_on_a_reference_through_a_build_argument_is_reported_as_redundant(self):
        """Test that `allow[hash-drift]` is redundant on a reference naming a build argument: it never gets a digest."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
        dockerfile = "ARG V={}\n# update-time: allow[hash-drift]\nFROM python:$V\n"
        mock_dockerfile = mock_path(dockerfile.format("3.14"))
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_once_with(dockerfile.format("3.15"))
        location = Location(mock_dockerfile, 3)
        self.assert_redundant_directive_logged(Reason.NO_HASH_PIN, "python", location, "allow[hash-drift]")

    def test_redundant_yank_scope_in_a_combined_bracket_names_the_scope_alone(self):
        """Test that a yank scope sharing a bracket is named on its own, without the other item in that bracket."""
        self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
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
        marker = self.marker_line("set[cooldown=30] allow[hash-drift]")
        mock_dockerfile = mock_path(marker + self.reference(f"ghcr.io/owner/python:3.14@{DIGEST1}"))
        self.run_updater(mock_dockerfile)
        self.assert_redundant_directive_logged(
            Reason.NO_COOLDOWN_DATES, "ghcr.io/owner/python", Location(mock_dockerfile, 2), "set[cooldown=30]"
        )

    def test_vulnerable_scope_is_reported_as_redundant(self):
        """Test that each `vulnerable` marker is reported when an image has no vulnerability to silence."""
        directives = (
            "set[vulnerability-level=none]",
            "ignore[vulnerable=GHSA-2gwj-7jmv-h26r]",
            "set[vulnerability-level=high]",
        )
        for directive in directives:
            with self.subTest(directive=directive):
                self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
                mock_dockerfile = mock_path(f"# update-time: {directive}\nFROM python:3.14\n")
                self.run_updater(mock_dockerfile)
                mock_dockerfile.write_text.assert_called_with(
                    f"# update-time: {directive}\nFROM python:3.15@{DIGEST}\n"
                )
                self.assert_redundant_directive_logged(
                    Reason.NO_VULNERABILITY_REPORTS, "python", Location(mock_dockerfile, 2), directive
                )

    def test_redundant_vulnerable_scope_beside_another_ignore_names_the_scope_alone(self):
        """Test that a `vulnerable` scope is named on its own, without an `ignore` beside it that freezes the image."""
        for directive in (
            "set[vulnerability-level=none]",
            "ignore[vulnerable=GHSA-2gwj-7jmv-h26r]",
            "set[vulnerability-level=high]",
        ):
            with self.subTest(directive=directive):
                self.requests.side_effect = mock_docker_registry(docker_tag("3.15", DIGEST))
                mock_dockerfile = mock_path(f"# update-time: ignore[update] {directive}\nFROM python:3.14\n")
                self.run_updater(mock_dockerfile)
                mock_dockerfile.write_text.assert_not_called()  # `ignore[update]` holds the update back
                self.assert_redundant_directive_logged(
                    Reason.NO_VULNERABILITY_REPORTS, "python", Location(mock_dockerfile, 2), directive
                )

    def test_label_prefixed_base_image_bumped_and_pinned(self):
        """Test that a label-prefixed base image (ghcr.io/astral-sh/uv:python3.12-...) is bumped and pinned."""
        self.requests.side_effect = mock_docker_registry(docker_tag("python3.13-bookworm-slim", DIGEST))
        mock_dockerfile = mock_path("FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim\n")
        self.run_updater(mock_dockerfile)
        mock_dockerfile.write_text.assert_called_with(f"FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@{DIGEST}\n")
        self.assert_new_version_logged("ghcr.io/astral-sh/uv", "python3.13-bookworm-slim", Location(mock_dockerfile, 1))
        self.assert_no_warnings_logged()


def _scanned_dockerfiles(*tree: str) -> list[str]:
    """Return the files of the tree that the Dockerfile scan finds, once for each pattern that keeps the file."""

    def glob(self: Path, pattern: str, *, case_sensitive: bool | None = None, **_: object) -> list[Path]:
        return [self / path for path in tree if PurePath(path).full_match(pattern, case_sensitive=case_sensitive)]

    with patch("pathlib.Path.cwd", return_value=Path("/")), patch("pathlib.Path.glob", autospec=True, side_effect=glob):
        return [str(path.relative_to("/")) for path in glob_for(file_type.DOCKERFILES)]


class ScannedDockerfilesTest(unittest.TestCase):
    """Unit tests for which Dockerfiles are scanned."""

    def test_the_conventional_names_are_scanned(self):
        """Test that a `Dockerfile` or `Containerfile` is scanned, alone or with a purpose before or after it."""
        tree = (
            "Dockerfile",
            "python.Dockerfile",
            "web.dockerfile",
            "Dockerfile.dev",
            "app/dockerfile",
            "Containerfile",
            "python.Containerfile",
            "web.containerfile",
            "Containerfile.dev",
            "app/containerfile",
        )
        self.assertCountEqual(_scanned_dockerfiles(*tree), tree)

    def test_a_lowercase_dockerfile_or_containerfile_with_an_extension_is_skipped(self):
        """Test that a lowercase `dockerfile.<extension>` or `containerfile.<extension>` is skipped."""
        tree = (
            "dockerfile",
            "dockerfile.py",
            "src/pkg/dockerfile.py",
            "dockerfile.dev",
            "containerfile.py",
            "containerfile.dev",
        )
        self.assertEqual(_scanned_dockerfiles(*tree), ["dockerfile"])

    def test_a_devcontainer_dockerfile_is_scanned(self):
        """Test that a `Dockerfile` in a `.devcontainer` folder or in one of its configuration folders is scanned."""
        tree = ".devcontainer/Dockerfile", ".devcontainer/python/Dockerfile", ".devcontainer/node/dockerfile"
        self.assertCountEqual(_scanned_dockerfiles(*tree), tree)

    def test_a_hidden_dockerfile_is_scanned(self):
        """Test that a hidden `.<purpose>.Dockerfile` or `.<purpose>.Containerfile` is scanned, in any case."""
        tree = ".gitpod.dockerfile", "pkg/.web.Dockerfile", ".gitpod.containerfile"
        self.assertCountEqual(_scanned_dockerfiles(*tree), tree)

    @kills(
        Mutation(
            file_type,
            'Dockerfile" for folder in _DEVCONTAINER_FOLDERS',
            'Dockerfile" for folder in (".*", ".*/*")',
            "the Dockerfiles in every hidden folder are scanned, such as the ones under `.git`",
        )
    )
    def test_a_dockerfile_in_another_hidden_folder_is_skipped(self):
        """Test that a `Dockerfile` in a hidden folder other than `.devcontainer` is skipped."""
        self.assertEqual(_scanned_dockerfiles("Dockerfile", ".docker/Dockerfile"), ["Dockerfile"])
