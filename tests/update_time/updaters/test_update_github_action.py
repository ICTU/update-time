"""Unit tests for the GitHub Action update script."""

import itertools
from datetime import timedelta
from logging import ERROR, WARNING
from typing import cast
from unittest.mock import Mock, patch

import requests

from update_time.domain.bound import NO_BOUND, Redundancy, Verb, VersionBound
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion, PinnedDependency, Project, Release
from update_time.domain.reference import DriftedPin, RefKind
from update_time.io.log import Drift, Logger
from update_time.markers.directive import Reason
from update_time.primitives.location import Location
from update_time.references import github as references_github
from update_time.sources import github as sources_github
from update_time.updaters import update_github_action
from update_time.updaters.update_github_action import update_github_actions

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import COMMIT_SHA1 as OLD_SHA
from tests.update_time.fixtures import COMMIT_SHA2 as NEW_SHA
from tests.update_time.fixtures import COMMIT_SHA3 as PAST_COOLDOWN_SHA
from tests.update_time.fixtures import FRESH_DATE, GITHUB_UNCACHED, PAST_COOLDOWN_DATE, STALE_DATE
from tests.update_time.helpers import (
    GITHUB_RATE_LIMITED_REASON,
    LoggingTestCase,
    bound,
    floating_pin_allowed,
    full_commit_sha,
    github_commit_listing_queries,
    github_commits_json,
    github_fresh_line,
    github_not_found,
    github_rate_limited,
    github_refs_whose_commits_were_listed,
    github_release_json,
    github_requests,
    github_tag_json,
    github_unknown_ref,
    hash_drift_allowed,
    no_other_version_at_the_tag,
    patch_github,
    staleness_disabled,
)
from tests.update_time.updaters.helpers import github_version

# The directives the tests write in a marker, and expect named as what opted a reference in.
_ALLOW_HASH_DRIFT = "update-time: allow[hash-drift]"
_ALLOW_FLOATING_PIN = "update-time: allow[floating-pin]"
# As many tags as GitHub lists on its first page, naming neither a version nor a ref a test pins.
_FULL_PAGE_OF_TAGS = [github_tag_json(f"build-{index:03}", PAST_COOLDOWN_SHA) for index in range(100)]


def _drifted(
    workflow_yml: Mock, dependency: str = "actions/checkout", version: str = "main", new_sha: str = NEW_SHA
) -> DriftedPin:
    """Return the drift of the workflow's first reference, pinned to the old commit, onto the new one by default."""
    return DriftedPin(dependency, version, Location(workflow_yml, 1), OLD_SHA, new_sha=new_sha)


def _commits_url(ref: str, dependency: str = "actions/checkout") -> str:
    """Return the URL the GitHub API serves the commit the ref names at."""
    return f"https://api.github.com/repos/{dependency}/commits/{ref}"


