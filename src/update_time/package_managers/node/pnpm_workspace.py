"""Update a pnpm workspace that a pnpm-workspace.yaml roots, and judge the markers of the file's catalogs."""

from typing import TYPE_CHECKING

from update_time.io.process import is_installed

from .manager_choice import choice_in_directory
from .marker_policy import MARKER_POLICY, NO_LOCK_FILE_POLICY
from .pnpm import PNPM

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.manifests.npm_declaration import Declaration
    from update_time.package_managers.policy import ManagerPolicy


def update_pnpm_workspace(workspace_file: Path) -> ManagerPolicy[Declaration]:
    """Update the pnpm workspace the file roots, and return the policy for the markers of the file's catalogs.

    The run for the package.json at the root updates a workspace whose root holds one. Each member reports a pnpm that
    is not on the path.
    """
    policy = _catalog_policy(workspace_file)
    workspace = PNPM.workspace_of(workspace_file.parent)
    if (
        policy is MARKER_POLICY
        and not (workspace.root / "package.json").exists()
        and is_installed(PNPM.update.executable)
    ):
        workspace.update(None)
    return policy


def _catalog_policy(workspace_file: Path) -> ManagerPolicy[Declaration]:
    """Return the policy for the markers of a pnpm-workspace.yaml, by whether pnpm runs for the workspace it roots."""
    directory = workspace_file.parent
    if (choice := choice_in_directory(directory)) is not None and choice.names_more_than_one_manager():
        return MARKER_POLICY.policy_for_more_than_one_manager
    if not PNPM.workspace_of(directory).has_a_lock():
        return NO_LOCK_FILE_POLICY
    return MARKER_POLICY
