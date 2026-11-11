"""uv's policies for the markers of a pyproject.toml and of inline script metadata, and the report of those markers."""

from dataclasses import replace
from typing import TYPE_CHECKING

from update_time.domain.dependency import normalized_python_name
from update_time.markers.directive import Reason
from update_time.package_managers.policy import ManagerPolicy, report_markers
from update_time.package_managers.uv.uv import declarations_of

if TYPE_CHECKING:
    from update_time.domain.dependency import VersionString
    from update_time.io.log import Logger
    from update_time.lock_files.uv_lock import RecordedVersions
    from update_time.manifests.python_dependency_toml import Declaration, PythonDependencyToml
    from update_time.markers.marker import Scope


def report_file_markers(
    file: PythonDependencyToml, log: Logger, policy: ManagerPolicy[Declaration]
) -> list[Declaration]:
    """Report each declaration's marker, and return the declarations."""
    declarations = declarations_of(file)
    report_markers(declarations, log, policy)
    return declarations


def _unchecked_scopes(declaration: Declaration) -> dict[Scope, Reason]:
    """Return each warning scope that Update-time leaves unchecked for the declaration, with the reason.

    Where PyPI does not serve a release for the declaration, Update-time leaves every warning scope unchecked.
    """
    return {} if declaration.names_a_release else ManagerPolicy.every_warning_scope(Reason.NO_PYPI_RELEASE)


def _hold_back_redundancy(declaration: Declaration) -> Reason | None:
    """Return why an `ignore[update]` keeps nothing for the declaration.

    uv resolves a version the declaration does not pin. Update-time leaves the pin of a build requirement as it is, and
    a pin with an environment marker too.
    """
    if not declaration.pins_a_version:
        return Reason.MANAGER_RESOLVES_THE_VERSION
    if declaration.build_requirement:
        return Reason.BUILD_REQUIREMENT_KEPT
    return Reason.ENVIRONMENT_MARKER_PIN_LEFT if declaration.environment_marker else None


# The policy for the markers of a pyproject.toml and of an inline script metadata block, judged without a lock. uv
# resolves the version an exact pin names, so a pin Update-time leaves as the file writes it stays where it is. A bound
# narrows the lock. The policies for a run that keeps the lock, for one that removes it, and for a file Update-time
# leaves as it is build on this one.
MARKER_POLICY = ManagerPolicy(
    hold_back_redundancy=_hold_back_redundancy,
    bound_redundancy=lambda declaration: declaration.unnarrowable_reason,
    cooldown_redundancy=Reason.COOLDOWN_PER_RUN,
    unchecked_scopes=_unchecked_scopes,
)


# The policy for the markers of a pyproject.toml that uv does not manage.
UNMANAGED_FILE_POLICY = MARKER_POLICY.policy_for_files_left_as_they_are(
    Reason.UNMANAGED_FILE_LEFT, Reason.UNMANAGED_FILE_KEPT
)


# The policy for the markers of a pyproject.toml whose uv workspace root lies outside the scan root.
WORKSPACE_ROOT_OUTSIDE_THE_SCAN_POLICY = MARKER_POLICY.policy_for_files_left_as_they_are(
    Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_LEFT, Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_KEPT
)


def kept_lock_policy(locked_as_written: RecordedVersions | None) -> ManagerPolicy[Declaration]:
    """Return the policy for the markers of a member's file, where the run keeps the lock.

    An `ignore[update]` keeps a declaration where it has a version to keep: an exact pin, or the version uv locks for a
    narrowable declaration as written. Such an `ignore[update]` leaves the bound beside it nothing to decide. A level
    bound needs that version to anchor at. The versions locked as written are None where uv did not lock the files, or
    where the lock it wrote cannot be read.
    """

    def hold_back_redundancy(declaration: Declaration) -> Reason | None:
        if declaration.pins_a_version or not declaration.narrowable:
            return MARKER_POLICY.hold_back_redundancy(declaration)
        return _current_version_redundancy(declaration, locked_as_written)

    def bound_redundancy(declaration: Declaration) -> Reason | None:
        if (reason := MARKER_POLICY.bound_redundancy(declaration)) or locked_as_written is None:
            return reason
        if not declaration.updatable and hold_back_redundancy(declaration) is None:
            return Reason.BOUND_ON_A_HELD_BACK_UPDATE
        if declaration.marker.version_bound.level is None:
            return None
        return _current_version_redundancy(declaration, locked_as_written)

    return replace(MARKER_POLICY, hold_back_redundancy=hold_back_redundancy, bound_redundancy=bound_redundancy)


def _unkept_lock_bound_redundancy(declaration: Declaration) -> Reason | None:
    """Return why a bound decides nothing where the run does not keep the lock: uv resolves a range afresh each run."""
    if reason := declaration.unnarrowable_reason:
        return reason
    return None if declaration.pins_a_version else Reason.BOUND_DECIDES_NOTHING


# A run can remove the lock it created. What uv locks for a dependency without an exact pin then lasts for the run
# alone, so an `ignore[update]` or a bound on one holds nothing back. This policy judges the markers of such a run.
UNKEPT_LOCK_POLICY = replace(MARKER_POLICY, bound_redundancy=_unkept_lock_bound_redundancy)


def current_version(declaration: Declaration, locked_as_written: RecordedVersions | None) -> VersionString:
    """Return the version the declaration pins, or else the one version uv locks for it as written, if it locks one."""
    if declaration.current_version:
        return declaration.current_version
    versions = _versions_locked_for(declaration, locked_as_written)
    return versions.pop() if len(versions) == 1 else ""


def _current_version_redundancy(declaration: Declaration, locked_as_written: RecordedVersions | None) -> Reason | None:
    """Return why a directive needing the declaration's current version does not have one, or None where it has one.

    Without the versions locked as written, nothing says whether it has one, so the directive goes unjudged.
    """
    if locked_as_written is None or current_version(declaration, locked_as_written):
        return None
    several = len(_versions_locked_for(declaration, locked_as_written)) > 1
    return Reason.SEVERAL_LOCKED_VERSIONS if several else Reason.NO_LOCKED_VERSION


def _versions_locked_for(declaration: Declaration, locked_as_written: RecordedVersions | None) -> set[VersionString]:
    """Return the versions uv locks for the declaration's dependency as written."""
    by_name = {normalized_python_name(name): versions for name, versions in (locked_as_written or {}).items()}
    return set(by_name.get(normalized_python_name(declaration.dependency), []))
