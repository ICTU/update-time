"""Render the sample log lines the README quotes, by logging them through Update-time's own `Logger`.

Logging those samples here rather than transcribing them into the template is what keeps them true: a reworded
message rewrites the README on the next `just readme`, instead of leaving it quoting output the tool no longer
produces. The lines are rendered as the plain `LEVEL message` the README's `console` blocks show, so unlike the
screenshot (see `generate_log_svg`) they carry neither colour nor a timestamp.

An elided value stands in where a real one would be noise: a digest, a commit, and an integrity hash say only that
two of them differ, and their full length would wrap the line several times over.
"""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packaging.version import InvalidVersion

from tools.log_fixtures import VULNERABILITY, reference, resolved, stale_publication_date
from update_time.domain.dependency import (
    Archival,
    ArchivedSubject,
    DependencyVersion,
    FloatingPin,
    ImageNotLookedUp,
    Project,
    Release,
    Yank,
)
from update_time.domain.reference import DriftedPin, RefKind
from update_time.domain.staleness import STALE_AFTER
from update_time.io.console import undelimited
from update_time.io.log import Logger
from update_time.markers.directive import Consequence, ImageNotLookedUpReason, Reason
from update_time.markers.marker import Marker, Scope, Threshold, parse_directives
from update_time.primitives.location import Location
from update_time.sources.github import BEYOND_THE_COMMITS_EXAMINED

_ELIDED = "…"
_ELIDED_DIGEST = f"sha256:{_ELIDED}"
_ELIDED_INTEGRITY_HASH = f"sha256-{_ELIDED}"


class _Capture(logging.Handler):
    """Collect the records logged to it as the `LEVEL message` lines the README's console blocks show."""

    def __init__(self) -> None:
        """Start with nothing collected."""
        super().__init__()
        self._lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record and keep it, dropping the delimiters that only the highlighter reads."""
        self._lines.append(f"{record.levelname} {undelimited(record.getMessage())}")

    def take(self) -> str:
        """Return the lines collected since the previous call, as one block, and start a new one."""
        block, self._lines = "\n".join(self._lines), []
        return block


def sample_log_lines() -> dict[str, str]:
    """Return the README's sample log blocks, keyed by the placeholder each one fills.

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
        return _blocks(logger, capture)
    finally:
        logger.log.removeHandler(capture)


