"""Unit tests for the package.json updater (discovery and orchestration of the node package managers)."""

import json
import posixpath
import subprocess  # nosec
import unittest
from datetime import datetime
from logging import WARNING
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import ANY, Mock, call, patch
from urllib.parse import urlparse

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import Yank
from update_time.io.log import Logger
from update_time.markers.directive import Reason
from update_time.markers.marker import Marker, Scope, Threshold
from update_time.package_managers import node as node_module
from update_time.package_managers import policy as policy_module
from update_time.primitives.command import Command
from update_time.primitives.location import Location
from update_time.references import delegated as delegated_module
from update_time.updaters import update_package_json as update_package_json_module
from update_time.updaters.update_package_json import update_package_jsons

from tests.helpers import FakeFiles, mock_path, mock_response, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import FRESH_DATE, STALE_DATE
from tests.update_time.helpers import LoggingTestCase, message_of, published_version, staleness_disabled
from tests.update_time.sources.helpers import github_release_json, npm_registry
from tests.update_time.updaters.fixtures import ADVISORY, VULNERABILITY
from tests.update_time.updaters.helpers import assert_osv_asked_about, no_vulnerabilities, osv

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

_NPM_COOLDOWN_OPTION = f"--min-release-age={COOLDOWN.default}"  # the cooldown npm option Update-time adds by default
_PNPM_COOLDOWN_OPTION = f"--config.minimumReleaseAge={COOLDOWN.default * 24 * 60}"  # pnpm's, in minutes
_NPM_UNSET = "null\n"  # what `npm config get <cooldown key>` prints when the key is not set
_NPM_PREFIX = "/\n"  # what `npm prefix` prints for a package.json in the root directory, outside a workspace
_PNPM_UNSET = "undefined\n"  # what `pnpm config get minimumReleaseAge` prints when the key is not set
_NPM_LIST = Command("npm", "list", "--json", "--depth=0", "--include=dev", "--silent", "--package-lock-only")
_NPM_COOLDOWN_PROBE = Command("npm", "config", "get", "min-release-age")
_RUN_KWARGS = {"capture_output": True, "text": True, "check": True}  # what Update-time passes to subprocess.run


# When the registry fixture below says version 1.1 was published.
_PUBLICATION_TIME = "20260530T10:26:45.543Z"


def _package_lookup_responses() -> Mock:
    """Return a `Session.get` mock for the npmjs/GitHub lookups that describe an update to version 1.1.

    After a manager reports a package updated to 1.1, Update-time resolves its changelog and push date: the npm
    registry's version document names the repository, GitHub serves its releases, and the npm registry's package
    document dates the version. Each request is answered by the URL it names, so the answers do not depend on how
    many of them the lookups' caches already hold.
    """

    def serve(url: str, **_kwargs: object) -> Mock:
        parsed = urlparse(url)
        if parsed.hostname == "registry.npmjs.org":
            if parsed.path.count("/") > 1:
                return mock_response({"repository": {"url": "https://github.com/package/1.1"}})
            return mock_response({"time": {"1.1": _PUBLICATION_TIME}})
        if parsed.path.endswith("/releases"):
            return mock_response([github_release_json("v1.1", body="Changelog")])
        unexpected = f"No answer for a request for {url}"
        raise AssertionError(unexpected)

    return Mock(side_effect=serve)


def _npm_lock(**versions: str) -> Mock:
    """Return what `npm list` prints for a lock file holding the versions."""
    return Mock(stdout=json.dumps({"dependencies": {name: {"version": version} for name, version in versions.items()}}))


def _no_lock_file() -> subprocess.CalledProcessError:
    """Return the error `npm list` fails with where the project does not have a lock file."""
    return subprocess.CalledProcessError(cmd="", returncode=1, output='{"error": {"code": "ENOLOCK"}}', stderr="")


def _npm_member(name: str, directory: str, **versions: str) -> dict:
    """Return the entry npm lists for the workspace member in the directory, with its dependencies' versions.

    npm resolves the member relative to the folder holding its link, such as `node_modules/@scope` for `@scope/b`,
    and leaves the dependencies out of the entry of a member that has none.
    """
    resolved = posixpath.relpath(directory, posixpath.dirname(f"node_modules/{name}"))
    entry: dict = {"version": "1.0.0", "resolved": f"file:{resolved}"}
    if versions:
        entry["dependencies"] = {package: {"version": version} for package, version in versions.items()}
    return {name: entry}


# Workspace members by case: each package's name and its directory relative to the workspace root.
_MEMBERS = {"unscoped": ("a", "packages/a"), "scoped": ("@scope/b", "packages/b")}


# A workspace root's package.json, declaring its members under `packages/` and a dependency of its own.
_WORKSPACE_ROOT_CONTENTS = (
    '{\n  "workspaces": ["packages/*"],\n  "dependencies": {\n    "is-number": "^7.0.0"\n  }\n}\n'
)


def _member_contents(name: str) -> str:
    """Return a member's package.json, named after its package, declaring the dependency `package` on line 4."""
    return f'{{\n  "name": "{name}",\n  "dependencies": {{\n    "package": "^1.0.0"\n  }}\n}}\n'


def _npm_workspace_lock(*members: dict) -> Mock:
    """Return what `npm list` prints in a workspace for a lock file holding the members' entries."""
    entries = {name: entry for member in members for name, entry in member.items()}
    return Mock(stdout=json.dumps({"name": "root", "dependencies": entries}))


def _pnpm_lock(section: str = "dependencies", **versions: str) -> Mock:
    """Return what `pnpm list` prints for a lock file holding the versions in the section."""
    project = {"name": "root", section: {name: {"version": version} for name, version in versions.items()}}
    return Mock(stdout=json.dumps([project]))


# The yank check asks the npm registry about every exact pin, whichever check a test is about. The tests that are
# about another check answer it with a registry document that does not deprecate any version.
_no_deprecations = patch("requests.Session.get", Mock(return_value=npm_registry({})))

# The tests that log a new version without a changelog answer every registry request with this document.
_publishing_version_1_1 = patch("requests.Session.get", Mock(return_value=npm_registry({"1.1": _PUBLICATION_TIME})))

# A package.json section declaring a git dependency: the package manager runs for it, and the checks pass it over.
_GIT_DEPENDENCY = {"devDependencies": {"bats": "git+https://github.com/calj/bats.git"}}

# A package.json pinning left-pad to version 1.3.0, on line 4.
_LEFT_PAD_PINNED = '{\n  "name": "app",\n  "dependencies": {\n    "left-pad": "1.3.0"\n  }\n}\n'

# A package.json declaring minimatch with the range ^3.1.2, on line 3.
_MINIMATCH_RANGE = '{\n  "dependencies": {\n    "minimatch": "^3.1.2"\n  }\n}\n'

# The fields npm's rewrite of minimatch's range to ^3.1.5 is logged with, where the lock file held 3.1.5 already.
_MINIMATCH_REWRITE = {"dependency": "minimatch", "range": "^3.1.2", "new_range": "^3.1.5", "version": "3.1.5"}

# What a package.json holds that cannot be read, and the error reading it raises.
_UNREADABLE = "{"
_UNREADABLE_ERROR = json.JSONDecodeError("Expecting property name enclosed in double quotes", _UNREADABLE, 1)
# The error reading a package.json raises whose JSON is not an object, such as `[]`.
_NOT_AN_OBJECT_ERROR = ValueError("the JSON is not an object")


def _discover_package_json(glob: Mock, contents: str) -> Mock:
    """Return a mock package.json holding the contents, as the only one the glob finds."""
    package_json = mock_path(contents, parent=Path("/"))
    glob.return_value = [package_json]
    return package_json


def _npm_no_update() -> list:
    """Return what npm prints when the update moves nothing: an empty listing, the update, and the listing again."""
    return [Mock(stdout="{}"), Mock(stdout=""), Mock(stdout="{}")]


def _npm_unmoved(locked: Mock) -> list:
    """Return the npm runs of an update that leaves the lock file's versions as the listing holds them."""
    return _npm_runs(locked, Mock(stdout=""), locked)


