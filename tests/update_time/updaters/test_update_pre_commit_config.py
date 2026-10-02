"""Unit tests for the pre-commit config update script."""

from unittest.mock import ANY, Mock, patch

from update_time.domain.bound import BLOCK_ALL_UPDATES, NO_BOUND, Verb, VersionBound
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import (
    Archival,
    ArchivedSubject,
    DependencyVersion,
    PinnedDependency,
    Project,
    Release,
)
from update_time.domain.reference import DriftedPin
from update_time.io.log import Logger
from update_time.primitives import digest
from update_time.primitives.location import Location
from update_time.updaters.update_pre_commit_config import update_pre_commit_configs

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import COMMIT_SHA1 as OLD_SHA
from tests.update_time.fixtures import COMMIT_SHA2 as NEW_SHA
from tests.update_time.fixtures import FRESH_DATE, PAST_COOLDOWN_DATE, STALE_DATE
from tests.update_time.helpers import (
    LoggingTestCase,
    bound,
    full_commit_sha,
    github_commits_json,
    github_release_json,
    github_requests,
    github_tag_json,
    no_other_version_at_the_tag,
    patch_github,
)
from tests.update_time.updaters.helpers import github_version

_HOOKS = "hooks:\n      - id: trailing-whitespace\n"


def config(rev_block: str) -> str:
    """Return a pre-commit config with a single GitHub-hosted hook repository carrying the given rev block."""
    return f"repos:\n  - repo: https://github.com/pre-commit/pre-commit-hooks\n    {rev_block}    {_HOOKS}"


