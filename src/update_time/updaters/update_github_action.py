"""GitHub Action updater script finds YAML files in the GitHub directory and updates 'uses' keys to latest versions.

Actions referenced by a version tag only (e.g. `@v4`) are automatically pinned to the commit SHA of the latest
version, with the version added as a trailing comment. Already-pinned actions are bumped to the latest version.

If an environment variable GITHUB_TOKEN is set, the script will use it to increase the GitHub rate limit.
"""

import re
import sys
from functools import cache, partial
from pathlib import Path

from packaging.version import Version

from update_time.domain.version import DependencyName, DependencyVersion, VersionString, is_valid
from update_time.io.filesystem import YAML_GLOB_PATTERNS, glob
from update_time.io.log import get_logger
from update_time.io.rewrite import updated_lines
from update_time.sources.github import get_latest_release, newest_publication_date

LOG = get_logger("github action")
# Match a `uses:` reference that is either already pinned to a commit SHA with a version comment
# (`<sha> # vX.Y.Z`) or unpinned to a version tag (`@vX` / `@vX.Y.Z`). Branch references (e.g. `@main`) and
# local actions (no `@`) don't carry a resolvable version, so they don't match and are left untouched.
ACTION_RE = re.compile(
    r"uses: (?P<dependency>[\w\d\./-]+)@"
    r"(?:(?P<sha>[a-f0-9]{40}) # v?(?P<version>[\d\w\.\-]+)|v(?P<ref>[\d\w\.\-]+))"
)


@cache
def get_latest_version(action: DependencyName, current_version_string: VersionString) -> DependencyVersion:
    """Fetch the latest version for the action."""
    owner, repository, *_path = action.split("/")
    # The newest release date rides on the cached releases list, so it's free; it feeds the staleness check.
    newest_published = newest_publication_date(owner, repository)
    release = get_latest_release(owner, repository)
    if release is None or release.commit_sha is None or release.version < Version(current_version_string):
        return DependencyVersion(current_version_string, newest_published=newest_published)
    return DependencyVersion(
        str(release.version), release.body, release.commit_sha, release.published_at, newest_published
    )


def _update_action(match: re.Match[str], path: Path, scope: str | None) -> str:
    """Pin or update a single `uses:` reference to the latest version's commit SHA, or leave it unchanged.

    `scope` is the reference's `# update-time: ignore[...]` scope: `"update"` holds back the (re)pin, `"stale"` the
    staleness warning.
    """
    dependency = match.group("dependency")
    current_sha = match.group("sha")
    current_version = match.group("version") if current_sha else match.group("ref")
    if not is_valid(current_version):
        return match.group(0)  # Ignore references that aren't versions (e.g. a branch name)
    latest = get_latest_version(dependency, current_version)
    if scope != "stale":
        LOG.warn_if_stale(dependency, latest, path)
    if scope == "update" or not latest.sha:
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

        # Rewrite per line (keeping line endings) so an `# update-time: ignore[...]` marker can hold back a single
        # `uses:`; the marker's scope reaches `_update_action` through the per-line substitution.
        def update_line(line: str, scope: str | None, path: Path = yaml_file) -> str:
            return ACTION_RE.sub(partial(_update_action, path=path, scope=scope), line)

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
