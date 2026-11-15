"""The policies for the markers of a package.json and of a pnpm-workspace.yaml, and a package.json judged by one."""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from update_time.domain.directive import Reason
from update_time.domain.marker import Scope
from update_time.package_managers.policy import ManagerPolicy

if TYPE_CHECKING:
    from collections.abc import Mapping

    from update_time.manifests.npm_declaration import Catalogs, Declaration
    from update_time.manifests.package_json import PackageJson


def _unchecked_scopes(declaration: Declaration) -> dict[Scope, Reason]:
    """Return each warning scope that Update-time leaves unchecked for the declaration, with the reason.

    The npm registry does not signal archival, so that scope is always among them. Where the declaration does not
    name a release, Update-time leaves every warning scope unchecked. A version that a catalog names is checked at
    the catalog's entry instead.
    """
    if declaration.names_a_release:
        return {Scope.ARCHIVED: Reason.NO_ARCHIVAL_SIGNAL}
    if declaration.catalog is not None:
        return ManagerPolicy.every_warning_scope(Reason.CHECKED_AT_THE_CATALOG)
    return ManagerPolicy.every_warning_scope(Reason.NO_NPM_RELEASE)


# npm, pnpm, and bun move a dependency within the range its package.json declares, whatever a marker says about it.
MARKER_POLICY = ManagerPolicy(
    hold_back_redundancy=lambda _declaration: Reason.MANAGER_RESOLVES_THE_VERSION,
    bound_redundancy=lambda _declaration: Reason.BOUND_DECIDES_NOTHING,
    cooldown_redundancy=Reason.COOLDOWN_PER_RUN,
    unchecked_scopes=_unchecked_scopes,
)

# The policy for the markers of a package.json whose project does not have a lock file.
NO_LOCK_FILE_POLICY = MARKER_POLICY.policy_for_files_left_as_they_are(
    Reason.NO_LOCK_FILE_LEFT, Reason.NO_LOCK_FILE_KEPT
)

# The policy for the markers of a package.json whose project is locked in a binary lock that Update-time cannot read.
BINARY_LOCK_POLICY = MARKER_POLICY.policy_for_files_left_as_they_are(Reason.BINARY_LOCK_LEFT, Reason.BINARY_LOCK_KEPT)

# The policy for the markers of a package.json whose project's files name more than one package manager.
MORE_THAN_ONE_MANAGER_POLICY = MARKER_POLICY.policy_for_files_left_as_they_are(
    Reason.MORE_THAN_ONE_MANAGER_LEFT, Reason.MORE_THAN_ONE_MANAGER_KEPT
)

# The policy for the markers of a package.json whose workspace root lies outside the scan root.
WORKSPACE_ROOT_OUTSIDE_THE_SCAN_POLICY = MARKER_POLICY.policy_for_files_left_as_they_are(
    Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_LEFT, Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_KEPT
)


@dataclass(frozen=True)
class CheckedPackageJson:
    """A package.json to check, as its package manager left it, with the policy its markers are judged by."""

    package_json: PackageJson
    policy: ManagerPolicy[Declaration]

    def judged_by(self, catalogs: Catalogs) -> CheckedPackageJson:
        """Return the package.json, with a policy checking a `catalog:` dependency where the catalogs hold its entry."""
        policy = self.policy

        def unchecked_scopes(declaration: Declaration) -> Mapping[Scope, Reason]:
            """Return the scopes the policy leaves unchecked, or every one where the catalogs do not hold the entry."""
            if declaration.catalog is not None and catalogs.location_of(declaration) is None:
                return ManagerPolicy.every_warning_scope(Reason.CATALOG_ENTRY_NOT_FOUND)
            return policy.unchecked_scopes(declaration)

        return CheckedPackageJson(self.package_json, replace(policy, unchecked_scopes=unchecked_scopes))
