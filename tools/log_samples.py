"""Render the sample log lines the README and the index of the log messages quote, by logging them through `Logger`.

Logging those samples here rather than transcribing them into the templates is what keeps them true: a reworded
message rewrites the README and the index on the next `just readme`, instead of leaving them quoting output the tool
no longer produces.

An elided value stands in where a real one would be noise: a digest, a commit, and an integrity hash say only that
two of them differ, and their full length would wrap the line several times over.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from signal import Signals
from typing import Final

from packaging.version import InvalidVersion
from requests import Response
from rich.text import Text

from update_time.domain.bound import BoundRedundancy
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import (
    Archival,
    ArchivedSubject,
    DependencyVersion,
    FloatingPin,
    ImageNotLookedUp,
    Release,
    Yank,
)
from update_time.domain.directive import Consequence, ImageNotLookedUpReason, Reason
from update_time.domain.marker import Marker, Scope, Threshold
from update_time.domain.reference import DriftedPin, DriftKind, RefKind
from update_time.domain.staleness import STALE_AFTER
from update_time.io.console import MESSAGE_NAME, named_message, undelimited
from update_time.io.log import FileContent, Logger, NotRunCause, ReactorCause, ToolFailure, ToolTask
from update_time.lock_files.compiled_requirements_txt import Unrecompilable
from update_time.markers.parser import parse_directives
from update_time.package_managers.maven.versions_plugin import OverrideConsequence
from update_time.primitives.command import Command
from update_time.primitives.location import Location

from .log_fixtures import VULNERABILITY, reference, resolved, stale_publication_date

_ELIDED: Final[str] = "…"
_ELIDED_DIGEST: Final[str] = f"sha256:{_ELIDED}"
_ELIDED_INTEGRITY_HASH: Final[str] = f"sha256-{_ELIDED}"
# The locations most samples name.
_DOCKERFILE: Final[Location] = Location(Path("Dockerfile"), 2)
_REQUIREMENTS: Final[Location] = Location(Path("docs/requirements.txt"), 12)
_WORKFLOW: Final[Location] = Location(Path(".github/workflows/ci.yml"), 17)


def sample_log_lines() -> _LogSamples:
    """Return the README's sample log blocks, and the sample of each name the index of the log messages shows.

    The samples log to a logger of their own, which collects them and passes them on to nobody: they are the
    README's content, not diagnostics about generating it, and its own level keeps them independent of whatever
    logging the generator configured before it.
    """
    logger = Logger("update-time.readme-samples")
    capture = _Capture()
    logger.log.addHandler(capture)
    logger.log.setLevel(logging.DEBUG)
    logger.log.propagate = False
    try:
        blocks = _blocks(logger, capture)
        _index_samples(logger, capture)
        return _LogSamples(blocks=blocks, by_name=capture.samples)
    finally:
        logger.log.removeHandler(capture)


class _Capture(logging.Handler):
    """Collect the records logged to it as the `LEVEL message` lines the README's console blocks show.

    It also keeps the first line logged for each name, which the index of the log messages shows as the name's sample.
    """

    def __init__(self) -> None:
        """Start with nothing collected."""
        super().__init__()
        self._lines: list[str] = []
        self.samples: dict[str, str] = {}
        self.blocks: dict[str, str] = {}

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record and keep it, with its name and without the delimiters only the highlighter reads."""
        line = f"{record.levelname} {named_message(record, Text(undelimited(record.getMessage()))).plain}"
        self._lines.append(line)
        if name := getattr(record, MESSAGE_NAME, ""):
            self.samples.setdefault(name, line)

    def block(self, placeholder: str) -> None:
        """Keep the lines collected since the previous block as the block of the placeholder, and start a new one."""
        self.blocks[placeholder] = self.take()

    def take(self) -> str:
        """Return the lines collected since the previous call, as one block, and start a new one."""
        block, self._lines = "\n".join(self._lines), []
        return block


