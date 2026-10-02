"""Unit tests for the GitHub Action update script."""

from typing import cast
from unittest.mock import Mock, patch

import requests

from update_time.domain.bound import NO_BOUND, Verb, VersionBound
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion, PinnedDependency, Project, Release
from update_time.domain.reference import DriftedPin
from update_time.io.log import Logger
from update_time.markers.directive import Reason
from update_time.primitives.location import Location
from update_time.references import github as references_github
from update_time.sources import github as sources_github
from update_time.updaters import update_github_action
from update_time.updaters.update_github_action import update_github_actions

from tests.helpers import mock_path, mock_response
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import COMMIT_SHA1 as OLD_SHA
from tests.update_time.fixtures import COMMIT_SHA2 as NEW_SHA
from tests.update_time.fixtures import FRESH_DATE, GITHUB_UNCACHED, PAST_COOLDOWN_DATE, STALE_DATE
from tests.update_time.helpers import (
    LoggingTestCase,
    bound,
    floating_pin_allowed,
    github_commits_json,
    github_release_json,
    github_requests,
    github_tag_json,
    hash_drift_allowed,
    patch_github,
    staleness_disabled,
)


def _drifted(workflow_yml: Mock, dependency: str = "actions/checkout", version: str = "main") -> DriftedPin:
    """Return the drift of the workflow's first reference, pinned to the old commit, onto the new one."""
    return DriftedPin(dependency, version, Location(workflow_yml, 1), OLD_SHA, new_sha=NEW_SHA)


def _commits_url(ref: str, dependency: str = "actions/checkout") -> str:
    """Return the URL the GitHub API serves the commit the ref names at."""
    return f"https://api.github.com/repos/{dependency}/commits/{ref}"


