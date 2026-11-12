"""Pin each GitHub Action `uses:` in the GitHub directory to a commit, bump it, and warn about a vulnerable version."""

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from packaging.version import Version

from update_time.domain.file_type import GITHUB_WORKFLOWS
from update_time.domain.vulnerability import Ecosystem
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.github_workflow import ACTION_REFERENCE, spell_action
from update_time.references.file import rewrite_file
from update_time.references.github import PinUpdater, names_a_version
from update_time.references.match import matched_reference, reference_matches
from update_time.references.rewrite import updated_lines
from update_time.references.vulnerability import warn_about_vulnerable_dependencies

if TYPE_CHECKING:
    from update_time.domain.reference import Reference
    from update_time.primitives.line import Line

_LOG = get_logger("github action")
_ACTION = PinUpdater(spell_action, _LOG, ecosystem=Ecosystem.GITHUB_ACTIONS)


def _versioned_actions(lines: list[Line]) -> list[Reference]:
    """Return each action that names a version, read at that version as the GitHub source spells it.

    So `uses: tj-actions/changed-files@<sha> # v45.0.0` is read at `45.0.0`, and so is the unpinned
    `uses: tj-actions/changed-files@v45.0.0` whose marker holds the update back.
    """
    actions = []
    for line, match, marker in reference_matches(lines, ACTION_REFERENCE):
        reference = matched_reference(match, line.location, marker=marker)
        if names_a_version(reference):
            normalised = replace(reference, current_version=str(Version(reference.current_version)))
            actions.append(normalised)
    return actions


def update_github_actions() -> None:
    """Update the GitHub Actions in all YAML files under the GitHub directory, including composite actions.

    The vulnerability check runs on the lines the rewrite produced, so it checks the version the run settled on.
    """
    pin_the_references = partial(
        updated_lines,
        regexp=ACTION_REFERENCE,
        update_line=_ACTION.update_line,
        logger=_LOG,
        steers_the_update=_ACTION.steers_the_update,
    )
    for yaml_file in glob_for(GITHUB_WORKFLOWS):
        lines = rewrite_file(yaml_file, pin_the_references, _LOG)
        warn_about_vulnerable_dependencies([_versioned_actions(lines)], _ACTION.source, _LOG)


def main() -> None:  # pragma: no cover
    """Update the GitHub Actions in the repository's workflows."""
    update_github_actions()


if __name__ == "__main__":  # pragma: no cover
    main()
