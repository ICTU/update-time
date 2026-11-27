"""The policies for the markers of a package.json and of a file holding catalogs, and a file judged by one."""

from dataclasses import dataclass, replace
from functools import cached_property
from typing import TYPE_CHECKING, Final

from update_time.domain.directive import Reason
from update_time.domain.marker import Scope
from update_time.package_managers.policy import ManagerPolicy

if TYPE_CHECKING:
    from collections.abc import Mapping

    from update_time.manifests.catalogs_yaml import CatalogsYaml
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


# npm, pnpm, yarn, and bun move a dependency within the range its package.json declares, whatever a marker says.
MARKER_POLICY: Final[ManagerPolicy[Declaration]] = ManagerPolicy(
    hold_back_redundancy=lambda _declaration: Reason.MANAGER_RESOLVES_THE_VERSION,
    bound_redundancy=lambda _declaration: Reason.BOUND_DECIDES_NOTHING,
    cooldown_redundancy=Reason.COOLDOWN_PER_RUN,
    unchecked_scopes=_unchecked_scopes,
)

# The policy for the markers of a package.json whose project does not have a lock file.
NO_LOCK_FILE_POLICY: Final[ManagerPolicy[Declaration]] = MARKER_POLICY.policy_for_files_left_as_they_are(
    Reason.NO_LOCK_FILE_LEFT, Reason.NO_LOCK_FILE_KEPT
)

# The policy for the markers of a package.json whose project is locked in a binary lock that Update-time cannot read.
BINARY_LOCK_POLICY: Final[ManagerPolicy[Declaration]] = MARKER_POLICY.policy_for_files_left_as_they_are(
    Reason.BINARY_LOCK_LEFT, Reason.BINARY_LOCK_KEPT
)

# The policy for the markers of a package.json whose yarn cannot apply a cooldown, such as yarn classic.
YARN_WITHOUT_A_COOLDOWN_POLICY: Final[ManagerPolicy[Declaration]] = MARKER_POLICY.policy_for_files_left_as_they_are(
    Reason.YARN_WITHOUT_A_COOLDOWN_LEFT, Reason.YARN_WITHOUT_A_COOLDOWN_KEPT
)


@dataclass(frozen=True)
class CheckedFile[FileT: PackageJson | CatalogsYaml]:
    """A file to check, as its package manager left it, with the policy its markers are judged by."""

    file: FileT
    policy: ManagerPolicy[Declaration]

    @cached_property
    def declarations(self) -> list[Declaration]:
        """Return the dependencies the file declares, read once for every check."""
        return list(self.file.declarations())

    def judged_by(self, catalogs: Catalogs) -> CheckedFile[FileT]:
        """Return the package.json, with a policy checking a `catalog:` dependency where the catalogs hold its entry."""
        policy = self.policy

        def unchecked_scopes(declaration: Declaration) -> Mapping[Scope, Reason]:
            """Return the scopes the policy leaves unchecked, or every one where the catalogs do not hold the entry."""
            if declaration.catalog is not None and catalogs.location_of(declaration) is None:
                return ManagerPolicy.every_warning_scope(Reason.CATALOG_ENTRY_NOT_FOUND)
            return policy.unchecked_scopes(declaration)

        return CheckedFile(self.file, replace(policy, unchecked_scopes=unchecked_scopes))