@patch("update_time.references.github.get_latest_version")
@patch("pathlib.Path.glob")
class UpdateGitHubActionsTest(LoggingTestCase):
    """Unit tests for the GitHub Actions updater, with the GitHub source's getter patched."""

    @staticmethod
    def drifted(workflow_yml: Mock) -> DriftedPin:
        """Return the drifted pin the moved `action/action` version tag these tests use produces."""
        return _drifted(workflow_yml, "action/action", "1.0")

    @staticmethod
    def assert_resolved(
        mock_get_latest_version: Mock, version: str = "4", version_bound: VersionBound = NO_BOUND
    ) -> None:
        """Assert the source was asked once about `actions/checkout` at the version, under the bound the marker sets."""
        mock_get_latest_version.assert_called_once_with(
            PinnedDependency("actions/checkout", version), version_bound, COOLDOWN.default, check_archival=True
        )

    def test_multiple_files(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that actions are updated in all YAML files under the GitHub directory, not just workflows."""
        mock_get_latest_version.return_value = DependencyVersion(version="1.1", sha=NEW_SHA, tag_name="v1.1")
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        composite_action_yaml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], [composite_action_yaml]]
        update_github_actions()
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
        update_github_actions()
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
        update_github_actions()
        workflow_yml.write_text.assert_not_called()
        self.assert_path_logged(workflow_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_moved_tag_warned_not_repinned(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a pinned action whose tag now points at another commit is warned about, not silently re-pinned."""
        mock_get_latest_version.return_value = DependencyVersion(version="1.0", sha=NEW_SHA)
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_not_called()
        self.assert_drift_logged(Logger.TAG_DRIFT, self.drifted(workflow_yml))
        self.assert_no_new_version_logged()

    @kills(
        Mutation(
            references_github._drifted_pin,
            "else pin.sha",
            "else reference.current_version",
            "a moved tag's new commit is dated by the version the reference names rather than by the commit's SHA",
        )
    )
    @patch_github(commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE))
    def test_allow_hash_drift_marker_adopts_moved_tag(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an `allow[hash-drift]` marker re-pins a moved tag to its new commit, dated by that commit's SHA."""
        mock_get_latest_version.return_value = DependencyVersion(version="1.0", sha=NEW_SHA, tag_name="v1.0")
        marker = "  # update-time: allow[hash-drift]"
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0{marker}\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_called_once_with(f"uses: action/action@{NEW_SHA} # v1.0{marker}\n")
        self.assertEqual(github_requests("commits"), [_commits_url(NEW_SHA, "action/action")])
        self.assert_adopted_drift_logged(Logger.TAG_DRIFT, self.drifted(workflow_yml), "update-time: allow[hash-drift]")
        self.assert_no_warnings_logged()

    @patch_github(commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE))
    def test_flag_adopts_moved_tag_repo_wide(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that the `--allow-hash-drift` flag re-pins a moved tag to the commit it now points at."""
        mock_get_latest_version.return_value = DependencyVersion(version="1.0", sha=NEW_SHA, tag_name="v1.0")
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], []]
        with hash_drift_allowed:
            update_github_actions()
        workflow_yml.write_text.assert_called_once_with(f"uses: action/action@{NEW_SHA} # v1.0\n")
        self.assert_adopted_drift_logged(Logger.TAG_DRIFT, self.drifted(workflow_yml), "--allow-hash-drift")
        self.assert_no_warnings_logged()

    def test_stale_action_warned(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an action whose newest release is old is warned about, even when it is up to date."""
        old = STALE_DATE
        newest = Release("1.2", old)
        project = Project(newest=newest)
        mock_get_latest_version.return_value = DependencyVersion(version="1.0", sha=OLD_SHA, project=project)
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_not_called()
        self.assert_stale_dependency_logged("action/action", "1.2", Location(workflow_yml, 1))

    def test_pin_unpinned_action(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an action referenced by version tag only is pinned to the commit SHA with a version comment."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.1.1", sha=NEW_SHA, tag_name="v4.1.1")
        workflow_yml = mock_path("uses: actions/checkout@v4\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_called_with(f"uses: actions/checkout@{NEW_SHA} # v4.1.1\n")
        self.assert_resolved(mock_get_latest_version)
        self.assert_path_logged(workflow_yml)
        self.assert_pinned_logged("actions/checkout", "4.1.1", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_pin_unpinned_action_already_at_latest(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an unpinned action already at the latest release is still pinned to that release's commit SHA."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.1.1", sha=NEW_SHA, tag_name="v4.1.1")
        workflow_yml = mock_path("uses: actions/checkout@v4.1.1\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_called_with(f"uses: actions/checkout@{NEW_SHA} # v4.1.1\n")
        self.assert_resolved(mock_get_latest_version, "4.1.1")
        self.assert_path_logged(workflow_yml)
        self.assert_pinned_logged("actions/checkout", "4.1.1", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_allow_update_bound_passes_bound_and_pins(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an `allow[update<…>]` marker passes the bound to the source and pins the bounded release."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.2.0", sha=NEW_SHA, tag_name="v4.2.0")
        workflow_yml = mock_path("uses: actions/checkout@v4  # update-time: allow[update<5]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_called_with(
            f"uses: actions/checkout@{NEW_SHA} # v4.2.0  # update-time: allow[update<5]\n"
        )
        self.assert_resolved(mock_get_latest_version, "4", bound(Verb.ALLOW, "update<5"))
        self.assert_pinned_logged("actions/checkout", "4.2.0", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_warnings_logged()  # a `<5` bound on a v4 pin is live, so no redundancy warning

    def test_level_bound_passes_bound_and_pins(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an `ignore[major-update]` marker passes the level bound to the source."""
        mock_get_latest_version.return_value = DependencyVersion(version="4.2.0", sha=NEW_SHA, tag_name="v4.2.0")
        workflow_yml = mock_path("uses: actions/checkout@v4  # update-time: ignore[major-update]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_called_with(
            f"uses: actions/checkout@{NEW_SHA} # v4.2.0  # update-time: ignore[major-update]\n"
        )
        self.assert_resolved(mock_get_latest_version, "4", bound(Verb.IGNORE, "major-update"))
        self.assert_pinned_logged("actions/checkout", "4.2.0", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_warnings_logged()  # a major-update bound on a v4 pin is live, so no redundancy warning

    def test_inline_ignore_marker_pins_action(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an inline `# update-time: ignore` comment leaves the action untouched, looking up no version."""
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0  # update-time: ignore\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_ignored_logged("action/action", Location(workflow_yml, 1))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_preceding_ignore_marker_pins_action(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a standalone `# update-time: ignore` comment pins the action on the line below it."""
        workflow_yml = mock_path(f"# update-time: ignore\nuses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_ignored_logged("action/action", Location(workflow_yml, 2))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_ignore_update_marker_skips_repin_but_still_checks_staleness(self, mock_glob: Mock, mock_latest: Mock):
        """Test that `ignore[update]` leaves the action's pin unchanged but still warns when it is stale."""
        old = STALE_DATE
        newest = Release("1.2", old)
        mock_latest.return_value = DependencyVersion(version="1.1", sha=NEW_SHA, project=Project(newest=newest))
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0  # update-time: ignore[update]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_not_called()  # the pin is held back
        location = Location(workflow_yml, 1)
        self.assert_stale_dependency_logged("action/action", "1.2", location)  # but staleness is still checked
        self.assert_ignored_logged("action/action", location)

    def test_ignore_stale_marker_repins_but_skips_staleness(self, mock_glob: Mock, mock_latest: Mock):
        """Test that `ignore[stale]` repins the action but skips the staleness check even for an old release."""
        old = STALE_DATE
        newest = Release("1.2", old)
        mock_latest.return_value = DependencyVersion(
            version="1.1", sha=NEW_SHA, project=Project(newest=newest), tag_name="v1.1"
        )
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0  # update-time: ignore[stale]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
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
        update_github_actions()
        workflow_yml.write_text.assert_not_called()
        self.assert_path_logged(workflow_yml)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch_github(
        releases=[github_release_json("v1.0", published_at=STALE_DATE)], tags=[], commit=github_commits_json(NEW_SHA)
    )
    def test_stale_branch_reference_warned(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an action referenced by a branch is warned about when its repository's newest release is old."""
        workflow_yml = mock_path("uses: actions/checkout@main\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        self.assert_stale_dependency_logged("actions/checkout", "1.0", Location(workflow_yml, 1))

    @patch_github(
        releases=[github_release_json("v1.0", published_at=STALE_DATE)], tags=[], commit=github_commits_json(NEW_SHA)
    )
    def test_an_ignore_stale_marker_on_a_branch_reference_silences_the_warning(
        self, mock_glob: Mock, mock_get_latest_version: Mock
    ):
        """Test that `ignore[stale]` on a branch reference silences the warning its old repository would get."""
        workflow_yml = mock_path("uses: actions/checkout@main  # update-time: ignore[stale]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        self.assert_ignored_staleness_logged("actions/checkout", Location(workflow_yml, 1), "ignore[stale]")
        self.assert_no_warnings_logged()

    @patch_github(
        releases=[github_release_json("v1.0", published_at=PAST_COOLDOWN_DATE)],
        tags=[],
        commit=github_commits_json(NEW_SHA),
    )
    def test_a_branch_reference_is_warned_about_at_its_own_threshold(
        self, mock_glob: Mock, mock_get_latest_version: Mock
    ):
        """Test that `ignore[stale<90]` on a branch reference warns at 90 days, where the default 365 would not."""
        workflow_yml = mock_path("uses: actions/checkout@main  # update-time: ignore[stale<90]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        self.assert_stale_dependency_logged("actions/checkout", "1.0", Location(workflow_yml, 1))

    @kills(GITHUB_UNCACHED)
    @patch_github(
        releases=[github_release_json("v1.0", published_at=STALE_DATE)], tags=[], commit=github_commits_json(NEW_SHA)
    )
    def test_two_references_to_one_branch_ask_for_its_releases_and_commit_once(
        self, mock_glob: Mock, mock_get_latest_version: Mock
    ):
        """Test that two references to one branch ask for its repository's releases and its commit once each."""
        workflow_ymls = [mock_path("uses: actions/checkout@main\n"), mock_path("uses: actions/checkout@main\n")]
        mock_glob.side_effect = [workflow_ymls, []]
        update_github_actions()
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        self.assertEqual(len(github_requests("releases")), 1)
        self.assertEqual(len(github_requests("commits")), 1)
        self.assert_stale_dependency_logged(
            "actions/checkout", "1.0", Location(workflow_ymls[0], 1), Location(workflow_ymls[1], 1)
        )

    @patch("update_time.references.github.project")
    def test_a_local_action_is_passed_over(self, mock_project: Mock, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an action in the repository itself names no GitHub repository, so none is asked about."""
        workflow_yml = mock_path("uses: ./.github/actions/build\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_not_called()
        mock_project.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_no_warnings_logged()

    @patch("update_time.references.github.project")
    def test_a_bare_ignore_on_a_branch_reference_asks_github_nothing(
        self, mock_project: Mock, mock_glob: Mock, mock_get_latest_version: Mock
    ):
        """Test that a bare `ignore` on a branch reference silences the staleness check, GitHub unasked."""
        workflow_yml = mock_path("uses: actions/checkout@main  # update-time: ignore\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        mock_project.assert_not_called()
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        self.assert_no_warnings_logged()

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA))
    def test_a_branch_reference_whose_repository_has_released_nothing(
        self, mock_glob: Mock, mock_get_latest_version: Mock
    ):
        """Test that a branch reference is not warned about when its repository has published no release to date."""
        workflow_yml = mock_path("uses: actions/checkout@main\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        mock_get_latest_version.assert_not_called()  # A branch names no version to resolve an update for.
        self.assert_no_warnings_logged()


@patch("pathlib.Path.glob")
class UpdateGitHubActionsThroughTheSourceTest(LoggingTestCase):
    """Unit tests for the GitHub Actions updater, resolving versions through the GitHub source itself."""

    @staticmethod
    def scanned_workflow(mock_glob: Mock, workflow: str) -> Mock:
        """Run the updater on a workflow file holding the given line, and return that file."""
        workflow_yml = mock_path(workflow)
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        return workflow_yml

    @patch_github(releases=[github_release_json("v1.1")], tags=[], commit=github_commits_json(NEW_SHA))
    def test_cooldown_marker_is_not_reported_as_redundant(self, mock_glob: Mock):
        """Test that a `cooldown` marker on an action holds something back, since GitHub dates its versions."""
        marker = "  # update-time: ignore[cooldown<30]"
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0{marker}\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_called_once_with(f"uses: action/action@{NEW_SHA} # v1.1{marker}\n")
        self.assert_no_warnings_logged()

    @patch_github(releases=[github_release_json("v1.1")], tags=[], commit=github_commits_json(NEW_SHA), archived=True)
    def test_archived_repository_warned(self, mock_glob: Mock):
        """Test that an action whose repository GitHub declares archived is warned about, and still updated."""
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: action/action@{OLD_SHA} # v1.0\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: action/action@{NEW_SHA} # v1.1\n")
        self.assert_archived_repository_logged("action/action", Location(workflow_yml, 1))

    @kills(
        Mutation(
            update_github_action,
            r'r"uses: (?P<dependency>[\w\d\.-]+/[\w\d\./-]+)@"',
            r'r"uses: (?P<dependency>[\w\d\./-]+)@"',
            "a reference naming no repository is read as one, ending the run over that single line",
            raises="ValueError: not enough values to unpack (expected at least 2, got 1)",
        )
    )
    @patch_github(releases=[github_release_json("1.1")], tags=[], commit=github_commits_json(NEW_SHA))
    def test_a_reference_naming_no_repository_is_passed_over(self, mock_glob: Mock):
        """Test that a `uses:` naming no owner and repository is left alone rather than ending the run."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: myaction@v1\n")
        workflow_yml.write_text.assert_not_called()
        cast("Mock", requests.get).assert_not_called()  # A reference naming no repository asks GitHub nothing.
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            references_github._latest_pin,
            "        report_directives_that_set_nothing(marker, get_latest_version, reference, log)",
            "",
            "a reference naming no version has its marker passed over, so a directive holding nothing back is "
            "never reported for it",
        )
    )
    @patch_github(releases=[github_release_json("1.1")], tags=[], commit=github_commits_json(NEW_SHA))
    def test_a_scope_the_source_cannot_apply_is_reported_on_a_reference_naming_no_version(self, mock_glob: Mock):
        """Test that a scope GitHub can never apply is reported for a branch reference, which resolves no update."""
        marker = "  # update-time: ignore[yanked]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: action/action@main{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: action/action@{NEW_SHA} # main{marker}\n")
        self.assert_redundant_directive_logged(
            Reason.NO_YANK_CONCEPT, "action/action", Location(workflow_yml, 1), "ignore[yanked]"
        )

    @patch_github(releases=[github_release_json("v1.1")], tags=[], commit=github_commits_json(NEW_SHA), archived=True)
    def test_ignore_archived_marker_silences_the_warning(self, mock_glob: Mock):
        """Test that `ignore[archived]` on an action silences the warning its archived repository would get."""
        marker = "  # update-time: ignore[archived]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: action/action@{OLD_SHA} # v1.0{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: action/action@{NEW_SHA} # v1.1{marker}\n")
        self.assert_ignored_archival_logged("action/action", Location(workflow_yml, 1), "ignore[archived]")
        self.assert_no_warnings_logged()

    @patch_github(
        releases=[github_release_json("v1.0", published_at=FRESH_DATE)],
        tags=[github_tag_json("v1.0", OLD_SHA)],
        commit=github_commits_json(NEW_SHA),
    )
    def test_pin_branch_reference(self, mock_glob: Mock):
        """Test that a branch reference is pinned to its commit as the branch, the version tags pointing elsewhere."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@main\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # main\n")
        self.assert_pinned_logged("actions/checkout", "main", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_warnings_logged()

    @patch_github(
        releases=[github_release_json("v4.3.0", published_at=PAST_COOLDOWN_DATE)],
        tags=[
            github_tag_json("v4.4.0", OLD_SHA),
            github_tag_json("v4.3", NEW_SHA),
            github_tag_json("v4.3.0", NEW_SHA),
            github_tag_json("v4", NEW_SHA),
        ],
        commit=github_commits_json(NEW_SHA),
    )
    def test_pin_branch_reference_as_the_version_tag_at_its_commit(self, mock_glob: Mock):
        """Test that a branch reference is pinned as the highest version tag pointing at the branch's commit."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@main\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # v4.3.0\n")
        self.assert_pinned_logged("actions/checkout", "4.3.0", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_warnings_logged()

    def test_pin_branch_reference_as_the_branch_when_a_pre_release_tags_its_commit(self, mock_glob: Mock):
        """Test that a branch reference is pinned as the branch when only a pre-release tags the branch's commit."""
        cases = {
            "owner/pre-release-version": ([], [github_tag_json("v5.0.0-rc.1", NEW_SHA)]),
            "owner/pre-release-flagged": (
                [github_release_json("v5.0.0", prerelease=True, published_at=PAST_COOLDOWN_DATE)],
                [github_tag_json("v5.0.0", NEW_SHA)],
            ),
        }
        commit = github_commits_json(NEW_SHA)
        for dependency, (releases, tags) in cases.items():
            with self.subTest(dependency), patch_github(releases=releases, tags=tags, commit=commit):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: {dependency}@main\n")
                workflow_yml.write_text.assert_called_once_with(f"uses: {dependency}@{NEW_SHA} # main\n")

    @patch_github(
        releases=[github_release_json("v4.3.0", published_at=FRESH_DATE)],
        tags=[github_tag_json("v4.3.0", NEW_SHA)],
        commit=github_commits_json(NEW_SHA, date=FRESH_DATE),
    )
    def test_pin_branch_reference_inside_the_cooldown(self, mock_glob: Mock):
        """Test that a branch reference is pinned although its commit and the tag there are inside the cooldown."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@main\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # v4.3.0\n")

    @patch_github(
        releases=[],
        tags=[],
        commit=mock_response({"message": "API rate limit exceeded"}, ok=False, status_code=403),
    )
    def test_branch_whose_commit_cannot_be_fetched_is_left_alone_and_reported_as_an_error(self, mock_glob: Mock):
        """Test that a branch whose commit cannot be fetched is left as it is, the reason logged as an error."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@main\n")
        workflow_yml.write_text.assert_not_called()
        location = Location(workflow_yml, 1)
        self.assert_unpinned_branch_logged("actions/checkout", "main", location, "HTTP 403, API rate limit exceeded")
        self.assert_no_warnings_logged()

    @patch_github(releases=[], tags=[], commit=github_commits_json(OLD_SHA))
    def test_pinned_branch_still_at_its_commit_is_left_alone(self, mock_glob: Mock):
        """Test that a branch pinned to the commit it still points at is left as it is, its commit looked up."""
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main\n")
        workflow_yml.write_text.assert_not_called()
        self.assertEqual(github_requests("commits"), [_commits_url("main")])
        self.assert_no_warnings_logged()

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE))
    def test_moved_branch_warned_not_repinned(self, mock_glob: Mock):
        """Test that a pinned branch that now points at another commit is warned about, not silently re-pinned."""
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main\n")
        workflow_yml.write_text.assert_not_called()
        drifted = _drifted(workflow_yml)
        self.assert_drift_logged(Logger.BRANCH_DRIFT, drifted)

    @kills(
        Mutation(
            references_github._drifted_pin,
            "reference.current_version if kind",
            "pin.sha if kind",
            "a moved branch's new commit is fetched a second time, by its SHA, to date it",
        )
    )
    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE))
    def test_allow_hash_drift_marker_adopts_moved_branch(self, mock_glob: Mock):
        """Test that an `allow[hash-drift]` marker re-pins a moved branch to its new commit, fetched once."""
        marker = "  # update-time: allow[hash-drift]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # main{marker}\n")
        self.assertEqual(github_requests("commits"), [_commits_url("main")])
        drifted = _drifted(workflow_yml)
        self.assert_adopted_drift_logged(Logger.BRANCH_DRIFT, drifted, "update-time: allow[hash-drift]")
        self.assert_no_warnings_logged()

    @patch_github(
        releases=[github_release_json("v1.0", published_at=FRESH_DATE)], tags=[github_tag_json("v1.0", NEW_SHA)]
    )
    def test_pin_version_tag_inside_the_cooldown(self, mock_glob: Mock):
        """Test that a reference to a version tag inside the cooldown is pinned to the commit the tag points at."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@v1.0\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # v1.0\n")
        self.assert_pinned_logged("actions/checkout", "1.0", NEW_SHA, Location(workflow_yml, 1))

    def test_pin_comment_names_the_tag_as_the_repository_spells_it(self, mock_glob: Mock):
        """Test that the pin's comment names the tag as the repository spells it, whatever the reference's spelling."""
        cases = {"owner/bare-tags": ("v1.0", "1.1"), "owner/v-tags": ("1.0", "v4.1.1")}
        for dependency, (current, tag) in cases.items():
            releases, tags = (
                [github_release_json(tag, published_at=PAST_COOLDOWN_DATE)],
                [github_tag_json(tag, NEW_SHA)],
            )
            with self.subTest(dependency), patch_github(releases=releases, tags=tags):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: {dependency}@{current}\n")
                workflow_yml.write_text.assert_called_once_with(f"uses: {dependency}@{NEW_SHA} # {tag}\n")

    def test_moved_tag_inside_the_cooldown_warned_not_adopted(self, mock_glob: Mock):
        """Test that a tag opted into hash drift is warned about while the commit it moved to is inside the cooldown."""
        cases = {"owner/released": [github_release_json("v1.0", published_at=PAST_COOLDOWN_DATE)], "owner/tagged": []}
        tags, commit = [github_tag_json("v1.0", NEW_SHA)], github_commits_json(NEW_SHA, date=FRESH_DATE)
        marker = "  # update-time: allow[hash-drift]"
        for dependency, releases in cases.items():
            with self.subTest(dependency), patch_github(releases=releases, tags=tags, commit=commit):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: {dependency}@{OLD_SHA} # v1.0{marker}\n")
                workflow_yml.write_text.assert_not_called()
                drifted = _drifted(workflow_yml, dependency, "1.0")
                self.assert_drift_logged(Logger.TAG_DRIFT, drifted)

    @patch_github(
        releases=[github_release_json("v1.0", published_at=PAST_COOLDOWN_DATE)],
        tags=[github_tag_json("v1.0", NEW_SHA)],
        commit=mock_response({"message": "API rate limit exceeded"}, ok=False, status_code=403),
    )
    def test_moved_tag_whose_commit_cannot_be_dated_warned_not_adopted(self, mock_glob: Mock):
        """Test that a tag opted into hash drift is warned about when its new commit cannot be dated, naming why."""
        marker = "  # update-time: allow[hash-drift]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # v1.0{marker}\n")
        workflow_yml.write_text.assert_not_called()
        drifted = _drifted(workflow_yml, version="1.0")
        self.assert_drift_logged(Logger.TAG_DRIFT, drifted)
        self.assert_no_commit_date_logged("actions/checkout", NEW_SHA, "HTTP 403, API rate limit exceeded")

    @patch_github(
        releases=[],
        tags=[github_tag_json("v4.4.0", NEW_SHA)],
        commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE),
    )
    def test_moved_branch_at_a_version_tag_is_pinned_as_that_version(self, mock_glob: Mock):
        """Test that a branch opted into hash drift, moved to a version tag's commit, is pinned as that version."""
        marker = "  # update-time: allow[hash-drift]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # v4.4.0{marker}\n")
        self.assert_pinned_logged("actions/checkout", "4.4.0", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_warnings_logged()

    @patch_github(
        releases=[],
        tags=[github_tag_json("v4.4.0", NEW_SHA)],
        commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE),
    )
    def test_moved_branch_kept_floating_is_re_pinned_as_the_branch(self, mock_glob: Mock):
        """Test that a branch kept floating and opted into hash drift is re-pinned as the branch, past a version tag."""
        marker = "  # update-time: allow[hash-drift] allow[floating-pin]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # main{marker}\n")
        drifted = _drifted(workflow_yml)
        self.assert_adopted_drift_logged(Logger.BRANCH_DRIFT, drifted, "update-time: allow[hash-drift]")
        self.assert_no_warnings_logged()

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE))
    def test_moved_branch_kept_floating_warned_not_repinned(self, mock_glob: Mock):
        """Test that a branch kept floating, though not opted into hash drift, is warned about when it moved."""
        marker = "  # update-time: allow[floating-pin]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main{marker}\n")
        workflow_yml.write_text.assert_not_called()
        drifted = _drifted(workflow_yml)
        self.assert_drift_logged(Logger.BRANCH_DRIFT, drifted)

    @patch_github(releases=[], tags=[github_tag_json("v4.3.1", OLD_SHA)], commit=github_commits_json(OLD_SHA))
    def test_pinned_branch_at_a_commit_now_tagged_is_pinned_as_that_version(self, mock_glob: Mock):
        """Test that a branch pinned to the commit it still points at is pinned as the version tag now there."""
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{OLD_SHA} # v4.3.1\n")
        self.assert_pinned_logged("actions/checkout", "4.3.1", OLD_SHA, Location(workflow_yml, 1))
        self.assert_no_warnings_logged()

    def test_unmoved_branch_kept_floating_is_left_alone_and_reported_as_kept(self, mock_glob: Mock):
        """Test that a pinned branch kept floating, still at its commit, is reported with what it resolves to."""
        marker = "  # update-time: allow[floating-pin]"
        cases = {"owner/untagged": ([], "main"), "owner/tagged": ([github_tag_json("v4.3.1", OLD_SHA)], "4.3.1")}
        for dependency, (tags, resolved) in cases.items():
            with self.subTest(dependency), patch_github(releases=[], tags=tags, commit=github_commits_json(OLD_SHA)):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: {dependency}@{OLD_SHA} # main{marker}\n")
                workflow_yml.write_text.assert_not_called()
                release = DependencyVersion(resolved, sha=OLD_SHA)
                cause = "update-time: allow[floating-pin]"
                self.assert_kept_branch_logged(dependency, "main", release, Location(workflow_yml, 1), cause)
                self.assert_no_warnings_logged()

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA))
    def test_inverted_item_on_a_branch_is_reported_and_the_branch_still_pinned(self, mock_glob: Mock):
        """Test that an inverted comparison on a branch reference is reported, the branch pinned as usual."""
        marker = "  # update-time: ignore[cooldown>=30]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@main{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # main{marker}\n")
        self.assert_logged_among_others(
            Logger._MESSAGE_INVERTED_COOLDOWN_ITEM,
            item="cooldown>=30",
            dependency="actions/checkout",
            location=Location(workflow_yml, 1),
        )

    def test_pin_branch_by_its_whole_name(self, mock_glob: Mock):
        """Test that a branch is looked up and pinned by its whole name, a leading `v` or a slash included."""
        for branch in ("vnext", "release/v1"):
            with self.subTest(branch), patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA)):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{branch}\n")
                workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # {branch}\n")
                commits = _commits_url(branch)
                self.assertEqual(github_requests("commits"), [commits])

    def test_pinned_branch_is_read_by_its_whole_name(self, mock_glob: Mock):
        """Test that a pinned branch is looked up by its whole name, a leading `v` or a slash included."""
        for branch in ("vnext", "release/v1"):
            with self.subTest(branch), patch_github(releases=[], tags=[], commit=github_commits_json(OLD_SHA)):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # {branch}\n")
                workflow_yml.write_text.assert_not_called()
                commits = _commits_url(branch)
                self.assertEqual(github_requests("commits"), [commits])

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA, date=FRESH_DATE))
    def test_moved_branch_inside_the_cooldown_warned_not_adopted(self, mock_glob: Mock):
        """Test that a branch opted into hash drift is warned about while its new commit is inside the cooldown."""
        marker = "  # update-time: allow[hash-drift]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main{marker}\n")
        workflow_yml.write_text.assert_not_called()
        drifted = _drifted(workflow_yml)
        self.assert_drift_logged(Logger.BRANCH_DRIFT, drifted)

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE))
    def test_flag_adopts_moved_branch_repo_wide(self, mock_glob: Mock):
        """Test that the `--allow-hash-drift` flag re-pins a moved branch to the commit it now points at."""
        with hash_drift_allowed:
            workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # main\n")
        drifted = _drifted(workflow_yml)
        self.assert_adopted_drift_logged(Logger.BRANCH_DRIFT, drifted, "--allow-hash-drift")
        self.assert_no_warnings_logged()

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA))
    def test_a_floating_pin_directive_on_a_held_back_branch_is_reported(self, mock_glob: Mock):
        """Test that a branch whose update is held back stays unpinned, its `allow[floating-pin]` reported."""
        for marker in ("ignore[update] allow[floating-pin]", "ignore allow[floating-pin]"):
            with self.subTest(marker=marker):
                workflow_yml = self.scanned_workflow(
                    mock_glob, f"uses: actions/checkout@main  # update-time: {marker}\n"
                )
                workflow_yml.write_text.assert_not_called()
                self.assert_redundant_directive_logged(
                    Reason.UPDATE_HELD_BACK, "actions/checkout", Location(workflow_yml, 1), "allow[floating-pin]"
                )

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA))
    def test_a_floating_pin_directive_on_a_bare_commit_sha_is_reported(self, mock_glob: Mock):
        """Test that a bare commit SHA is left as it is, its `allow[floating-pin]` reported as redundant."""
        workflow_yml = self.scanned_workflow(
            mock_glob, f"uses: actions/checkout@{OLD_SHA}  # update-time: allow[floating-pin]\n"
        )
        workflow_yml.write_text.assert_not_called()
        self.assert_redundant_directive_logged(
            Reason.PIN_NOT_FLOATING, "actions/checkout", Location(workflow_yml, 1), "allow[floating-pin]"
        )

    @kills(
        Mutation(
            sources_github._highest_version_at,
            "tagged = _tagged_versions(owner, repository) or ()",
            "tagged = _tagged_versions(owner, repository)",
            "a branch whose repository's tags cannot be listed ends the run rather than being pinned as the branch",
            raises="TypeError: 'NoneType' object is not iterable",
        )
    )
    @patch_github(releases=None, tags=None, commit=github_commits_json(NEW_SHA))
    def test_pin_branch_as_the_branch_when_its_tags_cannot_be_listed(self, mock_glob: Mock):
        """Test that a branch is pinned to its commit as the branch when the repository's tags cannot be listed."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@main\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # main\n")
        self.assert_pinned_logged("actions/checkout", "main", NEW_SHA, Location(workflow_yml, 1))

    @patch_github(releases=[], tags=[github_tag_json("v4.3.0", NEW_SHA)], commit=github_commits_json(NEW_SHA))
    def test_marker_keeps_the_branch_floating(self, mock_glob: Mock):
        """Test that a marker allowing the floating pin leaves a branch as it is, naming what it resolves to."""
        marker = "  # update-time: allow[floating-pin]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@main{marker}\n")
        workflow_yml.write_text.assert_not_called()
        location = Location(workflow_yml, 1)
        resolved = DependencyVersion("4.3.0", sha=NEW_SHA)
        self.assert_kept_branch_logged(
            "actions/checkout", "main", resolved, location, "update-time: allow[floating-pin]"
        )
        self.assert_no_warnings_logged()

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA))
    def test_flag_keeps_every_branch_floating(self, mock_glob: Mock):
        """Test that --allow-floating-pin leaves a branch reference as it is, naming the flag as what kept it."""
        with floating_pin_allowed:
            workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@main\n")
        workflow_yml.write_text.assert_not_called()
        location = Location(workflow_yml, 1)
        resolved = DependencyVersion("main", sha=NEW_SHA)
        self.assert_kept_branch_logged("actions/checkout", "main", resolved, location, "--allow-floating-pin")
        self.assert_no_warnings_logged()

    @patch_github(
        releases=[github_release_json("v1.0", published_at=FRESH_DATE)],
        tags=[],
        commit=github_commits_json(NEW_SHA),
        archived=True,
    )
    def test_archived_repository_of_a_branch_reference_warned(self, mock_glob: Mock):
        """Test that an action referenced by a branch is warned about when GitHub declares its repository archived."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@main\n")
        self.assert_archived_repository_logged("actions/checkout", Location(workflow_yml, 1))

    @kills(
        Mutation(
            sources_github,
            "@archival_reporting\ndef project(dependency: DependencyName, *, check_archival: bool) -> Project:",
            "def project(dependency: DependencyName, *, check_archival: bool) -> Project:",
            "the source claims to report no archival, so switching the staleness check off leaves it unasked",
        )
    )
    @patch_github(
        releases=[github_release_json("v1.0", published_at=FRESH_DATE)],
        tags=[],
        commit=github_commits_json(NEW_SHA),
        archived=True,
    )
    def test_a_branch_reference_is_looked_up_with_the_staleness_check_switched_off(self, mock_glob: Mock):
        """Test that `--stale-after 0` still asks GitHub about a branch reference, since GitHub reports archival."""
        with staleness_disabled:
            workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@main\n")
        self.assert_archived_repository_logged("actions/checkout", Location(workflow_yml, 1))