@patch("update_time.references.github.get_latest_version")
@no_other_version_at_the_tag
@patch("pathlib.Path.glob")
class UpdateGitHubActionsTest(LoggingTestCase):
    """Unit tests for the GitHub Actions updater, with the GitHub source's getters patched."""

    @staticmethod
    def drifted(workflow_yml: Mock) -> DriftedPin:
        """Return the drifted pin the moved `action/action` version tag these tests use produces."""
        return _drifted(workflow_yml, "action/action", "1.0")

    @staticmethod
    def assert_resolved(
        mock_get_latest_version: Mock, version: str = "v4", version_bound: VersionBound = NO_BOUND
    ) -> None:
        """Assert the source was asked once about `actions/checkout` at the version, under the bound the marker sets."""
        mock_get_latest_version.assert_called_once_with(
            PinnedDependency("actions/checkout", version), version_bound, COOLDOWN.default, check_archival=True
        )

    def test_multiple_files(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that actions are updated in all YAML files under the GitHub directory, not just workflows."""
        mock_get_latest_version.return_value = github_version("1.1")
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

    @patch_github(commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE))
    def test_allow_hash_drift_marker_adopts_moved_tag(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an `allow[hash-drift]` marker re-pins a moved tag to its new commit, dated by that commit's SHA."""
        mock_get_latest_version.return_value = github_version("1.0")
        marker = f"  # {_ALLOW_HASH_DRIFT}"
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0{marker}\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_called_once_with(f"uses: action/action@{NEW_SHA} # v1.0{marker}\n")
        self.assertEqual(github_requests("commits"), [_commits_url(NEW_SHA, "action/action")])
        self.assert_adopted_drift_logged(Logger.TAG_DRIFT, self.drifted(workflow_yml), _ALLOW_HASH_DRIFT)
        self.assert_no_warnings_logged()

    @patch_github(commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE))
    def test_flag_adopts_moved_tag_repo_wide(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that the `--allow-hash-drift` flag re-pins a moved tag to the commit it now points at."""
        mock_get_latest_version.return_value = github_version("1.0")
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], []]
        with hash_drift_allowed:
            update_github_actions()
        workflow_yml.write_text.assert_called_once_with(f"uses: action/action@{NEW_SHA} # v1.0\n")
        self.assert_adopted_drift_logged(Logger.TAG_DRIFT, self.drifted(workflow_yml), "--allow-hash-drift")
        self.assert_no_warnings_logged()

    def test_stale_action_warned(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an action whose newest release is old is warned about, even when it is up to date."""
        newest = Release("1.2", STALE_DATE)
        project = Project(newest=newest)
        mock_get_latest_version.return_value = DependencyVersion(version="1.0", sha=OLD_SHA, project=project)
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_not_called()
        self.assert_stale_dependency_logged("action/action", "1.2", Location(workflow_yml, 1))

    def test_pin_unpinned_action(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an action referenced by version tag only is pinned to the commit SHA with a version comment."""
        mock_get_latest_version.return_value = github_version("4.1.1")
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
        mock_get_latest_version.return_value = github_version("4.1.1")
        workflow_yml = mock_path("uses: actions/checkout@v4.1.1\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_called_with(f"uses: actions/checkout@{NEW_SHA} # v4.1.1\n")
        self.assert_resolved(mock_get_latest_version, "v4.1.1")
        self.assert_path_logged(workflow_yml)
        self.assert_pinned_logged("actions/checkout", "4.1.1", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_allow_update_bound_passes_bound_and_pins(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an `allow[update<…>]` marker passes the bound to the source and pins the bounded release."""
        mock_get_latest_version.return_value = github_version("4.2.0")
        workflow_yml = mock_path("uses: actions/checkout@v4  # update-time: allow[update<5]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_called_with(
            f"uses: actions/checkout@{NEW_SHA} # v4.2.0  # update-time: allow[update<5]\n"
        )
        self.assert_resolved(mock_get_latest_version, "v4", bound(Verb.ALLOW, "update<5"))
        self.assert_pinned_logged("actions/checkout", "4.2.0", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_warnings_logged()  # a `<5` bound on a v4 pin is live, so no redundancy warning

    def test_level_bound_passes_bound_and_pins(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an `ignore[major-update]` marker passes the level bound to the source."""
        mock_get_latest_version.return_value = github_version("4.2.0")
        workflow_yml = mock_path("uses: actions/checkout@v4  # update-time: ignore[major-update]\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_called_with(
            f"uses: actions/checkout@{NEW_SHA} # v4.2.0  # update-time: ignore[major-update]\n"
        )
        self.assert_resolved(mock_get_latest_version, "v4", bound(Verb.IGNORE, "major-update"))
        self.assert_pinned_logged("actions/checkout", "4.2.0", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_warnings_logged()  # a major-update bound on a v4 pin is live, so no redundancy warning

    def test_inline_ignore_marker_leaves_action_untouched(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that an inline `# update-time: ignore` comment leaves the action untouched, looking up no version."""
        workflow_yml = mock_path(f"uses: action/action@{OLD_SHA} # v1.0  # update-time: ignore\n")
        mock_glob.side_effect = [[workflow_yml], []]
        update_github_actions()
        workflow_yml.write_text.assert_not_called()
        mock_get_latest_version.assert_not_called()
        self.assert_ignored_logged("action/action", Location(workflow_yml, 1))
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_preceding_ignore_marker_leaves_action_untouched(self, mock_glob: Mock, mock_get_latest_version: Mock):
        """Test that a standalone `# update-time: ignore` comment leaves the action on the line below it untouched."""
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
        newest = Release("1.2", STALE_DATE)
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
        newest = Release("1.2", STALE_DATE)
        mock_latest.return_value = github_version("1.1", Project(newest=newest))
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

    @patch_github(
        releases=[github_release_json("v1.0", published_at=STALE_DATE)], tags=[], commit=github_commits_json(NEW_SHA)
    )
    def test_stale_branch_reference_warned(self, mock_glob: Mock):
        """Test that an action referenced by a branch is warned about when its repository's newest release is old."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@main\n")
        self.assert_stale_dependency_logged("actions/checkout", "1.0", Location(workflow_yml, 1))

    @patch_github(
        releases=[github_release_json("v1.0", published_at=STALE_DATE)], tags=[], commit=github_commits_json(NEW_SHA)
    )
    def test_an_ignore_stale_marker_on_a_branch_reference_silences_the_warning(self, mock_glob: Mock):
        """Test that `ignore[stale]` on a branch reference silences the warning its old repository would get."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@main  # update-time: ignore[stale]\n")
        self.assert_ignored_staleness_logged("actions/checkout", Location(workflow_yml, 1), "ignore[stale]")
        self.assert_no_warnings_logged()

    @patch_github(
        releases=[github_release_json("v1.0", published_at=PAST_COOLDOWN_DATE)],
        tags=[],
        commit=github_commits_json(NEW_SHA),
    )
    def test_a_branch_reference_is_warned_about_at_its_own_threshold(self, mock_glob: Mock):
        """Test that `ignore[stale<90]` on a branch reference warns at 90 days, where the default 365 would not."""
        workflow = "uses: actions/checkout@main  # update-time: ignore[stale<90]\n"
        workflow_yml = self.scanned_workflow(mock_glob, workflow)
        self.assert_stale_dependency_logged("actions/checkout", "1.0", Location(workflow_yml, 1))

    @kills(GITHUB_UNCACHED)
    @patch_github(
        releases=[github_release_json("v1.0", published_at=STALE_DATE)], tags=[], commit=github_commits_json(NEW_SHA)
    )
    def test_two_references_to_one_branch_ask_for_its_releases_and_commit_once(self, mock_glob: Mock):
        """Test that two references to one branch ask for its repository's releases and its commit once each."""
        workflow_ymls = [mock_path("uses: actions/checkout@main\n"), mock_path("uses: actions/checkout@main\n")]
        mock_glob.side_effect = [workflow_ymls, []]
        update_github_actions()
        self.assertEqual(len(github_requests("releases")), 1)
        self.assertEqual(len(github_requests("commits")), 1)
        self.assert_stale_dependency_logged(
            "actions/checkout", "1.0", Location(workflow_ymls[0], 1), Location(workflow_ymls[1], 1)
        )

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA))
    def test_a_branch_reference_whose_repository_has_released_nothing(self, mock_glob: Mock):
        """Test that a branch reference is not warned about when its repository has published no release to date."""
        self.scanned_workflow(mock_glob, "uses: actions/checkout@main\n")
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            references_github._PinResolver._report_redundant_directives,
            "if names_a_commit and (cooldown := ",
            "if (cooldown := ",
            "a cooldown on a branch is reported as redundant, although it holds the branch's drift back",
        )
    )
    def test_cooldown_marker_is_not_reported_as_redundant(self, mock_glob: Mock):
        """Test that a `cooldown` marker on a version, or what this run pins as one or as a branch, is not reported."""
        marker = "  # update-time: ignore[cooldown<30]"
        cases = {
            "version": (f"{OLD_SHA} # v1.0", [], f"{NEW_SHA} # v1.1"),
            "branch": ("main", [], f"{NEW_SHA} # main"),
            "short commit SHA a version tag names": (
                "3e8a870",
                [github_tag_json("v6.0.0", NEW_SHA)],
                f"{NEW_SHA} # v6.0.0",
            ),
        }
        commit = github_commits_json(NEW_SHA)
        for case, (ref, tags, pinned) in cases.items():
            with self.subTest(case), patch_github(releases=[github_release_json("v1.1")], tags=tags, commit=commit):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: action/action@{ref}{marker}\n")
                workflow_yml.write_text.assert_called_once_with(f"uses: action/action@{pinned}{marker}\n")
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

    def test_a_bare_ignore_asks_github_nothing(self, mock_glob: Mock):
        """Test that a bare `ignore` on a version tag or a short commit SHA leaves it as it is, GitHub unasked."""
        tags = [github_tag_json("v4", NEW_SHA)]
        cases = {"version tag": "v4", "short commit SHA": "3e8a870", "short commit SHA parsing as a version": "1234567"}
        for case, ref in cases.items():
            with self.subTest(case), patch_github(releases=[github_release_json("v4.1.1")], tags=tags):
                workflow_yml = self.scanned_workflow(
                    mock_glob, f"uses: actions/checkout@{ref}  # update-time: ignore\n"
                )
                workflow_yml.write_text.assert_not_called()
                cast("Mock", requests.get).assert_not_called()
                self.assert_ignored_logged("actions/checkout", Location(workflow_yml, 1))

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

    @kills(
        Mutation(
            references_github._PinResolver._report_unfetched_commit,
            "reference.current_sha and found.absent",
            "found.absent",
            "a reference to a branch that is not in the repository is left as it is without an error",
        ),
        Mutation(
            references_github._PinResolver._report_unfetched_commit,
            "reference.current_sha and found.absent",
            "reference.current_sha",
            "a pinned branch whose commit cannot be fetched is left as it is without an error",
        ),
    )
    def test_branch_whose_commit_cannot_be_fetched_is_left_alone_and_reported_as_an_error(self, mock_glob: Mock):
        """Test that a branch whose commit cannot be fetched is left as it is, the reason logged as an error."""
        unknown = "HTTP 422, No commit found for SHA: pinned"
        cases = {
            "branch": ("main", "main", github_rate_limited(), GITHUB_RATE_LIMITED_REASON),
            "pinned branch": (f"{OLD_SHA} # main", "main", github_rate_limited(), GITHUB_RATE_LIMITED_REASON),
            "unknown branch": ("pinned", "pinned", github_unknown_ref("pinned"), unknown),
        }
        for case, (ref, branch, commit, reason) in cases.items():
            with self.subTest(case), patch_github(releases=[], tags=[], commit=commit):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{ref}\n")
                workflow_yml.write_text.assert_not_called()
                self.assert_unpinned_ref_logged("actions/checkout", branch, Location(workflow_yml, 1), reason)
                self.assert_no_warnings_logged()

    @kills(
        Mutation(
            sources_github._newest_on_the_line,
            "cooldown_days > 0 and within_cooldown(",
            "within_cooldown(",
            "a zero-day cooldown skips the head of a branch when its committer dated it in the future",
        )
    )
    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA, date=FRESH_DATE + timedelta(days=1)))
    def test_branch_under_a_zero_day_cooldown_moves_to_its_head_whatever_its_date(self, mock_glob: Mock):
        """Test that a branch under a zero-day cooldown lists its head alone, and moves to it however it is dated."""
        marker = "  # update-time: allow[hash-drift] allow[cooldown>=0]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # main{marker}\n")
        self.assertEqual(github_commit_listing_queries(), [{"sha": ["main"], "per_page": ["1"]}])

    @kills(
        Mutation(
            sources_github.newest_commit_past_cooldown,
            "if response is None or not response.ok:",
            "if response is None or not (response.ok or commits):",
            "a page after the first that GitHub refuses ends the run rather than being reported",
            raises="TypeError: string indices must be integers, not 'str'",
        )
    )
    def test_pinned_branch_whose_commits_cannot_be_listed_is_left_alone_and_reported_as_an_error(self, mock_glob: Mock):
        """Test that a pinned branch whose commits cannot be listed is left as it is, the reason logged as an error."""
        commit = github_commits_json(NEW_SHA, date=FRESH_DATE)
        past = github_commits_json(PAST_COOLDOWN_SHA, date=PAST_COOLDOWN_DATE, parents=[OLD_SHA])
        beyond = "the branch's newest commit older than the cooldown is not among the 500 newest commits examined"
        # Each case names the listing, the reason the branch stays put, and the number of pages the run requests.
        cases: dict[str, tuple[list | Mock, str, int]] = {
            "rate limited": (github_rate_limited(), GITHUB_RATE_LIMITED_REASON, 1),
            "second page rate limited": (
                [*github_fresh_line(100, PAST_COOLDOWN_SHA), github_rate_limited()],
                GITHUB_RATE_LIMITED_REASON,
                2,
            ),
            "line beyond the pages examined": ([*github_fresh_line(500, PAST_COOLDOWN_SHA), past], beyond, 5),
        }
        for case, (listed_commits, reason, pages) in cases.items():
            with self.subTest(case), patch_github(releases=[], tags=[], commit=commit, listed_commits=listed_commits):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main\n")
                workflow_yml.write_text.assert_not_called()
                self.assert_unpinned_ref_logged("actions/checkout", "main", Location(workflow_yml, 1), reason)
                self.assert_no_warnings_logged()
                self.assertEqual(len(github_commit_listing_queries()), pages)

    @kills(
        Mutation(
            sources_github.is_tag,
            "return None",
            "pass",
            "a ref whose commit and repository's tags cannot be fetched ends the run rather than being reported",
            raises="TypeError: 'NoneType' object is not iterable",
        )
    )
    @patch_github(releases=None, tags=None, commit=github_rate_limited())
    def test_ref_whose_commit_and_tags_cannot_be_fetched_is_reported_as_a_ref(self, mock_glob: Mock):
        """Test that a ref is reported as a ref, neither branch nor tag, when GitHub refuses its commit and the tags."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@stable-2024\n")
        workflow_yml.write_text.assert_not_called()
        location, reason = Location(workflow_yml, 1), GITHUB_RATE_LIMITED_REASON
        self.assert_unpinned_ref_logged("actions/checkout", "stable-2024", location, reason, ref=RefKind.REF)

    @kills(
        Mutation(
            sources_github._looked_up_tag,
            "return False",
            "return True",
            "a branch in a repository with a full page of tags is followed as a tag",
        )
    )
    def test_pinned_branch_still_at_its_commit_is_left_alone(self, mock_glob: Mock):
        """Test that a branch still at its pinned commit is left alone, looked up as a tag only past a full page."""
        lookup = "https://api.github.com/repos/actions/checkout/git/ref/tags/main"
        for case, (tags, lookups) in {
            "no tags": ([], []),
            "a full page of tags": (_FULL_PAGE_OF_TAGS, [lookup]),
        }.items():
            with self.subTest(case), patch_github(releases=[], tags=tags, commit=github_commits_json(OLD_SHA)):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main\n")
                workflow_yml.write_text.assert_not_called()
                self.assertEqual(github_refs_whose_commits_were_listed(), ["main"])
                self.assertEqual(len(github_requests("commits")), 1)
                self.assertEqual(github_requests("git"), lookups)
                self.assert_no_info_logged()
                self.assert_no_warnings_logged()

    def test_moved_branch_warned_about_its_newest_commit_past_the_cooldown(self, mock_glob: Mock):
        """Test that a moved branch is warned about the newest commit older than the cooldown on its own line."""
        commit = github_commits_json(NEW_SHA, date=FRESH_DATE)
        past = github_commits_json(PAST_COOLDOWN_SHA, date=PAST_COOLDOWN_DATE, parents=[OLD_SHA])
        listings = {
            "head older than the cooldown": [past],
            "a page of fresh commits above it": [*github_fresh_line(100, PAST_COOLDOWN_SHA), past],
        }
        for listing, listed_commits in listings.items():
            with (
                self.subTest(listing),
                patch_github(releases=[], tags=[], commit=commit, listed_commits=listed_commits),
            ):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main\n")
                workflow_yml.write_text.assert_not_called()
                self.assert_drift_logged(Logger.BRANCH_DRIFT, _drifted(workflow_yml, new_sha=PAST_COOLDOWN_SHA))

    @kills(
        Mutation(
            sources_github._newest_on_the_line,
            'commit = listed.get(parents[0]["sha"]) if parents else None',
            'commit = listed.get(parents[0]["sha"])',
            "a branch whose whole history is fresh ends the run rather than being held back",
            raises="IndexError: list index out of range",
        )
    )
    def test_branch_moved_only_inside_the_cooldown_is_left_alone(self, mock_glob: Mock):
        """Test that a pinned branch whose own line moved only inside the cooldown is left alone, whatever it merged."""
        commit = github_commits_json(NEW_SHA, date=FRESH_DATE)
        pin = github_commits_json(OLD_SHA, date=STALE_DATE)
        merge = github_commits_json(NEW_SHA, date=FRESH_DATE, parents=[OLD_SHA, PAST_COOLDOWN_SHA])
        merged = github_commits_json(PAST_COOLDOWN_SHA, date=PAST_COOLDOWN_DATE, parents=[OLD_SHA])
        listings = {
            "pin older than the cooldown": [pin],
            "merged branch older than the cooldown": [merge, merged, pin],
            "every commit fresh": [commit],  # The head is the first commit of a fresh history
        }
        markers = {"not opted into drift": "", "opted into drift": f"  # {_ALLOW_HASH_DRIFT}"}
        for (listing, listed_commits), (opt_in, marker) in itertools.product(listings.items(), markers.items()):
            with (
                self.subTest(listing=listing, opt_in=opt_in),
                patch_github(releases=[], tags=[], commit=commit, listed_commits=listed_commits),
            ):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main{marker}\n")
                workflow_yml.write_text.assert_not_called()
                self.assertEqual(github_refs_whose_commits_were_listed(), ["main"])
                self.assert_no_warnings_logged()
                self.assert_no_info_logged()

    @kills(
        Mutation(
            references_github._PinResolver._moved_ref_pin,
            "if kind is RefKind.BRANCH and not self._branch_moved_on(reference, moved):",
            "if False:",
            "a branch pinned to a fresh commit moves back to the earlier commit that is older than the cooldown",
        )
    )
    def test_branch_whose_newest_commit_past_the_cooldown_is_older_than_its_pin_is_left_alone(self, mock_glob: Mock):
        """Test that a pinned branch never moves back to a commit older than its pin, and is not warned about."""
        commit = github_commits_json(NEW_SHA, date=FRESH_DATE)
        listed_commits = [github_commits_json(PAST_COOLDOWN_SHA, date=PAST_COOLDOWN_DATE)]
        for case, marker in {"not opted into drift": "", "opted into drift": f"  # {_ALLOW_HASH_DRIFT}"}.items():
            with (
                self.subTest(case),
                patch_github(
                    releases=[],
                    tags=[],
                    commit=commit,
                    listed_commits=listed_commits,
                    comparison_status="behind",
                ),
            ):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main{marker}\n")
                workflow_yml.write_text.assert_not_called()
                self.assert_no_warnings_logged()
                self.assert_no_info_logged()
                compare = f"https://api.github.com/repos/actions/checkout/compare/{OLD_SHA}...{PAST_COOLDOWN_SHA}"
                self.assertEqual(github_requests("compare"), [f"{compare}?per_page=1"])

    @kills(
        Mutation(
            references_github._PinResolver._branch_moved_on,
            "self.log.unchecked_branch_drift(reference, moved.sha, compared.reason)",
            "pass",
            "a branch whose drift could not be checked is left as it is without an error",
        )
    )
    @patch_github(
        releases=[],
        tags=[],
        commit=github_commits_json(NEW_SHA, date=FRESH_DATE),
        listed_commits=[github_commits_json(PAST_COOLDOWN_SHA, date=PAST_COOLDOWN_DATE)],
        comparison_status=github_rate_limited(),
    )
    def test_branch_whose_move_cannot_be_compared_is_left_alone_and_reported_as_an_error(self, mock_glob: Mock):
        """Test that a pinned branch is left as it is when GitHub can't compare its pin with the commit it moved to."""
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main\n")
        workflow_yml.write_text.assert_not_called()
        self.assert_error_logged(
            Logger._MESSAGE_UNCHECKED_BRANCH_DRIFT,
            dependency="actions/checkout",
            name="main",
            location=Location(workflow_yml, 1),
            new_sha=PAST_COOLDOWN_SHA,
            reason=GITHUB_RATE_LIMITED_REASON,
        )
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            sources_github,
            '_MOVED_ON = frozenset({"ahead", "diverged"})',
            '_MOVED_ON = frozenset({"ahead"})',
            "a branch force-pushed past its pin is held there silently, its drift unreported",
        )
    )
    @patch_github(
        releases=[],
        tags=[],
        commit=github_commits_json(NEW_SHA, date=FRESH_DATE),
        listed_commits=[github_commits_json(PAST_COOLDOWN_SHA, date=PAST_COOLDOWN_DATE)],
        comparison_status="diverged",
    )
    def test_branch_force_pushed_past_its_pin_is_warned_about(self, mock_glob: Mock):
        """Test that a branch whose newest commit older than the cooldown diverged from the pin is warned about."""
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main\n")
        workflow_yml.write_text.assert_not_called()
        self.assert_drift_logged(Logger.BRANCH_DRIFT, _drifted(workflow_yml, new_sha=PAST_COOLDOWN_SHA))

    @patch_github(
        releases=[],
        tags=[],
        commit=github_commits_json(NEW_SHA, date=FRESH_DATE),
        listed_commits=[github_commits_json(PAST_COOLDOWN_SHA, date=PAST_COOLDOWN_DATE)],
        comparison_status="diverged",
    )
    def test_branch_force_pushed_past_its_pin_is_adopted_when_opted_into_drift(self, mock_glob: Mock):
        """Test that a branch opted into drift adopts its newest commit older than the cooldown, diverged or not."""
        marker = f"  # {_ALLOW_HASH_DRIFT}"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{PAST_COOLDOWN_SHA} # main{marker}\n")
        drifted = _drifted(workflow_yml, new_sha=PAST_COOLDOWN_SHA)
        self.assert_adopted_drift_logged(Logger.BRANCH_DRIFT, drifted, _ALLOW_HASH_DRIFT)

    @kills(
        Mutation(
            references_github._PinResolver._ref_commit,
            "if reference.current_sha and _ref_kind(reference) is RefKind.BRANCH:",
            "if reference.current_sha:",
            "a moved tag is held to an older commit on its history, which the tag never named",
        ),
        Mutation(
            sources_github._looked_up_tag,
            "_LOG.response(response)",
            "pass",
            "a ref GitHub refuses to look up as a tag is reported as a ref without the refusal being logged",
        ),
        Mutation(
            sources_github,
            "@cache\ndef _looked_up_tag(",
            "def _looked_up_tag(",
            "a refused tag lookup is logged once for every time the run asks what kind of ref the reference names",
        ),
    )
    def test_moved_tag_naming_no_version_warned_as_tag_drift_or_else_as_ref_drift(self, mock_glob: Mock):
        """Test that a moved tag is warned about as tag drift, or as ref drift when GitHub refuses to say it is a tag.

        The warning names the commit the tag points at, never an earlier commit older than the cooldown.
        """
        moved = github_tag_json("stable-2024", NEW_SHA)
        cases: dict[str, tuple[list | None, Mock | Exception | None, Drift]] = {  # Tags, tag lookup, and drift
            "listed tag": ([moved], None, Logger.TAG_DRIFT),
            "tag past the first page": ([*_FULL_PAGE_OF_TAGS, moved], None, Logger.TAG_DRIFT),
            "tag lookup refused": ([*_FULL_PAGE_OF_TAGS, moved], github_rate_limited(), Logger.REF_DRIFT),
            "tag lookup failed": (
                [*_FULL_PAGE_OF_TAGS, moved],
                requests.exceptions.ConnectionError("connection refused"),
                Logger.REF_DRIFT,
            ),
            "unlisted tags": (None, None, Logger.REF_DRIFT),
        }
        commit = github_commits_json(NEW_SHA)
        listed_commits = [github_commits_json(PAST_COOLDOWN_SHA, date=PAST_COOLDOWN_DATE)]
        for case, (tags, tag_ref, kind) in cases.items():
            with (
                self.subTest(case),
                patch_github(releases=[], tags=tags, commit=commit, listed_commits=listed_commits, tag_ref=tag_ref),
            ):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # stable-2024\n")
                workflow_yml.write_text.assert_not_called()
                drifted = _drifted(workflow_yml, version="stable-2024")
                refused = kind is Logger.REF_DRIFT
                self.assert_drift_logged(kind, drifted, among_others=refused)  # Beside the refused request
                self.assertEqual(len(self.records(WARNING)), 2 if refused else 1)  # The refusal is a warning of its own
                self.assertEqual(github_refs_whose_commits_were_listed(), [])

    @patch_github(releases=[], tags=[github_tag_json("stable-2024", NEW_SHA)], commit=github_commits_json(NEW_SHA))
    def test_pin_tag_naming_no_version_as_the_tag(self, mock_glob: Mock):
        """Test that a reference to a tag such as `stable-2024` is pinned to its commit, the comment naming the tag."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@stable-2024\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # stable-2024\n")
        self.assert_pinned_logged("actions/checkout", "stable-2024", NEW_SHA, Location(workflow_yml, 1))
        self.assert_no_warnings_logged()

    def test_tag_kept_floating_is_reported_as_a_tag_or_else_as_a_ref(self, mock_glob: Mock):
        """Test that a tag kept floating is reported as a tag, or as a ref when the tags cannot be listed."""
        marker = f"  # {_ALLOW_FLOATING_PIN}"
        cases = {
            "listed tag": ([github_tag_json("stable-2024", NEW_SHA)], RefKind.TAG),
            "unlisted tags": (None, RefKind.REF),
        }
        release, cause = DependencyVersion("stable-2024", sha=NEW_SHA), _ALLOW_FLOATING_PIN
        for case, (tags, kind) in cases.items():
            with self.subTest(case), patch_github(releases=[], tags=tags, commit=github_commits_json(NEW_SHA)):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@stable-2024{marker}\n")
                workflow_yml.write_text.assert_not_called()
                location = Location(workflow_yml, 1)
                self.assert_kept_ref_logged("actions/checkout", "stable-2024", release, location, cause, ref=kind)

    @patch_github(
        releases=[],
        tags=[github_tag_json("stable-2024", NEW_SHA)],
        commit=github_rate_limited(),
    )
    def test_tag_whose_commit_cannot_be_fetched_is_reported_as_a_tag(self, mock_glob: Mock):
        """Test that a tag such as `stable-2024` whose commit cannot be fetched is reported as a tag."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@stable-2024\n")
        workflow_yml.write_text.assert_not_called()
        location, reason = Location(workflow_yml, 1), GITHUB_RATE_LIMITED_REASON
        self.assert_unpinned_ref_logged("actions/checkout", "stable-2024", location, reason, ref=RefKind.TAG)

    @patch_github(releases=[], tags=[], commit=github_rate_limited())
    def test_short_commit_sha_whose_commit_cannot_be_fetched_is_reported_as_a_short_commit_sha(self, mock_glob: Mock):
        """Test that a short commit SHA whose commit cannot be fetched is reported as a short commit SHA."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@3e8a870\n")
        workflow_yml.write_text.assert_not_called()
        location, reason = Location(workflow_yml, 1), GITHUB_RATE_LIMITED_REASON
        self.assert_unpinned_ref_logged("actions/checkout", "3e8a870", location, reason, ref=RefKind.SHORT_COMMIT_SHA)

    @patch_github(
        releases=[github_release_json("20240101", PAST_COOLDOWN_DATE)],
        tags=[github_tag_json("20240101", NEW_SHA)],
        commit=github_rate_limited(),
    )
    def test_pinned_version_spelled_in_hex_digits_is_not_asked_about_as_a_commit(self, mock_glob: Mock):
        """Test that a version such as `20240101`, pinned to its commit, is left alone without a commit request."""
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{NEW_SHA} # 20240101\n")
        workflow_yml.write_text.assert_not_called()
        self.assertEqual(github_requests("commits"), [])
        self.assert_no_warnings_logged()

    def test_allow_hash_drift_marker_adopts_moved_ref(self, mock_glob: Mock):
        """Test that an `allow[hash-drift]` marker re-pins a moved branch or tag to its new commit, fetched once."""
        marker = f"  # {_ALLOW_HASH_DRIFT}"
        commit = github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE)
        cases = {
            "main": ([], Logger.BRANCH_DRIFT),
            "stable-2024": ([github_tag_json("stable-2024", NEW_SHA)], Logger.TAG_DRIFT),
        }
        for ref, (tags, kind) in cases.items():
            with self.subTest(ref), patch_github(releases=[], tags=tags, commit=commit):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # {ref}{marker}\n")
                workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # {ref}{marker}\n")
                self.assertEqual(len(github_requests("commits")), 1)
                drifted = _drifted(workflow_yml, version=ref)
                self.assert_adopted_drift_logged(kind, drifted, _ALLOW_HASH_DRIFT)
                self.assert_no_warnings_logged()

    @patch_github(releases=[], tags=None, commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE))
    def test_allow_hash_drift_marker_adopts_moved_ref_as_ref_drift_when_tags_cannot_be_listed(self, mock_glob: Mock):
        """Test that a moved ref opted into hash drift, its tags unlisted, is reported as adopted ref drift."""
        marker = f"  # {_ALLOW_HASH_DRIFT}"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # stable-2024{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # stable-2024{marker}\n")
        drifted = _drifted(workflow_yml, version="stable-2024")
        self.assert_adopted_drift_logged(Logger.REF_DRIFT, drifted, _ALLOW_HASH_DRIFT)

    @patch_github(
        releases=[github_release_json("v1.0", published_at=FRESH_DATE)], tags=[github_tag_json("v1.0", NEW_SHA)]
    )
    def test_pin_version_tag_inside_the_cooldown(self, mock_glob: Mock):
        """Test that a reference to a version tag inside the cooldown is pinned to the commit the tag points at."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@v1.0\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # v1.0\n")
        self.assert_pinned_logged("actions/checkout", "1.0", NEW_SHA, Location(workflow_yml, 1))

    @kills(
        Mutation(
            sources_github._own_tag,
            "if tagged.sha",
            "if True",
            "a release whose tag is unlisted counts as the reference's own tag, so a newer release replaces it",
        )
    )
    @patch_github(
        releases=[
            github_release_json("v2.0", published_at=FRESH_DATE),
            github_release_json("v1.0", published_at=PAST_COOLDOWN_DATE),
        ],
        tags=[],
        commit=github_commits_json(NEW_SHA),
    )
    def test_released_version_whose_tag_is_unlisted_keeps_its_version(self, mock_glob: Mock):
        """Test that a released version whose tag the listing misses is pinned as itself, not as a newer release."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@v1.0\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # v1.0\n")

    @patch_github(releases=[], tags=[github_tag_json("v1.0", NEW_SHA)], commit=github_rate_limited())
    def test_pin_version_tag_whose_commit_cannot_be_dated(self, mock_glob: Mock):
        """Test that a reference to a tag without a release is pinned to its commit, though that commit is undated."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@v1.0\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # v1.0\n")
        self.assert_pinned_logged("actions/checkout", "1.0", NEW_SHA, Location(workflow_yml, 1))

    @kills(
        Mutation(
            sources_github.TaggedVersion.publication,
            "return DeferredLookup(partial(commit_date, self.dependency, self.commit_ref))",
            "return commit_date(self.dependency, self.commit_ref)",
            "the version tag a reference keeps costs a commit request that the cooldown never reads",
        )
    )
    @patch_github(
        releases=[],
        tags=[github_tag_json("v1.1", NEW_SHA), github_tag_json("v1.0", OLD_SHA)],
        commit=github_commits_json(NEW_SHA, date=FRESH_DATE),
    )
    def test_version_tag_the_reference_keeps_is_pinned_without_dating_its_commit(self, mock_glob: Mock):
        """Test that a tag without a release, kept since the newer one is fresh, is pinned without dating its commit."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@v1.0\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{OLD_SHA} # v1.0\n")
        self.assertEqual(github_requests("commits"), [_commits_url(NEW_SHA)])

    @patch_github(
        releases=[
            github_release_json("v4.3.0", published_at=FRESH_DATE),
            github_release_json("v4.0.0", published_at=PAST_COOLDOWN_DATE),
        ],
        tags=[github_tag_json("v4.0.0", OLD_SHA), github_tag_json("v4", NEW_SHA), github_tag_json("v4.3.0", NEW_SHA)],
        commit=github_commits_json(NEW_SHA, date=FRESH_DATE),
    )
    def test_pin_moving_version_tag_to_its_commit_named_by_the_version_there(self, mock_glob: Mock):
        """Test that `@v4` is pinned to the commit `v4` points at, named by the highest version tag there."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@v4\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # v4.3.0\n")
        self.assert_pinned_logged("actions/checkout", "4.3.0", NEW_SHA, Location(workflow_yml, 1))

    @patch_github(
        releases=[github_release_json("v4.0.0", published_at=PAST_COOLDOWN_DATE)],
        tags=[github_tag_json("v4.0.0", OLD_SHA), github_tag_json("v4", NEW_SHA)],
        commit=github_commits_json(NEW_SHA, date=FRESH_DATE),
    )
    def test_pin_moving_version_tag_as_itself_when_no_other_version_tag_is_at_its_commit(self, mock_glob: Mock):
        """Test that `@v4` is pinned to the commit `v4` points at as `v4`, not to `v4.0.0` on another commit."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@v4\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # v4\n")
        self.assert_pinned_logged("actions/checkout", "4", NEW_SHA, Location(workflow_yml, 1))

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

    @patch_github(
        releases=[github_release_json("v1.0", published_at=FRESH_DATE)], tags=[github_tag_json("v1.0", NEW_SHA)]
    )
    def test_moved_tag_inside_the_cooldown_is_warned_about(self, mock_glob: Mock):
        """Test that a tag moved inside the cooldown is warned about when the reference does not opt into hash drift."""
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # v1.0\n")
        workflow_yml.write_text.assert_not_called()
        self.assert_drift_logged(Logger.TAG_DRIFT, _drifted(workflow_yml, version="1.0"))

    def test_moved_tag_inside_the_cooldown_is_held_back_silently(self, mock_glob: Mock):
        """Test that a tag opted into hash drift is left as it is, unreported, while the commit it moved to is fresh."""
        cases = {"owner/released": [github_release_json("v1.0", published_at=PAST_COOLDOWN_DATE)], "owner/tagged": []}
        tags, commit = [github_tag_json("v1.0", NEW_SHA)], github_commits_json(NEW_SHA, date=FRESH_DATE)
        marker = f"  # {_ALLOW_HASH_DRIFT}"
        for dependency, releases in cases.items():
            with self.subTest(dependency), patch_github(releases=releases, tags=tags, commit=commit):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: {dependency}@{OLD_SHA} # v1.0{marker}\n")
                workflow_yml.write_text.assert_not_called()
                self.assertEqual(github_requests("commits"), [_commits_url(NEW_SHA, dependency)])
                self.assert_no_warnings_logged()
                self.assert_no_info_logged()

    @patch_github(
        releases=[github_release_json("v1.0", published_at=PAST_COOLDOWN_DATE)],
        tags=[github_tag_json("v1.0", NEW_SHA)],
        commit=github_rate_limited(),
    )
    def test_moved_tag_whose_commit_cannot_be_dated_is_held_back_with_the_reason(self, mock_glob: Mock):
        """Test that a tag opted into hash drift is left as it is when its new commit cannot be dated, naming why."""
        marker = f"  # {_ALLOW_HASH_DRIFT}"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # v1.0{marker}\n")
        workflow_yml.write_text.assert_not_called()
        self.assert_no_warnings_logged()
        drifted = _drifted(workflow_yml, version="1.0")
        self.assert_undated_commit_logged(Logger.TAG_DRIFT, drifted, GITHUB_RATE_LIMITED_REASON)

    @patch_github(
        releases=[github_release_json("v1.0", published_at=PAST_COOLDOWN_DATE)],
        tags=[github_tag_json("v1.0", NEW_SHA)],
        commit=github_rate_limited(),
    )
    def test_moved_tag_under_a_zero_day_cooldown_is_adopted_without_dating_its_commit(self, mock_glob: Mock):
        """Test that a tag opted into hash drift with a zero-day cooldown is re-pinned, its new commit left undated."""
        marker = "  # update-time: allow[hash-drift] allow[cooldown>=0]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # v1.0{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # v1.0{marker}\n")
        self.assertEqual(github_requests("commits"), [])
        drifted = _drifted(workflow_yml, version="1.0")
        self.assert_adopted_drift_logged(Logger.TAG_DRIFT, drifted, _ALLOW_HASH_DRIFT)

    @patch_github(
        releases=[],
        tags=[github_tag_json("v4.4.0", PAST_COOLDOWN_SHA)],
        commit=github_commits_json(NEW_SHA, date=FRESH_DATE),
        listed_commits=[github_commits_json(PAST_COOLDOWN_SHA, date=PAST_COOLDOWN_DATE)],
    )
    def test_moved_branch_at_a_version_tag_is_pinned_as_that_version(self, mock_glob: Mock):
        """Test that a branch opted into drift adopts its newest commit older than the cooldown as the version there."""
        marker = f"  # {_ALLOW_HASH_DRIFT}"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main{marker}\n")
        workflow_yml.write_text.assert_called_once_with(
            f"uses: actions/checkout@{PAST_COOLDOWN_SHA} # v4.4.0{marker}\n"
        )
        drifted, location = _drifted(workflow_yml, new_sha=PAST_COOLDOWN_SHA), Location(workflow_yml, 1)
        self.assert_adopted_drift_logged(Logger.BRANCH_DRIFT, drifted, _ALLOW_HASH_DRIFT, among_others=True)
        self.assert_pinned_logged("actions/checkout", "4.4.0", PAST_COOLDOWN_SHA, location, among_others=True)
        self.assert_no_warnings_logged()

    @patch_github(
        releases=[],
        tags=[github_tag_json("v4.4.0", PAST_COOLDOWN_SHA)],
        commit=github_commits_json(NEW_SHA, date=FRESH_DATE),
        listed_commits=[github_commits_json(PAST_COOLDOWN_SHA, date=PAST_COOLDOWN_DATE)],
    )
    def test_moved_branch_kept_floating_is_re_pinned_as_the_branch(self, mock_glob: Mock):
        """Test that a branch kept floating is re-pinned to its newest commit older than the cooldown as the branch."""
        marker = "  # update-time: allow[hash-drift] allow[floating-pin]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{PAST_COOLDOWN_SHA} # main{marker}\n")
        drifted = _drifted(workflow_yml, new_sha=PAST_COOLDOWN_SHA)
        self.assert_adopted_drift_logged(Logger.BRANCH_DRIFT, drifted, _ALLOW_HASH_DRIFT)
        self.assert_no_warnings_logged()

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA))
    def test_moved_branch_kept_floating_warned_not_repinned(self, mock_glob: Mock):
        """Test that a branch kept floating, though not opted into hash drift, is warned about when it moved."""
        marker = f"  # {_ALLOW_FLOATING_PIN}"
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
        marker = f"  # {_ALLOW_FLOATING_PIN}"
        cases = {"owner/untagged": ([], "main"), "owner/tagged": ([github_tag_json("v4.3.1", OLD_SHA)], "4.3.1")}
        for dependency, (tags, resolved) in cases.items():
            with self.subTest(dependency), patch_github(releases=[], tags=tags, commit=github_commits_json(OLD_SHA)):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: {dependency}@{OLD_SHA} # main{marker}\n")
                workflow_yml.write_text.assert_not_called()
                release = DependencyVersion(resolved, sha=OLD_SHA)
                cause = _ALLOW_FLOATING_PIN
                self.assert_kept_ref_logged(dependency, "main", release, Location(workflow_yml, 1), cause)
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
        for branch in ("v3-node20", "release/v1"):
            with self.subTest(branch), patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA)):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{branch}\n")
                workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # {branch}\n")
                commits = _commits_url(branch)
                self.assertEqual(github_requests("commits"), [commits])

    def test_pinned_branch_is_read_by_its_whole_name(self, mock_glob: Mock):
        """Test that a pinned branch is looked up by its whole name, a leading `v` or a slash included."""
        for branch in ("v3-node20", "release/v1"):
            with self.subTest(branch), patch_github(releases=[], tags=[], commit=github_commits_json(OLD_SHA)):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # {branch}\n")
                workflow_yml.write_text.assert_not_called()
                self.assertEqual(github_refs_whose_commits_were_listed(), [branch])

    @kills(
        Mutation(
            update_github_action,
            r"(?!{_REF_CHARACTER})",
            r"(?={_ALONE})",
            "a version comment followed by more text leaves the commit SHA bare, as a branch comment does",
        ),
        Mutation(
            update_github_action,
            r"(?!{_REF_CHARACTER})",
            r"(?=\s|$)",
            "a version comment followed by punctuation leaves the commit SHA bare, with nothing logged",
        ),
    )
    def test_version_comment_followed_by_text_is_read_as_the_version(self, mock_glob: Mock):
        """Test that a comment naming a version and then more text is read as that version, the text kept."""
        releases = [github_release_json("v4.2.0", published_at=PAST_COOLDOWN_DATE)]
        for case, text in {"words": " pinned by hand", "punctuation": ", see notes"}.items():
            with self.subTest(case), patch_github(releases=releases, tags=[github_tag_json("v4.2.0", NEW_SHA)]):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # v4{text}\n")
                workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # v4.2.0{text}\n")

    def test_commit_sha_whose_comment_names_no_ref_is_left_alone(self, mock_glob: Mock):
        """Test that a commit SHA is read as bare when its comment does not name a tag or branch."""
        for comment in ("ratchet:actions/checkout@v4", "pin@v4.1.1", "tag=v4.1.1", "pinned by hand", "TODO", "FIXME"):
            with self.subTest(comment), patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA)):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # {comment}\n")
                workflow_yml.write_text.assert_not_called()
                self.assertEqual(github_requests("commits"), [])
                self.assertEqual(len(github_requests("releases")), 1)

    @patch_github(releases=None, tags=None, commit=github_not_found())
    def test_pinned_branch_of_a_repository_that_is_gone_is_left_alone_and_reported_as_an_error(self, mock_glob: Mock):
        """Test that a pinned branch whose repository no longer exists is left as it is, reported as an error."""
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main\n")
        workflow_yml.write_text.assert_not_called()
        location, reason = Location(workflow_yml, 1), "HTTP 404, Not Found"
        self.assert_unpinned_ref_logged("actions/checkout", "main", location, reason, ref=RefKind.REF)

    @kills(
        Mutation(
            references_github._PinResolver._report_unfetched_commit,
            "if reference.current_sha and found.absent:",
            "if False:",
            "a commit SHA whose comment names a deleted branch is reported as an error rather than at DEBUG",
        ),
        Mutation(
            sources_github.newest_commit_past_cooldown,
            "return LookedUp(None, failure_reason(response), absent=True)",
            "return LookedUp(None, failure_reason(response), absent=False)",
            "a pinned branch that was deleted is reported as an error rather than at DEBUG",
        ),
    )
    @patch_github(releases=[], tags=[], listed_commits=github_not_found())
    def test_commit_sha_whose_one_word_comment_names_no_ref_is_left_alone_and_reported_at_debug(self, mock_glob: Mock):
        """Test that a commit SHA is left as it is, reported at DEBUG, when its one-word comment does not name a ref."""
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # pinned\n")
        workflow_yml.write_text.assert_not_called()
        self.assertEqual(github_refs_whose_commits_were_listed(), ["pinned"])
        self.assertEqual(len(github_requests("commits")), 1)
        self.assertEqual(self.records(ERROR), [])
        self.assert_comment_naming_no_ref_logged("actions/checkout", "pinned", Location(workflow_yml, 1))

    @patch_github(
        releases=[],
        tags=[],
        commit=github_commits_json(NEW_SHA, date=FRESH_DATE),
        listed_commits=[github_commits_json(PAST_COOLDOWN_SHA, date=PAST_COOLDOWN_DATE)],
    )
    def test_moved_branch_inside_the_cooldown_is_re_pinned_to_its_newest_commit_past_the_cooldown(
        self, mock_glob: Mock
    ):
        """Test that a branch opted into drift, its head fresh, adopts the newest commit older than the cooldown."""
        marker = f"  # {_ALLOW_HASH_DRIFT}"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{PAST_COOLDOWN_SHA} # main{marker}\n")
        drifted = _drifted(workflow_yml, new_sha=PAST_COOLDOWN_SHA)
        self.assert_adopted_drift_logged(Logger.BRANCH_DRIFT, drifted, _ALLOW_HASH_DRIFT)
        self.assert_no_warnings_logged()

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA, date=PAST_COOLDOWN_DATE))
    def test_flag_adopts_moved_branch_repo_wide(self, mock_glob: Mock):
        """Test that the `--allow-hash-drift` flag re-pins a moved branch to the commit it now points at."""
        with hash_drift_allowed:
            workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # main\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # main\n")
        drifted = _drifted(workflow_yml)
        self.assert_adopted_drift_logged(Logger.BRANCH_DRIFT, drifted, "--allow-hash-drift")
        self.assert_no_warnings_logged()

    @kills(
        Mutation(
            references_github._PinResolver._ref_pin,
            "marker.ignores(Scope.UPDATE)",
            "marker.holds_back_source_checks",
            "an `ignore[update]` on a branch reference pins the branch all the same",
        ),
        Mutation(
            references_github._PinResolver._comment_names_no_ref,
            "if not reference.current_sha or self.marker.ignores(Scope.UPDATE):",
            "if not reference.current_sha:",
            "a pinned branch held back by `ignore[update]` still lists its commits",
        ),
    )
    def test_ignore_update_holds_a_branch_back(self, mock_glob: Mock):
        """Test that `ignore[update]` leaves a branch reference as it is, its commits unasked and its releases read."""
        marker = "  # update-time: ignore[update]"
        for case, ref in {"branch": "main", "pinned branch": f"{OLD_SHA} # main"}.items():
            with self.subTest(case), patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA)):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{ref}{marker}\n")
                workflow_yml.write_text.assert_not_called()
                self.assertEqual(github_requests("commits"), [])
                self.assertTrue(github_requests("releases"))

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

    @kills(
        Mutation(
            references_github._PinResolver._report_redundant_directives,
            "log.redundant_directive(reference, drift, Reason.NO_DRIFT_FOR_A_COMMIT)",
            "pass",
            "an `allow[hash-drift]` beside a commit SHA, which cannot drift, goes unreported",
        )
    )
    def test_a_hash_drift_directive_on_a_bare_commit_sha_is_reported(self, mock_glob: Mock):
        """Test that a bare commit SHA is left as it is, its `allow[hash-drift]` reported as redundant.

        A commit SHA whose comment does not name a tag or branch is left bare too.
        """
        cases = {"bare commit SHA": OLD_SHA, "commit SHA whose comment does not name a ref": f"{OLD_SHA} # pinned"}
        commit, listed_commits = github_commits_json(NEW_SHA), github_not_found()
        for case, ref in cases.items():
            with self.subTest(case), patch_github(releases=[], tags=[], commit=commit, listed_commits=listed_commits):
                workflow_yml = self.scanned_workflow(
                    mock_glob, f"uses: actions/checkout@{ref}  # update-time: allow[hash-drift]\n"
                )
                workflow_yml.write_text.assert_not_called()
                self.assert_redundant_directive_logged(
                    Reason.NO_DRIFT_FOR_A_COMMIT, "actions/checkout", Location(workflow_yml, 1), "allow[hash-drift]"
                )

    @kills(
        Mutation(
            references_github._PinResolver._unversioned_pin,
            "commit_sha.names_a_commit or self._comment_names_no_ref(reference)",
            "commit_sha.names_a_commit",
            "an `allow[floating-pin]` beside a commit SHA whose comment does not name a ref goes unreported",
        )
    )
    def test_a_floating_pin_directive_on_a_bare_commit_sha_is_reported(self, mock_glob: Mock):
        """Test that a bare commit SHA is left as it is, its `allow[floating-pin]` reported as redundant.

        A commit SHA whose comment does not name a tag or branch is left bare too.
        """
        cases = {"bare commit SHA": OLD_SHA, "commit SHA whose comment does not name a ref": f"{OLD_SHA} # pinned"}
        commit, listed_commits = github_commits_json(NEW_SHA), github_not_found()
        for case, ref in cases.items():
            with self.subTest(case), patch_github(releases=[], tags=[], commit=commit, listed_commits=listed_commits):
                workflow_yml = self.scanned_workflow(
                    mock_glob, f"uses: actions/checkout@{ref}  # update-time: allow[floating-pin]\n"
                )
                workflow_yml.write_text.assert_not_called()
                self.assert_redundant_directive_logged(
                    Reason.PIN_NOT_FLOATING, "actions/checkout", Location(workflow_yml, 1), "allow[floating-pin]"
                )

    @patch_github(releases=[], tags=[], commit=github_commits_json(full_commit_sha("3e8a870")))
    def test_a_floating_pin_directive_on_a_short_commit_sha_is_reported(self, mock_glob: Mock):
        """Test that a short commit SHA is pinned to its full SHA, its `allow[floating-pin]` reported as redundant."""
        marker = f"  # {_ALLOW_FLOATING_PIN}"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@3e8a870{marker}\n")
        workflow_yml.write_text.assert_called_once_with(
            f"uses: actions/checkout@{full_commit_sha('3e8a870')}{marker}\n"
        )
        self.assert_redundant_directive_logged(
            Reason.PIN_NOT_FLOATING, "actions/checkout", Location(workflow_yml, 1), "allow[floating-pin]"
        )

    @kills(
        Mutation(
            references_github._PinResolver._comment_names_no_ref,
            "return self._ref_commit(reference).absent",
            "return False",
            "a commit SHA whose comment does not name a ref has its directives judged as if it followed a branch",
        )
    )
    def test_a_cooldown_on_a_commit_sha_is_reported(self, mock_glob: Mock):
        """Test that a cooldown on a commit SHA that no version tag names is reported as redundant.

        A full commit SHA whose comment does not name a tag or branch is such a commit SHA too.
        """
        marker = "  # update-time: ignore[cooldown<30]"
        commit, listed_commits = github_commits_json(full_commit_sha("3e8a870")), github_not_found()
        cases = {
            "full commit SHA": OLD_SHA,
            "short commit SHA": "3e8a870",
            "full commit SHA whose comment does not name a ref": f"{OLD_SHA} # pinned",
        }
        for case, ref in cases.items():
            with self.subTest(case), patch_github(releases=[], tags=[], commit=commit, listed_commits=listed_commits):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{ref}{marker}\n")
                self.assert_redundant_directive_logged(
                    Reason.NO_COOLDOWN_FOR_A_COMMIT,
                    "actions/checkout",
                    Location(workflow_yml, 1),
                    "ignore[cooldown<30]",
                )

    def test_a_bound_on_a_bare_commit_sha_is_reported(self, mock_glob: Mock):
        """Test that a bare commit SHA is left as it is, its bound reported as redundant.

        A commit SHA whose comment does not name a tag or branch is left bare too.
        """
        cases = {"bare commit SHA": OLD_SHA, "commit SHA whose comment does not name a ref": f"{OLD_SHA} # pinned"}
        commit, listed_commits = github_commits_json(NEW_SHA), github_not_found()
        for case, ref in cases.items():
            with self.subTest(case), patch_github(releases=[], tags=[], commit=commit, listed_commits=listed_commits):
                workflow_yml = self.scanned_workflow(
                    mock_glob, f"uses: actions/checkout@{ref}  # update-time: allow[update<4]\n"
                )
                workflow_yml.write_text.assert_not_called()
                self.assert_redundant_directive_logged(
                    Reason.PINS_A_COMMIT, "actions/checkout", Location(workflow_yml, 1), "allow[update<4]"
                )

    @patch_github(releases=[], tags=[], commit=github_commits_json(full_commit_sha("3e8a870")))
    def test_a_bound_on_a_short_commit_sha_is_reported(self, mock_glob: Mock):
        """Test that a short commit SHA is pinned to its full SHA, its bound reported as redundant."""
        marker = "  # update-time: allow[update<4]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@3e8a870{marker}\n")
        workflow_yml.write_text.assert_called_once_with(
            f"uses: actions/checkout@{full_commit_sha('3e8a870')}{marker}\n"
        )
        self.assert_redundant_directive_logged(
            Reason.PINS_A_COMMIT, "actions/checkout", Location(workflow_yml, 1), "allow[update<4]"
        )

    def test_a_live_bound_on_a_branch_pinned_to_a_version_tag_is_not_reported(self, mock_glob: Mock):
        """Test that a bound on a branch this run pins to a version tag is not reported when it bounds that version."""
        cases = {
            "first pin": ("main", "allow[update<6]", OLD_SHA),
            "re-pin": (f"{OLD_SHA} # main", "allow[update<6]", OLD_SHA),
            "adopted drift": (f"{OLD_SHA} # main", "allow[update<6] allow[hash-drift]", NEW_SHA),
        }
        for case, (uses, directives, sha) in cases.items():
            tags, commit = [github_tag_json("v5.0.0", sha)], github_commits_json(sha, date=PAST_COOLDOWN_DATE)
            with self.subTest(case), patch_github(releases=[], tags=tags, commit=commit):
                marker = f"  # update-time: {directives}"
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{uses}{marker}\n")
                workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{sha} # v5.0.0{marker}\n")
                self.assert_no_warnings_logged()

    @patch_github(releases=[], tags=[github_tag_json("v5.0.0", OLD_SHA)], commit=github_commits_json(OLD_SHA))
    def test_a_dead_bound_on_a_branch_pinned_to_a_version_tag_is_reported_at_that_version(self, mock_glob: Mock):
        """Test that a bound on a branch this run pins to a version tag is reported if it blocks every update there."""
        marker = "  # update-time: allow[update<4]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@main{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{OLD_SHA} # v5.0.0{marker}\n")
        self.assert_logged(
            Logger._MESSAGE_REDUNDANT_BOUND,
            dependency="actions/checkout",
            location=Location(workflow_yml, 1),
            bound="allow[update<4]",
            version="5.0.0",
            redundancy=Redundancy.BLOCKS_ALL,
        )

    @kills(
        Mutation(
            sources_github.get_latest_version,
            "and (version.version == current or version_bound.keeps(version.version, current_version))",
            "and version_bound.keeps(version.version, current_version)",
            "a version tag whose bound excludes the version it serves is left unpinned",
        )
    )
    @patch_github(
        releases=[
            github_release_json("v4.3.0", published_at=PAST_COOLDOWN_DATE),
            github_release_json("v4.0.5", published_at=PAST_COOLDOWN_DATE),
        ],
        tags=[github_tag_json("v4", NEW_SHA), github_tag_json("v4.3.0", NEW_SHA), github_tag_json("v4.0.5", OLD_SHA)],
    )
    def test_a_moving_version_tag_is_pinned_to_its_commit_although_a_bound_excludes_it(self, mock_glob: Mock):
        """Test that `@v4` is pinned as the version its commit carries, its bound reported if it blocks every update."""
        marker = "  # update-time: allow[update<4.1]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@v4{marker}\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: actions/checkout@{NEW_SHA} # v4.3.0{marker}\n")
        location = Location(workflow_yml, 1)
        self.assert_pinned_logged("actions/checkout", "4.3.0", NEW_SHA, location, among_others=True)
        self.assert_logged(
            Logger._MESSAGE_REDUNDANT_BOUND,
            dependency="actions/checkout",
            location=location,
            bound="allow[update<4.1]",
            version="4.3.0",
            redundancy=Redundancy.BLOCKS_ALL,
        )

    @patch_github(
        releases=[github_release_json("v4.3.0", published_at=PAST_COOLDOWN_DATE)],
        tags=[github_tag_json("v4.3.0", NEW_SHA)],
    )
    def test_a_moved_version_tag_is_warned_about_although_a_bound_excludes_it(self, mock_glob: Mock):
        """Test that a pinned version tag that moved is warned about as tag drift, whatever its bound excludes."""
        marker = "  # update-time: allow[update<4.1]"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@{OLD_SHA} # v4.3.0{marker}\n")
        workflow_yml.write_text.assert_not_called()
        drifted = _drifted(workflow_yml, version="4.3.0")
        self.assert_drift_logged(Logger.TAG_DRIFT, drifted, among_others=True)  # Beside the bound blocking every update

    @kills(
        Mutation(
            references_github._names_a_short_sha,
            "return sha is None or sha.startswith(ref)",
            "return True",
            "a version tag of hex digits alone that the tags listing misses is pinned as a bare commit SHA",
        )
    )
    @patch_github(
        releases=[
            github_release_json("20240301", published_at=PAST_COOLDOWN_DATE),
            github_release_json("20240101", published_at=PAST_COOLDOWN_DATE),
        ],
        tags=[github_tag_json("20240301", NEW_SHA)],
        commit=github_commits_json(OLD_SHA),
    )
    def test_hex_version_tag_beyond_the_listed_tags_is_updated_as_a_version(self, mock_glob: Mock):
        """Test that a version tag of hex digits alone, missing from the listed tags, is updated as a version."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: owner/repo@20240101\n")
        workflow_yml.write_text.assert_called_once_with(f"uses: owner/repo@{NEW_SHA} # 20240301\n")

    def test_a_bound_on_a_branch_is_reported(self, mock_glob: Mock):
        """Test that a bound on a branch is reported as redundant.

        Beside an `ignore[update]`, the bound alone is reported.
        """
        ceiling = "allow[update<4]"
        cases = {
            "branch": ("actions/checkout@main", ceiling, ceiling),
            "level bound": ("actions/checkout@main", "ignore[major-update]", "ignore[major-update]"),
            "pinned branch": (f"actions/checkout@{OLD_SHA} # main", ceiling, ceiling),
            "held-back branch": ("actions/checkout@main", f"ignore[update] {ceiling}", ceiling),
        }
        commit = github_commits_json(OLD_SHA)
        for case, (uses, marker, directive) in cases.items():
            with self.subTest(case), patch_github(releases=[], tags=[], commit=commit):
                workflow_yml = self.scanned_workflow(mock_glob, f"uses: {uses}  # update-time: {marker}\n")
                self.assert_redundant_directive_logged(
                    Reason.FOLLOWS_A_BRANCH, "actions/checkout", Location(workflow_yml, 1), directive
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

    @kills(
        Mutation(
            sources_github.version_at_tag,
            "_tagged_versions(owner, repository) or ()",
            "_tagged_versions(owner, repository)",
            "a version tag whose repository's tags cannot be listed ends the run rather than being left as it is",
            raises="TypeError: 'NoneType' object is not iterable",
        )
    )
    @patch_github(releases=None, tags=None, commit=github_commits_json(NEW_SHA))
    def test_version_tag_is_left_alone_when_its_repository_cannot_be_listed(self, mock_glob: Mock):
        """Test that a reference to a version tag is left as it is when the repository's tags cannot be listed."""
        workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@v4\n")
        workflow_yml.write_text.assert_not_called()
        self.assertEqual(len(github_requests("tags")), 1)
        repository = "https://api.github.com/repos/actions/checkout"
        self.assert_could_not_fetch_logged(f"{repository}/releases?per_page=100", f"{repository}/tags?per_page=100")
        self.assert_no_info_logged()

    @patch_github(releases=[], tags=[github_tag_json("v4.3.0", NEW_SHA)], commit=github_commits_json(NEW_SHA))
    def test_marker_keeps_the_branch_floating(self, mock_glob: Mock):
        """Test that a marker allowing the floating pin leaves a branch as it is, naming what it resolves to."""
        marker = f"  # {_ALLOW_FLOATING_PIN}"
        workflow_yml = self.scanned_workflow(mock_glob, f"uses: actions/checkout@main{marker}\n")
        workflow_yml.write_text.assert_not_called()
        location = Location(workflow_yml, 1)
        resolved = DependencyVersion("4.3.0", sha=NEW_SHA)
        self.assert_kept_ref_logged("actions/checkout", "main", resolved, location, _ALLOW_FLOATING_PIN)
        self.assert_no_warnings_logged()

    @patch_github(releases=[], tags=[], commit=github_commits_json(NEW_SHA))
    def test_flag_keeps_every_branch_floating(self, mock_glob: Mock):
        """Test that --allow-floating-pin leaves a branch reference as it is, naming the flag as what kept it."""
        with floating_pin_allowed:
            workflow_yml = self.scanned_workflow(mock_glob, "uses: actions/checkout@main\n")
        workflow_yml.write_text.assert_not_called()
        location = Location(workflow_yml, 1)
        resolved = DependencyVersion("main", sha=NEW_SHA)
        self.assert_kept_ref_logged("actions/checkout", "main", resolved, location, "--allow-floating-pin")
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
