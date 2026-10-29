"""Pin each GitHub Action `uses:` in the GitHub directory to a commit, bump it, and warn about a vulnerable version."""

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from packaging.version import Version

from update_time.domain.file_type import GITHUB_WORKFLOWS
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.github_workflow import ACTION_REFERENCE, spell_action
from update_time.markers.reference import SteeredReference
from update_time.references.file import rewrite_file
from update_time.references.github import PinUpdater, github_reference, names_a_version
from update_time.references.match import reference_matches
from update_time.references.rewrite import updated_lines
from update_time.references.vulnerability import warn_about_vulnerable_dependencies
from update_time.sources.osv import Ecosystem

if TYPE_CHECKING:
    from update_time.domain.line import Line

_LOG = get_logger("github action")
_ACTION = PinUpdater(spell_action, _LOG, checks_vulnerabilities=True)


def _versioned_actions(lines: list[Line]) -> list[SteeredReference]:
    """Return each action that names a version, read at that version as the GitHub source spells it.

    So `uses: tj-actions/changed-files@<sha> # v45.0.0` is read at `45.0.0`, and so is the unpinned
    `uses: tj-actions/changed-files@v45.0.0` whose marker holds the update back.
    """
    actions = []
    for line, match, marker in reference_matches(lines, ACTION_REFERENCE):
        reference = github_reference(match, line.location, match.group("dependency"))
        if names_a_version(reference):
            normalised = replace(reference, current_version=str(Version(reference.current_version)))
            actions.append(SteeredReference.from_reference(normalised, marker=marker))
    return actions


def update_github_actions() -> None:
    """Update the GitHub Actions in all YAML files under the GitHub directory, including composite actions.

    The vulnerability check runs on the lines the rewrite produced, so it checks the version the run settled on.
    """
    pin_the_references = partial(updated_lines, regexp=ACTION_REFERENCE, update_line=_ACTION.update_line, logger=_LOG)
    for yaml_file in glob_for(GITHUB_WORKFLOWS):
        lines = rewrite_file(yaml_file, pin_the_references, _LOG)
        warn_about_vulnerable_dependencies([_versioned_actions(lines)], Ecosystem.GITHUB_ACTIONS, _LOG)


def main() -> None:  # pragma: no cover
    """Update the GitHub Actions in the repository's workflows."""
    update_github_actions()


if __name__ == "__main__":  # pragma: no cover
    main()
