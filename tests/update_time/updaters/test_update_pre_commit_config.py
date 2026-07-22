"""Unit tests for the pre-commit config update script."""

from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, Mock, patch

from update_time.domain.bound import NO_BOUND, Verb
from update_time.domain.version import DependencyVersion
from update_time.io.log import Logger
from update_time.updaters.update_pre_commit_config import update_pre_commit_configs

from tests.update_time.fixtures import COMMIT_SHA1 as OLD_SHA
from tests.update_time.fixtures import COMMIT_SHA2 as NEW_SHA
from tests.update_time.helpers import LoggingTestCase, bound, mock_path

HOOKS = "hooks:\n      - id: trailing-whitespace\n"


def config(rev_block: str) -> str:
    """Return a pre-commit config with a single GitHub-hosted hook repository carrying the given rev block."""
    return f"repos:\n  - repo: https://github.com/pre-commit/pre-commit-hooks\n    {rev_block}    {HOOKS}"


@patch("update_time.references.github.get_latest_version")
@patch("pathlib.Path.glob")
class UpdatePreCommitConfigsTest(LoggingTestCase):
    """Unit tests for the update pre-commit configs function."""

    HOOK = "pre-commit/pre-commit-hooks"

    def test_pin_unpinned_tag(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a rev given as a version tag only is pinned to the commit SHA with a frozen version comment."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.6.0", sha=NEW_SHA)
        config_file = mock_path(config("rev: v4.5.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(config(f"rev: {NEW_SHA}  # frozen: v4.6.0\n"))
        mock_get_latest_version.assert_called_once_with(self.HOOK, "v4.5.0", NO_BOUND)
        self.assert_path_logged(config_file)
        self.assert_pinned_logged(config_file, self.HOOK, "4.6.0", NEW_SHA, line=3)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_pin_unpinned_tag_already_at_latest(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an unpinned tag already at the latest version is still pinned to that version's commit SHA."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.5.0", sha=NEW_SHA)
        config_file = mock_path(config("rev: v4.5.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(config(f"rev: {NEW_SHA}  # frozen: v4.5.0\n"))
        self.assert_pinned_logged(config_file, self.HOOK, "4.5.0", NEW_SHA, line=3)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_pin_unpinned_tag_without_v_prefix(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a tag without a `v` prefix keeps that convention in the frozen version comment."""
        mock_get_latest_version.return_value = DependencyVersion(version="24.1.0", sha=NEW_SHA)
        config_file = mock_path(config("rev: 22.10.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(config(f"rev: {NEW_SHA}  # frozen: 24.1.0\n"))
        mock_get_latest_version.assert_called_once_with(self.HOOK, "22.10.0", NO_BOUND)
        self.assert_pinned_logged(config_file, self.HOOK, "24.1.0", NEW_SHA, line=3)

    def test_pin_quoted_tag(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a quoted rev tag is pinned, dropping the quotes like pre-commit's own freeze does."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.5.0", sha=NEW_SHA)
        config_file = mock_path(config('rev: "v4.5.0"\n'))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(config(f"rev: {NEW_SHA}  # frozen: v4.5.0\n"))
        mock_get_latest_version.assert_called_once_with(self.HOOK, "v4.5.0", NO_BOUND)

    def test_bump_frozen_rev(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a rev already pinned to a SHA with a frozen comment is bumped to the latest version's SHA."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.6.0", sha=NEW_SHA)
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(config(f"rev: {NEW_SHA}  # frozen: v4.6.0\n"))
        mock_get_latest_version.assert_called_once_with(self.HOOK, "v4.5.0", NO_BOUND)
        self.assert_new_version_logged(config_file, self.HOOK, "4.6.0", line=3)
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

    def test_branch_rev_is_left_alone(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a rev that is a branch name rather than a version is left untouched."""
        config_file = mock_path(config("rev: main\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_no_warnings_logged()

    def test_bare_sha_without_frozen_comment_is_left_alone(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a rev pinned to a bare commit SHA without a frozen comment is left untouched."""
        config_file = mock_path(config(f"rev: {OLD_SHA}\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
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
            DependencyVersion(version="4.6.0", sha=NEW_SHA),
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
            [((self.HOOK, "v4.5.0", NO_BOUND),), (("psf/black", "22.10.0", NO_BOUND),)],
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
        old = datetime.now(UTC) - timedelta(days=512)
        mock_get_latest_version.return_value = DependencyVersion(version="4.5.0", sha=OLD_SHA, newest_published=old)
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        self.assert_stale_dependency_logged(config_file, self.HOOK, "4.5.0", line=3)

    def test_inline_ignore_marker(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an inline `# update-time: ignore` comment leaves the rev untouched, looking up no version."""
        config_file = mock_path(config("rev: v4.5.0  # update-time: ignore\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_ignored_logged(self.HOOK, config_file, line=3)
        self.assert_no_warnings_logged()

    def test_inline_ignore_marker_after_frozen_comment(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an inline marker following the frozen comment on the same line holds the rev back."""
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0  # update-time: ignore\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_ignored_logged(self.HOOK, config_file, line=3)

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
        self.assert_ignored_logged(self.HOOK, config_file, line=4)

    def test_ignore_update_marker_skips_repin_but_still_checks_staleness(self, mock_glob: Mock, mock_latest: Mock):
        """Test that `ignore[update]` leaves the rev unchanged but still warns when the hook is stale."""
        old = datetime.now(UTC) - timedelta(days=512)
        mock_latest.return_value = DependencyVersion(version="4.6.0", sha=NEW_SHA, newest_published=old)
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0  # update-time: ignore[update]\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        self.assert_stale_dependency_logged(config_file, self.HOOK, "4.6.0", line=3)
        self.assert_ignored_logged(self.HOOK, config_file, line=3)

    def test_ignore_stale_marker_repins_but_skips_staleness(self, mock_glob: Mock, mock_latest: Mock):
        """Test that `ignore[stale]` bumps the rev but skips the staleness check even for an old release."""
        old = datetime.now(UTC) - timedelta(days=512)
        mock_latest.return_value = DependencyVersion(version="4.6.0", sha=NEW_SHA, newest_published=old)
        config_file = mock_path(config(f"rev: {OLD_SHA}  # frozen: v4.5.0  # update-time: ignore[stale]\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(
            config(f"rev: {NEW_SHA}  # frozen: v4.6.0  # update-time: ignore[stale]\n")
        )
        self.assert_new_version_logged(config_file, self.HOOK, "4.6.0", line=3)
        self.assert_no_warnings_logged()

    def test_allow_update_bound_passes_bound_and_pins(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an `allow[update<…>]` marker passes the bound to the source and pins the bounded release."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.6.0", sha=NEW_SHA)
        config_file = mock_path(config("rev: v4.5.0  # update-time: allow[update<5]\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(
            config(f"rev: {NEW_SHA}  # frozen: v4.6.0  # update-time: allow[update<5]\n")
        )
        mock_get_latest_version.assert_called_once_with(self.HOOK, "v4.5.0", bound(Verb.ALLOW, "update<5"))
        self.assert_pinned_logged(config_file, self.HOOK, "4.6.0", NEW_SHA, line=3)
        self.assert_no_warnings_logged()

    def test_level_bound_passes_bound_and_pins(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an `ignore[major-update]` marker passes the level bound to the source."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.6.0", sha=NEW_SHA)
        config_file = mock_path(config("rev: v4.5.0  # update-time: ignore[major-update]\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_called_once_with(
            config(f"rev: {NEW_SHA}  # frozen: v4.6.0  # update-time: ignore[major-update]\n")
        )
        mock_get_latest_version.assert_called_once_with(self.HOOK, "v4.5.0", bound(Verb.IGNORE, "major-update"))
        self.assert_pinned_logged(config_file, self.HOOK, "4.6.0", NEW_SHA, line=3)
        self.assert_no_warnings_logged()

    def test_invalid_specifier_leaves_rev_unchanged(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a marker with an unparsable version specifier warns and leaves the rev unchanged."""
        config_file = mock_path(config("rev: v4.5.0  # update-time: allow[update@@@]\n"))
        mock_glob.return_value = [config_file]
        update_pre_commit_configs()
        config_file.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.mock_warning.assert_called_once_with(
            Logger._MESSAGE_INVALID_SPECIFIER, "@@@", Logger._render_dependency(self.HOOK), ANY, stacklevel=ANY
        )
