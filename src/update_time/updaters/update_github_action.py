"""GitHub Action updater script finds YAML files in the GitHub directory and updates 'uses' keys to latest versions.

Actions referenced by a version tag only (e.g. `@v4`) are automatically pinned to the commit SHA of the latest
version, with the version added as a trailing comment. Already-pinned actions are bumped to the latest version.

If an environment variable GITHUB_TOKEN is set, the script will use it to increase the GitHub rate limit.
"""

import re
import sys
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain.version import is_valid
from update_time.io.filesystem import YAML_GLOB_PATTERNS, glob
from update_time.io.log import get_logger
from update_time.io.rewrite import updated_lines
from update_time.sources.github import get_latest_version

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


def _update_action(match: re.Match[str], path: Path, marker: Marker) -> str:
    """Pin or update a single `uses:` reference to the latest version's commit SHA, or leave it unchanged.

    `marker` carries the reference's `# update-time:` directives: `ignore_update` holds back the (re)pin,
    `ignore_stale` the staleness warning, and a `version_filter` bounds which release the (re)pin may adopt.
    """
    dependency = match.group("dependency")
    current_sha = match.group("sha")
    current_version = match.group("version") if current_sha else match.group("ref")
    if not is_valid(current_version):
        return match.group(0)  # Ignore references that aren't versions (e.g. a branch name)
    LOG.warn_if_redundant_bound(dependency, marker.version_filter, current_version, path)
    latest = get_latest_version(dependency, current_version, marker.version_filter)
    if not marker.ignore_stale:
        LOG.warn_if_stale(dependency, latest, path)
    if marker.ignore_update or not latest.sha:
        return match.group(0)  # Held back by the marker, or can't (re)pin without a commit SHA
    if current_sha is None:
        LOG.pinned(dependency, latest, path)
    elif latest.version != current_version:
        LOG.new_version(dependency, latest, path)
    else:
        return match.group(0)  # Already pinned and up to date
    return f"uses: {dependency}@{latest.sha} # v{latest.version}"


def update_github_actions(github_dir: Path) -> int:
    """Update the GitHub Actions in all YAML files under the GitHub directory, including composite actions."""
    for yaml_file in glob(*YAML_GLOB_PATTERNS, start=github_dir):
        LOG.path(yaml_file)
        old_content = yaml_file.read_text()

        # Rewrite per line (keeping line endings) so an `# update-time:` marker can hold back or bound a single
        # `uses:`; the marker reaches `_update_action` through the per-line substitution. Actions pin a commit SHA,
        # not an image digest, so the marker's `allow_drift` opt-in does not apply here.
        def update_line(line: str, marker: Marker, path: Path = yaml_file) -> str:
            return ACTION_RE.sub(partial(_update_action, path=path, marker=marker), line)

        new_lines = updated_lines(old_content.splitlines(keepends=True), ACTION_RE, update_line, LOG, yaml_file)
        new_content = "".join(new_lines)
        if new_content != old_content:
            yaml_file.write_text(new_content)
    return 0


def main() -> int:  # pragma: no cover
    """Update the GitHub Actions in the repository's workflows."""
    return update_github_actions(Path.cwd() / ".github")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