@no_other_version_at_the_tag
@patch("update_time.references.github.get_latest_version")
@patch("pathlib.Path.glob")
class UpdatePreCommitConfigsTest(LoggingTestCase):
    """Unit tests for the update pre-commit configs function."""

    HOOK = "pre-commit/pre-commit-hooks"

    def drifted(self, config_file: Mock, version: str = "4.5.0") -> DriftedPin:
        """Return the drifted pin the moved tag or branch of the hook repository these tests use produces."""
        return DriftedPin(self.HOOK, version, Location(config_file, 3), OLD_SHA, new_sha=NEW_SHA)

    def assert_resolved(
        self, mock_get_latest_version: Mock, rev: str = "v4.5.0", version_bound: VersionBound = NO_BOUND
    ) -> None:
        """Assert the source was asked once about the hook repository at the rev, under the bound the marker sets."""
        mock_get_latest_version.assert_called_once_with(
            PinnedDependency(self.HOOK, rev), version_bound, COOLDOWN.default, check_archival=True
        )

    def test_pin_unpinned_tag(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a rev given as a version tag only is pinned to the commit SHA with a frozen version comment."""
        mock_get_latest_version.return_value = github_version("4.6.0")
        config_file = mock_path(config("rev: v4.5.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(config(f"rev: {NEW_SHA}  # frozen: v4.6.0\n"))
        self.assert_resolved(mock_get_latest_version)
        self.assert_path_logged(config_file)
        self.assert_pinned_logged(self.HOOK, "4.6.0", NEW_SHA, Location(config_file, 3))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_pin_unpinned_tag_already_at_latest(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an unpinned tag already at the latest version is still pinned to that version's commit SHA."""
        mock_get_latest_version.return_value = github_version("4.5.0")
        config_file = mock_path(config("rev: v4.5.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(config(f"rev: {NEW_SHA}  # frozen: v4.5.0\n"))
        self.assert_pinned_logged(self.HOOK, "4.5.0", NEW_SHA, Location(config_file, 3))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_frozen_comment_names_the_tag_as_the_repository_spells_it(
        self, mock_glob: Mock, mock_get_latest_version: Mock
    ):
        """Test that a rev is frozen with the tag as the repository spells it, whatever the rev's own spelling."""
        mock_get_latest_version.return_value = github_version("24.1.0")
        config_file = mock_path(config("rev: 22.10.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(config(f"rev: {NEW_SHA}  # frozen: v24.1.0\n"))
        self.assert_resolved(mock_get_latest_version, "22.10.0")
        self.assert_pinned_logged(self.HOOK, "24.1.0", NEW_SHA, Location(config_file, 3))

    def test_pin_quoted_tag(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a quoted rev tag is pinned, dropping the quotes like pre-commit's own freeze does."""
        mock_get_latest_version.return_value = github_version("4.5.0")
        config_file = mock_path(config('rev: "v4.5.0"\n'))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(config(f"rev: {NEW_SHA}  # frozen: v4.5.0\n"))
        self.assert_resolved(mock_get_latest_version)

    def test_bump_frozen_rev(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a rev already pinned to a SHA with a frozen comment is bumped to the latest version's SHA."""
        mock_get_latest_version.return_value = github_version("4.6.0")
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(config(f"rev: {NEW_SHA}  # frozen: v4.6.0\n"))
        self.assert_resolved(mock_get_latest_version)
        self.assert_new_version_logged(self.HOOK, "4.6.0", Location(config_file, 3))
        self.assert_no_warnings_logged()

    def test_frozen_rev_up_to_date(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a frozen rev that is already up to date is left unchanged."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.5.0", sha=OLD_SHA)
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_moved_tag_warned_not_refrozen(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a frozen rev whose tag now points at another commit is warned about, not silently re-frozen."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.5.0", sha=NEW_SHA)
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        self.assert_drift_logged(Logger.TAG_DRIFT, self.drifted(config_file))
        self.assert_no_new_version_logged()

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA))
    def test_moved_branch_warned_not_refrozen(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a rev frozen to a branch that now points at another commit is warned about, not re-frozen."""
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: main\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        drifted = self.drifted(config_file, "main")
        self.assert_drift_logged(Logger.BRANCH_DRIFT, drifted)

    @patch_github(
        releases=[],
        tags=[github_tag_json("v6.0.0", NEW_SHA)],
        commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE),
    )
    def test_moved_branch_kept_floating_is_refrozen_as_the_branch(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a rev kept floating and opted into hash drift is re-frozen as the branch, past a version tag."""
        marker = "  # update-time: allow[floating-pin, hash-drift]"
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: main{marker}\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(config(f"rev: {NEW_SHA}  # frozen: main{marker}\n"))
        mock_get_latest_version.assert_not_called()
        drifted = self.drifted(config_file, "main")
        self.assert_adopted_drift_logged(Logger.BRANCH_DRIFT, drifted, "update-time: allow[hash-drift]")
        self.assert_no_warnings_logged()

    @patch_github(commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE))
    def test_allow_hash_drift_marker_adopts_moved_tag(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a rev opted into hash drift is re-frozen to the tag's new commit, leaving its comments intact."""
        mock_get_latest_version.return_value = github_version("4.5.0")
        marker = "  # update-time: allow[hash-drift]"
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0{marker}\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(config(f"rev: {NEW_SHA}  # frozen: v4.5.0{marker}\n"))
        self.assert_adopted_drift_logged(Logger.TAG_DRIFT, self.drifted(config_file), "update-time: allow[hash-drift]")
        self.assert_no_warnings_logged()

    def test_local_repo_is_left_alone(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a `repo: local` entry (which carries no rev) is left untouched."""
        config_file = mock_path("repos:\n  - repo: local\n    hooks:\n      - id: my-hook\n")
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_no_warnings_logged()

    def test_non_github_repo_is_left_alone(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a hook repository hosted outside GitHub is left untouched."""
        config_file = mock_path("repos:\n  - repo: https://gitlab.com/owner/repo\n    rev: v1.0.0\n")
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_no_warnings_logged()

    def test_ssh_repo_is_updated(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a hook repository given as an ssh URL is updated."""
        mock_get_latest_version.return_value = github_version("4.6.0")
        repo = f"repos:\n  - repo: ssh://git@github.com/{self.HOOK}\n"
        config_file = mock_path(f"{repo}    rev: v4.5.0\n")
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(f"{repo}    rev: {NEW_SHA}  # frozen: v4.6.0\n")
        self.assert_resolved(mock_get_latest_version)
        self.assert_pinned_logged(self.HOOK, "4.6.0", NEW_SHA, Location(config_file, 3))
        self.assert_no_warnings_logged()

    @patch_github(
        releases=[github_release_json("v4.5.0", published_at=STALE_DATE)], tags=[], commit=github_commits_json(NEW_SHA)
    )
    def test_stale_branch_rev_warned(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a rev naming a branch is warned about when its repository's newest release is old."""
        config_file = mock_path(config("rev: main\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        self.assert_stale_dependency_logged(self.HOOK, "4.5.0", Location(config_file, 3))

    @kills(
        Mutation(
            digest,
            'SHORT_COMMIT_SHA = rf"[0-9a-f]',
            'SHORT_COMMIT_SHA = rf"(?=[0-9]*[a-f])[0-9a-f]',
            "a short commit SHA of digits alone is resolved as a version and never pinned",
        )
    )
    def test_branch_or_short_sha_rev_is_frozen_to_its_commit(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a branch is frozen as written, a short SHA as its full SHA, or either as the version tag there."""
        cases = {
            "main → main": ("main", [], NEW_SHA, f"{NEW_SHA}  # frozen: main"),
            "vnext → vnext": ("vnext", [], NEW_SHA, f"{NEW_SHA}  # frozen: vnext"),
            "557067c → full SHA": ("557067c", [], full_commit_sha("557067c"), full_commit_sha("557067c")),
            "1234567 → full SHA": ("1234567", [], full_commit_sha("1234567"), full_commit_sha("1234567")),
            "main → v6.0.0": ("main", [github_tag_json("v6.0.0", NEW_SHA)], NEW_SHA, f"{NEW_SHA}  # frozen: v6.0.0"),
            "main → 6.0.0": ("main", [github_tag_json("6.0.0", NEW_SHA)], NEW_SHA, f"{NEW_SHA}  # frozen: 6.0.0"),
        }
        mock_get_latest_version.return_value = DependencyVersion("0")  # A rev read as a version does not get pinned
        for case, (rev, tags, sha, pinned) in cases.items():
            with self.subTest(case), patch_github(releases=[], tags=tags, commit=github_commits_json(sha)):
                mock_get_latest_version.reset_mock()
                config_file = mock_path(config(f"rev: {rev}\n"))
                mock_glob.return_value = [config_file]
                update_pre_commit_configs()
                config_file.write_text.assert_called_once_with(config(f"rev: {pinned}\n"))
                mock_get_latest_version.assert_not_called()

    @patch_github(releases=[], tags=[github_tag_json("20240101", OLD_SHA)], commit=github_commits_json(OLD_SHA))
    def test_hex_rev_naming_a_tag_is_resolved_as_a_version(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a rev of hex digits alone is resolved as a version when it names a tag, not a commit's SHA."""
        mock_get_latest_version.return_value = DependencyVersion(version="20240301", sha=NEW_SHA, tag_name="20240301")
        config_file = mock_path(config("rev: 20240101\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(config(f"rev: {NEW_SHA}  # frozen: 20240301\n"))
        self.assert_resolved(mock_get_latest_version, "20240101")

    @patch_github(
        releases=[github_release_json("v4.5.0", published_at=FRESH_DATE)], tags=[], commit=github_commits_json(NEW_SHA)
    )
    def test_bare_sha_without_frozen_comment_is_left_alone(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a rev pinned to a bare commit SHA without a frozen comment is not rewritten, its commit unasked."""
        config_file = mock_path(config(f"rev: {OLD_SHA}\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assertEqual(github_requests("commits"), [])
        self.assertEqual(len(github_requests("releases")), 1)
        self.assert_no_warnings_logged()

    def test_rev_without_repo_is_left_alone(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a rev appearing before any repo (so no repository is in scope) is left untouched."""
        config_file = mock_path("rev: v4.5.0\n")
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()

    def test_no_sha_available_leaves_rev_alone(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an unpinned tag is not changed when no commit SHA is available to pin it to."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.5.0")
        config_file = mock_path(config("rev: v4.5.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_multiple_repositories(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that each hook repository is resolved and pinned against its own repo, in one file."""
        mock_get_latest_version.side_effect = [
            github_version("4.6.0"),
            DependencyVersion(version="24.1.0", sha=NEW_SHA),
        ]
        content = (
            "repos:\n"
            "  - repo: https://github.com/pre-commit/pre-commit-hooks\n    rev: v4.5.0\n"
            "  - repo: https://github.com/psf/black\n    rev: 22.10.0\n"
        )
        config_file = mock_path(content)
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(
            "repos:\n"
            f"  - repo: https://github.com/pre-commit/pre-commit-hooks\n    rev: {NEW_SHA}  # frozen: v4.6.0\n"
            f"  - repo: https://github.com/psf/black\n    rev: {NEW_SHA}  # frozen: 24.1.0\n"
        )
        self.assertEqual(
            mock_get_latest_version.call_args_list,
            [
                ((PinnedDependency(self.HOOK, "v4.5.0"), NO_BOUND, COOLDOWN.default), {"check_archival": True}),
                ((PinnedDependency("psf/black", "22.10.0"), NO_BOUND, COOLDOWN.default), {"check_archival": True}),
            ],
        )

    def test_config_without_hooks(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a config without any hook repositories is left untouched."""
        config_file = mock_path("repos: []\n")
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_stale_hook_warned(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a hook whose newest version is old is warned about, even when it is up to date."""
        newest = Release("4.7.0", STALE_DATE)
        project = Project(newest=newest)
        mock_get_latest_version.return_value = DependencyVersion(version="4.5.0", sha=OLD_SHA, project=project)
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        self.assert_stale_dependency_logged(self.HOOK, "4.7.0", Location(config_file, 3))

    def test_archived_hook_warned(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a hook whose repository GitHub declares archived is warned about, even when it is up to date."""
        archival = Archival(archived=True, subject=ArchivedSubject.REPOSITORY)
        project = Project(archival=archival)
        mock_get_latest_version.return_value = DependencyVersion(version="4.5.0", sha=OLD_SHA, project=project)
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        self.assert_archived_repository_logged(self.HOOK, Location(config_file, 3))

    def test_inline_ignore_marker(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an inline `# update-time: ignore` comment leaves the rev untouched, looking up no version."""
        config_file = mock_path(config("rev: v4.5.0  # update-time: ignore\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_ignored_logged(self.HOOK, Location(config_file, 3))
        self.assert_no_warnings_logged()

    def test_inline_ignore_marker_after_frozen_comment(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an inline marker following the frozen comment on the same line holds the rev back."""
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0  # update-time: ignore\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_ignored_logged(self.HOOK, Location(config_file, 3))

    def test_preceding_ignore_marker(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a standalone `# update-time: ignore` comment holds back the rev on the line below it."""
        content = (
            "repos:\n  - repo: https://github.com/pre-commit/pre-commit-hooks\n"
            "    # update-time: ignore\n    rev: v4.5.0\n"
        )
        config_file = mock_path(content)
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_ignored_logged(self.HOOK, Location(config_file, 4))

    def test_ignore_update_marker_skips_repin_but_still_checks_staleness(self, mock_glob: Mock, mock_latest: Mock):
        """Test that `ignore[update]` leaves the rev unchanged but still warns when the hook is stale."""
        newest = Release("4.7.0", STALE_DATE)
        mock_latest.return_value = DependencyVersion(version="4.6.0", sha=NEW_SHA, project=Project(newest=newest))
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0  # update-time: ignore[update]\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        location = Location(config_file, 3)
        self.assert_stale_dependency_logged(self.HOOK, "4.7.0", location)
        self.assert_ignored_logged(self.HOOK, location)

    def test_ignore_stale_marker_repins_but_skips_staleness(self, mock_glob: Mock, mock_latest: Mock):
        """Test that `ignore[stale]` bumps the rev but skips the staleness check even for an old release."""
        newest = Release("4.7.0", STALE_DATE)
        mock_latest.return_value = github_version("4.6.0", Project(newest=newest))
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0  # update-time: ignore[stale]\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(
            config(f"rev: {NEW_SHA}  # frozen: v4.6.0  # update-time: ignore[stale]\n")
        )
        location = Location(config_file, 3)
        self.assert_new_version_logged(self.HOOK, "4.6.0", location)
        self.assert_no_warnings_logged()
        self.assert_ignored_staleness_logged(self.HOOK, location, "ignore[stale]")

    def test_allow_update_bound_passes_bound_and_pins(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an `allow[update<…>]` marker passes the bound to the source and pins the bounded release."""
        mock_get_latest_version.return_value = github_version("4.6.0")
        config_file = mock_path(config("rev: v4.5.0  # update-time: allow[update<5]\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(
            config(f"rev: {NEW_SHA}  # frozen: v4.6.0  # update-time: allow[update<5]\n")
        )
        self.assert_resolved(mock_get_latest_version, "v4.5.0", bound(Verb.ALLOW, "update<5"))
        self.assert_pinned_logged(self.HOOK, "4.6.0", NEW_SHA, Location(config_file, 3))
        self.assert_no_warnings_logged()

    def test_level_bound_passes_bound_and_pins(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an `ignore[major-update]` marker passes the level bound to the source."""
        mock_get_latest_version.return_value = github_version("4.6.0")
        config_file = mock_path(config("rev: v4.5.0  # update-time: ignore[major-update]\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(
            config(f"rev: {NEW_SHA}  # frozen: v4.6.0  # update-time: ignore[major-update]\n")
        )
        self.assert_resolved(mock_get_latest_version, "v4.5.0", bound(Verb.IGNORE, "major-update"))
        self.assert_pinned_logged(self.HOOK, "4.6.0", NEW_SHA, Location(config_file, 3))
        self.assert_no_warnings_logged()

    def test_invalid_specifier_leaves_rev_unchanged(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a marker with an unparsable version specifier warns and leaves the rev unchanged."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.6.0", sha=NEW_SHA)
        config_file = mock_path(config("rev: v4.5.0  # update-time: allow[update@@@]\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        self.assert_resolved(mock_get_latest_version, "v4.5.0", BLOCK_ALL_UPDATES)
        self.assert_invalid_bracket_item_logged(self.HOOK, ANY, "@@@")
