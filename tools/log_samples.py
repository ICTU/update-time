"""Render the sample log lines the README and the index of the log messages quote, by logging them through `Logger`.

Logging those samples here rather than transcribing them into the templates is what keeps them true: a reworded
message rewrites the README and the index on the next `just readme`, instead of leaving them quoting output the tool
no longer produces. The lines are rendered as the plain `LEVEL message` the README's `console` blocks show, so unlike
the screenshot (see `generate_log_svg`) they carry neither colour nor a timestamp.

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
from update_time.domain.reference import DriftedPin, DriftKind, Reference, RefKind
from update_time.domain.staleness import STALE_AFTER
from update_time.io.console import MESSAGE_NAME, named_message, undelimited
from update_time.io.log import FileContent, Logger, ToolTask
from update_time.lock_files.compiled_requirements_txt import Unrecompilable
from update_time.markers.parser import parse_directives
from update_time.package_managers.maven.versions_plugin import OverrideConsequence
from update_time.primitives.command import Command
from update_time.primitives.location import Location

from .log_fixtures import VULNERABILITY, reference, resolved, stale_publication_date

_ELIDED: Final[str] = "…"
_ELIDED_DIGEST: Final[str] = f"sha256:{_ELIDED}"
_ELIDED_INTEGRITY_HASH: Final[str] = f"sha256-{_ELIDED}"


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

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record and keep it, with its name and without the delimiters only the highlighter reads."""
        line = f"{record.levelname} {named_message(record, Text(undelimited(record.getMessage()))).plain}"
        self._lines.append(line)
        if name := getattr(record, MESSAGE_NAME, ""):
            self.samples.setdefault(name, line)

    def take(self) -> str:
        """Return the lines collected since the previous call, as one block, and start a new one."""
        block, self._lines = "\n".join(self._lines), []
        return block


