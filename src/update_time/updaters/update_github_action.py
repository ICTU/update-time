"""GitHub Action updater script finds YAML files in the GitHub directory and updates 'uses' keys to latest versions.

Actions referenced by a version tag only (e.g. `@v4`) are automatically pinned to the commit SHA of the latest
version, with the version added as a trailing comment. Already-pinned actions are bumped to the latest version.

If an environment variable GITHUB_TOKEN is set, the script will use it to increase the GitHub rate limit.
"""

import re
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain.location import Location
from update_time.io.filesystem import YAML_GLOB_PATTERNS, glob
from update_time.io.log import get_logger
from update_time.references.github import GitHubReference, latest_pin
from update_time.references.rewrite import updated_lines

if TYPE_CHECKING:
    from update_time.domain.marker import Marker

LOG = get_logger("github action")
# Match a `uses:` reference that is either already pinned to a commit SHA with a version comment
# (`<sha> # vX.Y.Z`) or unpinned to a version tag (`@vX` / `@vX.Y.Z`). Branch references (e.g. `@main`) and
# local actions (no `@`) don't carry a resolvable version, so they don't match and are left untouched.
ACTION_RE = re.compile(
    r"uses: (?P<dependency>[\w\d\./-]+)@"
    r"(?:(?P<sha>[a-f0-9]{40}) # v?(?P<version>[\d\w\.\-]+)|v(?P<ref>[\d\w\.\-]+))"
)


def _update_action(match: re.Match[str], location: Location, marker: Marker) -> str:
    """Pin or update a single `uses:` reference to the latest version's commit SHA, or leave it unchanged.

    An action pinned to a commit SHA with a version comment (`<sha> # v4.1.1`), or referenced by version tag only,
    is the same GitHub-SHA-pinned reference as a pre-commit hook `rev:`, so the decision is shared with
    `_update_rev` via `latest_pin`; only the `uses:` output syntax is spelled here.
    """
    dependency = match.group("dependency")
    current_sha = match.group("sha")
    current_version = match.group("version") if current_sha else match.group("ref")
    latest = latest_pin(GitHubReference(dependency, current_version, current_sha), marker, location, LOG)
    if latest is None:
        return match.group(0)  # Invalid, held back, unpinnable, or already up to date: leave the reference as it is
    return f"uses: {dependency}@{latest.sha} # v{latest.version}"


def update_github_actions(github_dir: Path) -> None:
    """Update the GitHub Actions in all YAML files under the GitHub directory, including composite actions."""
    for yaml_file in glob(*YAML_GLOB_PATTERNS, start=github_dir):
        LOG.path(yaml_file)
        old_content = yaml_file.read_text()

        # Rewrite per line (keeping line endings) so an `# update-time:` marker can hold back or bound a single
        # `uses:`; the marker reaches `_update_action` through the per-line substitution. The line's number rides
        # along so the reference is logged with it. Actions pin a commit SHA, not an image digest, so the marker's
        # `allow_drift` opt-in does not apply here.
        def update_line(match: re.Match[str], marker: Marker, line_number: int, path: Path = yaml_file) -> str:
            location = Location(path, line_number)
            return ACTION_RE.sub(partial(_update_action, location=location, marker=marker), match.string)

        new_lines = updated_lines(old_content.splitlines(keepends=True), ACTION_RE, update_line, LOG, yaml_file)
        new_content = "".join(new_lines)
        if new_content != old_content:
            yaml_file.write_text(new_content)


def main() -> None:  # pragma: no cover
    """Update the GitHub Actions in the repository's workflows."""
    update_github_actions(Path.cwd() / ".github")


if __name__ == "__main__":  # pragma: no cover
    main()