def _redundant_directives(
    log: Logger, capture: _Capture, requirements: Location, dockerfile: Location
) -> dict[str, str]:
    """Log a sample per warning about a marker directive that holds nothing back, paired with the block's placeholder.

    A redundancy warning is handed the directive it names, so most samples pass one as text; the two about a
    vulnerability suppression read theirs off a marker, which is what their warnings take.
    """
    suppression = Marker(ignored_advisories=frozenset({"CVE-2022-28346"}))
    log.redundant_vulnerable_advisory(reference("django", requirements, "4.2.0"), suppression)
    redundant_vulnerable_advisory = capture.take()

    level = Marker(vulnerable=Threshold(value="high", directive="set[vulnerability-level=high]"))
    log.redundant_vulnerable_level(reference("django", requirements, "4.2.0"), level, "high", "low")
    redundant_vulnerable_level = capture.take()

    log.redundant_directive(
        reference("python", dockerfile), "set[vulnerability-level=none]", Reason.NO_VULNERABILITY_REPORTS
    )
    redundant_vulnerable_source = capture.take()

    log.redundant_directive(reference("python", dockerfile), "ignore[yanked]", Reason.NO_YANK_CONCEPT)
    redundant_yank_scope = capture.take()

    log.redundant_directive(
        reference("humanize", requirements), "ignore[yanked]", Reason.NO_VERSION_TO_CHECK_FOR_A_YANK
    )
    redundant_yank_without_a_version = capture.take()

    log.redundant_directive(reference("python", dockerfile), "ignore[archived]", Reason.NO_ARCHIVAL_SIGNAL)
    redundant_archived_scope = capture.take()

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
    log.redundant_directive(
        reference("nl.example:internal-lib", Location(Path("pom.xml"), 12)),
        "set[vulnerability-level=none]",
        Reason.NO_RESOLVED_VERSION_TO_CHECK_FOR_A_VULNERABILITY,
    )
    log.redundant_directive(
        reference("actions/checkout", Location(Path(".github/workflows/ci.yml"), 17)),
        "set[vulnerability-level=none]",
        Reason.NO_NAMED_VERSION_TO_CHECK_FOR_A_VULNERABILITY,
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
    redundant_frozen_floating_pin = capture.take()

    log.redundant_directive(reference("python", dockerfile), "allow[hash-drift]", Reason.DRIFT_NEVER_ADOPTED)
    redundant_frozen_hash_drift = capture.take()

    built = reference("acme/api", Location(Path("docker-compose.yml"), 4), "dev")
    log.redundant_directive(
        built, "allow[floating-pin]", ImageNotLookedUpReason(ImageNotLookedUp.BUILT_IMAGE, Consequence.TAG_NEVER_PINNED)
    )
    return {
        "@@REDUNDANT_VULNERABLE_ADVISORY_WARNING@@": redundant_vulnerable_advisory,
        "@@REDUNDANT_VULNERABLE_LEVEL_WARNING@@": redundant_vulnerable_level,
        "@@REDUNDANT_VULNERABLE_SOURCE_WARNING@@": redundant_vulnerable_source,
        "@@REDUNDANT_YANK_SCOPE_WARNING@@": redundant_yank_scope,
        "@@REDUNDANT_YANK_WITHOUT_A_VERSION_WARNING@@": redundant_yank_without_a_version,
        "@@REDUNDANT_ARCHIVED_SCOPE_WARNING@@": redundant_archived_scope,
        "@@REDUNDANT_ARCHIVED_WITHOUT_A_GITHUB_REPOSITORY_WARNING@@": redundant_archived_without_a_github_repository,
        "@@REDUNDANT_UNSERVED_MAVEN_ARTEFACT_WARNING@@": redundant_unserved_maven_artefact,
        "@@REDUNDANT_VULNERABLE_WITHOUT_A_VERSION_WARNINGS@@": redundant_vulnerable_without_a_version,
        "@@REDUNDANT_SCOPE_ON_A_MANAGED_VERSION_WARNING@@": redundant_scope_on_a_managed_version,
        "@@REDUNDANT_COOLDOWN_ITEM_WARNING@@": redundant_cooldown_item,
        "@@REDUNDANT_STALE_SOURCE_WARNING@@": redundant_stale_source,
        "@@REDUNDANT_STALE_NOT_LOOKED_UP_WARNING@@": redundant_stale_not_looked_up,
        "@@REDUNDANT_FLOATING_PIN_WARNING@@": redundant_floating_pin,
        "@@REDUNDANT_RANGE_FLOATING_PIN_WARNING@@": redundant_range_floating_pin,
        "@@REDUNDANT_FROZEN_FLOATING_PIN_WARNING@@": redundant_frozen_floating_pin,
        "@@REDUNDANT_FROZEN_HASH_DRIFT_WARNING@@": redundant_frozen_hash_drift,
        "@@REDUNDANT_NOT_PULLED_FLOATING_PIN_WARNING@@": capture.take(),
    }


def _redundant_package_json_markers(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log a sample per redundancy warning particular to a package.json marker, paired with the placeholder."""
    log.redundant_directive(
        reference("bats", Location(Path("package.json"), 4)), "set[stale-after=0]", Reason.NO_NPM_RELEASE
    )
    unserved = capture.take()

    undeclared = reference("lodash", Location(Path("package.json"), 9))
    log.redundant_directive(undeclared, "set[stale-after=0]", Reason.UNDECLARED_DEPENDENCY)
    return {
        "@@REDUNDANT_UNSERVED_NPM_DEPENDENCY_WARNING@@": unserved,
        "@@REDUNDANT_UNDECLARED_DEPENDENCY_WARNING@@": capture.take(),
    }


def _redundant_for_the_dependency_type(
    log: Logger, capture: _Capture, requirements: Location, workflow: Location
) -> dict[str, str]:
    """Log a sample per redundant cooldown, drift opt-in, and bound, paired with the block's placeholder."""
    managed = reference("humanize", Location(Path("pyproject.toml"), 12))
    loose = reference("humanize", requirements)
    commit = reference("actions/checkout", workflow)
    branch = reference("actions/setup-python", Location(Path(".github/workflows/ci.yml"), 18))
    held_elsewhere = reference("commons-io:commons-io", Location(Path("child/pom.xml"), 5))
    plugin = reference("org.apache.maven.plugins:maven-surefire-plugin", Location(Path("pom.xml"), 30))
    log.redundant_directive(managed, "set[cooldown=30]", Reason.COOLDOWN_PER_RUN)
    log.redundant_directive(loose, "set[cooldown=30]", Reason.NO_VERSION_TO_UPDATE)
    log.redundant_directive(commit, "set[cooldown=30]", Reason.NO_COOLDOWN_FOR_A_COMMIT)
    log.redundant_directive(held_elsewhere, "set[cooldown=30]", Reason.VERSION_HELD_ELSEWHERE)
    variable = reference("redis", Location(Path("docker-compose.yml"), 7))
    never_updated = ImageNotLookedUpReason(ImageNotLookedUp.THROUGH_A_VARIABLE, Consequence.NEVER_UPDATED)
    log.redundant_directive(variable, "set[cooldown=30]", never_updated)
    cooldowns = capture.take()

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
    drift = capture.take()

    log.redundant_directive(managed, "allow[update<5]", Reason.BOUND_DECIDES_NOTHING)
    log.redundant_directive(managed, "ignore[update]", Reason.MANAGER_RESOLVES_THE_VERSION)
    log.redundant_directive(loose, "allow[update<5]", Reason.NO_VERSION_TO_UPDATE)
    log.redundant_directive(commit, "allow[update<5]", Reason.PINS_A_COMMIT)
    log.redundant_directive(branch, "allow[update<6]", Reason.FOLLOWS_A_BRANCH)
    log.redundant_directive(held_elsewhere, "ignore[major-update]", Reason.VERSION_HELD_ELSEWHERE)
    log.redundant_directive(plugin, "allow[update<4]", Reason.PLUGIN_NOT_UPDATED)
    return {
        "@@REDUNDANT_COOLDOWN_WARNINGS@@": cooldowns,
        "@@REDUNDANT_HASH_DRIFT_WARNINGS@@": drift,
        "@@REDUNDANT_BOUND_WARNINGS@@": capture.take(),
    }


def _invalid_items(log: Logger, capture: _Capture, dockerfile: Location) -> dict[str, str]:
    """Log a sample per warning about an item Update-time cannot read, paired with the block's placeholder."""
    log.invalid_bracket_item("python", "stlae", dockerfile)
    unrecognised = capture.take()

    log.invalid_bracket_item("node", "update-time.engines.node", Location(Path("package.json"), 3))
    return {"@@UNRECOGNISED_ITEM_WARNING@@": unrecognised, "@@INVALID_FIELD_WARNING@@": capture.take()}


def _directives_asking_for_the_default(
    log: Logger, capture: _Capture, requirements: Location, dockerfile: Location
) -> dict[str, str]:
    """Log a directive asking for what Update-time does without it, on an image and on a requirement."""
    log.redundant_directive(reference("python", dockerfile), "ignore[hash-drift]", Reason.ASKS_FOR_THE_DEFAULT)
    log.redundant_directive(reference("humanize", requirements), "allow[update]", Reason.ASKS_FOR_THE_DEFAULT)
    return {"@@DIRECTIVES_ASKING_FOR_THE_DEFAULT@@": capture.take()}


def _bounds_deciding_nothing(log: Logger, capture: _Capture, dockerfile: Location) -> dict[str, str]:
    """Log a bound that never has an effect and one that blocks every update, and pair the lines."""
    for directives in ("allow[update>=3.12]", "ignore[update>=3.12]"):
        log.warn_if_redundant_bound(reference("python", dockerfile, "3.12"), parse_directives(directives))
    return {"@@BOUNDS_DECIDING_NOTHING@@": capture.take()}


def _unreadable_reactors(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log a sample per error about a reactor Maven cannot read, paired with the block's placeholder."""
    log.unreadable_reactor_since_update(Path(), Path("dubbo-dependencies-bom/pom.xml"), 50)
    log.unreadable_reactor_as_found(Path(), 118)
    with_a_cause = capture.take()

    log.unreadable_reactor(Path(), 50)
    without_a_cause = capture.take()

    log.unparsable_reactor(Path(), Path("sub/pom.xml"), 118)
    return {
        "@@UNREADABLE_REACTORS@@": with_a_cause,
        "@@UNREADABLE_REACTOR_WITHOUT_A_CAUSE@@": without_a_cause,
        "@@UNPARSABLE_REACTOR@@": capture.take(),
    }


def _references_left_alone(log: Logger, capture: _Capture, dockerfile: Location) -> dict[str, str]:
    """Log a reference of each kind left alone: whose image is not looked up, or whose repository is not served."""
    compose = reference("acme/api", Location(Path("docker-compose.yml"), 4), "1.2.3")
    log.image_not_looked_up(compose, ImageNotLookedUp.BUILT_IMAGE)
    machine = reference("ubuntu-2204", Location(Path(".circleci/config.yml"), 4), "2024.01.1")
    log.image_not_looked_up(machine, ImageNotLookedUp.MACHINE_EXECUTOR_IMAGE)
    log.image_not_looked_up(reference("scratch", Location(Path("Dockerfile"), 1)), ImageNotLookedUp.EMPTY_BASE_IMAGE)
    log.image_not_looked_up(reference("deps", dockerfile), ImageNotLookedUp.BUILD_STAGE)
    variable = reference("redis", Location(Path("docker-compose.yml"), 7), "7.2-${VARIANT}")
    log.image_not_looked_up(variable, ImageNotLookedUp.THROUGH_A_VARIABLE)
    log.image_not_looked_up(reference("python", dockerfile, "3.14"), ImageNotLookedUp.VERSION_ACROSS_LINES)
    log.image_not_looked_up(
        reference("python", dockerfile, "3.14"), ImageNotLookedUp.ARGUMENT_SHARED_WITH_ANOTHER_FROM_LINE
    )
    log.image_not_looked_up(reference("python", dockerfile, "$PY"), ImageNotLookedUp.DIGEST_OVERRULES_BUILD_ARGUMENTS)
    not_looked_up = capture.take()
    log.repository_not_served(reference("spark-rm-base", dockerfile, "3.5.1"))
    return {"@@IMAGES_NOT_LOOKED_UP@@": not_looked_up, "@@REPOSITORY_NOT_SERVED@@": capture.take()}


def _build_argument_samples(log: Logger, capture: _Capture, dockerfile: Location) -> dict[str, str]:
    """Log a reference naming build arguments as Docker builds on it, and a floating default pinned to its version."""
    log.substituted_reference(reference("python", dockerfile, "$PY"), "python:3.14")
    substituted = capture.take()
    log.pinned_version(reference("python", dockerfile, "$PY"), DependencyVersion("3.14.7"))
    return {"@@SUBSTITUTED_REFERENCE@@": substituted, "@@PINNED_VERSION@@": capture.take()}


def _unpinned_floating_tags(log: Logger, capture: _Capture) -> str:
    """Log one floating tag for each reason Update-time leaves it unpinned, and return the lines they render as."""
    compose = Location(Path("docker-compose.yml"), 7)
    log.unpinned_floating_tag(
        reference("acme/api", compose, "dev"), DependencyVersion("dev"), FloatingPin.NO_VERSION_TAG
    )
    log.unpinned_floating_tag(
        reference("acme/api", compose, "nightly"), DependencyVersion("nightly"), FloatingPin.NOT_LISTED
    )
    log.unpinned_floating_tag(
        reference("acme/api", compose, "canary"), DependencyVersion("canary"), FloatingPin.NOT_AMONG_EXAMINED
    )
    ghcr = Location(Path("Dockerfile"), 1)
    log.unpinned_floating_tag(
        reference("ghcr.io/acme/api", ghcr, "latest"), DependencyVersion("latest"), FloatingPin.NO_VERSION_TAG_EXAMINED
    )
    log.unpinned_floating_tag(
        reference("ghcr.io/acme/api", ghcr, "edge"), DependencyVersion("edge"), FloatingPin.NO_MANIFEST
    )
    return capture.take()


def _no_release_tagged(log: Logger, capture: _Capture, workflow: Location) -> dict[str, str]:
    """Log that a reference was left as it is, since its repository does not tag a release, and pair the line."""
    log.no_release_tagged(reference("actions/checkout", workflow, "1"))
    return {"@@NO_RELEASE_TAGGED@@": capture.take()}


def _github_refs(log: Logger, capture: _Capture, workflow: Location) -> dict[str, str]:
    """Log a branch kept floating, refs left unpinned, unchecked drift, and undated commits, with placeholders."""
    branch = reference("actions/checkout", workflow, "main")
    directive, rate_limited = "allow[floating-pin]", "HTTP 403, API rate limit exceeded"
    log.keeping_ref(branch, DependencyVersion("4.3.0", sha=_ELIDED), directive, RefKind.BRANCH)
    kept = capture.take()
    log.unpinned_ref(branch, rate_limited, RefKind.REF)
    unpinned = capture.take()
    log.unpinned_ref(branch, BEYOND_THE_COMMITS_EXAMINED, RefKind.BRANCH)
    beyond = capture.take()
    log.unchecked_branch_drift(branch, _ELIDED, rate_limited)
    unchecked = capture.take()
    log.undated_commit("actions/checkout", "v4.4.0", "4.3.0", rate_limited)
    undated = capture.take()
    moved = DriftedPin("actions/checkout", "4.1.1", workflow, _ELIDED, new_sha=_ELIDED)
    log.no_commit_date(Logger.TAG_DRIFT, moved, rate_limited)
    return {
        "@@KEPT_BRANCH@@": kept,
        "@@UNPINNED_REF@@": unpinned,
        "@@BRANCH_BEYOND_THE_COMMITS_EXAMINED@@": beyond,
        "@@UNCHECKED_BRANCH_DRIFT@@": unchecked,
        "@@UNDATED_TAGGED_COMMIT@@": undated,
        "@@NO_COMMIT_DATE@@": capture.take(),
    }


def _undated_pushes(log: Logger, capture: _Capture, dockerfile: Location) -> str:
    """Log a newer tag and a re-pushed digest whose push date Docker Hub failed to report, and return their lines."""
    log.undated_push("python", "3.15.0", "3.14.6", "HTTP 429")
    log.no_push_date(
        Logger.DIGEST_DRIFT, DriftedPin("python", "3.14", dockerfile, _ELIDED, new_sha=_ELIDED), "HTTP 429"
    )
    return capture.take()


def _blocks(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log each block's sample records and pair the lines they render as with the block's placeholder."""
    log.drift(
        Logger.DIGEST_DRIFT,
        DriftedPin("python", "3.14", Location(Path("Dockerfile"), 1), _ELIDED_DIGEST, new_sha=_ELIDED_DIGEST),
    )
    workflow = Location(Path(".github/workflows/ci.yml"), 17)
    log.drift(Logger.TAG_DRIFT, DriftedPin("actions/checkout", "4.1.1", workflow, _ELIDED, new_sha=_ELIDED))
    log.drift(Logger.BRANCH_DRIFT, DriftedPin("actions/checkout", "main", workflow, _ELIDED, new_sha=_ELIDED))
    log.drift(Logger.REF_DRIFT, DriftedPin("actions/checkout", "stable-2024", workflow, _ELIDED, new_sha=_ELIDED))
    clipboard = reference("clipboard", Location(Path("docs/conf.py"), 4), "2.0.11")
    log.hash_mismatch(clipboard, _ELIDED_INTEGRITY_HASH, _ELIDED_INTEGRITY_HASH)
    drift = capture.take()

    requirements = Location(Path("docs/requirements.txt"), 12)
    dockerfile = Location(Path("Dockerfile"), 2)

    published = stale_publication_date()
    stale = DependencyVersion("4.15.0", project=Project(newest=Release("4.15.0", published)))
    log.report_staleness(resolved("humanize", requirements, stale), STALE_AFTER.get())
    staleness = capture.take()

    yanked = DependencyVersion("4.15.0", yank=Yank(yanked=True, reason="accidentally broke Python 3.10 support"))
    log.report_yank(resolved("humanize", requirements, yanked), Marker())
    yank = capture.take()

    log.vulnerable_dependency(reference("django", requirements, "3.2.0"), VULNERABILITY)
    vulnerable = capture.take()

    log.unreadable_affected_versions("GHSA-1111-2222-3333", InvalidVersion("Invalid version: 'unknown'"))
    unreadable_affected_versions = capture.take()

    archived_project = DependencyVersion.unpinned(Project(archival=Archival(archived=True)))
    log.report_archival(resolved("aioredis", requirements, archived_project), Marker())
    repository = Archival(archived=True, subject=ArchivedSubject.REPOSITORY)
    archived_repository = DependencyVersion.unpinned(Project(archival=repository))
    log.report_archival(resolved("actions/setup-ruby", workflow, archived_repository), Marker())
    archival = capture.take()

    redundant = _redundant_directives(log, capture, requirements, dockerfile)

    redundant_for_the_type = _redundant_for_the_dependency_type(log, capture, requirements, workflow)

    invalid = _invalid_items(log, capture, dockerfile)

    pinned_tag = DependencyVersion("3.14.7", sha=_ELIDED_DIGEST)
    log.pinned(reference("python", Location(Path("Dockerfile"), 1)), pinned_tag)
    pinned_floating_tag = capture.take()

    pinned_url = DependencyVersion("2.0.11", sha=_ELIDED_INTEGRITY_HASH)
    log.pinned(reference("clipboard", Location(Path("docs/conf.py"), 4)), pinned_url)
    pinned_integrity_hash = capture.take()

    maven_runs = _unreadable_reactors(log, capture)

    unpinned_floating_tag = _unpinned_floating_tags(log, capture)

    log.keeping_floating_tag(reference("python", dockerfile, "latest"), pinned_tag, "allow[floating-pin]")
    kept_floating_tag = capture.take()

    github_refs = _github_refs(log, capture, workflow)

    undated_pushes = _undated_pushes(log, capture, dockerfile)

    log.no_release_metadata("humanize", "4.16.0", "4.15.0", "HTTP 503")
    no_release_metadata = capture.take()

    rate_limits = _rate_limits(log, capture)

    held_back_by_cooldown = _held_back_by_cooldown(log, capture, dockerfile)

    understood_markers = _understood_markers(log, capture, requirements)
    return {
        "@@DRIFT_WARNINGS@@": drift,
        "@@PINNED_FLOATING_TAG@@": pinned_floating_tag,
        "@@PINNED_INTEGRITY_HASH@@": pinned_integrity_hash,
        **maven_runs,
        **_property_moved_for_other_artefacts(log, capture),
        "@@UNPINNED_FLOATING_TAG@@": unpinned_floating_tag,
        "@@KEPT_FLOATING_TAG@@": kept_floating_tag,
        **github_refs,
        **_no_release_tagged(log, capture, workflow),
        "@@NO_RELEASE_METADATA@@": no_release_metadata,
        **_uv_dependencies_not_updated(log, capture),
        **_manifests_without_dependencies(log, capture),
        **_range_rewrites(log, capture),
        **_uv_cooldown_not_set(log, capture),
        **rate_limits,
        "@@HELD_BACK_BY_COOLDOWN@@": held_back_by_cooldown,
        "@@NO_PUSH_DATE@@": undated_pushes,
        **_references_left_alone(log, capture, dockerfile),
        **_build_argument_samples(log, capture, dockerfile),
        "@@STALE_WARNING@@": staleness,
        "@@YANKED_WARNING@@": yank,
        "@@VULNERABILITY_WARNING@@": vulnerable,
        "@@UNREADABLE_AFFECTED_VERSIONS_WARNING@@": unreadable_affected_versions,
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


def _redundant_suppressions(
    log: Logger, capture: _Capture, requirements: Location, workflow: Location
) -> dict[str, str]:
    """Log a sample per warning about a staleness threshold, yank, or archival directive that decides nothing."""
    recent = Release("4.15.0", datetime.now(UTC) - timedelta(days=30))
    standing = DependencyVersion("4.15.0", yank=Yank(), project=Project(newest=recent, archival=Archival()))
    log.report_yank(resolved("humanize", requirements, standing), Marker(ignored_scopes=Scope.YANKED))
    yank = capture.take()
    log.report_archival(resolved("humanize", requirements, standing), Marker(ignored_scopes=Scope.ARCHIVED))
    active_repository = DependencyVersion.unpinned(Project(archival=Archival(subject=ArchivedSubject.REPOSITORY)))
    log.report_archival(
        resolved("actions/checkout", workflow, active_repository), Marker(ignored_scopes=Scope.ARCHIVED)
    )
    archival = capture.take()
    threshold = Threshold(value=90, directive="set[stale-after=90]")
    log.redundant_stale_threshold(reference("humanize", requirements), threshold, recent, STALE_AFTER.get())
    return {
        "@@REDUNDANT_YANK_SUPPRESSION_WARNING@@": yank,
        "@@REDUNDANT_ARCHIVED_SUPPRESSION_WARNINGS@@": archival,
        "@@REDUNDANT_STALE_THRESHOLD_WARNING@@": capture.take(),
    }


def _manifests_without_dependencies(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log that npm and uv do not run for a manifest that does not declare dependencies, and pair the lines."""
    log.no_dependencies(Path("ui/static/package.json"), "npm", "install")
    package_json = capture.take()
    log.no_dependencies(Path("pyproject.toml"), "uv", "resolve")
    return {"@@NO_NODE_DEPENDENCIES@@": package_json, "@@NO_PYTHON_DEPENDENCIES@@": capture.take()}


def _range_rewrites(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log that npm rewrote a range where the lock file held its version already, and where it held none."""
    package_json = Location(Path("dev/package.json"), 5)
    log.range_rewrite("npm", reference("minimatch", package_json, "^3.1.2"), "^3.1.5", "3.1.5")
    log.range_rewrite("npm", reference("minimatch", package_json, "^3.0.0"), "^3.1.5", "")
    return {"@@RANGE_REWRITES@@": capture.take()}


def _uv_cooldown_not_set(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log that a pyproject.toml whose tool tables cannot hold the cooldown gets none, and pair the line."""
    log.uv_cooldown_not_set(Path("pyproject.toml"))
    return {"@@UV_COOLDOWN_NOT_SET@@": capture.take()}


def _property_moved_for_other_artefacts(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log the warning about an unserved artefact whose property Maven moved, and pair the line."""
    mistaken = reference("org.glassfish.jersey:jersey-client", Location(Path("pom.xml"), 206))
    log.new_version_not_reported(mistaken, "jersey.version", "3.1.12")
    return {"@@PROPERTY_MOVED_FOR_OTHER_ARTEFACTS@@": capture.take()}


def _uv_dependencies_not_updated(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log that the dependencies in a pyproject.toml are not updated, because `uv tree` failed, and pair the line."""
    log.uv_dependencies_not_updated(Path("pyproject.toml"))
    return {"@@UV_DEPENDENCIES_NOT_UPDATED@@": capture.take()}


def _rate_limits(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log which credential variables are set, and that the anonymous GitHub rate limit is used up."""
    log.credentials({"GITHUB_TOKEN": True, "DOCKER_HUB_USERNAME": True, "DOCKER_HUB_TOKEN": False})  # nosec B105
    credentials = capture.take()
    log.github_rate_limit_used_up(60, datetime(2026, 10, 17, 15, 42, tzinfo=UTC))
    return {"@@CREDENTIALS@@": credentials, "@@GITHUB_RATE_LIMIT_USED_UP@@": capture.take()}


def _held_back_by_cooldown(log: Logger, capture: _Capture, dockerfile: Location) -> str:
    """Log a version the run's cooldown holds back, and one a marker's cooldown holds back.

    The publication date is fixed rather than counted back from now, so the README renders the same on every run.
    """
    published = datetime(2026, 10, 8, 13, 1, tzinfo=UTC)
    workflow = Location(Path(".github/workflows/ci.yml"), 5)
    log.held_back_by_cooldown(reference("github/codeql-action/init", workflow), Release("4.38.3", published), 7, "")
    log.held_back_by_cooldown(reference("python", dockerfile), Release("3.15.0", published), 30, "set[cooldown=30]")
    return capture.take()


def _understood_markers(log: Logger, capture: _Capture, requirements: Location) -> dict[str, str]:
    """Log the lines confirming a marker was understood, and pair them with their placeholders."""
    marker = Marker(ignored_scopes=Scope.YANKED, raw="ignore[yanked]")
    log.recognised_marker("humanize", marker, requirements)
    recognised = capture.take()
    yanked = DependencyVersion("4.15.0", yank=Yank(yanked=True, reason=""))
    log.report_yank(resolved("humanize", requirements, yanked), marker)
    silenced = capture.take()
    log.holding_back_range(
        reference("com.google.guava:guava", Location(Path("pom.xml"), 12)), "[34,)", "ignore[update>=34]"
    )
    return {
        "@@RECOGNISED_MARKER@@": recognised,
        "@@SILENCED_WARNING@@": silenced,
        "@@RANGE_HELD_BACK@@": capture.take(),
    }