def _blocks(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log each block's sample records and pair the lines they render as with the block's placeholder."""
    log.drift(
        DriftedPin(
            "python",
            "3.14",
            Location(Path("Dockerfile"), 1),
            _ELIDED_DIGEST,
            kind=DriftKind.DIGEST,
            new_sha=_ELIDED_DIGEST,
        ),
    )
    workflow = Location(Path(".github/workflows/ci.yml"), 17)
    log.drift(DriftedPin("actions/checkout", "4.1.1", workflow, _ELIDED, kind=DriftKind.TAG, new_sha=_ELIDED))
    log.drift(DriftedPin("actions/checkout", "main", workflow, _ELIDED, kind=DriftKind.BRANCH, new_sha=_ELIDED))
    log.drift(DriftedPin("actions/checkout", "stable-2024", workflow, _ELIDED, kind=DriftKind.REF, new_sha=_ELIDED))
    clipboard = reference("clipboard", Location(Path("docs/conf.py"), 4), "2.0.11")
    log.hash_mismatch(clipboard, _ELIDED_INTEGRITY_HASH, _ELIDED_INTEGRITY_HASH)
    drift = capture.take()

    requirements = Location(Path("docs/requirements.txt"), 12)
    dockerfile = Location(Path("Dockerfile"), 2)

    stale = Release("4.15.0", stale_publication_date())
    log.stale_dependency(reference("humanize", requirements), stale, STALE_AFTER.get())
    staleness = capture.take()

    yanked = DependencyVersion("4.15.0", yank=Yank(yanked=True, reason="accidentally broke Python 3.10 support"))
    log.yanked_dependency(resolved("humanize", requirements, yanked))
    yank = capture.take()

    log.vulnerable_dependency(reference("django", requirements, "3.2.0"), VULNERABILITY)
    vulnerable = capture.take()

    log.archived_dependency(reference("aioredis", requirements), Archival(archived=True))
    repository = Archival(archived=True, subject=ArchivedSubject.REPOSITORY)
    log.archived_dependency(reference("actions/setup-ruby", workflow), repository)
    archival = capture.take()

    redundant = _redundant_directives(log, capture, requirements, dockerfile)

    redundant_for_the_type = _redundant_for_the_dependency_type(log, capture, requirements, workflow)

    invalid = _invalid_items(log, capture, dockerfile)

    unpinned_floating_tag = _unpinned_floating_tags(log, capture)

    understood_markers = _understood_markers(log, capture, requirements)
    return {
        "@@DRIFT_WARNINGS@@": drift,
        "@@UNPINNED_FLOATING_TAG@@": unpinned_floating_tag,
        **_references_left_alone(log, capture, dockerfile),
        "@@STALE_WARNING@@": staleness,
        "@@YANKED_WARNING@@": yank,
        "@@VULNERABILITY_WARNING@@": vulnerable,
        "@@ARCHIVED_WARNING@@": archival,
        **redundant,
        **_redundant_suppressions(log, capture, requirements, workflow),
        **redundant_for_the_type,
        **_redundant_package_json_markers(log, capture),
        **invalid,
        **_bounds_deciding_nothing(log, capture, dockerfile),
        **_directives_asking_for_the_default(log, capture, requirements, dockerfile),
        **understood_markers,
    }


def _redundant_directives(
    log: Logger, capture: _Capture, requirements: Location, dockerfile: Location
) -> dict[str, str]:
    """Log a sample per warning about a marker directive that holds nothing back, paired with the block's placeholder.

    A redundancy warning is handed the directive it names, so most samples pass one as text; the one about a risk
    level reads its own off the marker the reference carries.
    """
    suppression = Marker(ignored_advisories=frozenset({"CVE-2022-28346"}))
    django = reference("django", requirements, "4.2.0", suppression)
    log.redundant_ignore(django, Scope.VULNERABLE, suppression.advisory_directives, "4.2.0")
    redundant_vulnerable_advisory = capture.take()

    level = Marker(vulnerable=Threshold(value="high", directive="set[vulnerability-level=high]"))
    log.redundant_vulnerable_level(reference("django", requirements, "4.2.0", level), "low")
    redundant_vulnerable_level = capture.take()

    log.redundant_directive(reference("python", dockerfile), "ignore[yanked]", Reason.NO_YANK_CONCEPT)
    redundant_yank_scope = capture.take()

    log.redundant_directive(
        reference("humanize", requirements), "ignore[yanked]", Reason.NO_VERSION_TO_CHECK_FOR_A_YANK
    )
    redundant_yank_without_a_version = capture.take()

    commons_lang = reference("org.apache.commons:commons-lang3", Location(Path("pom.xml"), 12))
    log.redundant_directive(commons_lang, "ignore[archived]", Reason.NO_GITHUB_REPOSITORY)
    redundant_archived_without_a_github_repository = capture.take()

    internal_lib = reference("nl.example:internal-lib", Location(Path("pom.xml"), 12))
    log.redundant_directive(internal_lib, "set[cooldown=30]", Reason.NOT_ON_MAVEN_CENTRAL)
    redundant_unserved_maven_artefact = capture.take()

    log.redundant_directive(
        reference("humanize", requirements),
        "set[vulnerability-level=none]",
        Reason.NO_VERSION_TO_CHECK_FOR_A_VULNERABILITY,
    )
    redundant_vulnerable_without_a_version = capture.take()

    held_elsewhere = reference("commons-io:commons-io", Location(Path("child/pom.xml"), 5))
    log.redundant_directive(held_elsewhere, "set[stale-after=0]", Reason.CHECKED_AT_THE_MANAGING_DECLARATION)
    redundant_scope_on_a_managed_version = capture.take()

    log.redundant_directive(reference("ghcr.io/astral-sh/uv", dockerfile), "set[cooldown=30]", Reason.NO_COOLDOWN_DATES)
    redundant_cooldown_item = capture.take()

    log.redundant_directive(
        reference("ghcr.io/astral-sh/uv", dockerfile), "set[stale-after=90]", Reason.NO_STALENESS_DATES
    )
    redundant_stale_source = capture.take()

    variable = reference("redis", Location(Path("docker-compose.yml"), 7))
    through_a_variable = ImageNotLookedUp.THROUGH_A_VARIABLE
    stale_never_checked = ImageNotLookedUpReason(through_a_variable, Consequence.STALENESS_NEVER_CHECKED)
    log.redundant_directive(variable, "set[stale-after=90]", stale_never_checked)
    redundant_stale_not_looked_up = capture.take()

    log.redundant_directive(reference("python", dockerfile), "allow[floating-pin]", Reason.PIN_NOT_FLOATING)
    redundant_floating_pin = capture.take()

    log.redundant_directive(reference("humanize", requirements), "allow[floating-pin]", Reason.RANGE_NEVER_PINNED)
    redundant_range_floating_pin = capture.take()

    log.redundant_directive(reference("python", dockerfile), "allow[floating-pin]", Reason.NEVER_PINNED)
    redundant_held_back_floating_pin = capture.take()

    log.redundant_directive(reference("python", dockerfile), "allow[hash-drift]", Reason.DRIFT_NEVER_ADOPTED)
    redundant_held_back_hash_drift = capture.take()

    built = reference("acme/api", Location(Path("docker-compose.yml"), 4), "dev")
    log.redundant_directive(
        built, "allow[floating-pin]", ImageNotLookedUpReason(ImageNotLookedUp.BUILT_IMAGE, Consequence.TAG_NEVER_PINNED)
    )
    return {
        "@@REDUNDANT_VULNERABLE_ADVISORY_WARNING@@": redundant_vulnerable_advisory,
        "@@REDUNDANT_VULNERABLE_LEVEL_WARNING@@": redundant_vulnerable_level,
        "@@REDUNDANT_YANK_SCOPE_WARNING@@": redundant_yank_scope,
        "@@REDUNDANT_YANK_WITHOUT_A_VERSION_WARNING@@": redundant_yank_without_a_version,
        "@@REDUNDANT_ARCHIVED_WITHOUT_A_GITHUB_REPOSITORY_WARNING@@": redundant_archived_without_a_github_repository,
        "@@REDUNDANT_UNSERVED_MAVEN_ARTEFACT_WARNING@@": redundant_unserved_maven_artefact,
        "@@REDUNDANT_VULNERABLE_WITHOUT_A_VERSION_WARNINGS@@": redundant_vulnerable_without_a_version,
        "@@REDUNDANT_SCOPE_ON_A_MANAGED_VERSION_WARNING@@": redundant_scope_on_a_managed_version,
        "@@REDUNDANT_COOLDOWN_ITEM_WARNING@@": redundant_cooldown_item,
        "@@REDUNDANT_STALE_SOURCE_WARNING@@": redundant_stale_source,
        "@@REDUNDANT_STALE_NOT_LOOKED_UP_WARNING@@": redundant_stale_not_looked_up,
        "@@REDUNDANT_FLOATING_PIN_WARNING@@": redundant_floating_pin,
        "@@REDUNDANT_RANGE_FLOATING_PIN_WARNING@@": redundant_range_floating_pin,
        "@@REDUNDANT_HELD_BACK_FLOATING_PIN_WARNING@@": redundant_held_back_floating_pin,
        "@@REDUNDANT_HELD_BACK_HASH_DRIFT_WARNING@@": redundant_held_back_hash_drift,
        "@@REDUNDANT_NOT_PULLED_FLOATING_PIN_WARNING@@": capture.take(),
    }


def _redundant_for_the_dependency_type(
    log: Logger, capture: _Capture, requirements: Location, workflow: Location
) -> dict[str, str]:
    """Log a sample per redundant cooldown, drift opt-in, and bound, paired with the block's placeholder."""
    managed = reference("humanize", Location(Path("pyproject.toml"), 12))
    unmanaged = reference("humanize", Location(Path("legacy/pyproject.toml"), 4))
    unlocked = reference("left-pad", Location(Path("web/package.json"), 4))
    member = reference("humanize", Location(Path("app/pyproject.toml"), 5))
    loose = reference("humanize", requirements)
    commit = reference("actions/checkout", workflow)
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
    not_steered = capture.take()

    log.redundant_directive(managed, "set[cooldown=30]", Reason.COOLDOWN_PER_RUN)
    log.redundant_directive(commit, "set[cooldown=30]", Reason.NO_COOLDOWN_FOR_A_COMMIT)
    cooldowns = capture.take()

    drift = _redundant_hash_drift_opt_ins(log, capture, loose, commit)

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
    bounds = capture.take()

    log.redundant_directive(managed, "ignore[major-update]", Reason.NO_LOCKED_VERSION)
    log.redundant_directive(managed, "ignore[update]", Reason.SEVERAL_LOCKED_VERSIONS)
    return {
        "@@REDUNDANT_UPDATE_DIRECTIVE_WARNINGS@@": not_steered,
        "@@REDUNDANT_COOLDOWN_WARNINGS@@": cooldowns,
        "@@REDUNDANT_HASH_DRIFT_WARNINGS@@": drift,
        "@@REDUNDANT_BOUND_WARNINGS@@": bounds,
        "@@REDUNDANT_UNANCHORED_WARNINGS@@": capture.take(),
    }


def _redundant_hash_drift_opt_ins(log: Logger, capture: _Capture, loose: Reference, commit: Reference) -> str:
    """Log a sample per redundant drift opt-in, and return the lines."""
    log.redundant_directive(loose, "allow[hash-drift]", Reason.NO_HASH_PIN)
    clipboard = reference("clipboard", Location(Path("docs/conf.py"), 4))
    log.redundant_directive(clipboard, "allow[hash-drift]", Reason.INTEGRITY_MISMATCH_NEVER_ADOPTED)
    built = reference("acme/api", Location(Path("docker-compose.yml"), 4))
    log.redundant_directive(
        built,
        "allow[hash-drift]",
        ImageNotLookedUpReason(ImageNotLookedUp.BUILT_IMAGE, Consequence.DIGEST_NEVER_CHECKED),
    )
    log.redundant_directive(commit, "allow[hash-drift]", Reason.NO_DRIFT_FOR_A_COMMIT)
    return capture.take()


def _invalid_items(log: Logger, capture: _Capture, dockerfile: Location) -> dict[str, str]:
    """Log a sample per warning about an item Update-time cannot read, paired with the block's placeholder."""
    log.invalid_bracket_item(reference("python", dockerfile), "stlae")
    unrecognised = capture.take()

    log.invalid_bracket_item(reference("node", Location(Path("package.json"), 3)), "update-time.engines.node")
    return {"@@UNRECOGNISED_ITEM_WARNING@@": unrecognised, "@@INVALID_FIELD_WARNING@@": capture.take()}


def _unpinned_floating_tags(log: Logger, capture: _Capture) -> str:
    """Log a floating tag left unpinned without a version tag, and one left unpinned without a manifest."""
    compose = Location(Path("docker-compose.yml"), 7)
    log.unpinned_floating_tag(
        reference("acme/api", compose, "dev"), DependencyVersion("dev"), FloatingPin.NO_VERSION_TAG
    )
    ghcr = Location(Path("Dockerfile"), 1)
    log.unpinned_floating_tag(
        reference("ghcr.io/acme/api", ghcr, "edge"), DependencyVersion("edge"), FloatingPin.NO_MANIFEST
    )
    return capture.take()


def _understood_markers(log: Logger, capture: _Capture, requirements: Location) -> dict[str, str]:
    """Log the line confirming a marker was recognised, and pair it with its placeholder."""
    marker = Marker(ignored_scopes=Scope.YANKED, raw="ignore[yanked]")
    log.recognised_marker(reference("humanize", requirements, marker=marker))
    return {"@@RECOGNISED_MARKER@@": capture.take()}


def _references_left_alone(log: Logger, capture: _Capture, dockerfile: Location) -> dict[str, str]:
    """Log references whose image Update-time does not look up, one per reason, and pair the lines."""
    compose = reference("acme/api", Location(Path("docker-compose.yml"), 4), "1.2.3")
    log.image_not_looked_up(compose, ImageNotLookedUp.BUILT_IMAGE)
    variable = reference("redis", Location(Path("docker-compose.yml"), 7), "7.2-${VARIANT}")
    log.image_not_looked_up(variable, ImageNotLookedUp.THROUGH_A_VARIABLE)
    log.image_not_looked_up(reference("python", dockerfile, "3.14"), ImageNotLookedUp.VERSION_ACROSS_LINES)
    log.image_not_looked_up(
        reference("python", dockerfile, "3.14"), ImageNotLookedUp.ARGUMENT_SHARED_WITH_ANOTHER_FROM_LINE
    )
    log.image_not_looked_up(reference("python", dockerfile, "$PY"), ImageNotLookedUp.DIGEST_OVERRULES_BUILD_ARGUMENTS)
    return {"@@IMAGES_NOT_LOOKED_UP@@": capture.take()}


def _redundant_suppressions(
    log: Logger, capture: _Capture, requirements: Location, workflow: Location
) -> dict[str, str]:
    """Log a sample per warning about a staleness threshold, yank, or archival directive that decides nothing."""
    recent = Release("4.15.0", datetime.now(UTC) - timedelta(days=30))
    standing = DependencyVersion("4.15.0", yank=Yank())
    log.redundant_ignore(resolved("humanize", requirements, standing), Scope.YANKED, "ignore[yanked]", "4.15.0")
    yank = capture.take()
    log.redundant_ignore(
        reference("humanize", requirements), Scope.ARCHIVED, "ignore[archived]", ArchivedSubject.PROJECT
    )
    active_repository = Archival(subject=ArchivedSubject.REPOSITORY)
    log.redundant_ignore(
        reference("actions/checkout", workflow), Scope.ARCHIVED, "ignore[archived]", active_repository.subject
    )
    archival = capture.take()
    threshold = Threshold(value=90, directive="set[stale-after=90]")
    log.redundant_stale_threshold(reference("humanize", requirements), threshold, recent, STALE_AFTER.get())
    return {
        "@@REDUNDANT_YANK_SUPPRESSION_WARNING@@": yank,
        "@@REDUNDANT_ARCHIVED_SUPPRESSION_WARNINGS@@": archival,
        "@@REDUNDANT_STALE_THRESHOLD_WARNING@@": capture.take(),
    }


def _redundant_package_json_markers(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log a sample per redundancy warning particular to a package.json marker, paired with the placeholder."""
    log.redundant_directive(
        reference("bats", Location(Path("package.json"), 4)), "set[stale-after=0]", Reason.NO_NPM_RELEASE
    )
    unserved = capture.take()

    undeclared = reference("lodash", Location(Path("package.json"), 9))
    log.redundant_directive(undeclared, "set[stale-after=0]", Reason.UNDECLARED_DEPENDENCY)
    undeclared_warning = capture.take()

    catalog_dependency = reference("semver", Location(Path("packages/a/package.json"), 4))
    log.redundant_directive(catalog_dependency, "set[stale-after=0]", Reason.CHECKED_AT_THE_CATALOG)
    log.redundant_directive(catalog_dependency, "set[stale-after=0]", Reason.CATALOG_ENTRY_NOT_FOUND)
    return {
        "@@REDUNDANT_UNSERVED_NPM_DEPENDENCY_WARNING@@": unserved,
        "@@REDUNDANT_UNDECLARED_DEPENDENCY_WARNING@@": undeclared_warning,
        "@@REDUNDANT_CATALOG_DEPENDENCY_WARNING@@": capture.take(),
    }


def _bounds_deciding_nothing(log: Logger, capture: _Capture, dockerfile: Location) -> dict[str, str]:
    """Log a bound that never has an effect and one that blocks every update, and pair the lines."""
    for directives, redundancy in (
        ("allow[update>=3.12]", BoundRedundancy.NO_EFFECT),
        ("ignore[update>=3.12]", BoundRedundancy.BLOCKS_ALL),
    ):
        log.redundant_bound(reference("python", dockerfile, "3.12", parse_directives(directives)), redundancy)
    return {"@@BOUNDS_DECIDING_NOTHING@@": capture.take()}


def _directives_asking_for_the_default(
    log: Logger, capture: _Capture, requirements: Location, dockerfile: Location
) -> dict[str, str]:
    """Log a directive asking for what Update-time does without it, on an image and on a requirement."""
    log.redundant_directive(reference("python", dockerfile), "ignore[hash-drift]", Reason.ASKS_FOR_THE_DEFAULT)
    log.redundant_directive(reference("humanize", requirements), "allow[update]", Reason.ASKS_FOR_THE_DEFAULT)
    return {"@@DIRECTIVES_ASKING_FOR_THE_DEFAULT@@": capture.take()}


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
    _debug_silenced_samples(log)
    _debug_left_as_it_is_samples(log)
    _debug_project_samples(log)
    _debug_fetch_samples(log)


def _debug_held_back_samples(log: Logger) -> None:
    """Log a sample of each debug message about an update held back, the cooldown, or a pin kept floating.

    The publication date is fixed rather than counted back from now, so the index renders the same on every run.
    """
    published = datetime(2026, 10, 8, 13, 1, tzinfo=UTC)
    dockerfile = Location(Path("Dockerfile"), 2)
    workflow = Location(Path(".github/workflows/ci.yml"), 5)
    log.held_back_by_cooldown(reference("github/codeql-action/init", workflow), Release("4.38.3", published), 7, "")
    log.held_back_by_cooldown(reference("python", dockerfile), Release("3.15.0", published), 30, "set[cooldown=30]")
    log.project_cooldown(Path("package.json"), "bun minimumReleaseAge", COOLDOWN.get())
    log.keeping_floating_tag(
        reference("python", dockerfile, "latest"),
        DependencyVersion("3.14.7", sha=_ELIDED_DIGEST),
        "allow[floating-pin]",
    )
    branch = reference("actions/checkout", Location(Path(".github/workflows/ci.yml"), 17), "main")
    log.keeping_ref(branch, DependencyVersion("4.3.0", sha=_ELIDED), "allow[floating-pin]", RefKind.BRANCH)
    guava = reference("com.google.guava:guava", Location(Path("pom.xml"), 12))
    log.holding_back_range(guava, "[34,)", "ignore[update>=34]")
    requires_python = reference("python", Location(Path("pyproject.toml"), 2), ">=3.10,<3.12")
    log.held_back_by_requires_python(
        reference("python", Location(Path(".python-version"), 1), "3.11.9"), requires_python
    )
    log.ignored(
        reference("humanize", Location(Path("docs/requirements.txt"), 12), "4.15.0", parse_directives("ignore[update]"))
    )


def _debug_silenced_samples(log: Logger) -> None:
    """Log a sample of each debug message about a warning that a marker or an option silenced."""
    requirements = Location(Path("docs/requirements.txt"), 12)
    log.ignored_warning(reference("humanize", requirements), Scope.YANKED, "ignore[yanked]")
    log.ignored_warning(
        reference("django", requirements, "3.2.0"),
        Scope.VULNERABLE,
        "ignore[vulnerable=GHSA-2gwj-7jmv-h26r]",
        advisory=VULNERABILITY.advisory,
    )
    log.globally_ignored_vulnerability(
        reference("django", requirements, "3.2.0"), VULNERABILITY, frozenset({"GHSA-2gwj-7jmv-h26r"})
    )


def _debug_left_as_it_is_samples(log: Logger) -> None:
    """Log a sample of each debug message about how Update-time reads a reference, or why it leaves one as it is."""
    workflow = Location(Path(".github/workflows/ci.yml"), 17)
    log.comment_naming_no_ref(reference("actions/checkout", workflow, "pinned"))
    log.no_release_tagged(reference("actions/checkout", workflow, "1"))
    dockerfile = Location(Path("Dockerfile"), 2)
    log.substituted_reference(reference("python", dockerfile, "$PY"), "python:3.14")
    log.repository_not_served(reference("spark-rm-base", dockerfile, "3.5.1"))
    guava = reference("com.google.guava:guava", Location(Path("pom.xml"), 12), "33.0.0-jre")
    log.new_version_not_reported(guava, "guava.version", "33.5.0-jre")


def _debug_project_samples(log: Logger) -> None:
    """Log a sample of each debug message about a file or a project that Update-time does not run a tool for."""
    log.source_outside_the_scan(Path("docs/requirements.txt"), Path("vendor/requirements.in"))
    log.compiled_file_without_a_source(Path("requirements-lock.txt"))
    log.no_dependencies(Path("pyproject.toml"), "uv", "resolve")
    log.not_a_uv_project(Path("legacy/pyproject.toml"), Path("legacy/uv.lock"))
    log.project_marked_unmanaged(Path("vendored/pyproject.toml"), Path("vendored/pyproject.toml"))
    log.workspace_root_outside_the_scan(Path("pyproject.toml"), "uv", Path("../pyproject.toml"))
    log.path_source_cycle([Path("lib/pyproject.toml"), Path("app/pyproject.toml")])
    log.no_lock_file(Path("web/package.json"), "npm", Path("web/package-lock.json"))
    log.left_with_the_workspace_root(Path("packages/ui/package.json"), "yarn", Path("package.json"))
    log.updated_by_the_workspace_root(Path("packages/ui/package.json"), "bun", Path("package.json"))
    log.maven_not_run(Path("sub/pom.xml"))
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
    dockerfile = Location(Path("Dockerfile"), 2)
    log.pinned_version(reference("python", dockerfile, "$PY"), DependencyVersion("3.14.7"))
    log.cannot_pin(reference("clipboard", Location(Path("docs/conf.py"), 4)))
    log.version_moved_back("uv", reference("humanize", Location(Path("docs/requirements.in"), 2), "4.16.0"), "4.11.0")
    compiled = Path("docs/requirements.txt")
    log.compiled_file_left_as_it_is(compiled, [Path("docs/requirements.in")], Unrecompilable.OTHER_OUTPUT)
    log.configured_compiled_file_cooldown(Path("docs/requirements.in"), compiled, "7 days", "uv exclude-newer")
    log.configured_uv_cooldown(Path("pyproject.toml"), "7 days")
    log.new_version(reference("internal-lib", Location(Path("pyproject.toml"), 2)), DependencyVersion("1.3.0"))
    log.pin_with_environment_marker_left_as_is(reference("humanize", Location(Path("pyproject.toml"), 3), "4.15.0"))
    package_json = Location(Path("dev/package.json"), 5)
    log.range_rewrite("npm", reference("minimatch", package_json, "^3.1.2"), "^3.1.5", "3.1.5")
    log.range_rewrite("npm", reference("minimatch", package_json, "^3.0.0"), "^3.1.5", "")
    workflow = Location(Path(".github/workflows/ci.yml"), 17)
    tag_drift = DriftedPin("actions/checkout", "4.1.1", workflow, _ELIDED, kind=DriftKind.TAG, new_sha=_ELIDED)
    log.adopted_drift(tag_drift, "allow[hash-drift]")
    dockerfile = Location(Path("Dockerfile"), 1)
    digest_drift = DriftedPin(
        "python", "3.14", dockerfile, _ELIDED_DIGEST, kind=DriftKind.DIGEST, new_sha=_ELIDED_DIGEST
    )
    log.adopted_drift(digest_drift, "allow[hash-drift]")


def _index_warning_samples(log: Logger) -> None:
    """Log a sample of each warning the README does not quote."""
    requirements = Location(Path("docs/requirements.txt"), 12)
    log.forced_outside_git_repository(Path("/home/user/project"))
    log.missing_excluded_path(Path("vendor"))
    recent = Release("4.15.0", datetime.now(UTC) - timedelta(days=30))
    threshold = Threshold(value=90, directive="set[stale-after=90]")
    log.redundant_stale_threshold(reference("humanize", requirements), threshold, recent, 180)
    level = Marker(vulnerable=Threshold(value="high", directive="set[vulnerability-level=high]"))
    log.redundant_vulnerable_level(reference("django", requirements, "4.2.0", level), "moderate")
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
    log.uv_settings_unreadable(compiled)
    log.settings_unreadable(compiled, "pip", "uploaded-prior-to", "[Errno 2] No such file or directory: 'pip'")
    log.uv_cooldown_not_set(Path("pyproject.toml"))
    log.unsupported_package_manager(Path("pyproject.toml"), "poetry", "uv")
    log.signals_of_more_than_one_manager(Path("pyproject.toml"), [Path("uv.lock"), Path("poetry.lock")], [])
    log.binary_lock(Path("package.json"), Path("bun.lockb"), Path("bun.lock"))
    log.yarn_without_a_cooldown(Path("package.json"), Path(), "1.22.22")
    log.tool_version_unknown(
        "yarn", Path(), "Usage Error: Parse error when loading .yarnrc.yml; please check it's proper Yaml"
    )
    directory = Path("test/node/esm")
    log.files_of_more_than_one_manager(
        directory / "package.json", [directory / "pnpm-lock.yaml", directory / "bun.lockb"]
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
    log.command_not_found(Command("pnpm", "update", "-r"))
    log.command_error(Command("npm", "update"), PermissionError(13, "Permission denied", "npm"))
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
    log.tool_failed(compiled, ToolTask.RECOMPILE, "uv pip compile")
    log.tool_not_on_path(compiled, ToolTask.RECOMPILE, "uv")
    log.rolled_back_with_cycle(Path("lib/pyproject.toml"), [Path("app/pyproject.toml")])


def _uv_failures(log: Logger) -> None:
    """Log a sample of each failure that leaves the dependencies of a file uv locks as they were."""
    log.lock_not_read(Path("uv.lock"), PermissionError(13, "Permission denied", "uv.lock"))
    log.file_not_written(Path("pyproject.toml"), PermissionError(13, "Permission denied", "pyproject.toml"))
    log.file_not_restored(Path("uv.lock"), PermissionError(13, "Permission denied", "uv.lock"))
    log.lock_not_removed(Path("tool.py.lock"), PermissionError(13, "Permission denied", "tool.py.lock"))


def _error_maven_samples(log: Logger) -> None:
    """Log a sample of each error about a pom or the reactor it is part of."""
    log.unreadable_reactor(Path(), 50)
    log.unreadable_reactor_as_found(Path(), 118)
    log.unreadable_reactor_since_update(Path(), Path("dubbo-dependencies-bom/pom.xml"), 50)
    log.unparsable_reactor(Path(), Path("sub/pom.xml"), 118)
    log.invalid_xml_after_update(Path("pom.xml"))
    log.declarations_changed(Path("pom.xml"), 12, 11)


def _error_source_samples(log: Logger) -> None:
    """Log a sample of each error about a source that did not answer what an update needs."""
    rate_limited = "HTTP 403, API rate limit exceeded"
    log.github_rate_limit_used_up(60, datetime(2026, 10, 17, 15, 42, tzinfo=UTC))
    log.github_token_rate_limit_used_up(5000, datetime(2026, 10, 17, 15, 42, tzinfo=UTC))
    log.no_release_metadata("humanize", "4.16.0", "4.15.0", "HTTP 503")
    url = "https://github.com/actions/checkout/releases/tag/v4.3.0"
    log.no_commit_sha("actions/checkout", "v4.3.0", rate_limited, url)
    workflow = Location(Path(".github/workflows/ci.yml"), 17)
    branch = reference("actions/checkout", workflow, "main")
    log.unpinned_ref(branch, rate_limited, RefKind.REF)
    log.unchecked_branch_drift(branch, _ELIDED, rate_limited)
    log.undated_commit("actions/checkout", "v4.4.0", "4.3.0", rate_limited)
    moved = DriftedPin("actions/checkout", "4.1.1", workflow, _ELIDED, kind=DriftKind.TAG, new_sha=_ELIDED)
    log.undated_drift(moved, rate_limited)
    dockerfile = Location(Path("Dockerfile"), 2)
    log.undated_push("python", "3.15.0", "3.14.6", "HTTP 429")
    log.undated_drift(
        DriftedPin("python", "3.14", dockerfile, _ELIDED, kind=DriftKind.DIGEST, new_sha=_ELIDED), "HTTP 429"
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _LogSamples:
    """The sample log lines: the README's blocks, by the placeholder each one fills, and the index's line per name."""

    blocks: dict[str, str]
    by_name: dict[str, str]