def _blocks(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log each block's sample records, and return the lines they render as under each block's placeholder."""
    log.drift(
        DriftedPin(
            "python",
            "3.14",
            Location(Path("Dockerfile"), 1),
            _ELIDED_DIGEST,
            kind=DriftKind.DIGEST,
            new_sha=_ELIDED_DIGEST,
        )
    )
    log.drift(DriftedPin("actions/checkout", "4.1.1", _WORKFLOW, _ELIDED, kind=DriftKind.TAG, new_sha=_ELIDED))
    log.drift(DriftedPin("actions/checkout", "main", _WORKFLOW, _ELIDED, kind=DriftKind.BRANCH, new_sha=_ELIDED))
    log.drift(DriftedPin("actions/checkout", "stable-2024", _WORKFLOW, _ELIDED, kind=DriftKind.REF, new_sha=_ELIDED))
    clipboard = reference("clipboard", Location(Path("docs/conf.py"), 4), "2.0.11")
    log.hash_mismatch(clipboard, _ELIDED_INTEGRITY_HASH, _ELIDED_INTEGRITY_HASH)
    capture.block("@@DRIFT_WARNINGS@@")

    stale = Release("4.15.0", stale_publication_date())
    log.stale_dependency(reference("humanize", _REQUIREMENTS), stale, STALE_AFTER.get())
    capture.block("@@STALE_WARNING@@")

    yanked = DependencyVersion("4.15.0", yank=Yank(yanked=True, reason="accidentally broke Python 3.10 support"))
    log.yanked_dependency(resolved("humanize", _REQUIREMENTS, yanked))
    capture.block("@@YANKED_WARNING@@")

    log.vulnerable_dependency(reference("django", _REQUIREMENTS, "3.2.0"), VULNERABILITY)
    capture.block("@@VULNERABILITY_WARNING@@")

    log.archived_dependency(reference("aioredis", _REQUIREMENTS), Archival(archived=True))
    repository = Archival(archived=True, subject=ArchivedSubject.REPOSITORY)
    log.archived_dependency(reference("actions/setup-ruby", _WORKFLOW), repository)
    capture.block("@@ARCHIVED_WARNING@@")

    for samples in (
        _redundant_directives,
        _redundant_for_the_dependency_type,
        _invalid_items,
        _unpinned_floating_tags,
        _understood_markers,
        _references_left_alone,
        _redundant_suppressions,
        _redundant_package_json_markers,
        _bounds_deciding_nothing,
        _directives_asking_for_the_default,
    ):
        samples(log, capture)
    return capture.blocks


def _redundant_directives(log: Logger, capture: _Capture) -> None:
    """Log a sample per warning about a marker directive that holds nothing back, under the block's placeholder.

    A redundancy warning is handed the directive it names, so most samples pass one as text; the one about a risk
    level reads its own off the marker the reference carries.
    """
    suppression = Marker(ignored_advisories=frozenset({"CVE-2022-28346"}))
    django = reference("django", _REQUIREMENTS, "4.2.0", suppression)
    log.redundant_ignore(django, Scope.VULNERABLE, suppression.advisory_directives, "4.2.0")
    capture.block("@@REDUNDANT_VULNERABLE_ADVISORY_WARNING@@")

    level = Marker(vulnerable=Threshold(value="high", directive="set[vulnerability-level=high]"))
    log.redundant_vulnerable_level(reference("django", _REQUIREMENTS, "4.2.0", level), "low")
    capture.block("@@REDUNDANT_VULNERABLE_LEVEL_WARNING@@")

    log.redundant_directive(reference("python", _DOCKERFILE), "ignore[yanked]", Reason.NO_YANK_CONCEPT)
    capture.block("@@REDUNDANT_YANK_SCOPE_WARNING@@")

    log.redundant_directive(
        reference("humanize", _REQUIREMENTS), "ignore[yanked]", Reason.NO_VERSION_TO_CHECK_FOR_A_YANK
    )
    capture.block("@@REDUNDANT_YANK_WITHOUT_A_VERSION_WARNING@@")

    commons_lang = reference("org.apache.commons:commons-lang3", Location(Path("pom.xml"), 12))
    log.redundant_directive(commons_lang, "ignore[archived]", Reason.NO_GITHUB_REPOSITORY)
    capture.block("@@REDUNDANT_ARCHIVED_WITHOUT_A_GITHUB_REPOSITORY_WARNING@@")

    internal_lib = reference("nl.example:internal-lib", Location(Path("pom.xml"), 12))
    log.redundant_directive(internal_lib, "set[cooldown=30]", Reason.NOT_ON_MAVEN_CENTRAL)
    capture.block("@@REDUNDANT_UNSERVED_MAVEN_ARTEFACT_WARNING@@")

    log.redundant_directive(
        reference("humanize", _REQUIREMENTS),
        "set[vulnerability-level=none]",
        Reason.NO_VERSION_TO_CHECK_FOR_A_VULNERABILITY,
    )
    capture.block("@@REDUNDANT_VULNERABLE_WITHOUT_A_VERSION_WARNINGS@@")

    held_elsewhere = reference("commons-io:commons-io", Location(Path("child/pom.xml"), 5))
    log.redundant_directive(held_elsewhere, "set[stale-after=0]", Reason.CHECKED_AT_THE_MANAGING_DECLARATION)
    capture.block("@@REDUNDANT_SCOPE_ON_A_MANAGED_VERSION_WARNING@@")

    log.redundant_directive(
        reference("ghcr.io/astral-sh/uv", _DOCKERFILE), "set[cooldown=30]", Reason.NO_COOLDOWN_DATES
    )
    capture.block("@@REDUNDANT_COOLDOWN_ITEM_WARNING@@")

    log.redundant_directive(
        reference("ghcr.io/astral-sh/uv", _DOCKERFILE), "set[stale-after=90]", Reason.NO_STALENESS_DATES
    )
    capture.block("@@REDUNDANT_STALE_SOURCE_WARNING@@")

    variable = reference("redis", Location(Path("docker-compose.yml"), 7))
    through_a_variable = ImageNotLookedUp.THROUGH_A_VARIABLE
    stale_never_checked = ImageNotLookedUpReason(through_a_variable, Consequence.STALENESS_NEVER_CHECKED)
    log.redundant_directive(variable, "set[stale-after=90]", stale_never_checked)
    capture.block("@@REDUNDANT_STALE_NOT_LOOKED_UP_WARNING@@")

    log.redundant_directive(reference("python", _DOCKERFILE), "allow[floating-pin]", Reason.PIN_NOT_FLOATING)
    capture.block("@@REDUNDANT_FLOATING_PIN_WARNING@@")

    log.redundant_directive(reference("humanize", _REQUIREMENTS), "allow[floating-pin]", Reason.RANGE_NEVER_PINNED)
    capture.block("@@REDUNDANT_RANGE_FLOATING_PIN_WARNING@@")

    log.redundant_directive(reference("python", _DOCKERFILE), "allow[floating-pin]", Reason.NEVER_PINNED)
    capture.block("@@REDUNDANT_HELD_BACK_FLOATING_PIN_WARNING@@")

    log.redundant_directive(reference("python", _DOCKERFILE), "allow[hash-drift]", Reason.DRIFT_NEVER_ADOPTED)
    capture.block("@@REDUNDANT_HELD_BACK_HASH_DRIFT_WARNING@@")

    built = reference("acme/api", Location(Path("docker-compose.yml"), 4), "dev")
    log.redundant_directive(
        built, "allow[floating-pin]", ImageNotLookedUpReason(ImageNotLookedUp.BUILT_IMAGE, Consequence.TAG_NEVER_PINNED)
    )
    capture.block("@@REDUNDANT_NOT_PULLED_FLOATING_PIN_WARNING@@")


def _redundant_for_the_dependency_type(log: Logger, capture: _Capture) -> None:
    """Log a sample per redundant cooldown, drift opt-in, and bound, under the block's placeholder."""
    managed = reference("humanize", Location(Path("pyproject.toml"), 12))
    unmanaged = reference("humanize", Location(Path("legacy/pyproject.toml"), 4))
    unlocked = reference("left-pad", Location(Path("web/package.json"), 4))
    member = reference("humanize", Location(Path("app/pyproject.toml"), 5))
    loose = reference("humanize", _REQUIREMENTS)
    commit = reference("actions/checkout", _WORKFLOW)
    branch = reference("actions/setup-python", Location(Path(".github/workflows/ci.yml"), 18))
    held_elsewhere = reference("commons-io:commons-io", Location(Path("child/pom.xml"), 5))
    plugin = reference("org.apache.maven.plugins:maven-surefire-plugin", Location(Path("pom.xml"), 30))
    under_rules = reference("com.google.guava:guava", Location(Path("pom.xml"), 6))
    log.redundant_directive(loose, "allow[update<5]", Reason.NO_VERSION_TO_UPDATE)
    log.redundant_directive(held_elsewhere, "set[cooldown=30]", Reason.VERSION_HELD_ELSEWHERE)
    log.redundant_directive(plugin, "allow[update<4]", Reason.PLUGIN_NOT_UPDATED)
    log.redundant_directive(under_rules, "ignore[update]", Reason.RULES_OF_THE_POM)
    log.redundant_directive(unmanaged, "ignore[update]", Reason.UNMANAGED_FILE_LEFT)
    log.redundant_directive(unlocked, "set[cooldown=30]", Reason.NO_LOCK_FILE_LEFT)
    log.redundant_directive(member, "allow[update<5]", Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_KEPT)
    variable = reference("redis", Location(Path("docker-compose.yml"), 7))
    never_updated = ImageNotLookedUpReason(ImageNotLookedUp.THROUGH_A_VARIABLE, Consequence.NEVER_UPDATED)
    log.redundant_directive(variable, "allow[update<8]", never_updated)
    log.redundant_directive(commit, "ignore[update]", Reason.NO_UPDATE_FOR_A_COMMIT)
    capture.block("@@REDUNDANT_UPDATE_DIRECTIVE_WARNINGS@@")

    log.redundant_directive(managed, "set[cooldown=30]", Reason.COOLDOWN_PER_RUN)
    log.redundant_directive(commit, "set[cooldown=30]", Reason.NO_COOLDOWN_FOR_A_COMMIT)
    capture.block("@@REDUNDANT_COOLDOWN_WARNINGS@@")

    log.redundant_directive(loose, "allow[hash-drift]", Reason.NO_HASH_PIN)
    clipboard = reference("clipboard", Location(Path("docs/conf.py"), 4))
    log.redundant_directive(clipboard, "allow[hash-drift]", Reason.INTEGRITY_MISMATCH_NEVER_ADOPTED)
    built = reference("acme/api", Location(Path("docker-compose.yml"), 4))
    never_checked = ImageNotLookedUpReason(ImageNotLookedUp.BUILT_IMAGE, Consequence.DIGEST_NEVER_CHECKED)
    log.redundant_directive(built, "allow[hash-drift]", never_checked)
    log.redundant_directive(commit, "allow[hash-drift]", Reason.NO_DRIFT_FOR_A_COMMIT)
    capture.block("@@REDUNDANT_HASH_DRIFT_WARNINGS@@")

    script = reference("humanize", Location(Path("tool.py"), 4))
    log.redundant_directive(script, "allow[update<5]", Reason.BOUND_DECIDES_NOTHING)
    build_requirement = reference("hatchling", Location(Path("pyproject.toml"), 3))
    log.redundant_directive(build_requirement, "allow[update<2]", Reason.BUILD_REQUIREMENT_NOT_LOCKED)
    url = reference("internal-lib", Location(Path("pyproject.toml"), 9))
    log.redundant_directive(url, "allow[update<2]", Reason.URL_RATHER_THAN_VERSION)
    log.redundant_directive(managed, "allow[update<5]", Reason.ENVIRONMENT_MARKER_PIN_KEPT)
    log.redundant_directive(managed, "allow[update<5]", Reason.BOUND_ON_A_HELD_BACK_UPDATE)
    log.redundant_directive(commit, "allow[update<5]", Reason.PINS_A_COMMIT)
    log.redundant_directive(branch, "allow[update<6]", Reason.FOLLOWS_A_BRANCH)
    capture.block("@@REDUNDANT_BOUND_WARNINGS@@")

    log.redundant_directive(managed, "ignore[major-update]", Reason.NO_LOCKED_VERSION)
    log.redundant_directive(managed, "ignore[update]", Reason.SEVERAL_LOCKED_VERSIONS)
    capture.block("@@REDUNDANT_UNANCHORED_WARNINGS@@")


def _invalid_items(log: Logger, capture: _Capture) -> None:
    """Log a sample per warning about an item Update-time cannot read, under the block's placeholder."""
    log.invalid_bracket_item(reference("python", _DOCKERFILE), "stlae")
    capture.block("@@UNRECOGNISED_ITEM_WARNING@@")
    log.invalid_bracket_item(reference("node", Location(Path("package.json"), 3)), "update-time.engines.node")
    capture.block("@@INVALID_FIELD_WARNING@@")


def _unpinned_floating_tags(log: Logger, capture: _Capture) -> None:
    """Log a floating tag left unpinned without a version tag, and one left unpinned without a manifest."""
    compose = Location(Path("docker-compose.yml"), 7)
    log.unpinned_floating_tag(
        reference("acme/api", compose, "dev"), DependencyVersion("dev"), FloatingPin.NO_VERSION_TAG
    )
    ghcr = Location(Path("Dockerfile"), 1)
    log.unpinned_floating_tag(
        reference("ghcr.io/acme/api", ghcr, "edge"), DependencyVersion("edge"), FloatingPin.NO_MANIFEST
    )
    capture.block("@@UNPINNED_FLOATING_TAG@@")


def _understood_markers(log: Logger, capture: _Capture) -> None:
    """Log the line confirming a marker was recognised, under its placeholder."""
    marker = Marker(ignored_scopes=Scope.YANKED, raw="ignore[yanked]")
    log.recognised_marker(reference("humanize", _REQUIREMENTS, marker=marker))
    capture.block("@@RECOGNISED_MARKER@@")


def _references_left_alone(log: Logger, capture: _Capture) -> None:
    """Log references whose image Update-time does not look up, one per reason, under the block's placeholder."""
    compose = reference("acme/api", Location(Path("docker-compose.yml"), 4), "1.2.3")
    log.reference_left_as_is(compose, ImageNotLookedUp.BUILT_IMAGE)
    variable = reference("redis", Location(Path("docker-compose.yml"), 7), "7.2-${VARIANT}")
    log.reference_left_as_is(variable, ImageNotLookedUp.THROUGH_A_VARIABLE)
    log.reference_left_as_is(reference("python", _DOCKERFILE, "3.14"), ImageNotLookedUp.VERSION_ACROSS_LINES)
    log.reference_left_as_is(
        reference("python", _DOCKERFILE, "3.14"), ImageNotLookedUp.ARGUMENT_SHARED_WITH_ANOTHER_FROM_LINE
    )
    log.reference_left_as_is(reference("python", _DOCKERFILE, "$PY"), ImageNotLookedUp.DIGEST_OVERRULES_BUILD_ARGUMENTS)
    capture.block("@@IMAGES_NOT_LOOKED_UP@@")


def _redundant_suppressions(log: Logger, capture: _Capture) -> None:
    """Log a sample per warning about a staleness threshold, yank, or archival directive that decides nothing."""
    recent = Release("4.15.0", datetime.now(UTC) - timedelta(days=30))
    standing = DependencyVersion("4.15.0", yank=Yank())
    log.redundant_ignore(resolved("humanize", _REQUIREMENTS, standing), Scope.YANKED, "ignore[yanked]", "4.15.0")
    capture.block("@@REDUNDANT_YANK_SUPPRESSION_WARNING@@")
    log.redundant_ignore(
        reference("humanize", _REQUIREMENTS), Scope.ARCHIVED, "ignore[archived]", ArchivedSubject.PROJECT
    )
    active_repository = Archival(subject=ArchivedSubject.REPOSITORY)
    log.redundant_ignore(
        reference("actions/checkout", _WORKFLOW), Scope.ARCHIVED, "ignore[archived]", active_repository.subject
    )
    capture.block("@@REDUNDANT_ARCHIVED_SUPPRESSION_WARNINGS@@")
    threshold = Threshold(value=90, directive="set[stale-after=90]")
    log.redundant_stale_threshold(reference("humanize", _REQUIREMENTS), threshold, recent, STALE_AFTER.get())
    capture.block("@@REDUNDANT_STALE_THRESHOLD_WARNING@@")


def _redundant_package_json_markers(log: Logger, capture: _Capture) -> None:
    """Log a sample per redundancy warning particular to a package.json marker, under the block's placeholder."""
    log.redundant_directive(
        reference("bats", Location(Path("package.json"), 4)), "set[stale-after=0]", Reason.NO_NPM_RELEASE
    )
    capture.block("@@REDUNDANT_UNSERVED_NPM_DEPENDENCY_WARNING@@")

    undeclared = reference("lodash", Location(Path("package.json"), 9))
    log.redundant_directive(undeclared, "set[stale-after=0]", Reason.UNDECLARED_DEPENDENCY)
    capture.block("@@REDUNDANT_UNDECLARED_DEPENDENCY_WARNING@@")

    catalog_dependency = reference("semver", Location(Path("packages/a/package.json"), 4))
    log.redundant_directive(catalog_dependency, "set[stale-after=0]", Reason.CHECKED_AT_THE_CATALOG)
    log.redundant_directive(catalog_dependency, "set[stale-after=0]", Reason.CATALOG_ENTRY_NOT_FOUND)
    capture.block("@@REDUNDANT_CATALOG_DEPENDENCY_WARNING@@")


def _bounds_deciding_nothing(log: Logger, capture: _Capture) -> None:
    """Log a bound that never has an effect and one that blocks every update, under the block's placeholder."""
    for directives, redundancy in (
        ("allow[update>=3.12]", BoundRedundancy.NO_EFFECT),
        ("ignore[update>=3.12]", BoundRedundancy.BLOCKS_ALL),
    ):
        log.redundant_bound(reference("python", _DOCKERFILE, "3.12", parse_directives(directives)), redundancy)
    capture.block("@@BOUNDS_DECIDING_NOTHING@@")


def _directives_asking_for_the_default(log: Logger, capture: _Capture) -> None:
    """Log a directive asking for what Update-time does without it, on an image and on a requirement."""
    log.redundant_directive(reference("python", _DOCKERFILE), "ignore[hash-drift]", Reason.ASKS_FOR_THE_DEFAULT)
    log.redundant_directive(reference("humanize", _REQUIREMENTS), "allow[update]", Reason.ASKS_FOR_THE_DEFAULT)
    capture.block("@@DIRECTIVES_ASKING_FOR_THE_DEFAULT@@")


def _index_samples(log: Logger, capture: _Capture) -> None:
    """Log a sample of each log message the README does not quote, for the index of the log messages alone."""
    _index_debug_samples(log)
    _index_info_samples(log)
    _index_warning_samples(log)
    _index_error_samples(log)
    capture.take()


def _index_debug_samples(log: Logger) -> None:
    """Log a sample of each debug message the README does not quote."""
    log.running("0.0.61", "macOS-26.1-arm64", "3.14.2")
    log.arguments(["--cooldown", "14", "--log-level", "DEBUG"])
    log.credentials({"GITHUB_TOKEN": True, "DOCKER_HUB_USERNAME": True, "DOCKER_HUB_TOKEN": False})  # nosec B105
    log.network_settings({"https": "http://proxy.example.com:3128"}, "localhost", "")
    log.path(Path("docs/requirements.txt"))
    log.excluded_path(Path("vendor"))
    log.tool_version("yarn", Path(), "4.18.1")
    yarn_up = Command("yarn", "up", "-R", "--mode=update-lockfile", "left-pad")
    log.command_run(yarn_up, Path(), {"YARN_NPM_MINIMAL_AGE_GATE": "10080"})
    _debug_held_back_samples(log)
    log.ignored_warning(reference("humanize", _REQUIREMENTS), Scope.YANKED, "ignore[yanked]")
    _debug_left_as_it_is_samples(log)
    _debug_project_samples(log)
    _debug_fetch_samples(log)


def _debug_held_back_samples(log: Logger) -> None:
    """Log a sample of each debug message about an update held back, the cooldown, or a pin kept floating.

    The publication date is fixed rather than counted back from now, so the index renders the same on every run.
    """
    published = datetime(2026, 10, 8, 13, 1, tzinfo=UTC)
    workflow = Location(Path(".github/workflows/ci.yml"), 5)
    log.held_back_by_cooldown(reference("github/codeql-action/init", workflow), Release("4.38.3", published), 7, "")
    log.project_cooldown(Path("package.json"), "bun minimumReleaseAge", COOLDOWN.get())
    log.keeping_floating_pin(
        reference("python", _DOCKERFILE, "latest"),
        DependencyVersion("3.14.7", sha=_ELIDED_DIGEST),
        "allow[floating-pin]",
    )
    requires_python = reference("python", Location(Path("pyproject.toml"), 2), ">=3.10,<3.12")
    log.held_back_by_requires_python(
        reference("python", Location(Path(".python-version"), 1), "3.11.9"), requires_python
    )
    log.ignored(
        reference("humanize", Location(Path("docs/requirements.txt"), 12), "4.15.0", parse_directives("ignore[update]"))
    )


def _debug_left_as_it_is_samples(log: Logger) -> None:
    """Log a sample of each debug message about how Update-time reads a reference, or why it leaves one as it is."""
    log.substituted_reference(reference("python", _DOCKERFILE, "$PY"), "python:3.14")
    guava = reference("com.google.guava:guava", Location(Path("pom.xml"), 12), "33.0.0-jre")
    log.new_version_not_reported(guava, "guava.version", "33.5.0-jre")


def _debug_project_samples(log: Logger) -> None:
    """Log a sample of each debug message about a file or a project that Update-time does not run a tool for."""
    log.source_outside_the_scan(Path("docs/requirements.txt"), Path("vendor/requirements.in"))
    log.compiled_file_without_a_source(Path("requirements-lock.txt"))
    log.manager_not_run(Path("pyproject.toml"), "uv", NotRunCause.NO_DEPENDENCIES_TO_RESOLVE)
    log.manager_not_run(Path("legacy/pyproject.toml"), "uv", NotRunCause.NOT_A_UV_PROJECT, Path("legacy/uv.lock"))
    log.manager_not_run(
        Path("vendored/pyproject.toml"), "uv", NotRunCause.MARKED_UNMANAGED, Path("vendored/pyproject.toml")
    )
    log.manager_not_run(
        Path("pyproject.toml"), "uv", NotRunCause.WORKSPACE_ROOT_OUTSIDE_THE_SCAN, Path("../pyproject.toml")
    )
    log.path_source_cycle([Path("lib/pyproject.toml"), Path("app/pyproject.toml")])
    log.manager_not_run(Path("web/package.json"), "npm", NotRunCause.NO_LOCK_FILE, Path("web/package-lock.json"))
    log.manager_not_run(
        Path("packages/ui/package.json"), "yarn", NotRunCause.LEFT_WITH_THE_WORKSPACE_ROOT, Path("package.json")
    )
    log.updated_by_the_workspace_root(Path("packages/ui/package.json"), "bun", Path("package.json"))
    log.manager_not_run(Path("sub/pom.xml"), "Maven", NotRunCause.REACTOR_UNREADABLE)
    log.option_overridden_by_the_pom(
        Path("pom.xml"), "rulesUri", OverrideConsequence.RULES, "org.example:parent:1.0", 12
    )


def _debug_fetch_samples(log: Logger) -> None:
    """Log a sample of each debug message about a request a source did not answer as hoped."""
    response = Response()
    response.url, response.status_code, response.reason = "https://example.com/CHANGELOG.md", 404, "Not Found"
    log.unserved_changelog(response)
    log.unserved_listing("https://repo1.maven.org/maven2/nl/example/internal-lib/", "HTTP 404")
    log.unreadable_github_rate_limit(KeyError("X-RateLimit-Reset"))


def _index_info_samples(log: Logger) -> None:
    """Log a sample of each info message the README does not quote."""
    log.pinned(reference("python", Location(Path("Dockerfile"), 1)), DependencyVersion("3.14.7", sha=_ELIDED_DIGEST))
    log.cannot_pin(reference("clipboard", Location(Path("docs/conf.py"), 4)))
    log.version_moved_back("uv", reference("humanize", Location(Path("docs/requirements.in"), 2), "4.16.0"), "4.11.0")
    compiled = Path("docs/requirements.txt")
    log.compiled_file_left_as_it_is(compiled, [Path("docs/requirements.in")], Unrecompilable.OTHER_OUTPUT)
    log.configured_cooldown(compiled, Path("docs/requirements.in"), "7 days", "uv exclude-newer")
    log.new_version(reference("internal-lib", Location(Path("pyproject.toml"), 2)), DependencyVersion("1.3.0"))
    log.pin_with_environment_marker_left_as_is(reference("humanize", Location(Path("pyproject.toml"), 3), "4.15.0"))
    package_json = Location(Path("dev/package.json"), 5)
    log.range_rewrite("npm", reference("minimatch", package_json, "^3.1.2"), "^3.1.5", "3.1.5")
    log.range_rewrite("npm", reference("minimatch", package_json, "^3.0.0"), "^3.1.5", "")
    tag_drift = DriftedPin("actions/checkout", "4.1.1", _WORKFLOW, _ELIDED, kind=DriftKind.TAG, new_sha=_ELIDED)
    log.adopted_drift(tag_drift, "allow[hash-drift]")
    dockerfile = Location(Path("Dockerfile"), 1)
    digest_drift = DriftedPin(
        "python", "3.14", dockerfile, _ELIDED_DIGEST, kind=DriftKind.DIGEST, new_sha=_ELIDED_DIGEST
    )
    log.adopted_drift(digest_drift, "allow[hash-drift]")


def _index_warning_samples(log: Logger) -> None:
    """Log a sample of each warning the README does not quote."""
    log.forced_outside_git_repository(Path("/home/user/project"))
    log.missing_excluded_path(Path("vendor"))
    recent = Release("4.15.0", datetime.now(UTC) - timedelta(days=30))
    threshold = Threshold(value=90, directive="set[stale-after=90]")
    log.redundant_stale_threshold(reference("humanize", _REQUIREMENTS), threshold, recent, 180)
    level = Marker(vulnerable=Threshold(value="high", directive="set[vulnerability-level=high]"))
    log.redundant_vulnerable_level(reference("django", _REQUIREMENTS, "4.2.0", level), "moderate")
    log.malformed_cvss_vector("GHSA-1111-2222-3333", ValueError("unknown metric 'XX'"))
    log.unreadable_affected_versions("GHSA-1111-2222-3333", InvalidVersion("Invalid version: 'unknown'"))
    log.unreadable_publication_date("com.google.guava:guava", "33.5.0-jre", "yesterday")
    log.no_integrity_hash("clipboard", "2.0.12", "dist/clipboard.min.js")
    log.unreadable_archival("actions/checkout")
    version_file = Location(Path(".python-version"), 1)
    requires_python = reference("python", Location(Path("pyproject.toml"), 2), ">=3.10,<3.12")
    log.python_version_outside_requires_python(reference("python", version_file, "3.13.2"), requires_python)
    log.invalid_requires_python(requires_python)
    _warning_file_samples(log)
    _warning_project_samples(log)
    _warning_fetch_samples(log)


def _warning_file_samples(log: Logger) -> None:
    """Log a sample of each warning about a file Update-time cannot read."""
    unreadable = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    log.unreadable_file(Path("tool.py"), unreadable, FileContent.INLINE_SCRIPT_METADATA)
    log.invalid_file(Path(".github/workflows/ci.yml"), "YAML")
    log.no_entry("node", Path("package.json"))
    log.pinned_version_unreadable(Path("docs/requirements.txt"), "humanize", "4.15.0-final")
    log.effective_pom_unreadable(Path("pom.xml"))
    log.effective_pom_without_input_locations(Path("pom.xml"))


def _warning_project_samples(log: Logger) -> None:
    """Log a sample of each warning about a project Update-time cannot run a tool for as it would."""
    compiled = Path("docs/requirements.txt")
    log.source_not_found(compiled, Path("docs/dev.in"))
    log.compiled_source_unsupported(Path("requirements-dev.txt"), Path("requirements.txt"))
    log.settings_unreadable(compiled, "pip", "uploaded-prior-to", "[Errno 2] No such file or directory: 'pip'")
    log.uv_cooldown_not_set(Path("pyproject.toml"))
    log.binary_lock(Path("package.json"), Path("bun.lockb"), Path("bun.lock"))
    log.tool_version_unknown(
        "yarn", Path(), "Usage Error: Parse error when loading .yarnrc.yml; please check it's proper Yaml"
    )
    log.command_stderr(Command("pnpm", "update", "-r"), " WARN  deprecated inflight@1.0.6")


def _warning_fetch_samples(log: Logger) -> None:
    """Log a sample of each warning about a request a source did not answer."""
    response = Response()
    url = "https://pypi.org/pypi/humanize/json"
    response.url, response.status_code, response.reason = url, 503, "Service Unavailable"
    log.request_failed(url, response)
    log.invalid_response(url, "JSON")


def _index_error_samples(log: Logger) -> None:
    """Log a sample of each error the README does not quote."""
    log.stopped(Signals.SIGINT)
    log.update_stopped("Maven dependencies", Signals.SIGTERM)
    log.command_not_run(Command("pnpm", "update", "-r"), "is pnpm installed?")
    log.command_not_run(Command("npm", "update"), str(PermissionError(13, "Permission denied", "npm")))
    output = (
        "➤ YN0000: · Yarn 4.18.1\n"
        "➤ YN0000: ┌ Resolution step\n"
        "➤ YN0001: │ RequestError: getaddrinfo ENOTFOUND registry.yarnpkg.com\n"
        "➤ YN0000: └ Completed\n"
        "➤ YN0000: · Failed with errors in 0s 75ms"
    )
    log.command_failed(Command("yarn", "up", "-R", "--mode=update-lockfile", "left-pad"), output)
    _error_python_samples(log)
    _uv_failures(log)
    _error_maven_samples(log)
    _error_source_samples(log)


def _error_python_samples(log: Logger) -> None:
    """Log a sample of each error about a compiled requirements file or projects uv locks together."""
    compiled = Path("docs/requirements.txt")
    log.tool_failed(compiled, ToolTask.RECOMPILE, "uv pip compile", ToolFailure.FAILED)
    log.tool_failed(compiled, ToolTask.RECOMPILE, "uv", ToolFailure.NOT_ON_PATH)
    log.rolled_back_with_cycle(Path("lib/pyproject.toml"), [Path("app/pyproject.toml")])


def _uv_failures(log: Logger) -> None:
    """Log a sample of each failure that leaves the dependencies of a file uv locks as they were."""
    log.lock_not_read(Path("uv.lock"), PermissionError(13, "Permission denied", "uv.lock"))
    log.file_not_written(Path("pyproject.toml"), PermissionError(13, "Permission denied", "pyproject.toml"))
    log.file_not_restored(Path("uv.lock"), PermissionError(13, "Permission denied", "uv.lock"))
    log.lock_not_removed(Path("tool.py.lock"), PermissionError(13, "Permission denied", "tool.py.lock"))


def _error_maven_samples(log: Logger) -> None:
    """Log a sample of each error about a pom or the reactor it is part of."""
    log.unreadable_reactor(Path(), ReactorCause.UPDATED_POM, 50, Path("dubbo-dependencies-bom/pom.xml"))
    log.unreadable_reactor(Path(), ReactorCause.UNPARSABLE_POM, 118, Path("sub/pom.xml"))
    log.unreadable_reactor(Path(), ReactorCause.AS_FOUND, 118)
    log.unreadable_reactor(Path(), ReactorCause.READ_EARLIER, 50)
    log.invalid_xml_after_update(Path("pom.xml"))
    log.declarations_changed(Path("pom.xml"), 12, 11)


def _error_source_samples(log: Logger) -> None:
    """Log a sample of each error about a source that did not answer what an update needs."""
    rate_limited = "HTTP 403, API rate limit exceeded"
    log.github_rate_limit_used_up(60, datetime(2026, 10, 17, 15, 42, tzinfo=UTC), authenticated=False)
    log.no_release_metadata("humanize", "4.16.0", "4.15.0", "HTTP 503")
    url = "https://github.com/actions/checkout/releases/tag/v4.3.0"
    log.no_commit_sha("actions/checkout", "v4.3.0", rate_limited, url)
    branch = reference("actions/checkout", _WORKFLOW, "main")
    log.unpinned_ref(branch, rate_limited, RefKind.REF)
    log.unchecked_branch_drift(branch, _ELIDED, rate_limited)
    moved = DriftedPin("actions/checkout", "4.1.1", _WORKFLOW, _ELIDED, kind=DriftKind.TAG, new_sha=_ELIDED)
    log.undated_drift(moved, rate_limited)


@dataclass(frozen=True, slots=True, kw_only=True)
class _LogSamples:
    """The sample log lines: the README's blocks, by the placeholder each one fills, and the index's line per name."""

    blocks: dict[str, str]
    by_name: dict[str, str]