def _npm_runs(*results: object, prefix: object = None) -> list:
    """Prepend `npm prefix` and the two `npm config get` cooldown probes (both unset) to the npm command results."""
    return [prefix or Mock(stdout=_NPM_PREFIX), Mock(stdout=_NPM_UNSET), Mock(stdout=_NPM_UNSET), *results]


def _pnpm_runs(*results: object) -> list:
    """Prepend the `pnpm config get minimumReleaseAge` cooldown probe (unset) to the given command results."""
    return [Mock(stdout=_PNPM_UNSET), *results]


def _assert_manager_called(mock_run: Mock, locked: Command, update: Command, directory: Path = Path("/")) -> None:
    """Assert the manager listed the locked versions, updated, and listed them again, each in the directory."""
    run_kwargs = {**_RUN_KWARGS, "cwd": directory}
    mock_run.assert_has_calls((call(locked, **run_kwargs), call(update, **run_kwargs), call(locked, **run_kwargs)))


# Every log of a rewritten range carries these fields. The wording naming the version locked before adds `version`.
_RANGE_REWRITE_FIELDS = ("manager", "dependency", "location", "range", "new_range")


class PackageLookupResponsesTest(unittest.TestCase):
    """Unit tests for the registry and GitHub answers the tests that log a new version are given."""

    def test_each_lookup_gets_the_answer_for_the_url_it_names(self):
        """Test that each lookup gets the answer for its URL, whichever lookups a cache spared before it."""
        urls = [
            "https://registry.npmjs.org/package/1.1",
            "https://api.github.com/repos/package/1.1/releases",
            "https://registry.npmjs.org/package",
        ]
        get = _package_lookup_responses()
        in_order = [get(url).json() for url in urls]
        reversed_order = [get(url).json() for url in reversed(urls)]
        self.assertEqual(reversed_order, in_order[::-1])
        self.assertEqual(len({json.dumps(answer) for answer in in_order}), len(urls))

    def test_a_lookup_without_an_answer_fails_the_test(self):
        """Test that a request for a URL the lookups make none of fails, rather than getting another URL's answer."""
        with self.assertRaises(AssertionError):
            _package_lookup_responses()("https://api.github.com/repos/package/1.1/commits")


class _PackageJsonFilesTestCase(LoggingTestCase):
    """Base for the tests that read and write package.json files in a file system in memory."""

    def setUp(self) -> None:
        """Start each test with an empty file system in memory."""
        super().setUp()
        self.files = FakeFiles()
        self.enterContext(self.files.patched())

    def write_package_json(self, contents: str, directory: Path = Path("/")) -> Path:
        """Write a package.json with the contents in the directory, and return its path."""
        path = directory / "package.json"
        self.files.files[str(path)] = contents
        return path

    def assert_range_rewrite_logged(self, **fields: object) -> None:
        """Assert that the rewrite of a range was the only record logged at its level, with the given fields."""
        self.assert_logged(message_of(Logger.range_rewrite, *fields), **fields)

    def assert_unreadable_package_json_warned(self, path: Path, error: Exception = _UNREADABLE_ERROR) -> None:
        """Assert that a warning about the unreadable package.json was the only one logged, with the error."""
        self.assert_logged(message_of(Logger.unreadable_package_json), location=Location(path), error=str(error))

    def assert_no_range_rewrite_logged(self) -> None:
        """Assert that no rewrite of a range was logged, in either wording."""
        self.assert_none_logged(message_of(Logger.range_rewrite, *_RANGE_REWRITE_FIELDS), "range rewrite")
        self.assert_none_logged(message_of(Logger.range_rewrite, *_RANGE_REWRITE_FIELDS, "version"), "range rewrite")

    def update_stub(self, package_json: Path, saved: str, results: list) -> Callable[..., Mock]:
        """Return a `subprocess.run` stub that answers with the results and saves the package.json on an update."""
        answers = iter(results)

        def run(command: Command, **_kwargs: object) -> Mock:
            """Answer the next run, raising an error as the run's answer, and save the package.json on an update."""
            if command[1] == "update":
                self.files.files[str(package_json)] = saved
            if isinstance(answer := next(answers), BaseException):
                raise answer
            return answer

        return run


