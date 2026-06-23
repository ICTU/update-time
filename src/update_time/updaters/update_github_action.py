"""GitHub Action updater script finds YAML files in the GitHub directory and updates 'uses' keys to latest versions.

Actions referenced by a version tag only (e.g. ``@v4``) are automatically pinned to the commit SHA of the latest
version, with the version added as a trailing comment. Already-pinned actions are bumped to the latest version.

If an environment variable GITHUB_TOKEN is set, the script will use it to increase the GitHub rate limit.
"""

import re
import sys
from functools import cache
from pathlib import Path

from packaging.version import Version

from update_time.domain.version import DependencyVersion, is_valid
from update_time.io.filesystem import YAML_GLOB_PATTERNS, glob
from update_time.io.log import get_logger
from update_time.sources.github import get_latest_release

LOG = get_logger("github action")
# Match a `uses:` reference that is either already pinned to a commit SHA with a version comment
# (`<sha> # vX.Y.Z`) or unpinned to a version tag (`@vX` / `@vX.Y.Z`). Branch references (e.g. `@main`) and
# local actions (no `@`) don't carry a resolvable version, so they don't match and are left untouched.
ACTION_RE = re.compile(
    r"uses: (?P<dependency>[\w\d\./-]+)@"
    r"(?:(?P<sha>[a-f0-9]{40}) # v?(?P<version>[\d\w\.\-]+)|v(?P<ref>[\d\w\.\-]+))"
)


@cache
def get_latest_version(action: str, current_version_string: str) -> DependencyVersion:
    """Fetch the latest version for the action."""
    owner, repository, *_path = action.split("/")
    release = get_latest_release(owner, repository)
    if release is None:
        LOG.no_version(f"{owner}/{repository}")
        return DependencyVersion(current_version_string)
    if release.commit_sha is None:
        return DependencyVersion(current_version_string)
    latest_version = max(release.version, Version(current_version_string))
    return DependencyVersion(str(latest_version), release.body, release.commit_sha, published=release.published_at)


def _update_action(match: re.Match[str]) -> str:
    """Pin or update a single `uses:` reference to the latest version's commit SHA, or leave it unchanged."""
    dependency = match.group("dependency")
    current_sha = match.group("sha")
    current_version = match.group("version") if current_sha else match.group("ref")
    if not is_valid(current_version):
        return match.group(0)  # Ignore references that aren't versions (e.g. a branch name)
    latest = get_latest_version(dependency, current_version)
    if not latest.sha:
        return match.group(0)  # Can't (re)pin without a commit SHA
    if current_sha is None:
        LOG.pinned(dependency, latest)
    elif latest.version != current_version:
        LOG.new_version(dependency, latest)
    else:
        return match.group(0)  # Already pinned and up to date
    return f"uses: {dependency}@{latest.sha} # v{latest.version}"


def update_github_actions(github_dir: Path) -> int:
    """Update the GitHub Actions in all YAML files under the GitHub directory, including composite actions."""
    for yaml_file in glob(*YAML_GLOB_PATTERNS, start=github_dir):
        LOG.path(yaml_file)
        old_content = yaml_file.read_text()
        new_content = ACTION_RE.sub(_update_action, old_content)
        if new_content != old_content:
            yaml_file.write_text(new_content)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(update_github_actions(Path.cwd() / ".github"))
