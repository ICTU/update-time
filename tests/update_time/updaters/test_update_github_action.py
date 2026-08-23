"""Unit tests for the GitHub Action update script."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import requests

from update_time.domain.bound import NO_BOUND, Verb
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion, Release
from update_time.domain.drift import ALLOW_HASH_DRIFT, DriftedPin
from update_time.io.log import Logger
from update_time.primitives.location import Location
from update_time.references import github as references_github
from update_time.sources import github as sources_github
from update_time.updaters.update_github_action import update_github_actions

from tests.helpers import mock_path, patch_environ
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import COMMIT_SHA1 as OLD_SHA
from tests.update_time.fixtures import COMMIT_SHA2 as NEW_SHA
from tests.update_time.helpers import (
    LoggingTestCase,
    bound,
    github_commits_json,
    github_release_json,
    patch_github,
    staleness_disabled,
)

_GITHUB_DIR = Path("/repo/.github")
# A publication date old enough that the default staleness threshold warns about it, and one too fresh to.
_STALE_ISO = (datetime.now(UTC) - timedelta(days=512)).isoformat()
_FRESH_ISO = datetime.now(UTC).isoformat()
# A publication date the default threshold passes over, which a marker's own threshold of 90 days warns about.
_HUNDRED_DAYS_ISO = (datetime.now(UTC) - timedelta(days=100)).isoformat()


@patch("update_time.references.github.get_latest_version")
@patch("pathlib.Path.glob")
class UpdateGitHubActionsTest(LoggingTestCase):
    """Unit tests for the update GitHub Actions function."""

    @staticmethod
    def drifted(workflow_yml: Mock) -> DriftedPin:
        """Return the drifted pin the moved `action/action` version tag these tests use produces."""
        return DriftedPin("action/action", "1.0", Location(workflow_yml, 1), OLD_SHA, new_sha=NEW_SHA)

    def test_multiple_files(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that actions are updated in all YAML files under the GitHub directory, not just workflows."""
        mock_get_latest_version.return_value = DependencyVersion(version="1.1", sha=NEW_SHA)
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        composite_action_yaml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], [composite_action_yaml]]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_called_with(f"uses: action/action@{NEW_SHA} # v1.1\n")
        composite_action_yaml.write_text.assert_called_with(f"uses: action/action@{NEW_SHA} # v1.1\n")
        self.assert_path_logged(composite_action_yaml)
        self.assert_last_new_version_logged(
            "action/action", "1.1", Location(composite_action_yaml, 1), Logger._SUPPRESSING_CHANGELOG
        )
        self.assert_no_warnings_logged()

    def test_file_without_actions(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that YAML files without actions are left untouched."""
        dependabot_yml = mock_path("version: 2\n")
        mock_glob.side_effect = [[dependabot_yml], []]
        update_github_actions(_GITHUB_DIR)
        dependabot_yml.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_path_logged(dependabot_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_pinned_action_up_to_date(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an already pinned action that is up to date is left unchanged."""
        mock_get_latest_version.return_value = DependencyVersion(version="1.0", sha=OLD_SHA)
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_not_called()
        self.assert_path_logged(workflow_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_moved_tag_warned_not_repinned(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a pinned action whose tag now points at another commit is warned about, not silently re-pinned."""
        mock_get_latest_version.return_value = DependencyVersion(version="1.0", sha=NEW_SHA)
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_not_called()
        self.assert_tag_drift_logged(self.drifted(workflow_yml))
        self.assert_no_new_version_logged()

    def test_allow_hash_drift_marker_adopts_moved_tag(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an `allow[hash-drift]` marker re-pins a moved tag's commit instead of only warning about it."""
        mock_get_latest_version.return_value = DependencyVersion(version="1.0", sha=NEW_SHA)
        marker = "  # update-time: allow[hash-drift]"
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0{marker}\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_called_once_with(f"uses: action/action@{NEW_SHA} # v1.0{marker}\n")
        self.assert_adopted_tag_drift_logged(self.drifted(workflow_yml), "update-time: allow[hash-drift]")
        self.assert_no_warnings_logged()

    def test_flag_adopts_moved_tag_repo_wide(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that the --allow-hash-drift flag (via its env var) adopts a moved tag without a per-line marker."""
        mock_get_latest_version.return_value = DependencyVersion(version="1.0", sha=NEW_SHA)
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], []]
        with patch_environ({ALLOW_HASH_DRIFT.name: "1"}):
            update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_called_once_with(f"uses: action/action@{NEW_SHA} # v1.0\n")
        self.assert_adopted_tag_drift_logged(self.drifted(workflow_yml), "--allow-hash-drift")
        self.assert_no_warnings_logged()

    def test_stale_action_warned(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an action whose newest release is old is warned about, even when it is up to date."""
        old = datetime.now(UTC) - timedelta(days=512)
        newest = Release("1.2", old)
        mock_get_latest_version.return_value = DependencyVersion(version="1.0", sha=OLD_SHA, newest=newest)
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_not_called()
        self.assert_stale_dependency_logged("action/action", "1.2", Location(workflow_yml, 1))

    def test_pin_unpinned_action(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an action referenced by version tag only is pinned to the commit SHA with a version comment."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.1.1", sha=NEW_SHA)
        workflow_yml = mock_path("uses: actions/checkout@v4\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_called_with(f"uses: actions/checkout@{NEW_SHA} # v4.1.1\n")
        mock_get_latest_version.assert_called_once_with("actions/checkout", "4", NO_BOUND, COOLDOWN.default)
        self.assert_path_logged(workflow_yml)
        self.assert_pinned_logged("actions/checkout", "4.1.1", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_pin_unpinned_action_already_at_latest(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an unpinned action already at the latest release is still pinned to that release's commit SHA."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.1.1", sha=NEW_SHA)
        workflow_yml = mock_path("uses: actions/checkout@v4.1.1\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_called_with(f"uses: actions/checkout@{NEW_SHA} # v4.1.1\n")
        mock_get_latest_version.assert_called_once_with("actions/checkout", "4.1.1", NO_BOUND, COOLDOWN.default)
        self.assert_path_logged(workflow_yml)
        self.assert_pinned_logged("actions/checkout", "4.1.1", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_allow_update_bound_passes_bound_and_pins(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an `allow[update<…>]` marker passes the bound to the source and pins the bounded release."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.2.0", sha=NEW_SHA)
        workflow_yml = mock_path("uses: actions/checkout@v4  # update-time: allow[update<5]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_called_with(
            f"uses: actions/checkout@{NEW_SHA} # v4.2.0  # update-time: allow[update<5]\n"
        )
        mock_get_latest_version.assert_called_once_with(
            "actions/checkout", "4", bound(Verb.ALLOW, "update<5"), COOLDOWN.default
        )
        self.assert_pinned_logged("actions/checkout", "4.2.0", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_warnings_logged()  # a `<5` bound on a v4 pin is live, so no redundancy warning

    def test_level_bound_passes_bound_and_pins(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an `ignore[major-update]` marker passes the level bound to the source."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.2.0", sha=NEW_SHA)
        workflow_yml = mock_path("uses: actions/checkout@v4  # update-time: ignore[major-update]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_called_with(
            f"uses: actions/checkout@{NEW_SHA} # v4.2.0  # update-time: ignore[major-update]\n"
        )
        mock_get_latest_version.assert_called_once_with(
            "actions/checkout", "4", bound(Verb.IGNORE, "major-update"), COOLDOWN.default
        )
        self.assert_pinned_logged("actions/checkout", "4.2.0", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_warnings_logged()  # a major-update bound on a v4 pin is live, so no redundancy warning

    def test_inline_ignore_marker_pins_action(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an inline `# update-time: ignore` comment leaves the action untouched, looking up no version."""
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0  # update-time: ignore\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_ignored_logged("action/action", Location(workflow_yml, 1))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_preceding_ignore_marker_pins_action(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a standalone `# update-time: ignore` comment pins the action on the line below it."""
        workflow_yml = mock_path(f"# update-time: ignore\nuses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_ignored_logged("action/action", Location(workflow_yml, 2))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_ignore_update_marker_skips_repin_but_still_checks_staleness(self, mock_glob: Mock, mock_latest: Mock):
        """Test that `ignore[update]` leaves the action's pin unchanged but still warns when it is stale."""
        old = datetime.now(UTC) - timedelta(days=512)
        newest = Release("1.2", old)
        mock_latest.return_value = DependencyVersion(version="1.1", sha=NEW_SHA, newest=newest)
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0  # update-time: ignore[update]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_not_called()  # the pin is held back
        location = Location(workflow_yml, 1)
        self.assert_stale_dependency_logged("action/action", "1.2", location)  # but staleness is still checked
        self.assert_ignored_logged("action/action", location)

    def test_ignore_stale_marker_repins_but_skips_staleness(self, mock_glob: Mock, mock_latest: Mock):
        """Test that `ignore[stale]` repins the action but skips the staleness check even for an old release."""
        old = datetime.now(UTC) - timedelta(days=512)
        newest = Release("1.2", old)
        mock_latest.return_value = DependencyVersion(version="1.1", sha=NEW_SHA, newest=newest)
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0  # update-time: ignore[stale]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_called_once_with(
            f"uses: action/action@{NEW_SHA} # v1.1  # update-time: ignore[stale]\n"
        )
        location = Location(workflow_yml, 1)
        self.assert_new_version_logged("action/action", "1.1", location)
        self.assert_ignored_staleness_logged("action/action", location, "ignore[stale]")
        self.assert_no_warnings_logged()  # staleness skipped despite the old release

    def test_unpinned_action_without_sha_is_left_alone(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an unpinned action is not changed when no commit SHA is available to pin it to."""
        mock_get_latest_version.return_value = DependencyVersion(version="4")
        workflow_yml = mock_path("uses: actions/checkout@v4\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_not_called()
        self.assert_path_logged(workflow_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_github(releases=[github_release_json("v1.0", published_at=_STALE_ISO)], tags=[])
    def test_stale_branch_reference_warned(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an action referenced by a branch is warned about when its repository's newest release is old."""
        workflow_yml = mock_path("uses: actions/checkout@main\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        self.assert_stale_dependency_logged("actions/checkout", "1.0", Location(workflow_yml, 1))

    @kills(
        Mutation(
            references_github,
            "        log.report_staleness(resolved, marker, threshold)",
            "        log.warn_if_stale(resolved, threshold)",
            "a marker silencing the staleness warning of a branch reference is passed over",
        )
    )
    @patch_github(releases=[github_release_json("v1.0", published_at=_STALE_ISO)], tags=[])
    def test_an_ignore_stale_marker_on_a_branch_reference_holds_the_warning_back(
        self, mock_glob: Mock, mock_get_latest_version: Mock
    ):
        """Test that `ignore[stale]` on a branch reference silences the warning its old repository would get."""
        workflow_yml = mock_path("uses: actions/checkout@main  # update-time: ignore[stale]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        self.assert_ignored_staleness_logged("actions/checkout", Location(workflow_yml, 1), "ignore[stale]")
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            references_github,
            "    if (threshold := marker.stale.value_or(STALE_AFTER.get())) == NO_STALENESS_CHECK:",
            "    if (threshold := STALE_AFTER.get()) == NO_STALENESS_CHECK:",
            "a branch reference's own staleness threshold is passed over for the run-wide one",
        )
    )
    @patch_github(releases=[github_release_json("v1.0", published_at=_HUNDRED_DAYS_ISO)], tags=[])
    def test_a_branch_reference_is_warned_about_at_its_own_threshold(
        self, mock_glob: Mock, mock_get_latest_version: Mock
    ):
        """Test that `ignore[stale<90]` on a branch reference warns at 90 days, where the default 365 would not."""
        workflow_yml = mock_path("uses: actions/checkout@main  # update-time: ignore[stale<90]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        self.assert_stale_dependency_logged("actions/checkout", "1.0", Location(workflow_yml, 1))

    @kills(
        Mutation(
            sources_github,
            "@cache\ndef _list_releases(owner: str, repository: str) -> tuple[_ReleaseJSON, ...] | None:",
            "def _list_releases(owner: str, repository: str) -> tuple[_ReleaseJSON, ...] | None:",
            "every reference to a repository asks GitHub for its releases again",
        )
    )
    @patch_github(releases=[github_release_json("v1.0", published_at=_STALE_ISO)], tags=[])
    def test_two_branch_references_to_one_repository_cost_one_lookup(
        self, mock_glob: Mock, mock_get_latest_version: Mock
    ):
        """Test that a repository two branch references name is asked for its releases once, not once per file."""
        workflow_ymls = [mock_path("uses: actions/checkout@main\n"), mock_path("uses: actions/checkout@master\n")]
        mock_glob.side_effect = [workflow_ymls, []]
        update_github_actions(_GITHUB_DIR)
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        requests_get = cast("Mock", requests.get)  # Patched by `patch_github`, which hands the test no mock.
        releases_requests = [call for call in requests_get.call_args_list if "/releases" in call.args[0]]
        self.assertEqual(len(releases_requests), 1)
        self.assert_stale_dependency_logged(
            "actions/checkout", "1.0", Location(workflow_ymls[0], 1), Location(workflow_ymls[1], 1)
        )

    @patch("update_time.references.github.newest_release")
    def test_a_local_action_is_passed_over(
        self, mock_newest_release: Mock, mock_glob: Mock, mock_get_latest_version: Mock
    ):
        """Test that an action in the repository itself names no GitHub repository, so none is asked about."""
        workflow_yml = mock_path("uses: ./.github/actions/build\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_not_called()
        mock_newest_release.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_no_warnings_logged()

    @patch("update_time.references.github.newest_release")
    def test_a_bare_ignore_on_a_branch_reference_asks_github_nothing(
        self, mock_newest_release: Mock, mock_glob: Mock, mock_get_latest_version: Mock
    ):
        """Test that a bare `ignore` on a branch reference holds the staleness check back, GitHub unasked."""
        workflow_yml = mock_path("uses: actions/checkout@main  # update-time: ignore\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        mock_newest_release.assert_not_called()
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        self.assert_no_warnings_logged()

    @patch("update_time.references.github.newest_release")
    def test_a_branch_reference_is_not_looked_up_with_the_check_switched_off(
        self, mock_newest_release: Mock, mock_glob: Mock, mock_get_latest_version: Mock
    ):
        """Test that `--stale-after 0` leaves a branch reference unlooked-up, staleness being its only check."""
        workflow_yml = mock_path("uses: actions/checkout@main\n")
        mock_glob.side_effect = [[workflow_yml], []]
        with staleness_disabled:
            update_github_actions(_GITHUB_DIR)
        mock_newest_release.assert_not_called()
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        self.assert_no_warnings_logged()

    @patch_github(releases=[], tags=[])
    def test_a_branch_reference_whose_repository_has_released_nothing(
        self, mock_glob: Mock, mock_get_latest_version: Mock
    ):
        """Test that a branch reference is not warned about when its repository has published no release to date."""
        workflow_yml = mock_path("uses: actions/checkout@main\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        self.assert_no_warnings_logged()

    @patch_github(releases=[github_release_json("v1.0", published_at=_FRESH_ISO)], tags=[])
    def test_branch_reference_is_left_alone(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an action referenced by a branch is not rewritten, its repository still publishing."""
        workflow_yml = mock_path("uses: actions/checkout@main\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_path_logged(workflow_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_github(releases=[github_release_json("v1.0", published_at=_FRESH_ISO)], tags=[])
    def test_v_prefixed_non_version_reference_is_left_alone(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a v-prefixed reference that isn't a version (e.g. a floating `@vnext` tag) is not rewritten."""
        workflow_yml = mock_path("uses: actions/checkout@vnext\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_path_logged(workflow_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


@patch("pathlib.Path.glob")
class UpdateGitHubActionsThroughTheSourceTest(LoggingTestCase):
    """Unit tests for the actions updater resolving versions through the source rather than through a test double.

    The suite above patches the source's getter, which is not registered as publication-date-reporting the way the
    real one is.
    """

    @patch_github(releases=[github_release_json("1.1")], tags=[], commit=github_commits_json(NEW_SHA))
    def test_cooldown_marker_is_not_reported_as_redundant(self, mock_glob: Mock):
        """Test that a `cooldown` marker on an action holds something back, since GitHub dates its versions."""
        marker = "  # update-time: ignore[cooldown<30]"
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0{marker}\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions(_GITHUB_DIR)
        workflow_yml.write_text.assert_called_once_with(f"uses: action/action@{NEW_SHA} # v1.1{marker}\n")
        self.assert_no_warnings_logged()