# Staleness makes its own npm registry requests; it is disabled here and covered by StaleDependencyTest below.
@staleness_disabled
@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("subprocess.run")
class UpdateNpmPackageJsonTest(_PackageJsonFilesTestCase):
    """Unit tests for updating an npm-managed package.json."""

    def create_package_json(self, contents: str = json.dumps(_GIT_DEPENDENCY)) -> Path:
        """Create a package.json in the root directory."""
        return self.write_package_json(contents)

    def assert_npm_called(self, mock_run: Mock, *, cooldown: bool = True, directory: Path = Path("/")) -> None:
        """Assert npm list, update, and list were called in the directory (with the cooldown option when expected)."""
        cooldown_option = [_NPM_COOLDOWN_OPTION] if cooldown else []
        _assert_manager_called(
            mock_run,
            _NPM_LIST,
            Command("npm", "update", "--save", "--include=dev", "--silent", *cooldown_option),
            directory,
        )

    def test_npm_runs_for_peer_dependencies_alone(self, mock_run: Mock, mock_glob: Mock):
        """Test that npm runs for a package.json that declares peer dependencies alone."""
        mock_glob.return_value = [self.create_package_json('{"peerDependencies": {"is-number": "^7.0.0"}}')]
        mock_run.side_effect = _npm_runs(*_npm_no_update())
        update_package_jsons()
        self.assert_npm_called(mock_run)

    def test_nearest_directory_naming_a_manager_decides(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json naming npm, or one in a directory between, wins over a pnpm workspace above."""
        npm_project = json.dumps({"packageManager": "npm@11.0.0", **_GIT_DEPENDENCY})
        cases = {
            "the package.json's own field": {"/a/b/package.json": npm_project},
            "a package.json in a directory between": {
                "/a/package.json": npm_project,
                "/a/b/package.json": json.dumps(_GIT_DEPENDENCY),
            },
        }
        for case, files in cases.items():
            with self.subTest(case):
                self.files.files = {"/pnpm-workspace.yaml": "packages:\n  - a/b\n", **files}
                mock_glob.return_value = [Path("/a/b/package.json")]
                mock_run.side_effect = _npm_runs(*_npm_no_update())
                update_package_jsons()
                self.assert_npm_called(mock_run, directory=Path("/a/b"))

    def run_below_package_json(self, mock_run: Mock, mock_glob: Mock, contents: str) -> None:
        """Run on a member in /a, below a /package.json holding the contents, and assert npm ran for the member."""
        self.files.files = {"/package.json": contents}
        mock_glob.return_value = [self.write_package_json(json.dumps(_GIT_DEPENDENCY), Path("/a"))]
        mock_run.side_effect = _npm_runs(*_npm_no_update())
        update_package_jsons()
        self.assert_npm_called(mock_run, directory=Path("/a"))

    def test_invalid_package_json_above_is_warned_about_and_passed_over(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json above that does not parse is warned about, and passed over."""
        cases = {"invalid JSON": (_UNREADABLE, _UNREADABLE_ERROR), "not an object": ("[]", _NOT_AN_OBJECT_ERROR)}
        for case, (contents, error) in cases.items():
            with self.subTest(case):
                self.run_below_package_json(mock_run, mock_glob, contents)
                self.assert_logged(
                    message_of(Logger.unreadable_manager_declaration),
                    location=Location(Path("/package.json")),
                    error=str(error),
                )

    def test_package_json_that_does_not_parse_is_warned_about_and_passed_over(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json the scan finds that does not parse is warned about, and the others still update."""
        cases = {"invalid JSON": (_UNREADABLE, _UNREADABLE_ERROR), "not an object": ("[]", _NOT_AN_OBJECT_ERROR)}
        for case, (contents, error) in cases.items():
            with self.subTest(case):
                mock_run.reset_mock()
                root = self.write_package_json(json.dumps(_GIT_DEPENDENCY))
                fixture = self.write_package_json(contents, Path("/fixtures"))
                mock_glob.return_value = [root, fixture]
                mock_run.side_effect = _npm_runs(*_npm_no_update())
                update_package_jsons()
                self.assert_npm_called(mock_run)
                self.assert_unreadable_package_json_warned(fixture, error)

    def test_package_json_above_naming_no_manager_string_is_passed_over(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json above whose `packageManager` is a number is passed over silently."""
        self.run_below_package_json(mock_run, mock_glob, '{"packageManager": 9}')
        self.assert_no_warnings_logged()

    @patch("requests.Session.get", _package_lookup_responses())
    def test_update(self, mock_run: Mock, mock_glob: Mock):
        """Test that a version the update moved in the lock file is logged, and package.json is kept as npm saved it."""
        package_json = self.create_package_json()
        mock_glob.return_value = [package_json]
        mock_run.side_effect = _npm_runs(_npm_lock(package="1.0"), Mock(stdout=""), _npm_lock(package="1.1"))
        update_package_jsons()
        self.assertEqual(self.files[package_json], json.dumps(_GIT_DEPENDENCY))
        self.assert_npm_called(mock_run)
        self.assert_path_logged(package_json)
        self.assert_new_version_logged(
            "package", published_version("1.1", _PUBLICATION_TIME), Location(package_json), changes="Changelog"
        )
        self.assert_no_warnings_logged()

    @patch("requests.Session.get", _package_lookup_responses())
    def test_new_version_alone_is_logged_at_the_line_declaring_the_dependency(self, mock_run: Mock, mock_glob: Mock):
        """Test that the new version alone is logged at the line declaring the dependency, whose range npm rewrote."""
        original = '{\n  "dependencies": {\n    "package": "^1.0.0"\n  }\n}\n'
        package_json = self.create_package_json(original)
        mock_glob.return_value = [package_json]
        runs = _npm_runs(_npm_lock(package="1.0"), Mock(stdout=""), _npm_lock(package="1.1"))
        mock_run.side_effect = self.update_stub(package_json, original.replace("^1.0.0", "^1.1.0"), runs)
        update_package_jsons()
        self.assert_new_version_logged(
            "package", published_version("1.1", _PUBLICATION_TIME), Location(package_json, 3), changes="Changelog"
        )

    @patch("requests.Session.get", _package_lookup_responses())
    def test_new_version_is_logged_at_each_line_declaring_the_dependency(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency two sections declare has its new version logged at each of those lines."""
        contents = (
            "{\n"
            '  "dependencies": {\n    "package": "^1.0.0"\n  },\n'
            '  "devDependencies": {\n    "package": "^1.0.0"\n  }\n'
            "}\n"
        )
        package_json = self.create_package_json(contents)
        mock_glob.return_value = [package_json]
        mock_run.side_effect = _npm_runs(_npm_lock(package="1.0"), Mock(stdout=""), _npm_lock(package="1.1"))
        update_package_jsons()
        published = published_version("1.1", _PUBLICATION_TIME)
        self.assert_new_version_logged_among_others("package", published, Location(package_json, 3), changes=ANY)
        self.assert_new_version_logged_among_others("package", published, Location(package_json, 6), ANY)

    @patch("requests.Session.get", _package_lookup_responses())
    def test_new_version_of_a_dependency_the_manifest_declares_without_a_registry_spec(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a new version for a dependency that resolves to no registry release is logged at the file."""
        contents = '{\n  "dependencies": {\n    "package": "git+https://github.com/org/package.git"\n  }\n}\n'
        package_json = self.create_package_json(contents)
        mock_glob.return_value = [package_json]
        mock_run.side_effect = _npm_runs(_npm_lock(package="1.0"), Mock(stdout=""), _npm_lock(package="1.1"))
        update_package_jsons()
        self.assert_new_version_logged(
            "package", published_version("1.1", _PUBLICATION_TIME), Location(package_json), changes="Changelog"
        )

    def test_manifest_kept_as_npm_saved_it_when_no_version_moved(self, mock_run: Mock, mock_glob: Mock):
        """Test that the specs npm saved, such as a git URL in the github: shorthand, are kept and not logged."""
        original = '{\n  "devDependencies": {\n    "bats": "git+https://github.com/calj/bats.git"\n  }\n}\n'
        package_json = self.create_package_json(original)
        mock_glob.return_value = [package_json]
        saved = original.replace("git+https://github.com/calj/bats.git", "github:calj/bats")
        locked = _npm_lock(bats="1.0.0")
        mock_run.side_effect = self.update_stub(package_json, saved, _npm_unmoved(locked))
        update_package_jsons()
        self.assertEqual(self.files[package_json], saved)
        self.assert_npm_called(mock_run)
        self.assert_path_logged(package_json)
        self.assert_no_new_version_logged()
        self.assert_no_range_rewrite_logged()
        self.assert_no_warnings_logged()

    def test_range_rewrite_is_logged_at_each_line_declaring_the_dependency(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency two sections declare has the rewrite of its range logged at each of those lines."""
        original = (
            "{\n"
            '  "dependencies": {\n    "minimatch": "^3.1.2"\n  },\n'
            '  "devDependencies": {\n    "minimatch": "^3.1.2"\n  }\n'
            "}\n"
        )
        package_json = self.create_package_json(original)
        mock_glob.return_value = [package_json]
        locked = _npm_lock(minimatch="3.1.5")
        saved = original.replace("^3.1.2", "^3.1.5")
        mock_run.side_effect = self.update_stub(package_json, saved, _npm_unmoved(locked))
        update_package_jsons()
        fields = {"manager": "npm", **_MINIMATCH_REWRITE}
        message = message_of(Logger.range_rewrite, *fields)
        self.assert_logged_among_others(message, location=Location(package_json, 3), **fields)
        self.assert_logged_among_others(message, location=Location(package_json, 6), **fields)

    @kills(
        Mutation(
            node_module._ProjectUpdate.log_range_rewrites,
            "range_before.current_version == range_after.current_version",
            "range_before == range_after",
            "a range npm only moved to another line by sorting the keys is logged as rewritten",
        )
    )
    def test_range_npm_only_moved_by_sorting_the_keys_is_not_logged(self, mock_run: Mock, mock_glob: Mock):
        """Test that a range npm moved to another line by sorting the keys is not logged, beside one it rewrote."""
        original = '{\n  "dependencies": {\n    "minimatch": "^3.1.2",\n    "ansi-regex": "^5.0.1"\n  }\n}\n'
        package_json = self.create_package_json(original)
        mock_glob.return_value = [package_json]
        locked = _npm_lock(minimatch="3.1.5", **{"ansi-regex": "5.0.1"})
        saved = '{\n  "dependencies": {\n    "ansi-regex": "^5.0.1",\n    "minimatch": "^3.1.5"\n  }\n}\n'
        mock_run.side_effect = self.update_stub(package_json, saved, _npm_unmoved(locked))
        update_package_jsons()
        self.assert_range_rewrite_logged(manager="npm", location=Location(package_json, 4), **_MINIMATCH_REWRITE)

    def test_unmoved_version_not_logged(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package is not logged when npm update did not move its locked version."""
        package_json = self.create_package_json()
        mock_glob.return_value = [package_json]
        # The lock file does not record a version of "untracked":
        untracked = Mock(stdout='{"dependencies": {"held": {"version": "1.0"}, "untracked": {"missing": true}}}')
        mock_run.side_effect = _npm_runs(untracked, Mock(stdout=""), untracked)
        update_package_jsons()
        self.assert_npm_called(mock_run)
        self.assert_path_logged(package_json)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    @patch("requests.Session.get", _package_lookup_responses())
    def test_version_the_lock_file_did_not_record_not_logged(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package is not logged when the lock file did not record it before the update."""
        package_json = self.create_package_json()
        mock_glob.return_value = [package_json]
        mock_run.side_effect = _npm_runs(_no_lock_file(), Mock(stdout=""), _npm_lock(package="1.1"))
        update_package_jsons()
        self.assert_npm_called(mock_run)
        self.assert_no_new_version_logged()

    def test_range_rewrite_says_the_lock_file_did_not_record_a_version_before(self, mock_run: Mock, mock_glob: Mock):
        """Test that a range npm rewrote says so, where the lock file did not record a version before the update."""
        original = '{\n  "dependencies": {\n    "minimatch": "^3.0.0"\n  }\n}\n'
        package_json = self.create_package_json(original)
        mock_glob.return_value = [package_json]
        runs = _npm_runs(_no_lock_file(), Mock(stdout=""), _npm_lock(minimatch="3.1.5"))
        mock_run.side_effect = self.update_stub(package_json, original.replace("^3.0.0", "^3.1.5"), runs)
        update_package_jsons()
        self.assert_range_rewrite_logged(
            manager="npm",
            dependency="minimatch",
            location=Location(package_json, 3),
            range="^3.0.0",
            new_range="^3.1.5",
        )

    def test_empty_npm_output(self, mock_run: Mock, mock_glob: Mock):
        """Test that empty npm output (e.g. from a failed command) is treated as no data rather than crashing."""
        package_json = self.create_package_json()
        mock_glob.return_value = [package_json]
        # npm list returns nothing, as a failed command does (its failure is logged by `run`):
        mock_run.side_effect = _npm_runs(Mock(stdout=""), Mock(stdout=""), Mock(stdout=""))
        update_package_jsons()
        self.assert_npm_called(mock_run)
        self.assertEqual(self.files[package_json], json.dumps(_GIT_DEPENDENCY))
        self.assert_no_new_version_logged()

    def test_range_rewrite_not_logged_when_the_listing_after_the_update_fails(self, mock_run: Mock, mock_glob: Mock):
        """Test that a rewritten range is not logged when npm fails to list the lock file after the update."""
        package_json = self.create_package_json(_MINIMATCH_RANGE)
        mock_glob.return_value = [package_json]
        saved = _MINIMATCH_RANGE.replace("^3.1.2", "^3.1.5")
        failed_listing = subprocess.CalledProcessError(cmd="", returncode=1, output="", stderr="npm error EJSONPARSE")
        runs = _npm_runs(_npm_lock(minimatch="3.1.5"), Mock(stdout=""), failed_listing)
        mock_run.side_effect = self.update_stub(package_json, saved, runs)
        update_package_jsons()
        self.assertEqual(self.files[package_json], saved)
        self.assert_command_stderr_logged(stderr="npm error EJSONPARSE")
        self.assert_no_range_rewrite_logged()

    def test_package_json_unreadable_after_the_update_is_warned_about_and_logs_no_range(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a package.json npm left unreadable is warned about, and that its ranges are not logged."""
        package_json = self.create_package_json(_MINIMATCH_RANGE)
        mock_glob.return_value = [package_json]
        mock_run.side_effect = self.update_stub(package_json, _UNREADABLE, _npm_unmoved(_npm_lock(minimatch="3.1.5")))
        update_package_jsons()
        self.assert_unreadable_package_json_warned(package_json)
        self.assert_no_range_rewrite_logged()

    @_publishing_version_1_1
    def test_package_json_unreadable_after_the_update_is_warned_about_once_while_a_version_moved(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a package.json npm left unreadable is warned about once, and its new version logged at the file."""
        package_json = self.create_package_json('{\n  "dependencies": {\n    "package": "^1.0.0"\n  }\n}\n')
        mock_glob.return_value = [package_json]
        runs = _npm_runs(_npm_lock(package="1.0"), Mock(stdout=""), _npm_lock(package="1.1"))
        mock_run.side_effect = self.update_stub(package_json, _UNREADABLE, runs)
        update_package_jsons()
        self.assert_unreadable_package_json_warned(package_json)
        self.assert_new_version_logged("package", published_version("1.1", _PUBLICATION_TIME), Location(package_json))

    def test_failed_update_logs_its_failure_and_no_new_version(self, mock_run: Mock, mock_glob: Mock):
        """Test that a failed update logs its failure, and does not log a new version."""
        package_json = self.create_package_json()
        mock_glob.return_value = [package_json]
        mock_run.side_effect = _npm_runs(
            _npm_lock(package="1.0"),
            subprocess.CalledProcessError(cmd="", returncode=1, output="", stderr="npm error network"),
            _npm_lock(package="1.0"),
        )
        update_package_jsons()
        self.assert_npm_called(mock_run)
        self.assertEqual(self.files[package_json], json.dumps(_GIT_DEPENDENCY))
        self.assert_command_stderr_logged(stderr="npm error network")

    def test_project_cooldown_respected(self, mock_run: Mock, mock_glob: Mock):
        """Test that no cooldown option is added when the project's npm config already sets one."""
        package_json = self.create_package_json()
        mock_glob.return_value = [package_json]
        # The first cooldown probe (min-release-age) returns a value, so Update-time adds no cooldown of its own:
        mock_run.side_effect = [Mock(stdout=_NPM_PREFIX), Mock(stdout="14\n"), *_npm_no_update()]
        update_package_jsons()
        self.assert_npm_called(mock_run, cooldown=False)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_cooldown_applied_when_npm_config_get_fails(self, mock_run: Mock, mock_glob: Mock):
        """Test that the cooldown option is added when `npm config get` fails, as it does inside a workspace member."""
        mock_glob.return_value = [self.create_package_json()]
        failed = subprocess.CalledProcessError(cmd="", returncode=1, output="", stderr="npm error code ENOWORKSPACES")
        mock_run.side_effect = [Mock(stdout=_NPM_PREFIX), failed, failed, *_npm_no_update()]
        update_package_jsons()
        self.assert_npm_called(mock_run)

    def test_cooldown_of_a_workspace_member_read_in_the_workspace_root(self, mock_run: Mock, mock_glob: Mock):
        """Test that the cooldown of a workspace member is read in the workspace root that npm reports."""
        mock_glob.return_value = [self.write_package_json(json.dumps(_GIT_DEPENDENCY), Path("/ws/a"))]
        mock_run.side_effect = _npm_runs(*_npm_no_update(), prefix=Mock(stdout="/ws\n"))
        update_package_jsons()
        mock_run.assert_any_call(_NPM_COOLDOWN_PROBE, **_RUN_KWARGS, cwd=Path("/ws"))
        update = Command("npm", "update", "--save", "--include=dev", "--silent", _NPM_COOLDOWN_OPTION)
        mock_run.assert_any_call(update, **_RUN_KWARGS, cwd=Path("/ws/a"))

    @_publishing_version_1_1
    def test_new_version_of_a_workspace_member_dependency_is_logged(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dependency of a member has its new version logged, and one of another member not."""
        for case, (name, directory) in _MEMBERS.items():
            with self.subTest(case):
                package_json = self.write_package_json(_member_contents(name), Path("/ws", directory))
                mock_glob.return_value = [package_json]
                mock_run.side_effect = _npm_runs(
                    _npm_workspace_lock(
                        _npm_member(name, directory, package="1.0"), _npm_member("c", "packages/c", other="2.0")
                    ),
                    Mock(stdout=""),
                    _npm_workspace_lock(
                        _npm_member(name, directory, package="1.1"), _npm_member("c", "packages/c", other="2.1")
                    ),
                    prefix=Mock(stdout="/ws\n"),
                )
                update_package_jsons()
                self.assert_new_version_logged(
                    "package", published_version("1.1", _PUBLICATION_TIME), Location(package_json, 4)
                )

    @_publishing_version_1_1
    @patch("pathlib.Path.resolve", autospec=True, side_effect=lambda path: Path(str(path).replace("/link", "/real")))
    def test_new_version_logged_for_a_package_json_in_a_symlinked_directory(
        self, _resolve: Mock, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a package.json in a symlinked directory has its version logged, whatever npm prefix answers."""
        prefixes = {
            "resolved": Mock(stdout="/real/app\n"),
            "failing": subprocess.CalledProcessError(cmd="", returncode=1, output="", stderr="npm error prefix"),
        }
        for case, prefix in prefixes.items():
            with self.subTest(case):
                contents = '{\n  "dependencies": {\n    "package": "^1.0.0"\n  }\n}\n'
                package_json = self.write_package_json(contents, Path("/link/app"))
                mock_glob.return_value = [package_json]
                mock_run.side_effect = _npm_runs(
                    _npm_lock(package="1.0"), Mock(stdout=""), _npm_lock(package="1.1"), prefix=prefix
                )
                update_package_jsons()
                self.assert_new_version_logged(
                    "package", published_version("1.1", _PUBLICATION_TIME), Location(package_json, 3)
                )

    @_publishing_version_1_1
    def test_new_version_of_a_workspace_member_dependency_the_root_updated_is_logged_at_the_member(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that the new version of a member's dependency that npm moved when updating the root is logged."""
        root = self.write_package_json(_WORKSPACE_ROOT_CONTENTS)
        for case, (name, directory) in _MEMBERS.items():
            with self.subTest(case):
                member = self.write_package_json(_member_contents(name), Path("/", directory))
                mock_glob.return_value = [root, member]
                before = _npm_workspace_lock(_npm_member(name, directory, package="1.0"))
                after = _npm_workspace_lock(_npm_member(name, directory, package="1.1"))
                mock_run.side_effect = [
                    *_npm_runs(before, Mock(stdout=""), after),
                    *_npm_runs(after, Mock(stdout=""), after),
                ]
                update_package_jsons()
                published = published_version("1.1", _PUBLICATION_TIME)
                self.assert_new_version_logged("package", published, Location(member, 4))

    def test_workspace_root_is_updated_before_its_members(self, mock_run: Mock, mock_glob: Mock):
        """Test that npm updates a workspace root before its members, whichever the glob finds first."""
        root = self.write_package_json(_WORKSPACE_ROOT_CONTENTS)
        member = self.write_package_json(_member_contents("a"), Path("/packages/a"))
        mock_glob.return_value = [member, root]
        mock_run.side_effect = [*_npm_runs(*_npm_no_update()), *_npm_runs(*_npm_no_update())]
        update_package_jsons()
        updated_directories = [
            kwargs["cwd"] for command, kwargs in mock_run.call_args_list if command[0][:2] == ("npm", "update")
        ]
        self.assertEqual(updated_directories, [Path("/"), Path("/packages/a")])

    def test_range_of_a_workspace_member_the_root_rewrote_is_logged_at_the_member(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a member's range npm rewrote when updating the root is logged at the member's package.json."""
        root = self.write_package_json(_WORKSPACE_ROOT_CONTENTS)
        name, directory = _MEMBERS["unscoped"]
        original = _member_contents(name)
        member = self.write_package_json(original, Path("/", directory))
        mock_glob.return_value = [root]
        locked = _npm_workspace_lock(_npm_member(name, directory, package="1.1"))
        saved = original.replace("^1.0.0", "^1.1.0")
        mock_run.side_effect = self.update_stub(member, saved, _npm_unmoved(locked))
        update_package_jsons()
        self.assert_range_rewrite_logged(
            manager="npm",
            dependency="package",
            location=Location(member, 4),
            range="^1.0.0",
            new_range="^1.1.0",
            version="1.1",
        )

    @_publishing_version_1_1
    def test_new_version_of_a_member_whose_package_json_cannot_be_read_is_logged_at_the_file(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that an unreadable member package.json is warned about, and its new versions logged at the file."""
        root, member = Path("/package.json"), Path("/packages/a/package.json")
        cases: dict[str, tuple[dict[str | Path, str], Exception]] = {
            "missing": ({}, FileNotFoundError(2, "No such file or directory", str(member))),
            "invalid JSON": ({member: _UNREADABLE}, _UNREADABLE_ERROR),
        }
        for case, (member_files, error) in cases.items():
            with self.subTest(case):
                mock_glob.return_value = [root]
                mock_run.side_effect = _npm_runs(
                    _npm_workspace_lock(_npm_member("a", "packages/a", package="1.0")),
                    Mock(stdout=""),
                    _npm_workspace_lock(_npm_member("a", "packages/a", package="1.1")),
                )
                self.files.files = {
                    str(path): text for path, text in {root: _WORKSPACE_ROOT_CONTENTS, **member_files}.items()
                }
                update_package_jsons()
                self.assert_unreadable_package_json_warned(member, error)
                published = published_version("1.1", _PUBLICATION_TIME)
                self.assert_new_version_logged("package", published, Location(member))

    def test_unmoved_versions_of_workspace_members_not_logged(self, mock_run: Mock, mock_glob: Mock):
        """Test that members without dependencies, new to the lock, or with an unreadable package.json log nothing."""
        mock_glob.return_value = [self.write_package_json(_WORKSPACE_ROOT_CONTENTS)]
        without_dependencies = _npm_member("a", "packages/a")
        unreadable = _npm_member("c", "packages/c", package="1.0")
        new = _npm_member("d", "packages/d", package="1.1")
        mock_run.side_effect = _npm_runs(
            _npm_workspace_lock(without_dependencies, unreadable),
            Mock(stdout=""),
            _npm_workspace_lock(without_dependencies, unreadable, new),
        )
        update_package_jsons()
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_cooldown_read_in_the_directory_of_the_package_json_when_npm_prefix_fails(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that the cooldown is read in the directory of the package.json when `npm prefix` fails."""
        mock_glob.return_value = [self.create_package_json()]
        prefix_failed = subprocess.CalledProcessError(cmd="", returncode=1, output="", stderr="npm error prefix")
        mock_run.side_effect = _npm_runs(*_npm_no_update(), prefix=prefix_failed)
        update_package_jsons()
        self.assert_npm_called(mock_run)
        mock_run.assert_any_call(_NPM_COOLDOWN_PROBE, **_RUN_KWARGS, cwd=Path("/"))


@staleness_disabled
@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("subprocess.run")
class UpdatePnpmPackageJsonTest(_PackageJsonFilesTestCase):
    """Unit tests for updating a pnpm-managed package.json (detected via the corepack `packageManager` field)."""

    def create_package_json(
        self, contents: str = json.dumps({"packageManager": "pnpm@9.15.0", **_GIT_DEPENDENCY})
    ) -> Path:
        """Create a package.json in the root directory, declaring pnpm as its package manager."""
        return self.write_package_json(contents)

    def assert_pnpm_called(self, mock_run: Mock, *, cooldown: bool = True, directory: Path = Path("/")) -> None:
        """Assert pnpm list, update, and list were called in the directory (with the cooldown option when expected)."""
        cooldown_option = [_PNPM_COOLDOWN_OPTION] if cooldown else []
        _assert_manager_called(
            mock_run,
            Command("pnpm", "list", "--json", "--depth=0", "--lockfile-only"),
            Command("pnpm", "update", *cooldown_option),
            directory,
        )

    @patch("requests.Session.get", _package_lookup_responses())
    def test_update(self, mock_run: Mock, mock_glob: Mock):
        """Test that a dev dependency the update moved in the lock file is logged, using pnpm's list JSON shape."""
        package_json = self.create_package_json()
        mock_glob.return_value = [package_json]
        mock_run.side_effect = _pnpm_runs(
            _pnpm_lock("devDependencies", package="1.0"), Mock(stdout=""), _pnpm_lock("devDependencies", package="1.1")
        )
        update_package_jsons()
        self.assert_pnpm_called(mock_run)
        self.assert_path_logged(package_json)
        self.assert_new_version_logged(
            "package", published_version("1.1", _PUBLICATION_TIME), Location(package_json), changes="Changelog"
        )
        self.assert_no_warnings_logged()

    def test_unmoved_version_not_logged(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package is not logged when pnpm update did not move its locked version."""
        package_json = self.create_package_json()
        mock_glob.return_value = [package_json]
        # The lock file does not record a version of "untracked":
        untracked = Mock(
            stdout='[{"name": "root", "dependencies": {"held": {"version": "1.0"}}, '
            '"devDependencies": {"untracked": {"from": "untracked"}}}]'
        )
        mock_run.side_effect = _pnpm_runs(untracked, Mock(stdout=""), untracked)
        update_package_jsons()
        self.assert_pnpm_called(mock_run)
        self.assert_path_logged(package_json)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_range_pnpm_rewrote_is_logged_where_the_lock_file_did_not_move(self, mock_run: Mock, mock_glob: Mock):
        """Test that a range pnpm rewrote is logged at its line, naming pnpm, where the lock file did not move."""
        original = '{\n  "packageManager": "pnpm@9.15.0",\n  "dependencies": {\n    "minimatch": "^3.1.2"\n  }\n}\n'
        package_json = self.create_package_json(original)
        mock_glob.return_value = [package_json]
        locked = _pnpm_lock(minimatch="3.1.5")
        saved = original.replace("^3.1.2", "^3.1.5")
        mock_run.side_effect = self.update_stub(package_json, saved, _pnpm_runs(locked, Mock(stdout=""), locked))
        update_package_jsons()
        self.assert_range_rewrite_logged(manager="pnpm", location=Location(package_json, 4), **_MINIMATCH_REWRITE)

    @patch_pathlib_path(exists=True)
    def test_pnpm_detected_via_lockfile(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json with a sibling pnpm-lock.yaml (and no packageManager field) uses pnpm."""
        # No packageManager field; detection falls to the lockfile:
        package_json = self.create_package_json(json.dumps(_GIT_DEPENDENCY))
        mock_glob.return_value = [package_json]
        mock_run.side_effect = _pnpm_runs(Mock(stdout="{}"), Mock(stdout=""), Mock(stdout="[]"))
        update_package_jsons()
        self.assert_pnpm_called(mock_run)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()

    def test_pnpm_detected_above_a_workspace_member(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json without a signal of its own uses pnpm when a directory above signals pnpm."""
        cases = {
            "a pnpm-workspace.yaml": {"/pnpm-workspace.yaml": "packages:\n  - a\n"},
            "a pnpm-lock.yaml": {"/pnpm-lock.yaml": "lockfileVersion: '9.0'\n"},
            "a packageManager field": {"/package.json": '{"packageManager": "pnpm@9.15.0"}'},
        }
        contents = json.dumps(_GIT_DEPENDENCY)
        for case, files in cases.items():
            with self.subTest(case):
                self.files.files = dict(files)
                member = self.write_package_json(contents, Path("/a"))
                mock_glob.return_value = [member]
                mock_run.side_effect = _pnpm_runs(Mock(stdout="[]"), Mock(stdout=""), Mock(stdout="[]"))
                update_package_jsons()
                self.assert_pnpm_called(mock_run, directory=Path("/a"))

    def test_project_cooldown_respected(self, mock_run: Mock, mock_glob: Mock):
        """Test that no cooldown option is added when the project's pnpm config already sets minimumReleaseAge."""
        package_json = self.create_package_json()
        mock_glob.return_value = [package_json]
        # The cooldown probe returns a value, so Update-time adds no cooldown of its own:
        mock_run.side_effect = [Mock(stdout="4320\n"), Mock(stdout="{}"), Mock(stdout=""), Mock(stdout="[]")]
        update_package_jsons()
        self.assert_pnpm_called(mock_run, cooldown=False)
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("subprocess.run")
class SkipUnsupportedPackageManagerTest(LoggingTestCase):
    """Unit tests for skipping package.json files managed by an unsupported package manager (yarn, bun)."""

    def test_skip_unsupported_package_manager(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json with an unsupported `packageManager` field is skipped without running anything."""
        mock_package_json = _discover_package_json(mock_glob, '{"packageManager": "yarn@4.5.0"}')
        update_package_jsons()
        mock_run.assert_not_called()
        mock_package_json.write_text.assert_not_called()
        self.assert_unsupported_package_manager_logged(mock_package_json, "yarn", "npm and pnpm")
        self.assert_no_new_version_logged()

    def test_skip_unsupported_lockfile(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json with an unsupported lockfile (e.g. yarn.lock) is skipped without running it."""
        cases = {"beside the package.json": Path("/"), "in a directory above": Path("/a")}
        for case, directory in cases.items():
            with self.subTest(case):
                mock_package_json = mock_path("{}", parent=directory)
                mock_glob.return_value = [mock_package_json]
                # Only /yarn.lock exists (no pnpm-lock.yaml), so yarn is detected and the file skipped:
                with patch("pathlib.Path.exists", lambda self: str(self) == "/yarn.lock"):
                    update_package_jsons()
                mock_run.assert_not_called()
                mock_package_json.write_text.assert_not_called()
                self.assert_unsupported_package_manager_logged(mock_package_json, "yarn", "npm and pnpm")
                self.assert_no_new_version_logged()


@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("subprocess.run")
class SkipPackageJsonWithoutDependenciesTest(LoggingTestCase):
    """Unit tests for a package.json that declares nothing for its package manager to update."""

    def test_package_manager_not_run(self, mock_run: Mock, mock_glob: Mock):
        """Test that npm and pnpm do not run for a package.json that declares nothing to update, and say so."""
        cases = {
            "fields other than dependencies": (
                '{"name": "spark-ui", "license": "Apache License 2.0", "type": "module"}',
                "npm",
            ),
            "empty sections": ('{"dependencies": {}, "devDependencies": {}}', "npm"),
            "a pnpm-managed file": ('{"packageManager": "pnpm@9.15.0"}', "pnpm"),
        }
        mock_run.return_value = Mock(stdout="", stderr="")
        for case, (contents, manager) in cases.items():
            with self.subTest(case):
                mock_package_json = _discover_package_json(mock_glob, contents)
                update_package_jsons()
                mock_run.assert_not_called()
                mock_package_json.write_text.assert_not_called()
                self.assert_no_dependencies_logged(mock_package_json, manager, "install")


class NoUpdateTestCase(LoggingTestCase):
    """Base for the tests of the checks over the dependencies a package.json declares, run while none is updated."""

    @staticmethod
    def stub_no_update(mock_run: Mock) -> None:
        """Stub npm to leave every version where it was: `npm prefix`, two unset cooldown probes, then empty runs."""
        mock_run.side_effect = [
            Mock(stdout=_NPM_PREFIX),
            Mock(stdout=_NPM_UNSET),
            Mock(stdout=_NPM_UNSET),
            Mock(stdout="{}"),
            Mock(stdout=""),
            Mock(stdout='{"dependencies": {}}'),
        ]

    @staticmethod
    def registry_doc(version: str, published: str) -> Mock:
        """Mock the npm registry document dating the version it names."""
        return mock_response({"time": {version: published}})

    @staticmethod
    def clipboard_contents(marker: str = "", spec: str = "^2.0.11") -> str:
        """Return a package.json depending on `clipboard`, whose entry sits on line 4."""
        field = f',\n  "update-time": {{"dependencies": {{"clipboard": "{marker}"}}}}' if marker else ""
        return f'{{\n  "name": "app",\n  "dependencies": {{\n    "clipboard": "{spec}"\n  }}{field}\n}}\n'

    def run_on_package_json(
        self, get: Mock, mock_run: Mock, glob: Mock, contents: str, published: datetime = FRESH_DATE
    ) -> Mock:
        """Run Update-time on the only package.json, holding the contents, its newest release published then."""
        self.stub_no_update(mock_run)
        get.return_value = self.registry_doc("2.0.11", published.isoformat())
        package_json = _discover_package_json(glob, contents)
        update_package_jsons()
        return package_json


@_no_deprecations
@staleness_disabled
@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("subprocess.run")
class VulnerableDependencyTest(NoUpdateTestCase):
    """Unit tests for the package.json vulnerability check, which looks up each dependency pinned to a version."""

    def test_vulnerable_pin_warned(self, mock_run: Mock, glob: Mock):
        """Test that a dependency pinned to a version OSV reports an advisory for is warned about, at its line."""
        self.stub_no_update(mock_run)
        package_json = _discover_package_json(glob, _LEFT_PAD_PINNED)
        with osv(ADVISORY):
            update_package_jsons()
        self.assert_vulnerable_dependency_logged("left-pad", "1.3.0", VULNERABILITY, Location(package_json, 4))

    def test_each_pin_of_a_name_warned_at_its_line(self, mock_run: Mock, glob: Mock):
        """Test that each pin of a name two sections declare is warned about at its own line."""
        self.stub_no_update(mock_run)
        contents = (
            "{\n"
            '  "dependencies": {\n    "left-pad": "1.3.0"\n  },\n'
            '  "devDependencies": {\n    "left-pad": "1.2.0"\n  }\n'
            "}\n"
        )
        package_json = _discover_package_json(glob, contents)
        with osv(ADVISORY):
            update_package_jsons()
        self.assert_vulnerable_dependency_logged(
            "left-pad", "1.3.0", VULNERABILITY, Location(package_json, 3), among_others=True
        )
        self.assert_vulnerable_dependency_logged(
            "left-pad", "1.2.0", VULNERABILITY, Location(package_json, 6), among_others=True
        )

    def test_osv_is_asked_about_the_exact_pins_alone(self, mock_run: Mock, glob: Mock):
        """Test that OSV is asked in the npm ecosystem about a dependency pinned to a version, and not about a range."""
        self.stub_no_update(mock_run)
        _discover_package_json(glob, '{"dependencies": {"left-pad": "1.3.0", "react": "^17.0.2"}}')
        with osv() as mock_post:
            update_package_jsons()
        assert_osv_asked_about(mock_post, ("left-pad", "1.3.0"), ecosystem="npm")

    def test_pnpm_project_checked(self, mock_run: Mock, glob: Mock):
        """Test that a dependency a pnpm project pins to a version is looked up, as an npm project's is."""
        mock_run.side_effect = _pnpm_runs(Mock(stdout="{}"), Mock(stdout=""), Mock(stdout="[]"))
        _discover_package_json(glob, '{"packageManager": "pnpm@9.15.0", "dependencies": {"left-pad": "1.3.0"}}')
        with osv() as mock_post:
            update_package_jsons()
        assert_osv_asked_about(mock_post, ("left-pad", "1.3.0"), ecosystem="npm")


@no_vulnerabilities
@staleness_disabled
@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("subprocess.run")
@patch("requests.Session.get")
class YankedDependencyTest(NoUpdateTestCase):
    """Unit tests for the package.json yank check, which reads each pinned version's deprecation on npm."""

    def test_deprecated_pin_warned(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a pin on a version npm deprecates is warned about as yanked, and a pin on another one is not."""
        self.stub_no_update(mock_run)
        get.return_value = npm_registry({}, deprecated={"1.3.0": "use 2.0 instead"})
        contents = (
            '{\n  "name": "app",\n  "dependencies": {\n    "left-pad": "1.3.0",\n    "right-pad": "2.0.0"\n  }\n}\n'
        )
        package_json = _discover_package_json(glob, contents)
        update_package_jsons()
        deprecation = Yank(yanked=True, reason="use 2.0 instead")
        self.assert_yanked_dependency_logged("left-pad", "1.3.0", Location(package_json, 4), deprecation)

    def test_unreachable_registry_reported_without_a_yank(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a registry the yank check cannot reach is reported, and leaves the pin unreported as yanked."""
        self.stub_no_update(mock_run)
        url = "https://registry.npmjs.org/left-pad"
        get.return_value = mock_response(ok=False, status_code=503, reason="Service Unavailable", url=url)
        _discover_package_json(glob, _LEFT_PAD_PINNED)
        update_package_jsons()
        self.assert_could_not_fetch_logged(url, status=503, reason="Service Unavailable")


@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("subprocess.run")
class StaleDependencyTest(NoUpdateTestCase):
    """Unit tests for the package.json staleness check, which makes its own npm registry pass over the deps."""

    @patch("requests.Session.get")
    def test_stale_dependency_warned(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a direct dependency whose newest release is old is warned about, at the line it is declared on."""
        package_json = self.run_on_package_json(get, mock_run, glob, self.clipboard_contents(), STALE_DATE)
        self.assert_stale_dependency_logged("clipboard", "2.0.11", Location(package_json, 4))

    @patch("requests.Session.get")
    def test_dependency_declared_in_two_sections(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a dependency two sections declare is warned about at each of the lines declaring it."""
        contents = (
            "{\n"
            '  "dependencies": {\n    "clipboard": "^2.0.11"\n  },\n'
            '  "devDependencies": {\n    "clipboard": "^2.0.11"\n  }\n'
            "}\n"
        )
        package_json = self.run_on_package_json(get, mock_run, glob, contents, STALE_DATE)
        self.assert_stale_dependency_logged("clipboard", "2.0.11", Location(package_json, 3), Location(package_json, 6))

    @patch("requests.Session.get")
    def test_stale_dependency_a_marker_silences_not_warned(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a `set[stale-after=0]` marker is reported as recognised, and silences the staleness warning."""
        contents = self.clipboard_contents(marker="set[stale-after=0]")
        package_json = self.run_on_package_json(get, mock_run, glob, contents, STALE_DATE)
        self.assert_no_warnings_logged()
        self.assert_recognised_marker_logged("clipboard", Location(package_json, 4), Marker(stale=Threshold(value=0)))

    @patch("requests.Session.get")
    def test_recent_dependency_not_warned(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a direct dependency whose newest release is recent is not warned about as stale."""
        self.run_on_package_json(get, mock_run, glob, self.clipboard_contents())
        self.assert_no_warnings_logged()

    @patch("requests.Session.get")
    def test_dependency_without_release_skipped(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a declared dependency the registry has no release for is skipped without warning or crashing."""
        self.stub_no_update(mock_run)
        get.return_value = mock_response({})  # the document dates no version, so newest_release returns None
        _discover_package_json(glob, self.clipboard_contents())
        update_package_jsons()
        self.assert_no_warnings_logged()

    @staleness_disabled
    @patch("requests.Session.get")
    def test_disabled_makes_no_registry_request(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that `--stale-after 0` skips the staleness pass entirely, so it makes no npm registry request."""
        self.stub_no_update(mock_run)
        _discover_package_json(glob, self.clipboard_contents())
        update_package_jsons()
        get.assert_not_called()
        self.assert_no_warnings_logged()


@no_vulnerabilities
@patch_pathlib_path("rglob", cwd=Path("/"))
@patch("subprocess.run")
@patch("requests.Session.get")
class MarkerTest(NoUpdateTestCase):
    """Unit tests for the markers the `update-time` field of a package.json names for its dependencies."""

    def run_on_marker(self, get: Mock, mock_run: Mock, glob: Mock, marker: str, spec: str = "^2.0.11") -> Mock:
        """Run Update-time on a package.json whose `clipboard` entry the marker steers, its newest release recent."""
        return self.run_on_package_json(get, mock_run, glob, self.clipboard_contents(marker, spec))

    def run_on_undeclared_markers(self, get: Mock, mock_run: Mock, glob: Mock, markers: str) -> Mock:
        """Run Update-time on a package.json whose field holds the JSON members under `dependencies`, on line 5."""
        contents = (
            '{\n  "dependencies": {"clipboard": "^2.0.11"},\n  "devDependencies": {"is-number": "^7.0.0"},\n'
            f'  "update-time": {{\n    "dependencies": {{{markers}}}\n  }}\n}}\n'
        )
        return self.run_on_package_json(get, mock_run, glob, contents)

    @kills(
        Mutation(
            delegated_module._redundant_update_directives,
            "not policy.keeps_the_pin(reference)",
            "not reference.current_version",
            "an `ignore[update]` on an exact pin reads as keeping it, though npm and pnpm leave such a pin where it "
            "is whatever the marker says",
        )
    )
    def test_a_directive_deciding_nothing_is_reported(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that each directive that decides nothing for a `package.json` dependency is reported, saying why."""
        cases = {
            "archived": ("^2.0.11", "ignore[archived]", Reason.NO_ARCHIVAL_SIGNAL),
            "archived on an exact pin": ("2.0.11", "ignore[archived]", Reason.NO_ARCHIVAL_SIGNAL),
            "yanked on a range": ("^2.0.11", "ignore[yanked]", Reason.NO_VERSION_TO_CHECK_FOR_A_YANK),
            "vulnerable on a range": (
                "^2.0.11",
                "set[vulnerability-level=none]",
                Reason.NO_VERSION_TO_CHECK_FOR_A_VULNERABILITY,
            ),
            "cooldown": ("^2.0.11", "set[cooldown=30]", Reason.COOLDOWN_PER_RUN),
            "ignore[update] on a range": ("^2.0.11", "ignore[update]", Reason.MANAGER_RESOLVES_THE_VERSION),
            "ignore[update] on an exact pin": ("2.0.11", "ignore[update]", Reason.MANAGER_RESOLVES_THE_VERSION),
            "bound": ("^2.0.11", "allow[update<3]", Reason.BOUND_DECIDES_NOTHING),
            "hash drift": ("^2.0.11", "allow[hash-drift]", Reason.NO_HASH_PIN),
            "floating pin on an exact pin": ("2.0.11", "allow[floating-pin]", Reason.PIN_NOT_FLOATING),
            "floating pin on a range": ("^2.0.11", "allow[floating-pin]", Reason.RANGE_NEVER_PINNED),
        }
        for case, (spec, directive, reason) in cases.items():
            with self.subTest(case=case):
                package_json = self.run_on_marker(get, mock_run, glob, directive, spec)
                self.assert_redundant_directive_logged(reason, "clipboard", Location(package_json, 4), directive)

    def test_a_warning_scope_is_reported_for_a_dependency_the_registry_serves_no_release_for(
        self, get: Mock, mock_run: Mock, glob: Mock
    ):
        """Test that each warning scope is reported for a dependency whose spec does not name a registry release."""
        spec = "git+https://github.com/zenorocha/clipboard.js.git"
        for directive in ("set[stale-after=0]", "ignore[yanked]", "set[vulnerability-level=none]", "ignore[archived]"):
            with self.subTest(directive=directive):
                package_json = self.run_on_marker(get, mock_run, glob, directive, spec)
                self.assert_redundant_directive_logged(
                    Reason.NO_NPM_RELEASE, "clipboard", Location(package_json, 4), directive
                )

    def test_a_marker_naming_an_entry_its_section_does_not_declare_is_reported(
        self, get: Mock, mock_run: Mock, glob: Mock
    ):
        """Test that a marker under a section that does not declare the name is recognised and reported as redundant."""
        stale = Marker(stale=Threshold(value=0))
        for case, (name, directive, marker) in {
            "undeclared": ("lodash", "set[stale-after=0]", stale),
            "declared in another section": ("is-number", "set[stale-after=0]", stale),
        }.items():
            with self.subTest(case=case):
                package_json = self.run_on_undeclared_markers(get, mock_run, glob, f'"{name}": "{directive}"')
                location = Location(package_json, 5)
                self.assert_redundant_directive_logged(Reason.UNDECLARED_DEPENDENCY, name, location, directive)
                self.assert_recognised_marker_logged(name, location, marker)

    @kills(
        Mutation(
            update_package_json_module.update_package_jsons,
            '_LOG.no_dependencies(package_json, name, "install")',
            '_LOG.no_dependencies(package_json, name, "install")\n            continue',
            "a package.json that npm does not run for is not checked either",
        )
    )
    def test_a_marker_in_a_file_npm_does_not_run_for_is_reported(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a marker in a package.json that does not declare dependencies is reported as redundant."""
        contents = (
            '{\n  "name": "app",\n  "update-time": {\n    "dependencies": {"lodash": "set[stale-after=0]"}\n  }\n}\n'
        )
        package_json = _discover_package_json(glob, contents)
        update_package_jsons()
        mock_run.assert_not_called()
        get.assert_not_called()
        location = Location(package_json, 4)
        self.assert_redundant_directive_logged(Reason.UNDECLARED_DEPENDENCY, "lodash", location, "set[stale-after=0]")

    @kills(
        Mutation(
            update_package_json_module._report_undeclared_reference,
            "if marker.invalid_item is None and marker.raw:",
            "if marker.raw:",
            "an unreadable item on an undeclared dependency is reported as redundant as well as invalid",
        )
    )
    def test_an_undeclared_marker_that_cannot_be_read_is_reported_as_invalid(
        self, get: Mock, mock_run: Mock, glob: Mock
    ):
        """Test that an unreadable marker on an undeclared dependency is reported as invalid, not as redundant."""
        for case, (value, item) in {
            "a value that is not a string": ('["ignore"]', "update-time.dependencies.lodash"),
            "an unreadable item": ('"ignore[stlae]"', "stlae"),
        }.items():
            with self.subTest(case=case):
                package_json = self.run_on_undeclared_markers(get, mock_run, glob, f'"lodash": {value}')
                self.assert_invalid_bracket_item_logged("lodash", Location(package_json, 5), item)

    def test_an_undeclared_marker_without_a_directive_is_not_reported(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that an undeclared dependency's marker without a directive is left unreported."""
        for case, value in {"empty": '""', "bare text": '"pinned on purpose"'}.items():
            with self.subTest(case=case):
                markers = f'"lodash": {value}, "left-pad": "set[stale-after=0]"'
                package_json = self.run_on_undeclared_markers(get, mock_run, glob, markers)
                self.assert_redundant_directive_logged(
                    Reason.UNDECLARED_DEPENDENCY, "left-pad", Location(package_json, 5), "set[stale-after=0]"
                )

    def test_a_field_of_the_wrong_shape_is_reported_as_invalid_and_the_check_still_runs(
        self, get: Mock, mock_run: Mock, glob: Mock
    ):
        """Test that an `update-time` field or section that is not an object is reported as invalid, and checked."""
        for case, field in {"field": '"ignore"', "section": '{"dependencies": "ignore"}'}.items():
            with self.subTest(case=case):
                contents = (
                    '{\n  "name": "app",\n  "dependencies": {\n    "clipboard": "^2.0.11"\n  },\n'
                    f'  "update-time": {field}\n}}\n'
                )
                package_json = self.run_on_package_json(get, mock_run, glob, contents, STALE_DATE)
                location = Location(package_json, 4)
                self.assert_invalid_bracket_item_logged(
                    "clipboard", location, "update-time.dependencies.clipboard", among_others=True
                )
                self.assert_stale_dependency_logged("clipboard", "2.0.11", location, among_others=True)
                self.assertEqual(len(self.records(WARNING)), 2)

    def test_a_marker_steers_the_entry_of_its_own_section_alone(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a marker under `dependencies` leaves the name `devDependencies` declares as well warned about."""
        contents = (
            "{\n"
            '  "dependencies": {\n    "clipboard": "^2.0.11"\n  },\n'
            '  "devDependencies": {\n    "clipboard": "^2.0.11"\n  },\n'
            '  "update-time": {"dependencies": {"clipboard": "set[stale-after=0]"}}\n'
            "}\n"
        )
        package_json = self.run_on_package_json(get, mock_run, glob, contents, STALE_DATE)
        self.assert_stale_dependency_logged("clipboard", "2.0.11", Location(package_json, 6))
        self.assert_no_staleness_warning_logged("clipboard", Location(package_json, 3))

    @kills(
        Mutation(
            policy_module._report_reference_marker,
            "policy.keeps_the_pin(reference) and",
            "bool(reference.current_version) and",
            "an `ignore[update]` on an exact pin reads as holding its update back, though npm decides the update",
        )
    )
    def test_an_ignore_update_on_an_exact_pin_is_not_reported_as_holding_the_update_back(
        self, get: Mock, mock_run: Mock, glob: Mock
    ):
        """Test that an `ignore[update]` on an exact pin is not reported as holding the update back."""
        package_json = self.run_on_marker(get, mock_run, glob, "ignore[update]", "2.0.11")
        marker = Marker(ignored_scopes=Scope.UPDATE)
        self.assert_recognised_marker_logged("clipboard", Location(package_json, 4), marker)
        self.assert_none_logged(message_of(Logger.ignored), "update held back by a marker")
