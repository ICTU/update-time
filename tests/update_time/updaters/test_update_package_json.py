"""Unit tests for the package.json updater (discovery and orchestration of the node package managers)."""

import json
import os
import posixpath
import subprocess  # nosec
import unittest
from datetime import datetime
from logging import WARNING
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast
from unittest.mock import ANY, Mock, call, patch
from urllib.parse import urlparse

from mutation_time.mutation import kills
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import Yank
from update_time.domain.directive import Reason
from update_time.domain.marker import Marker, Scope, Threshold
from update_time.io import process as process_module
from update_time.io.log import Logger
from update_time.manifests import catalogs_yaml as catalogs_yaml_module
from update_time.manifests import npm_declaration as npm_declaration_module
from update_time.manifests import package_json as package_json_module
from update_time.package_managers.node import bun as bun_module
from update_time.package_managers.node import marker_policy as marker_policy_module
from update_time.package_managers.node import npm as npm_module
from update_time.package_managers.node import package_manager as package_manager_module
from update_time.package_managers.node import pnpm_workspace as pnpm_workspace_module
from update_time.package_managers.node import project_update as project_update_module
from update_time.package_managers.node import yarn as yarn_module
from update_time.primitives.command import Command
from update_time.primitives.location import Location
from update_time.primitives.timestamp import MINUTES_PER_DAY, SECONDS_PER_DAY
from update_time.updaters import update_package_json as update_package_json_module
from update_time.updaters.update_package_json import update_package_jsons

from tests.helpers import FakeFiles, mock_path, mock_response, patch_environ, patch_pathlib_path
from tests.update_time.fixtures import FRESH_DATE, STALE_DATE
from tests.update_time.helpers import (
    SCAN_ROOT,
    SUBPROCESS_RUN_OPTIONS,
    ExecutablesOnPathTestCase,
    LoggingTestCase,
    message_of,
    published_version,
    staleness_disabled,
)
from tests.update_time.lock_files.helpers import bun_lock_contents
from tests.update_time.sources.helpers import github_release_json, npm_registry
from tests.update_time.updaters.fixtures import ADVISORY, VULNERABILITY
from tests.update_time.updaters.helpers import assert_osv_asked_about, no_vulnerabilities, osv

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from update_time.io.log import LogMessage

_NPM_COOLDOWN_OPTION = f"--min-release-age={COOLDOWN.default}"  # the cooldown npm option Update-time adds by default
_PNPM_COOLDOWN_OPTION = f"--config.minimumReleaseAge={COOLDOWN.default * MINUTES_PER_DAY}"  # pnpm's
_BUN_COOLDOWN_OPTION = f"--minimum-release-age={COOLDOWN.default * SECONDS_PER_DAY}"  # bun's
_NPM_UNSET = "null\n"  # what `npm config get <cooldown key>` prints when the key is not set
_NPM_PREFIX = "/\n"  # what `npm prefix` prints for a package.json in the root directory, outside a workspace
_PNPM_UNSET = "undefined\n"  # what `pnpm config get minimumReleaseAge` prints when the key is not set
_NPM_LIST = Command("npm", "list", "--json", "--depth=0", "--include=dev", "--silent", "--package-lock-only")
_NPM_COOLDOWN_PROBE = Command("npm", "config", "get", "min-release-age")


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
_WORKSPACE_ROOT_WITH_A_DEPENDENCY = (
    '{\n  "workspaces": ["packages/*"],\n  "dependencies": {\n    "is-number": "^7.0.0"\n  }\n}\n'
)

# A workspace root's package.json that declares its members under `packages/`, but does not declare a dependency.
_WORKSPACE_ROOT_WITHOUT_DEPENDENCIES = '{\n  "workspaces": ["packages/*"]\n}\n'


def _member_contents(name: str) -> str:
    """Return a member's package.json, named after its package, declaring the dependency `package` on line 4."""
    return f'{{\n  "name": "{name}",\n  "dependencies": {{\n    "package": "^1.0.0"\n  }}\n}}\n'


def _npm_workspace_lock(*members: dict) -> Mock:
    """Return what `npm list` prints in a workspace for a lock file holding the members' entries."""
    entries = {name: entry for member in members for name, entry in member.items()}
    return Mock(stdout=json.dumps({"name": "root", "dependencies": entries}))


def _bun_lock(**versions: str) -> str:
    """Return a bun.lock whose root declares the packages as ^1.0.0 dependencies, locked at the versions."""
    root = {"dependencies": dict.fromkeys(versions, "^1.0.0")}
    return bun_lock_contents({"": root}, {name: f"{name}@{version}" for name, version in versions.items()})


_YARN_INFO = Command("yarn", "info", "--all", "--json")
# The update that yarn runs for a lock recording the package `package`.
_YARN_UP = Command("yarn", "up", "-R", "--mode=update-lockfile", "package")
_YARN_CONFIG = Command("yarn", "config", "--json")
_YARN_VERSION = Command("yarn", "--version")
# The variable that sets yarn's cooldown, at the run's default cooldown.
_YARN_GATE = {"YARN_NPM_MINIMAL_AGE_GATE": str(COOLDOWN.default * MINUTES_PER_DAY)}
# The variables Update-time adds to the environment of every yarn command.
_YARN_ENVIRONMENT = {"NO_COLOR": "1"}


def _yarn_call(command: Command, variables: dict[str, str] | None = None) -> object:
    """Return the call running the yarn command in the scan root without colour, with the variables added."""
    environment = os.environ | _YARN_ENVIRONMENT | (variables or {})
    return call(command, **(SUBPROCESS_RUN_OPTIONS | {"env": environment}), cwd=SCAN_ROOT)


def _yarn_config(source: str = "<default>") -> Mock:
    """Return what `yarn config` prints where its cooldown setting comes from the source, as a line per setting."""
    settings = [
        {"key": "enableTelemetry", "effective": True, "source": "<default>"},
        {"key": "npmMinimalAgeGate", "effective": 1440, "source": source},
    ]
    return Mock(stdout="".join(f"{json.dumps(setting)}\n" for setting in settings))


# What `yarn --version` prints where modern yarn runs.
_MODERN_YARN_VERSION = Mock(stdout="4.18.1\n")


def _yarn_runs(*results: object, version: object = _MODERN_YARN_VERSION, config: object = None) -> list:
    """Prepend the answers of `yarn --version` and of `yarn config`, by default modern yarn and its default cooldown."""
    return [version, _yarn_config() if config is None else config, *results]


def _yarn_unmoved(listing: object, **answers: object) -> list:
    """Return the yarn runs of an update that leaves the lock's versions as the listing holds them."""
    return _yarn_runs(listing, Mock(stdout=""), listing, **answers)


def _yarn_info(**versions: str) -> Mock:
    """Return what `yarn info` prints for a lock whose root workspace declares packages as ^1.0.0, at the versions."""
    return _yarn_workspaces_info({".": versions})


def _yarn_workspaces_info(workspaces: dict[str, dict[str, str]]) -> Mock:
    """Return what `yarn info` prints for a lock whose workspaces, by path, declare packages as ^1.0.0, at the versions.

    yarn prints a line per package, sorted by name, so a package line can follow a workspace's. A workspace's line
    names the range of each dependency, and what the lock resolves it to. It leaves the dependencies out of a workspace
    that declares none.
    """
    lines: list[dict] = []
    for path, versions in workspaces.items():
        children: dict = {"Version": "0.0.0"}
        if versions:
            children["Dependencies"] = [
                {"descriptor": f"{name}@npm:^1.0.0", "locator": f"{name}@npm:{version}"}
                for name, version in versions.items()
            ]
        name = "root" if path == "." else posixpath.basename(path)
        lines.append({"value": f"{name}@workspace:{path}", "children": children})
        lines.extend(
            {"value": f"{name}@npm:{version}", "children": {"Version": version}} for name, version in versions.items()
        )
    return Mock(stdout="".join(f"{json.dumps(line)}\n" for line in lines))


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

# A package.json naming pnpm as its package manager, and declaring clipboard with the range ^2.0.11, on line 4.
_PNPM_CLIPBOARD = '{\n  "packageManager": "pnpm@9.15.0",\n  "dependencies": {\n    "clipboard": "^2.0.11"\n  }\n}\n'

# A package.json declaring package with the range ^1.0.0, on line 3.
_PACKAGE_RANGE = '{\n  "dependencies": {\n    "package": "^1.0.0"\n  }\n}\n'

# A package.json declaring minimatch with the range ^3.1.2, on line 3.
_MINIMATCH_RANGE = '{\n  "dependencies": {\n    "minimatch": "^3.1.2"\n  }\n}\n'

# The fields npm's rewrite of minimatch's range to ^3.1.5 is logged with, where the lock file held 3.1.5 already.
_MINIMATCH_REWRITE = {"dependency": "minimatch", "range": "^3.1.2", "new_range": "^3.1.5", "version": "3.1.5"}

# What a package.json holds that cannot be read, and the error reading it raises.
_UNREADABLE = "{"
_UNREADABLE_ERROR = json.JSONDecodeError("Expecting property name enclosed in double quotes", _UNREADABLE, 1)
# The error reading a package.json raises whose JSON is not an object, such as `[]`.
_NOT_AN_OBJECT_ERROR = ValueError("the JSON is not an object")


def _package_json_walk() -> Mock:
    """Return a walk that finds the paths its return value holds where it looks for package.json files, else none."""
    walk = Mock()
    walk.side_effect = lambda pattern, **_kwargs: walk.return_value if pattern == "package.json" else []
    return walk


def _patch_package_json_walk[DecoratedT: Callable[..., object]](cwd: Path) -> Callable[[DecoratedT], DecoratedT]:
    """Patch the walk of the scan with a `_package_json_walk`, and the working directory with the one given."""

    def decorate(target: DecoratedT) -> DecoratedT:
        walked = patch("pathlib.Path.rglob", new_callable=_package_json_walk)(target)
        return patch_pathlib_path(cwd=cwd)(cast("DecoratedT", walked))

    return decorate


def _find_workspace_files(walk: Mock, *workspace_files: Path | Mock) -> None:
    """Make the walk find the files holding catalogs given, each by its name, as the only files of the scan."""
    walk.side_effect = lambda pattern, **_kwargs: [file for file in workspace_files if file.parts[-1] == pattern]


def _discover_package_json(glob: Mock, contents: str) -> Mock:
    """Return a mock package.json holding the contents, as the only one the glob finds."""
    package_json = mock_path(contents, parent=SCAN_ROOT)
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


def _pnpm_workspace_listing(version: str) -> Mock:
    """Return what `pnpm list` prints at the root of a workspace whose member `a` locks `package` at the version."""
    member = {"name": "a", "path": str(SCAN_ROOT / "a"), "dependencies": {"package": {"version": version}}}
    return Mock(stdout=json.dumps([member]))


def _pnpm_runs(*results: object) -> list:
    """Prepend the `pnpm config get minimumReleaseAge` cooldown probe (unset) to the given command results."""
    return [Mock(stdout=_PNPM_UNSET), *results]


def _assert_manager_called(mock_run: Mock, locked: Command, update: Command, directory: Path = SCAN_ROOT) -> None:
    """Assert the manager listed the locked versions, updated, and listed them again, each in the directory."""
    run_kwargs = {**SUBPROCESS_RUN_OPTIONS, "cwd": directory}
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


class _PackageJsonFilesTestCase(ExecutablesOnPathTestCase):
    """Base for the tests that read and write package.json files in a file system in memory."""

    lock_file_name: ClassVar[str]

    def setUp(self) -> None:
        """Start each test with a file system in memory holding the lock file of a project in the root directory."""
        super().setUp()
        self.files = FakeFiles(self.lock_file(SCAN_ROOT))
        self.enterContext(self.files.patched())

    def lock_file(self, directory: Path) -> dict[Path, str]:
        """Return the lock file of the package manager under test in the directory, as a file system holds it."""
        return {directory / self.lock_file_name: ""}

    def write_package_json(self, contents: str, directory: Path = SCAN_ROOT) -> Path:
        """Write a package.json with the contents in the directory, and return its path."""
        path = directory / "package.json"
        self.files[path] = contents
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

    def update_stub(self, path: Path, saved: str, results: list) -> Callable[..., Mock]:
        """Return a `subprocess.run` stub that answers with the results and saves the file on an update."""
        answers = iter(results)

        def run(command: Command, **_kwargs: object) -> Mock:
            """Answer the next run, raising an error as the run's answer, and save the file on an update."""
            if command[1] == "update":
                self.files[path] = saved
            if isinstance(answer := next(answers), BaseException):
                raise answer
            return answer

        return run


# Staleness makes its own npm registry requests; it is disabled here and covered by StaleDependencyTest below.
@staleness_disabled
@_patch_package_json_walk(SCAN_ROOT)
@patch("subprocess.run")
class UpdateNpmPackageJsonTest(_PackageJsonFilesTestCase):
    """Unit tests for updating an npm-managed package.json."""

    lock_file_name = "package-lock.json"

    def create_package_json(self, contents: str = json.dumps(_GIT_DEPENDENCY)) -> Path:
        """Create a package.json in the root directory."""
        return self.write_package_json(contents)

    def assert_npm_called(self, mock_run: Mock, *, cooldown: bool = True, directory: Path = SCAN_ROOT) -> None:
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

    @kills.replace(
        npm_module,
        '("package-lock.json", "npm-shrinkwrap.json")',
        '("package-lock.json",)',
        "a project that an npm-shrinkwrap.json locks is left as it is, as if it did not have a lock file",
    )
    def test_npm_runs_for_a_project_locked_by_a_shrinkwrap_file(self, mock_run: Mock, mock_glob: Mock):
        """Test that npm runs for a project whose workspace root holds an npm-shrinkwrap.json as its lock file."""
        self.files.replace_all({SCAN_ROOT / "npm-shrinkwrap.json": ""})
        mock_glob.return_value = [self.create_package_json()]
        mock_run.side_effect = _npm_runs(*_npm_no_update())
        update_package_jsons()
        self.assert_npm_called(mock_run)

    def test_nearest_directory_naming_a_manager_decides(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json naming npm, or one in a directory between, wins over a pnpm workspace above."""
        npm_project = json.dumps({"packageManager": "npm@11.0.0", **_GIT_DEPENDENCY})
        cases = {
            "the package.json's own field": {SCAN_ROOT / "a/b/package.json": npm_project},
            "a package.json in a directory between": {
                SCAN_ROOT / "a/package.json": npm_project,
                SCAN_ROOT / "a/b/package.json": json.dumps(_GIT_DEPENDENCY),
            },
        }
        for case, files in cases.items():
            with self.subTest(case):
                # Without a lock in a/b, the walk up has to reach the field that names npm:
                self.files.replace_all({SCAN_ROOT / "pnpm-workspace.yaml": "packages:\n  - a/b\n", **files})
                mock_glob.return_value = [package_json := SCAN_ROOT / "a/b/package.json"]
                mock_run.side_effect = [Mock(stdout="/a/b\n")]
                update_package_jsons()
                self.assert_logged_among_others(
                    message_of(Logger.no_lock_file),
                    location=Location(package_json),
                    manager="npm",
                    lock=Location(SCAN_ROOT / "a/b/package-lock.json"),
                )

    def test_npm_lock_beside_the_package_json_wins_over_a_pnpm_workspace_above(self, mock_run: Mock, mock_glob: Mock):
        """Test that npm runs for a package.json beside an npm lock, below a pnpm workspace that would include it."""
        workspace_file = {SCAN_ROOT / "pnpm-workspace.yaml": "packages:\n  - a/b\n"}
        self.files.replace_all({**workspace_file, **self.lock_file(SCAN_ROOT / "a/b")})
        mock_glob.return_value = [self.write_package_json(json.dumps(_GIT_DEPENDENCY), SCAN_ROOT / "a/b")]
        mock_run.side_effect = _npm_runs(*_npm_no_update(), prefix=Mock(stdout="/a/b\n"))
        update_package_jsons()
        self.assert_npm_called(mock_run, directory=SCAN_ROOT / "a/b")

    def run_below_package_json(self, mock_run: Mock, mock_glob: Mock, contents: str) -> None:
        """Run on a project in /a without a lock, below a /package.json holding the contents, and assert npm is chosen.

        A lock in /a would name the manager there, so the walk up would not reach the package.json above.
        """
        self.files.replace_all({SCAN_ROOT / "package.json": contents})
        package_json = self.write_package_json(json.dumps(_GIT_DEPENDENCY), SCAN_ROOT / "a")
        mock_glob.return_value = [package_json]
        mock_run.side_effect = [Mock(stdout="/a\n")]
        update_package_jsons()
        self.assert_logged_among_others(
            message_of(Logger.no_lock_file),
            location=Location(package_json),
            manager="npm",
            lock=Location(SCAN_ROOT / "a/package-lock.json"),
        )

    def test_invalid_package_json_above_is_warned_about_and_passed_over(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json above that does not parse is warned about, and passed over."""
        cases = {"invalid JSON": (_UNREADABLE, _UNREADABLE_ERROR), "not an object": ("[]", _NOT_AN_OBJECT_ERROR)}
        for case, (contents, error) in cases.items():
            with self.subTest(case):
                self.run_below_package_json(mock_run, mock_glob, contents)
                self.assert_logged(
                    message_of(Logger.unreadable_manager_declaration),
                    location=Location(SCAN_ROOT / "package.json"),
                    error=str(error),
                )

    @kills.replace(
        update_package_jsons,
        "_LOG.path(package_json)\n"
        "        if (file := PackageJson.readable(package_json, _LOG.unreadable_package_json)) is None:\n"
        "            continue",
        "if (file := PackageJson.readable(package_json, _LOG.unreadable_package_json)) is None:\n"
        "            continue\n"
        "        _LOG.path(package_json)",
        "a package.json that does not parse is warned about without being logged as checked first",
    )
    def test_package_json_that_does_not_parse_is_warned_about_and_passed_over(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json that does not parse is logged as checked and warned about, and the others update."""
        cases = {"invalid JSON": (_UNREADABLE, _UNREADABLE_ERROR), "not an object": ("[]", _NOT_AN_OBJECT_ERROR)}
        for case, (contents, error) in cases.items():
            with self.subTest(case):
                mock_run.reset_mock()
                root = self.write_package_json(json.dumps(_GIT_DEPENDENCY))
                fixture = self.write_package_json(contents, SCAN_ROOT / "fixtures")
                mock_glob.return_value = [root, fixture]
                mock_run.side_effect = _npm_runs(*_npm_no_update())
                update_package_jsons()
                self.assert_npm_called(mock_run)
                self.assert_unreadable_package_json_warned(fixture, error)
                self.assert_path_logged_first(fixture)

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

    @kills.replace(
        project_update_module.ProjectUpdate.log_moved_versions,
        "npmjs.SOURCE.release(name, new_version)",
        "npmjs.SOURCE.release(package, new_version)",
        "Update-time looks up an alias's changelog and date under the alias, which the registry does not serve",
    )
    def test_an_alias_is_reported_at_its_line_as_the_package_it_aliases(self, mock_run: Mock, mock_glob: Mock):
        """Test that an alias's moved version is logged at its line for the package it aliases, looked up as that."""
        package_json = self.create_package_json('{\n  "dependencies": {\n    "baz": "npm:package@^1.0.0"\n  }\n}\n')
        mock_glob.return_value = [package_json]
        mock_run.side_effect = _npm_runs(_npm_lock(baz="1.0"), Mock(stdout=""), _npm_lock(baz="1.1"))
        with patch("requests.Session.get", _package_lookup_responses()) as get:
            update_package_jsons()
        self.assert_new_version_logged(
            "package", published_version("1.1", _PUBLICATION_TIME), Location(package_json, 3), changes="Changelog"
        )
        urls = [each.args[0] for each in get.call_args_list]
        self.assertIn("https://registry.npmjs.org/package/1.1", urls)
        self.assertEqual([url for url in urls if "baz" in url], [])

    @patch("requests.Session.get", _package_lookup_responses())
    def test_new_version_alone_is_logged_at_the_line_declaring_the_dependency(self, mock_run: Mock, mock_glob: Mock):
        """Test that the new version alone is logged at the line declaring the dependency, whose range npm rewrote."""
        package_json = self.create_package_json(_PACKAGE_RANGE)
        mock_glob.return_value = [package_json]
        runs = _npm_runs(_npm_lock(package="1.0"), Mock(stdout=""), _npm_lock(package="1.1"))
        mock_run.side_effect = self.update_stub(package_json, _PACKAGE_RANGE.replace("^1.0.0", "^1.1.0"), runs)
        update_package_jsons()
        self.assert_new_version_logged(
            "package", published_version("1.1", _PUBLICATION_TIME), Location(package_json, 3), changes="Changelog"
        )

    @kills.replace(
        project_update_module.ProjectUpdate.log_moved_versions,
        "_LOG.version_moved_back(manager, reference, new_version)",
        "_LOG.version_moved_back(manager, moved[0], new_version)",
        "a version moved back is reported at the first line declaring it alone",
    )
    @patch("requests.Session.get", side_effect=_package_lookup_responses().side_effect)
    def test_version_moved_back_is_logged_as_moved_back_at_each_line_declaring_the_dependency(
        self, get: Mock, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a locked version npm moved back is logged as moved back, without a changelog or a range rewrite."""
        original = (
            "{\n"
            '  "dependencies": {\n    "package": "^1.1.0"\n  },\n'
            '  "devDependencies": {\n    "package": "^1.1.0"\n  }\n'
            "}\n"
        )
        package_json = self.create_package_json(original)
        mock_glob.return_value = [package_json]
        runs = _npm_runs(_npm_lock(package="1.1"), Mock(stdout=""), _npm_lock(package="1.0"))
        mock_run.side_effect = self.update_stub(package_json, original.replace("^1.1.0", "^1.0.0"), runs)
        update_package_jsons()
        self.assert_moved_back_logged(
            "npm", "package", "1.1", "1.0", Location(package_json, 3), Location(package_json, 6)
        )
        get.assert_not_called()

    @patch("requests.Session.get", _package_lookup_responses())
    def test_version_moved_back_from_a_pre_release_is_logged_as_moved_back(self, mock_run: Mock, mock_glob: Mock):
        """Test that npm moving a pre-release back to an older release is logged as moved back, ordered as semver."""
        package_json = self.create_package_json()
        mock_glob.return_value = [package_json]
        mock_run.side_effect = _npm_runs(
            _npm_lock(package="15.0.0-canary.5"), Mock(stdout=""), _npm_lock(package="14.2.3")
        )
        update_package_jsons()
        self.assert_moved_back_logged("npm", "package", "15.0.0-canary.5", "14.2.3", Location(package_json))

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

    @kills.replace(
        project_update_module._log_range_rewrites,
        "range_before.current_version == range_after.current_version",
        "range_before == range_after",
        "a range npm only moved to another line by sorting the keys is logged as rewritten",
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
        mock_run.side_effect = _npm_runs(_npm_lock(), Mock(stdout=""), _npm_lock(package="1.1"))
        update_package_jsons()
        self.assert_npm_called(mock_run)
        self.assert_no_new_version_logged()

    def test_range_rewrite_says_the_lock_file_did_not_record_a_version_before(self, mock_run: Mock, mock_glob: Mock):
        """Test that a range npm rewrote says so, where the lock file did not record a version before the update."""
        original = '{\n  "dependencies": {\n    "minimatch": "^3.0.0"\n  }\n}\n'
        package_json = self.create_package_json(original)
        mock_glob.return_value = [package_json]
        runs = _npm_runs(_npm_lock(), Mock(stdout=""), _npm_lock(minimatch="3.1.5"))
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
        package_json = self.create_package_json(_PACKAGE_RANGE)
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

    @kills.replace(
        package_manager_module.ConfiguredCooldown.setting,
        "return key",
        "return self.keys[0]",
        "a project whose npm config sets before is said to set min-release-age",
    )
    def test_project_cooldown_respected_and_named(self, mock_run: Mock, mock_glob: Mock):
        """Test that no cooldown option is added where the project's npm config sets one, and the setting is named."""
        cases = {
            "min-release-age": [Mock(stdout="14\n")],
            "before": [Mock(stdout="null\n"), Mock(stdout="2026-11-01\n")],
        }
        for setting, probes in cases.items():
            with self.subTest(setting):
                mock_run.reset_mock()
                package_json = self.create_package_json()
                mock_glob.return_value = [package_json]
                mock_run.side_effect = [Mock(stdout=_NPM_PREFIX), *probes, *_npm_no_update()]
                update_package_jsons()
                self.assert_npm_called(mock_run, cooldown=False)
                self.assert_project_cooldown_logged(package_json, f"npm {setting}")
                self.assert_no_new_version_logged()
                self.assert_no_warnings_logged()

    def test_cooldown_applied_when_npm_config_get_fails(self, mock_run: Mock, mock_glob: Mock):
        """Test that the cooldown option is added when `npm config get` fails, as it does inside a workspace member."""
        mock_glob.return_value = [self.create_package_json()]
        failed = subprocess.CalledProcessError(cmd="", returncode=1, output="", stderr="npm error code ENOWORKSPACES")
        mock_run.side_effect = [Mock(stdout=_NPM_PREFIX), failed, failed, *_npm_no_update()]
        update_package_jsons()
        self.assert_npm_called(mock_run)

    @_publishing_version_1_1
    @patch("pathlib.Path.resolve", autospec=True, side_effect=lambda path: Path(str(path).replace("/link", "/real")))
    def test_new_version_logged_for_a_package_json_in_a_symlinked_directory(
        self, _resolve: Mock, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a package.json in a symlinked directory has its version logged, whatever npm prefix answers."""
        # The workspace root holds the lock file: the directory npm prefix reports, or else the package.json's own.
        prefixes = {
            "resolved": (Mock(stdout="/real/app\n"), SCAN_ROOT / "real/app"),
            "failing": (
                subprocess.CalledProcessError(cmd="", returncode=1, output="", stderr="npm error prefix"),
                SCAN_ROOT / "link/app",
            ),
        }
        for case, (prefix, workspace_root) in prefixes.items():
            with self.subTest(case):
                self.files.replace_all(self.lock_file(workspace_root))
                package_json = self.write_package_json(_PACKAGE_RANGE, SCAN_ROOT / "link/app")
                mock_glob.return_value = [package_json]
                mock_run.side_effect = _npm_runs(
                    _npm_lock(package="1.0"), Mock(stdout=""), _npm_lock(package="1.1"), prefix=prefix
                )
                update_package_jsons()
                self.assert_new_version_logged(
                    "package", published_version("1.1", _PUBLICATION_TIME), Location(package_json, 3)
                )

    @_publishing_version_1_1
    def test_new_version_of_each_member_dependency_the_root_updated_is_logged_at_the_member(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that the new version of each member's dependency that npm moved when updating the root is logged."""
        roots = {
            "root with dependencies": _WORKSPACE_ROOT_WITH_A_DEPENDENCY,
            "root without dependencies": _WORKSPACE_ROOT_WITHOUT_DEPENDENCIES,
        }
        for case, contents in roots.items():
            with self.subTest(case):
                root = self.write_package_json(contents)
                members = [
                    self.write_package_json(_member_contents(name), SCAN_ROOT / directory)
                    for name, directory in _MEMBERS.values()
                ]
                mock_glob.return_value = [root, *members]
                before = _npm_workspace_lock(*(_npm_member(*member, package="1.0") for member in _MEMBERS.values()))
                after = _npm_workspace_lock(*(_npm_member(*member, package="1.1") for member in _MEMBERS.values()))
                unmoved = [run for _member in members for run in _npm_runs(after, Mock(stdout=""), after)]
                mock_run.side_effect = [*_npm_runs(before, Mock(stdout=""), after), *unmoved]
                update_package_jsons()
                published = published_version("1.1", _PUBLICATION_TIME)
                new_version = message_of(Logger.new_version)
                reports = [
                    self.records_of(new_version, dependency="package", version=published, location=Location(member, 4))
                    for member in members
                ]
                self.assertEqual([len(member_reports) for member_reports in reports], [1, 1])
                self.assertEqual(len(self.new_version_records()), len(members))
                self.assert_none_logged(message_of(Logger.no_dependencies), "skipped package manager")

    def test_member_is_left_to_the_run_of_its_workspace_root(self, mock_run: Mock, mock_glob: Mock):
        """Test that npm updates a workspace root, and not again for a member, which the root's run updates."""
        root = self.write_package_json(_WORKSPACE_ROOT_WITH_A_DEPENDENCY)
        member = self.write_package_json(_member_contents("a"), SCAN_ROOT / "packages/a")
        mock_glob.return_value = [member, root]
        mock_run.side_effect = [*_npm_runs(*_npm_no_update()), Mock(stdout=_NPM_PREFIX)]
        update_package_jsons()
        updated_directories = [
            kwargs["cwd"] for command, kwargs in mock_run.call_args_list if command[0][:2] == ("npm", "update")
        ]
        self.assertEqual(updated_directories, [SCAN_ROOT])
        self.assert_logged_among_others(
            message_of(Logger.updated_by_the_workspace_root),
            location=Location(member),
            manager="npm",
            root=Location(root),
        )

    def test_range_of_a_workspace_member_the_root_rewrote_is_logged_at_the_member(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a member's range npm rewrote when updating the root is logged at the member's package.json."""
        root = self.write_package_json(_WORKSPACE_ROOT_WITH_A_DEPENDENCY)
        name, directory = _MEMBERS["unscoped"]
        original = _member_contents(name)
        member = self.write_package_json(original, SCAN_ROOT / directory)
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
        root, member = SCAN_ROOT / "package.json", SCAN_ROOT / "packages/a/package.json"
        cases: dict[str, tuple[dict[Path, str], Exception]] = {
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
                self.files.replace_all(
                    {root: _WORKSPACE_ROOT_WITH_A_DEPENDENCY, **self.lock_file(SCAN_ROOT), **member_files}
                )
                update_package_jsons()
                self.assert_unreadable_package_json_warned(member, error)
                published = published_version("1.1", _PUBLICATION_TIME)
                self.assert_new_version_logged("package", published, Location(member))

    def test_unmoved_versions_of_workspace_members_not_logged(self, mock_run: Mock, mock_glob: Mock):
        """Test that members without dependencies, new to the lock, or with an unreadable package.json log nothing."""
        mock_glob.return_value = [self.write_package_json(_WORKSPACE_ROOT_WITH_A_DEPENDENCY)]
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
        mock_run.assert_any_call(_NPM_COOLDOWN_PROBE, **SUBPROCESS_RUN_OPTIONS, cwd=SCAN_ROOT)


@staleness_disabled
@_patch_package_json_walk(SCAN_ROOT)
@patch("subprocess.run")
class UpdatePnpmPackageJsonTest(_PackageJsonFilesTestCase):
    """Unit tests for updating a pnpm-managed package.json (detected via the corepack `packageManager` field)."""

    lock_file_name = "pnpm-lock.yaml"

    def create_package_json(
        self, contents: str = json.dumps({"packageManager": "pnpm@9.15.0", **_GIT_DEPENDENCY})
    ) -> Path:
        """Create a package.json in the root directory, declaring pnpm as its package manager."""
        return self.write_package_json(contents)

    def write_pnpm_workspace(
        self, mock_glob: Mock, member: str, catalogs: str = "", root: str | None = '{"name": "root"}'
    ) -> Path:
        """Write a locked workspace for the scan to find, and return the path of its member's package.json."""
        workspace_file = SCAN_ROOT / "pnpm-workspace.yaml"
        self.files.replace_all(
            {workspace_file: f"packages:\n  - a\n{catalogs}", SCAN_ROOT / "pnpm-lock.yaml": "lockfileVersion: '9.0'\n"}
        )
        package_jsons = [] if root is None else [self.write_package_json(root)]
        package_jsons.append(path := self.write_package_json(member, SCAN_ROOT / "a"))
        scanned = {"package.json": package_jsons, "pnpm-workspace.yaml": [workspace_file]}
        mock_glob.side_effect = lambda pattern, **_kwargs: scanned.get(pattern, [])
        return path

    def assert_pnpm_called(self, mock_run: Mock, *, cooldown: bool = True, directory: Path = SCAN_ROOT) -> None:
        """Assert pnpm list, update, and list were called in the directory (with the cooldown option when expected)."""
        cooldown_option = [_PNPM_COOLDOWN_OPTION] if cooldown else []
        _assert_manager_called(
            mock_run,
            Command("pnpm", "list", "--json", "--depth=0", "--lockfile-only"),
            Command("pnpm", "update", "-r", *cooldown_option),
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

    @patch("requests.Session.get", _package_lookup_responses())
    def test_a_workspace_root_without_dependencies_updates_its_members(self, mock_run: Mock, mock_glob: Mock):
        """Test that pnpm runs at a root holding a pnpm-workspace.yaml, though its package.json declares nothing."""
        member = self.write_pnpm_workspace(mock_glob, _PACKAGE_RANGE)
        before = _pnpm_workspace_listing("1.0")
        after = _pnpm_workspace_listing("1.1")
        mock_run.side_effect = _pnpm_runs(before, Mock(stdout=""), after)
        update_package_jsons()
        self.assert_pnpm_called(mock_run)
        version = published_version("1.1", _PUBLICATION_TIME)
        self.assert_new_version_logged("package", version, Location(member, 3), changes="Changelog")

    @kills.force_none(
        pnpm_workspace_module.update_pnpm_workspace,
        "workspace.update(None)",
        "the members of a workspace whose root lacks a package.json are left as they are",
    )
    @patch("requests.Session.get", _package_lookup_responses())
    def test_a_workspace_root_without_a_package_json_updates_its_members(self, mock_run: Mock, mock_glob: Mock):
        """Test that pnpm runs at a workspace root that does not hold a package.json, and updates the members."""
        member = self.write_pnpm_workspace(mock_glob, _PACKAGE_RANGE, root=None)
        mock_run.side_effect = _pnpm_runs(
            _pnpm_workspace_listing("1.0"), Mock(stdout=""), _pnpm_workspace_listing("1.1")
        )
        update_package_jsons()
        self.assert_pnpm_called(mock_run)
        version = published_version("1.1", _PUBLICATION_TIME)
        self.assert_new_version_logged("package", version, Location(member, 3), changes="Changelog")
        self.assert_logged_among_others(
            message_of(Logger.updated_by_the_workspace_root),
            location=Location(member),
            manager="pnpm",
            root=Location(SCAN_ROOT / "pnpm-workspace.yaml"),
        )

    @kills.force_true(
        pnpm_workspace_module.update_pnpm_workspace,
        "is_installed(PNPM.update.executable)",
        "a missing pnpm is run anyway at a workspace root without a package.json, failing once per command",
    )
    def test_a_missing_pnpm_is_reported_once_at_a_member_of_a_root_without_a_package_json(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that pnpm, not on the path, is reported at the member alone, and not run at the root."""
        self.leave_off_the_path("pnpm")
        mock_run.side_effect = FileNotFoundError
        member = self.write_pnpm_workspace(mock_glob, _PACKAGE_RANGE, root=None)
        update_package_jsons()
        mock_run.assert_not_called()
        self.assert_error_logged(
            message_of(Logger.package_manager_not_on_path), location=Location(member), executable="pnpm"
        )

    @kills.replace(
        project_update_module.log_updates,
        "update.log_moved_versions(manager, ",
        'update.log_moved_versions("npm", ',
        "a version pnpm moved back is reported as moved back by npm",
    )
    @patch("requests.Session.get", _package_lookup_responses())
    def test_version_moved_back_is_logged_naming_pnpm(self, mock_run: Mock, mock_glob: Mock):
        """Test that a locked version pnpm moved back is logged as moved back by pnpm."""
        package_json = self.create_package_json()
        mock_glob.return_value = [package_json]
        mock_run.side_effect = _pnpm_runs(_pnpm_lock(package="1.1"), Mock(stdout=""), _pnpm_lock(package="1.0"))
        update_package_jsons()
        self.assert_moved_back_logged("pnpm", "package", "1.1", "1.0", Location(package_json))

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
        # No case holds a lock file at the workspace root, so the line saying why pnpm does not run names the manager:
        cases = {
            "a pnpm-workspace.yaml": ({SCAN_ROOT / "pnpm-workspace.yaml": "packages:\n  - a\n"}, SCAN_ROOT),
            "a pnpm-lock.yaml": ({SCAN_ROOT / "pnpm-lock.yaml": "lockfileVersion: '9.0'\n"}, SCAN_ROOT / "a"),
            "a packageManager field": (
                {SCAN_ROOT / "package.json": '{"packageManager": "pnpm@9.15.0"}'},
                SCAN_ROOT / "a",
            ),
        }
        contents = json.dumps(_GIT_DEPENDENCY)
        for case, (files, lock_root) in cases.items():
            with self.subTest(case):
                self.files.replace_all(files)
                member = self.write_package_json(contents, SCAN_ROOT / "a")
                mock_glob.return_value = [member]
                update_package_jsons()
                mock_run.assert_not_called()
                self.assert_debug_logged_after_path(
                    member, message_of(Logger.no_lock_file), manager="pnpm", lock=Location(lock_root / "pnpm-lock.yaml")
                )

    @kills.replace(
        catalogs_yaml_module.CatalogsYaml.catalogs,
        "CatalogEntry(name, entry.key)",
        'CatalogEntry("", entry.key)',
        "the version pnpm moved for a `catalog:testing` dependency is reported at the member",
    )
    @patch("requests.Session.get", _package_lookup_responses())
    def test_new_version_of_a_catalog_dependency_is_logged_at_the_catalog(self, mock_run: Mock, mock_glob: Mock):
        """Test that the version pnpm moved for a member's `catalog:` dependency is logged at the catalog's line."""
        workspace_file = SCAN_ROOT / "pnpm-workspace.yaml"
        cases = {
            "the unnamed catalog": ("catalog:", "catalog:\n  package: ^1.0.0\n", 4),
            "a named catalog": ("catalog:testing", "catalogs:\n  testing:\n    package: ^1.0.0\n", 5),
        }
        for case, (spec, catalogs, line) in cases.items():
            with self.subTest(case):
                self.write_pnpm_workspace(mock_glob, json.dumps({"dependencies": {"package": spec}}), catalogs)
                before = _pnpm_workspace_listing("1.0")
                after = _pnpm_workspace_listing("1.1")
                mock_run.side_effect = _pnpm_runs(before, Mock(stdout=""), after)
                update_package_jsons()
                version = published_version("1.1", _PUBLICATION_TIME)
                self.assert_new_version_logged("package", version, Location(workspace_file, line), changes="Changelog")

    def test_range_pnpm_rewrote_in_a_catalog_is_logged_at_the_catalog(self, mock_run: Mock, mock_glob: Mock):
        """Test that a catalog's range pnpm rewrote, with the version the member locks unmoved, is logged there."""
        workspace_file = SCAN_ROOT / "pnpm-workspace.yaml"
        self.write_pnpm_workspace(
            mock_glob, '{"dependencies": {"package": "catalog:"}}', "catalog:\n  package: ^1.0.0\n"
        )
        original = self.files[workspace_file]
        listing = _pnpm_workspace_listing("1.1")
        locked = _pnpm_runs(listing, Mock(stdout=""), listing)
        mock_run.side_effect = self.update_stub(workspace_file, original.replace("^1.0.0", "^1.1.0"), locked)
        update_package_jsons()
        fields = {
            "manager": "pnpm",
            "dependency": "package",
            "range": "^1.0.0",
            "new_range": "^1.1.0",
            "version": "1.1",
        }
        self.assert_range_rewrite_logged(location=Location(workspace_file, 4), **fields)

    def test_project_cooldown_respected_and_named(self, mock_run: Mock, mock_glob: Mock):
        """Test that no cooldown option is added where the project's pnpm config sets one, and the setting is named."""
        package_json = self.create_package_json()
        mock_glob.return_value = [package_json]
        # The cooldown probe returns a value, so Update-time adds no cooldown of its own:
        mock_run.side_effect = [Mock(stdout="4320\n"), Mock(stdout="{}"), Mock(stdout=""), Mock(stdout="[]")]
        update_package_jsons()
        self.assert_pnpm_called(mock_run, cooldown=False)
        self.assert_project_cooldown_logged(package_json, "pnpm minimumReleaseAge")
        self.assert_no_new_version_logged()
        self.assert_no_warnings_logged()


@staleness_disabled
@_patch_package_json_walk(SCAN_ROOT)
@patch("subprocess.run")
class UpdateBunPackageJsonTest(_PackageJsonFilesTestCase):
    """Unit tests for updating a bun-managed package.json (detected via its bun.lock)."""

    lock_file_name = "bun.lock"

    def run_beside_bunfig(
        self, mock_run: Mock, mock_glob: Mock, bunfig: Path, contents: str, environment: dict[str, str]
    ) -> None:
        """Run the update of a package.json beside a bun.lock and the bunfig.toml, in the environment given.

        The environment is the test run's without XDG_CONFIG_HOME, plus the variables given.
        """
        without_xdg_config_home = {name: value for name, value in os.environ.items() if name != "XDG_CONFIG_HOME"}
        with patch_environ(without_xdg_config_home | environment, clear=True):
            mock_run.reset_mock()
            self.files.replace_all({bunfig: contents, SCAN_ROOT / self.lock_file_name: _bun_lock(package="1.0")})
            mock_glob.return_value = [self.write_package_json(_PACKAGE_RANGE)]
            mock_run.return_value = Mock(stdout="")
            update_package_jsons()

    @staticmethod
    def assert_bun_updated(mock_run: Mock, *, cooldown: bool = True, directory: Path = SCAN_ROOT) -> None:
        """Assert that bun updated every workspace once, from the directory (with the cooldown option when expected)."""
        cooldown_option = [_BUN_COOLDOWN_OPTION] if cooldown else []
        update = Command("bun", "update", "-r", *cooldown_option)
        mock_run.assert_called_once_with(update, **SUBPROCESS_RUN_OPTIONS, cwd=directory)

    @patch("requests.Session.get", _package_lookup_responses())
    def test_update(self, mock_run: Mock, mock_glob: Mock):
        """Test that bun updates every workspace under the cooldown, and each version it moved in bun.lock is logged."""
        lock = SCAN_ROOT / self.lock_file_name
        package_json = self.write_package_json(_PACKAGE_RANGE)
        mock_glob.return_value = [package_json]
        self.files[lock] = _bun_lock(package="1.0")
        mock_run.side_effect = self.update_stub(lock, _bun_lock(package="1.1"), [Mock(stdout="")])
        update_package_jsons()
        self.assert_bun_updated(mock_run)
        self.assert_new_version_logged(
            "package", published_version("1.1", _PUBLICATION_TIME), Location(package_json, 3), changes="Changelog"
        )
        self.assert_no_warnings_logged()

    @patch("requests.Session.get", _package_lookup_responses())
    def test_root_run_logs_the_new_version_of_a_member_at_the_member(self, mock_run: Mock, mock_glob: Mock):
        """Test that the root's run logs the version bun moved for a member at the member's package.json, alone."""
        lock = SCAN_ROOT / self.lock_file_name
        workspaces = {"": {}, "packages/a": {"name": "a", "dependencies": {"package": "^1.0.0"}}}
        self.files[lock] = bun_lock_contents(workspaces, {"package": "package@1.0"})
        root = self.write_package_json(_WORKSPACE_ROOT_WITHOUT_DEPENDENCIES)
        member = self.write_package_json(_PACKAGE_RANGE, SCAN_ROOT / "packages/a")
        mock_glob.return_value = [root, member]
        after = bun_lock_contents(workspaces, {"package": "package@1.1"})
        mock_run.side_effect = self.update_stub(lock, after, [Mock(stdout=""), Mock(stdout="")])
        update_package_jsons()
        self.assert_bun_updated(mock_run)
        self.assert_new_version_logged(
            "package", published_version("1.1", _PUBLICATION_TIME), Location(member, 3), changes="Changelog"
        )
        self.assert_logged_among_others(
            message_of(Logger.updated_by_the_workspace_root),
            location=Location(member),
            manager="bun",
            root=Location(root),
        )

    def write_catalog_workspace(
        self, mock_glob: Mock, spec: str, catalogs: dict[str, object], version: str
    ) -> tuple[Path, dict[str, object]]:
        """Write a root holding the catalogs, two members declaring `package` as the spec, and a lock at the version.

        Return the root's package.json, and the workspaces the lock records.
        """
        members = {f"packages/{name}": {"name": name, "dependencies": {"package": spec}} for name in "ab"}
        workspaces: dict[str, object] = {"": {}, **members}
        self.files.replace_all(
            {SCAN_ROOT / self.lock_file_name: bun_lock_contents(workspaces, {"package": f"package@{version}"})}
        )
        root = self.write_package_json(json.dumps({"workspaces": {"packages": ["packages/*"]} | catalogs}, indent=2))
        member_contents = json.dumps({"dependencies": {"package": spec}}, indent=2)
        paths = [self.write_package_json(member_contents, SCAN_ROOT / member) for member in members]
        mock_glob.return_value = [root, *paths]
        return root, workspaces

    @kills.delete(
        project_update_module.ProjectUpdate.log_moved_versions,
        " if each.location not in reported",
        "the version bun moved for a catalog's entry is reported once for each member taking it",
    )
    @patch("requests.Session.get", _package_lookup_responses())
    def test_new_version_of_a_catalog_dependency_is_logged_once_at_the_catalog(self, mock_run: Mock, mock_glob: Mock):
        """Test that the version bun moved for the members' `catalog:` dependency is logged at the catalog, once."""
        cases: dict[str, tuple[str, dict[str, object], int]] = {
            "the unnamed catalog": ("catalog:", {"catalog": {"package": "^1.0.0"}}, 7),
            "a named catalog": ("catalog:testing", {"catalogs": {"testing": {"package": "^1.0.0"}}}, 8),
        }
        for case, (spec, catalogs, line) in cases.items():
            with self.subTest(case):
                mock_run.reset_mock()
                root, workspaces = self.write_catalog_workspace(mock_glob, spec, catalogs, "1.0")
                after = bun_lock_contents(workspaces, {"package": "package@1.1"})
                lock = SCAN_ROOT / self.lock_file_name
                mock_run.side_effect = self.update_stub(lock, after, [Mock(stdout=""), Mock(stdout="")])
                update_package_jsons()
                self.assert_new_version_logged(
                    "package", published_version("1.1", _PUBLICATION_TIME), Location(root, line), changes="Changelog"
                )

    @patch("requests.Session.get", _package_lookup_responses())
    def test_range_bun_rewrote_in_a_catalog_is_logged_once_at_the_catalog(self, mock_run: Mock, mock_glob: Mock):
        """Test that a catalog's range bun rewrote, with the version it locks unmoved, is logged at the catalog once."""
        root, _workspaces = self.write_catalog_workspace(
            mock_glob, "catalog:", {"catalog": {"package": "^1.0.0"}}, "1.1"
        )
        rewritten = self.files[root].replace("^1.0.0", "^1.1.0")
        mock_run.side_effect = self.update_stub(root, rewritten, [Mock(stdout=""), Mock(stdout="")])
        update_package_jsons()
        fields = {"manager": "bun", "dependency": "package", "range": "^1.0.0", "new_range": "^1.1.0", "version": "1.1"}
        self.assert_range_rewrite_logged(location=Location(root, 7), **fields)

    def run_with_lock_rewritten(
        self,
        mock_run: Mock,
        mock_glob: Mock,
        lock_before: str,
        lock_after: str,
        read_errors_after: dict[Path, Exception],
    ) -> None:
        """Run the update of a package.json whose bun.lock holds the text before, and the update leaves after."""
        lock = SCAN_ROOT / self.lock_file_name
        self.files.replace_all({lock: lock_before})
        self.files.read_errors = {}

        def update(_command: Command, **_kwargs: object) -> Mock:
            """Rewrite the lock, and make it unreadable where the case says so."""
            self.files[lock] = lock_after
            self.files.read_errors = read_errors_after
            return Mock(stdout="")

        mock_run.reset_mock()
        mock_run.side_effect = update
        mock_glob.return_value = [self.write_package_json(_PACKAGE_RANGE)]
        update_package_jsons()

    @kills.replace(
        bun_module._bun_locked_versions,
        "_LOG.unreadable_bun_lock(lock, error)",
        "del error",
        "Update-time passes over a bun.lock it cannot read without a warning",
    )
    @kills.replace(
        bun_module._bun_locked_versions,
        "except (OSError, ValueError) as error:",
        "except KeyError as error:",
        "a bun.lock that Update-time cannot read stops the run",
        raises="ValueError: it does not hold the JSON bun writes",
    )
    def test_unreadable_lock_is_warned_about_and_no_version_logged(self, mock_run: Mock, mock_glob: Mock):
        """Test that a bun.lock that Update-time cannot read is warned about, and the run does not log a version."""
        lock = SCAN_ROOT / self.lock_file_name
        denied = PermissionError(13, "Permission denied", str(lock))
        not_json = "it does not hold the JSON bun writes"
        cases: dict[str, tuple[str, str, dict[Path, Exception], str]] = {
            "not JSON before the update": ("{", _bun_lock(package="1.1"), {}, not_json),
            "unreadable after the update": (_bun_lock(package="1.0"), "", {lock: denied}, str(denied)),
        }
        for case, (lock_before, lock_after, read_errors_after, error) in cases.items():
            with self.subTest(case):
                self.run_with_lock_rewritten(mock_run, mock_glob, lock_before, lock_after, read_errors_after)
                self.assert_logged(message_of(Logger.unreadable_bun_lock), location=Location(lock), error=error)
                self.assert_no_new_version_logged()

    @kills.replace(
        bun_module._bunfig_sets_a_cooldown,
        "_BUNFIG_COOLDOWN in install",
        "bool(install)",
        "a bunfig.toml with any [install] setting is taken to set a cooldown, so the run applies none",
    )
    @kills.delete(
        bun_module._global_bunfig_sets_a_cooldown,
        "xdg_config_home if xdg_config_home.is_absolute() else ",
        "Update-time reads the global bunfig.toml in the home directory, and bun the one in XDG_CONFIG_HOME",
    )
    @kills.replace(
        bun_module._global_bunfig_sets_a_cooldown,
        "xdg_config_home.is_absolute()",
        "xdg_config_home != Path()",
        "Update-time reads the global bunfig.toml in a relative XDG_CONFIG_HOME, which bun ignores for the home's",
    )
    def test_cooldown_a_bunfig_sets_respected(self, mock_run: Mock, mock_glob: Mock):
        """Test that no cooldown option is added where a bunfig.toml that bun reads sets one, and that it is logged."""
        own_cooldown, other_setting = "[install]\nminimumReleaseAge = 2592000\n", "[install]\nexact = true\n"
        home_set = {"HOME": "/home"}
        xdg_config_home_set = {"HOME": "/home", "XDG_CONFIG_HOME": "/xdg"}
        project_bunfig = SCAN_ROOT / "bunfig.toml"
        home_bunfig = Path("/home/.bunfig.toml")
        cases = {
            "the project's, setting a cooldown": (project_bunfig, own_cooldown, home_set, False),
            "the project's, setting something else": (project_bunfig, other_setting, home_set, True),
            "the home directory's": (home_bunfig, own_cooldown, home_set, False),
            "the one in XDG_CONFIG_HOME": (Path("/xdg/.bunfig.toml"), own_cooldown, xdg_config_home_set, False),
            "the home directory's, while XDG_CONFIG_HOME is set": (
                home_bunfig,
                own_cooldown,
                xdg_config_home_set,
                True,
            ),
            "the home directory's, while XDG_CONFIG_HOME is a relative path": (
                home_bunfig,
                own_cooldown,
                {"HOME": "/home", "XDG_CONFIG_HOME": "config"},
                False,
            ),
        }
        for case, (bunfig, contents, environment, cooldown) in cases.items():
            with self.subTest(case):
                self.run_beside_bunfig(mock_run, mock_glob, bunfig, contents, environment)
                self.assert_bun_updated(mock_run, cooldown=cooldown)
                if cooldown:
                    self.assert_none_logged(message_of(Logger.project_cooldown), "project cooldown")
                else:
                    self.assert_project_cooldown_logged(SCAN_ROOT / "package.json", "bun minimumReleaseAge")

    @kills.uncache(
        bun_module._global_bunfig_sets_a_cooldown,
        "every bun project reads the global bunfig.toml again, and repeats the warning about it",
    )
    def test_unreadable_global_bunfig_is_warned_about_once(self, mock_run: Mock, mock_glob: Mock):
        """Test that a global bunfig.toml that Update-time cannot read is warned about once, however many projects."""
        global_bunfig = Path("/home/.bunfig.toml")
        unclosed_error = "Expected ']' at the end of a table declaration (at line 1, column 9)"
        projects = [SCAN_ROOT / "a", SCAN_ROOT / "b"]
        without_xdg_config_home = {name: value for name, value in os.environ.items() if name != "XDG_CONFIG_HOME"}
        with patch_environ(without_xdg_config_home | {"HOME": "/home"}, clear=True):
            self.files.replace_all(
                {global_bunfig: "[install\n"} | {project / self.lock_file_name: _bun_lock() for project in projects}
            )
            mock_glob.return_value = [self.write_package_json(_PACKAGE_RANGE, project) for project in projects]
            mock_run.return_value = Mock(stdout="")
            update_package_jsons()
        self.assertEqual(len(mock_run.call_args_list), len(projects))
        self.assert_logged(message_of(Logger.unreadable_bunfig), location=Location(global_bunfig), error=unclosed_error)

    @kills.force_false(
        bun_module._bunfig_sets_a_cooldown,
        "not isinstance(install, dict)",
        "a bunfig.toml whose install is not a table stops the run",
        raises="TypeError: argument of type 'int' is not a container or iterable",
    )
    def test_unreadable_bunfig_is_warned_about_and_the_cooldown_applied(self, mock_run: Mock, mock_glob: Mock):
        """Test that a bunfig.toml that Update-time cannot read is warned about, and the run applies the cooldown."""
        unclosed, unclosed_error = "[install\n", "Expected ']' at the end of a table declaration (at line 1, column 9)"
        project_bunfig = SCAN_ROOT / "bunfig.toml"
        denied = PermissionError(13, "Permission denied", str(project_bunfig))
        cases: dict[str, tuple[Path, str, dict[Path, Exception], str]] = {
            "the project's, not valid TOML": (project_bunfig, unclosed, {}, unclosed_error),
            "the project's, unreadable": (project_bunfig, "", {project_bunfig: denied}, str(denied)),
            "the global one, not valid TOML": (Path("/home/.bunfig.toml"), unclosed, {}, unclosed_error),
            "the project's, whose install is not a table": (
                project_bunfig,
                "install = 5\n",
                {},
                "its install is not a table",
            ),
        }
        for case, (bunfig, contents, read_errors, error) in cases.items():
            with self.subTest(case):
                self.files.read_errors = read_errors
                self.run_beside_bunfig(mock_run, mock_glob, bunfig, contents, {"HOME": "/home"})
                self.assert_bun_updated(mock_run)
                self.assert_logged(message_of(Logger.unreadable_bunfig), location=Location(bunfig), error=error)


@staleness_disabled
@_patch_package_json_walk(SCAN_ROOT)
@patch("subprocess.run")
class UpdateYarnPackageJsonTest(_PackageJsonFilesTestCase):
    """Unit tests for updating a package.json that yarn manages (detected via its yarn.lock)."""

    lock_file_name = "yarn.lock"

    @kills.replace(
        yarn_module,
        '"up", "-R",',
        '"up",',
        "yarn moves each package to its latest version, across the range its package.json declares",
    )
    @patch("requests.Session.get", _package_lookup_responses())
    def test_update(self, mock_run: Mock, mock_glob: Mock):
        """Test that yarn updates each dependency its lock records under the cooldown, and each moved one is logged."""
        package_json = self.write_package_json(_PACKAGE_RANGE)
        mock_glob.return_value = [package_json]
        mock_run.side_effect = _yarn_runs(_yarn_info(package="1.0"), Mock(stdout=""), _yarn_info(package="1.1"))
        update_package_jsons()
        update = _YARN_UP
        versioned, configured, listed = _yarn_call(_YARN_VERSION), _yarn_call(_YARN_CONFIG), _yarn_call(_YARN_INFO)
        updated = _yarn_call(update, _YARN_GATE)
        self.assertEqual(mock_run.call_args_list, [versioned, configured, listed, updated, listed])
        self.assert_new_version_logged(
            "package", published_version("1.1", _PUBLICATION_TIME), Location(package_json, 3), changes="Changelog"
        )
        self.assert_no_warnings_logged()

    @kills.replace(
        yarn_module._yarn_locked_versions,
        '_declared_name(each["descriptor"])',
        '_declared_name(each["locator"])',
        "an alias is updated under the name of the package it aliases, which leaves the alias where it is",
    )
    @kills.replace(
        yarn_module,
        're.compile(r"[@#]npm:',
        're.compile(r"@npm:',
        "a dependency with peer dependencies, which yarn resolves to a virtual locator, is never updated",
    )
    @kills.delete(
        yarn_module._registry_version,
        " or _BUILTIN_PATCH_VERSION.search(locator)",
        "a package yarn patches itself, such as typescript, is never updated",
    )
    @_publishing_version_1_1
    def test_an_alias_a_dependency_with_peers_and_a_patched_one_are_updated(self, mock_run: Mock, mock_glob: Mock):
        """Test that the update names an alias, a virtual and a built-in patch locator's package, and logs each."""
        dependencies = {"baz": "npm:react-is@^1.0.0", "react-dom": "^1.0.0", "typescript": "^1.0.0"}
        contents = json.dumps({"dependencies": dependencies}, indent=2)
        package_json = self.write_package_json(contents)
        mock_glob.return_value = [package_json]

        def listing(version: str) -> Mock:
            """Return what `yarn info` prints where the lock resolves each of the three at the version."""
            patched = f"typescript@npm%3A{version}#optional!builtin<compat/typescript>"
            resolved = [
                {"descriptor": "baz@npm:react-is@^1.0.0", "locator": f"react-is@npm:{version}"},
                {"descriptor": "react-dom@virtual:1ec4#npm:^1.0.0", "locator": f"react-dom@virtual:1ec4#npm:{version}"},
                {
                    "descriptor": "typescript@patch:…",
                    "locator": f"typescript@patch:{patched}::version={version}&hash=5a",
                },
            ]
            root = {"value": "root@workspace:.", "children": {"Version": "0.0.0", "Dependencies": resolved}}
            return Mock(stdout=f"{json.dumps(root)}\n")

        mock_run.side_effect = _yarn_runs(listing("1.0"), Mock(stdout=""), listing("1.1"))
        update_package_jsons()
        update = Command("yarn", "up", "-R", "--mode=update-lockfile", "baz", "react-dom", "typescript")
        self.assertIn(update, [each.args[0] for each in mock_run.call_args_list])
        version = published_version("1.1", _PUBLICATION_TIME)
        self.assert_new_version_logged_among_others("react-is", version, Location(package_json, 3))
        self.assert_new_version_logged_among_others("react-dom", version, Location(package_json, 4))
        self.assert_new_version_logged_among_others("typescript", version, Location(package_json, 5))

    @kills.delete(
        yarn_module._yarn_locked_versions,
        " if version is not None",
        "yarn is asked to update a workspace, a file, or a project's own patch, which the registry does not serve",
    )
    def test_only_the_packages_the_registry_serves_are_updated(self, mock_run: Mock, mock_glob: Mock):
        """Test that the update names the registry packages alone, not a workspace, a patch, or a file dependency."""
        mock_glob.return_value = [self.write_package_json(_PACKAGE_RANGE)]
        resolved = [
            {"descriptor": "a@workspace:*", "locator": "a@workspace:packages/a"},
            {"descriptor": "package@npm:^1.0.0", "locator": "package@npm:1.0"},
            {
                "descriptor": "left-pad@patch:…",
                "locator": "left-pad@patch:left-pad@npm%3A1.3.0#~/lp.patch::version=1.3.0",
            },
            {"descriptor": "local@file:../local", "locator": "local@file:../local#../local::hash=cb25a5"},
        ]
        root = {"value": "root@workspace:.", "children": {"Version": "0.0.0", "Dependencies": resolved}}
        listing = Mock(stdout=f"{json.dumps(root)}\n")
        mock_run.side_effect = _yarn_unmoved(listing)
        update_package_jsons()
        update = _YARN_UP
        commands = [each.args[0] for each in mock_run.call_args_list]
        self.assertEqual(commands, [_YARN_VERSION, _YARN_CONFIG, _YARN_INFO, update, _YARN_INFO])
        self.assert_no_new_version_logged()

    @kills.replace(
        yarn_module._yarn_locked_versions,
        "projects[Path(path)]",
        "projects[Path()]",
        "the versions yarn locks for a member are taken for the root's, and logged at the root",
    )
    @patch("requests.Session.get", _package_lookup_responses())
    def test_root_run_logs_the_new_version_of_a_member_at_the_member(self, mock_run: Mock, mock_glob: Mock):
        """Test that yarn updates the members' packages from the root alone, and logs a member's at the member."""
        root = self.write_package_json(_WORKSPACE_ROOT_WITHOUT_DEPENDENCIES)
        member = self.write_package_json(_PACKAGE_RANGE, SCAN_ROOT / "packages/a")
        mock_glob.return_value = [root, member]
        before = _yarn_workspaces_info({".": {}, "packages/a": {"package": "1.0"}})
        after = _yarn_workspaces_info({".": {}, "packages/a": {"package": "1.1"}})
        mock_run.side_effect = _yarn_runs(before, Mock(stdout=""), after)
        update_package_jsons()
        update = _YARN_UP
        commands = [each.args[0] for each in mock_run.call_args_list]
        self.assertEqual(commands, [_YARN_VERSION, _YARN_CONFIG, _YARN_INFO, update, _YARN_INFO])
        self.assert_new_version_logged(
            "package", published_version("1.1", _PUBLICATION_TIME), Location(member, 3), changes="Changelog"
        )
        self.assert_logged_among_others(
            message_of(Logger.updated_by_the_workspace_root),
            location=Location(member),
            manager="yarn",
            root=Location(root),
        )

    def test_a_failed_yarn_command_is_reported_with_what_yarn_printed(self, mock_run: Mock, mock_glob: Mock):
        """Test that a yarn command that fails is reported with the output yarn printed, and the run goes on."""
        mock_glob.return_value = [self.write_package_json(_PACKAGE_RANGE)]
        update = _YARN_UP
        error = "➤ YN0001: │ RequestError: getaddrinfo ENOTFOUND registry.yarnpkg.com\n➤ YN0000: · Failed with errors\n"
        failure = subprocess.CalledProcessError(1, "", output=error, stderr="")
        listing = _yarn_info(package="1.0")
        cases: dict[str, tuple[list, Command]] = {
            "yarn up": (_yarn_runs(listing, failure, listing), update),
            "yarn info": (_yarn_runs(failure, Mock(stdout=""), listing), _YARN_INFO),
        }
        for case, (runs, command) in cases.items():
            with self.subTest(case):
                mock_run.side_effect = runs
                update_package_jsons()
                self.assert_error_logged(message_of(Logger.command_failed), command=command, output=error)
                self.assert_no_new_version_logged()

    @kills.replace(
        yarn_module._yarn_version_policy,
        ">= _FIRST_YARN_WITH_A_COOLDOWN",
        "> _FIRST_YARN_WITH_A_COOLDOWN",
        "yarn 4.10, the first yarn that applies a cooldown, is taken for one that cannot",
    )
    def test_yarn_runs_from_the_first_version_that_applies_a_cooldown(self, mock_run: Mock, mock_glob: Mock):
        """Test that yarn 4.10, the first yarn that applies a cooldown, updates the project."""
        mock_glob.return_value = [self.write_package_json(_PACKAGE_RANGE)]
        listing = _yarn_info(package="1.0")
        mock_run.side_effect = _yarn_unmoved(listing, version=Mock(stdout="4.10.1\n"))
        update_package_jsons()
        update = _YARN_UP
        self.assertIn(_yarn_call(update, _YARN_GATE), mock_run.call_args_list)

    @kills.delete(
        yarn_module._run_yarn,
        " and result.stdout",
        "a yarn command that fails without printing to stdout is reported as failed, with nothing to show",
    )
    def test_a_yarn_command_failing_without_output_is_reported_by_its_stderr_alone(
        self, mock_run: Mock, mock_glob: Mock
    ):
        """Test that a yarn command failing with an empty stdout gets its stderr warning alone, not an error."""
        mock_glob.return_value = [self.write_package_json(_PACKAGE_RANGE)]
        crash = subprocess.CalledProcessError(1, "", output="", stderr="Segmentation fault")
        listing = _yarn_info(package="1.0")
        mock_run.side_effect = _yarn_runs(listing, crash, listing)
        update_package_jsons()
        update = _YARN_UP
        self.assert_command_stderr_logged(update, "Segmentation fault")
        self.assert_none_logged(message_of(Logger.command_failed), "failed command")

    @kills.replace(
        package_manager_module._Workspace.update,
        "is None:\n            return package_json",
        "is None:\n            packages = ()",
        "yarn runs an update and lists again where it has no package to name, repeating a failed listing's error",
    )
    @kills.delete(
        yarn_module._every_locked_package,
        " or None",
        "yarn's empty list of packages is taken to mean every package, and an update that names none runs",
    )
    def test_yarn_neither_updates_nor_lists_again_without_a_package_to_name(self, mock_run: Mock, mock_glob: Mock):
        """Test that yarn stops after its listing where that failed, or where it does not hold a registry package."""
        mock_glob.return_value = [self.write_package_json(_PACKAGE_RANGE)]
        error = (
            "➤ YN0001: │ Error: The lockfile would have been modified by this install, which is explicitly forbidden.\n"
        )
        failed: tuple[LogMessage, dict[str, object]] = (
            message_of(Logger.command_failed),
            {"command": _YARN_INFO, "output": error},
        )
        cases: dict[str, tuple[object, tuple]] = {
            "yarn info fails": (subprocess.CalledProcessError(1, "", output=error, stderr=""), (failed,)),
            "the lock records no registry package": (_yarn_info(), ()),
        }
        for case, (listing, errors) in cases.items():
            with self.subTest(case):
                mock_run.reset_mock()
                mock_run.side_effect = _yarn_unmoved(listing)
                update_package_jsons()
                commands = [each.args[0] for each in mock_run.call_args_list]
                self.assertEqual(commands, [_YARN_VERSION, _YARN_CONFIG, _YARN_INFO])
                self.assert_errors_logged(*errors)

    def update_environment(self, mock_run: Mock, mock_glob: Mock, settings: object) -> object:
        """Run an update where `yarn config` gives the answer, and return the environment the update runs in."""
        mock_run.reset_mock()
        mock_glob.return_value = [self.write_package_json(_PACKAGE_RANGE)]
        listing = _yarn_info(package="1.0")
        mock_run.side_effect = _yarn_unmoved(listing, config=settings)
        update_package_jsons()
        self.assertEqual(mock_run.call_args_list[1], _yarn_call(_YARN_CONFIG))
        (update,) = [each for each in mock_run.call_args_list if each.args[0][:2] == ("yarn", "up")]
        return update.kwargs["env"]

    @kills.delete(
        yarn_module._yarn_cooldown_setting,
        " if sources.get(_YARN_COOLDOWN, _UNSET) == _UNSET else _YARN_COOLDOWN",
        "Update-time's cooldown overrides the one a .yarnrc.yml or the environment sets for yarn",
    )
    def test_cooldown_the_project_sets_is_respected(self, mock_run: Mock, mock_glob: Mock):
        """Test that the update leaves yarn's cooldown to a file or the environment that sets it, and logs that."""
        cases = {"a .yarnrc.yml": "/.yarnrc.yml", "the environment": "<environment>"}
        for case, source in cases.items():
            with self.subTest(case):
                environment = self.update_environment(mock_run, mock_glob, _yarn_config(source))
                self.assertEqual(environment, os.environ | _YARN_ENVIRONMENT)
                self.assert_project_cooldown_logged(SCAN_ROOT / "package.json", "yarn npmMinimalAgeGate")

    @kills.delete(
        yarn_module._yarn_cooldown_setting,
        '_LOG.settings_unreadable(directory / "package.json", "yarn", _YARN_COOLDOWN, str(error))',
        "Update-time applies its cooldown over yarn's settings without saying it could not read them",
    )
    @kills.replace(
        yarn_module._yarn_cooldown_setting,
        "except ValueError as error:",
        "except KeyError as error:",
        "yarn settings that Update-time cannot read stop the run",
        raises="ValueError: Usage Error: Parse error when loading /.yarnrc.yml; please check it's proper Yaml",
    )
    def test_unreadable_settings_are_warned_about_and_the_cooldown_applied(self, mock_run: Mock, mock_glob: Mock):
        """Test that settings yarn does not report are warned about, and that the update takes the run's cooldown."""
        parse_error = "Usage Error: Parse error when loading /.yarnrc.yml; please check it's proper Yaml"
        reported = f"{parse_error}\n\n━━━ Yarn Package Manager - 4.18.1 ━━━\n"
        cases: dict[str, tuple[object, str]] = {
            "yarn reports an error": (subprocess.CalledProcessError(1, "", output=reported, stderr=""), parse_error),
            "yarn fails without saying why": (
                subprocess.CalledProcessError(1, "", output="", stderr=""),
                "yarn failed without saying why",
            ),
            "yarn reports what is not JSON": (
                Mock(stdout="npmMinimalAgeGate: 1d\n"),
                "Expecting value: line 1 column 1 (char 0)",
            ),
        }
        for case, (answer, reason) in cases.items():
            with self.subTest(case):
                environment = self.update_environment(mock_run, mock_glob, answer)
                self.assertEqual(environment, os.environ | _YARN_ENVIRONMENT | _YARN_GATE)
                self.assert_logged(
                    message_of(Logger.settings_unreadable),
                    location=Location(SCAN_ROOT / "package.json"),
                    tool="yarn",
                    setting="npmMinimalAgeGate",
                    reason=reason,
                )


@_patch_package_json_walk(SCAN_ROOT)
@patch("subprocess.run")
class SkipUnsupportedPackageManagerTest(LoggingTestCase):
    """Unit tests for skipping package.json files whose `packageManager` field names an unsupported package manager."""

    def test_skip_unsupported_package_manager(self, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json with an unsupported `packageManager` field is skipped without running anything."""
        mock_package_json = _discover_package_json(mock_glob, '{"packageManager": "deno@2.5.0"}')
        update_package_jsons()
        mock_run.assert_not_called()
        mock_package_json.write_text.assert_not_called()
        self.assert_unsupported_package_manager_logged(mock_package_json, "deno", "npm, pnpm, yarn, and bun")
        self.assert_no_new_version_logged()


@_patch_package_json_walk(SCAN_ROOT)
@patch("subprocess.run")
class SkipPackageJsonWithoutDependenciesTest(ExecutablesOnPathTestCase):
    """Unit tests for a package.json that declares nothing for its package manager to update."""

    @kills.replace(
        package_json_module.PackageJson.declares_workspaces,
        "bool(self.workspace_patterns())",
        '"workspaces" in self.contents',
        "npm runs for a package.json whose workspaces do not name a member, and writes a lock beside it",
    )
    def test_package_manager_not_run(self, mock_run: Mock, mock_glob: Mock):
        """Test that npm and pnpm, both off the path, skip a package.json declaring nothing they install, saying why."""
        self.leave_off_the_path("npm", "pnpm")
        cases = {
            "fields other than dependencies": (
                '{"name": "spark-ui", "license": "Apache License 2.0", "type": "module"}',
                "npm",
            ),
            "empty sections": ('{"dependencies": {}, "devDependencies": {}}', "npm"),
            "empty workspaces": ('{"workspaces": []}', "npm"),
            "a workspaces object without packages": ('{"workspaces": {"packages": []}}', "npm"),
            "a pnpm-managed file": ('{"packageManager": "pnpm@9.15.0"}', "pnpm"),
            "a pnpm-managed file declaring workspaces": (
                '{"packageManager": "pnpm@9.15.0", "workspaces": ["a"]}',
                "pnpm",
            ),
        }
        mock_run.return_value = Mock(stdout="", stderr="")
        for case, (contents, manager) in cases.items():
            with self.subTest(case):
                mock_package_json = _discover_package_json(mock_glob, contents)
                update_package_jsons()
                mock_run.assert_not_called()
                mock_package_json.write_text.assert_not_called()
                self.assert_debug_logged_after_path(
                    mock_package_json, message_of(Logger.no_dependencies), manager=manager, verb="install"
                )


class NoUpdateTestCase(ExecutablesOnPathTestCase):
    """Base for the tests of the checks over the dependencies a package.json declares, run while none is updated."""

    @staticmethod
    def stub_npm_prefix(mock_run: Mock) -> None:
        """Stub `npm prefix`, the only npm run for a project without a lock file."""
        mock_run.side_effect = [Mock(stdout=_NPM_PREFIX)]

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
        self.stub_npm_prefix(mock_run)
        get.return_value = self.registry_doc("2.0.11", published.isoformat())
        package_json = _discover_package_json(glob, contents)
        update_package_jsons()
        return package_json


@_no_deprecations
@staleness_disabled
@_patch_package_json_walk(SCAN_ROOT)
@patch("subprocess.run")
class VulnerableDependencyTest(NoUpdateTestCase):
    """Unit tests for the package.json vulnerability check, which looks up each dependency pinned to a version."""

    def test_vulnerable_pin_warned(self, mock_run: Mock, glob: Mock):
        """Test that a dependency pinned to a version OSV reports an advisory for is warned about, at its line."""
        self.stub_npm_prefix(mock_run)
        package_json = _discover_package_json(glob, _LEFT_PAD_PINNED)
        with osv(ADVISORY):
            update_package_jsons()
        self.assert_vulnerable_dependency_logged("left-pad", "1.3.0", VULNERABILITY, Location(package_json, 4))

    def test_each_pin_of_a_name_warned_at_its_line(self, mock_run: Mock, glob: Mock):
        """Test that each pin of a name two sections declare is warned about at its own line."""
        self.stub_npm_prefix(mock_run)
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

    @kills.replace(
        update_package_json_module._checked_workspace_files,
        "_LOG.path(path)",
        "str(path)",
        "the log at DEBUG does not show that Update-time checked a pnpm-workspace.yaml",
    )
    def test_vulnerable_pin_in_a_pnpm_catalog_warned(self, _mock_run: Mock, glob: Mock):
        """Test that a pnpm-workspace.yaml is logged as checked, and its vulnerable catalog entry warned about."""
        workspace = mock_path("catalog:\n  left-pad: 1.3.0\n", parent=SCAN_ROOT, name="pnpm-workspace.yaml")
        _find_workspace_files(glob, workspace)
        with osv(ADVISORY):
            update_package_jsons()
        self.assert_path_logged_first(workspace)
        self.assert_vulnerable_dependency_logged("left-pad", "1.3.0", VULNERABILITY, Location(workspace, 2))

    @kills.delete(
        update_package_json_module._checked_workspace_files,
        ", _LOG.unreadable_catalogs",
        "Update-time passes over a pnpm-workspace.yaml it cannot read without a warning",
    )
    def test_a_pnpm_workspace_file_that_cannot_be_read_is_warned_about_and_passed_over(
        self, _mock_run: Mock, glob: Mock
    ):
        """Test that a pnpm-workspace.yaml that cannot be read is warned about, and the one after it still checked."""
        undecodable = mock_path("", parent=SCAN_ROOT / "binary", name="pnpm-workspace.yaml")
        undecodable.read_text.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        invalid = mock_path("catalog: [\n", parent=SCAN_ROOT / "broken", name="pnpm-workspace.yaml")
        workspace = mock_path("catalog:\n  left-pad: 1.3.0\n", parent=SCAN_ROOT, name="pnpm-workspace.yaml")
        _find_workspace_files(glob, undecodable, invalid, workspace)
        with osv(ADVISORY):
            update_package_jsons()
        message = message_of(Logger.unreadable_catalogs)
        error = "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
        self.assert_logged_among_others(message, location=Location(undecodable), error=error)
        self.assert_logged_among_others(message, location=Location(invalid), error="it is not valid YAML")
        location = Location(workspace, 2)
        self.assert_vulnerable_dependency_logged("left-pad", "1.3.0", VULNERABILITY, location, among_others=True)

    @kills.delete(
        npm_declaration_module.declaration,
        'aliased["range"] or ',
        "an alias pinned to a version is checked for neither a yank nor a vulnerability",
    )
    def test_osv_is_asked_about_the_exact_pins_alone(self, mock_run: Mock, glob: Mock):
        """Test that OSV is asked about each exact pin, an alias's as the package it aliases, and not about a range."""
        self.stub_npm_prefix(mock_run)
        dependencies = '{"left-pad": "1.3.0", "react": "^17.0.2", "baz": "npm:react-is@18.0.0"}'
        _discover_package_json(glob, f'{{"dependencies": {dependencies}}}')
        with osv() as mock_post:
            update_package_jsons()
        assert_osv_asked_about(mock_post, ("left-pad", "1.3.0"), ("react-is", "18.0.0"), ecosystem="npm")

    def test_pnpm_project_checked(self, _mock_run: Mock, glob: Mock):
        """Test that a dependency a pnpm project pins to a version is looked up, as an npm project's is."""
        _discover_package_json(glob, '{"packageManager": "pnpm@9.15.0", "dependencies": {"left-pad": "1.3.0"}}')
        with osv() as mock_post:
            update_package_jsons()
        assert_osv_asked_about(mock_post, ("left-pad", "1.3.0"), ecosystem="npm")


@no_vulnerabilities
@staleness_disabled
@_patch_package_json_walk(SCAN_ROOT)
@patch("subprocess.run")
@patch("requests.Session.get")
class YankedDependencyTest(NoUpdateTestCase):
    """Unit tests for the package.json yank check, which reads each pinned version's deprecation on npm."""

    def test_deprecated_pin_warned(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a pin on a version npm deprecates is warned about as yanked, and a pin on another one is not."""
        self.stub_npm_prefix(mock_run)
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
        self.stub_npm_prefix(mock_run)
        url = "https://registry.npmjs.org/left-pad"
        get.return_value = mock_response(ok=False, status_code=503, reason="Service Unavailable", url=url)
        _discover_package_json(glob, _LEFT_PAD_PINNED)
        update_package_jsons()
        self.assert_could_not_fetch_logged(url, status=503, reason="Service Unavailable")


@_patch_package_json_walk(SCAN_ROOT)
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
        """Test that a `set[stale-after=0]` marker is recognised, quoted as the field, and silences the warning."""
        contents = self.clipboard_contents(marker="set[stale-after=0]")
        package_json = self.run_on_package_json(get, mock_run, glob, contents, STALE_DATE)
        self.assert_no_warnings_logged()
        self.assert_recognised_marker_logged("clipboard", Location(package_json, 4), Marker(stale=Threshold(value=0)))
        self.assert_recognised_marker_spelled('{"dependencies": {"clipboard": "set[stale-after=0]"}}')

    @patch("requests.Session.get")
    def test_catalog_entry_warned_unless_the_marker_its_catalog_names_silences_it(
        self, get: Mock, mock_run: Mock, glob: Mock
    ):
        """Test that a catalog entry is warned about at its line, and that the marker named under its path steers it."""
        contents = json.dumps(
            {
                "workspaces": {
                    "catalog": {"clipboard": "^2.0.11"},
                    "catalogs": {"legacy": {"clipboard": "^2.0.11"}},
                },
                "update-time": {"workspaces": {"catalogs": {"legacy": {"clipboard": "set[stale-after=0]"}}}},
            },
            indent=2,
        )
        package_json = self.run_on_package_json(get, mock_run, glob, contents, STALE_DATE)
        self.assert_stale_dependency_logged("clipboard", "2.0.11", Location(package_json, 4))
        self.assert_recognised_marker_logged("clipboard", Location(package_json, 8), Marker(stale=Threshold(value=0)))

    @patch("requests.Session.get")
    def test_recent_dependency_not_warned(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a direct dependency whose newest release is recent is not warned about as stale."""
        self.run_on_package_json(get, mock_run, glob, self.clipboard_contents())
        self.assert_no_warnings_logged()

    @patch("requests.Session.get")
    def test_dependency_without_release_skipped(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a declared dependency the registry has no release for is skipped without warning or crashing."""
        self.stub_npm_prefix(mock_run)
        get.return_value = mock_response({})  # the document dates no version, so newest_release returns None
        _discover_package_json(glob, self.clipboard_contents())
        update_package_jsons()
        self.assert_no_warnings_logged()

    @staleness_disabled
    @patch("requests.Session.get")
    def test_disabled_makes_no_registry_request(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that `--stale-after 0` skips the staleness pass entirely, so it makes no npm registry request."""
        self.stub_npm_prefix(mock_run)
        _discover_package_json(glob, self.clipboard_contents())
        update_package_jsons()
        get.assert_not_called()
        self.assert_no_warnings_logged()


@_patch_package_json_walk(SCAN_ROOT)
@patch("subprocess.run", side_effect=FileNotFoundError)
class MissingPackageManagerTest(NoUpdateTestCase):
    """Unit tests for a package.json whose package manager is not on the path."""

    @patch("requests.Session.get")
    @kills.replace(
        package_manager_module.PackageManager.update_package_json,
        "_LOG.package_manager_not_on_path(path, executable)\n"
        "            return CheckedPackageJson(package_json, MARKER_POLICY)",
        "_LOG.package_manager_not_on_path(path, executable)",
        "a missing package manager is reported, and then run anyway, failing once per command",
    )
    def test_a_missing_manager_is_reported_once_and_the_checks_still_run(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that a missing npm or pnpm is reported in one line naming the file, and that the checks still run."""
        for manager, contents in {"npm": self.clipboard_contents(), "pnpm": _PNPM_CLIPBOARD}.items():
            with self.subTest(manager):
                self.leave_off_the_path(manager)
                mock_run.reset_mock()
                get.return_value = self.registry_doc("2.0.11", STALE_DATE.isoformat())
                package_json = _discover_package_json(glob, contents)
                update_package_jsons()
                self.assert_error_logged(
                    message_of(Logger.package_manager_not_on_path), location=Location(package_json), executable=manager
                )
                mock_run.assert_not_called()
                self.assert_stale_dependency_logged("clipboard", "2.0.11", Location(package_json, 4))


# The tests below update a package.json in this member of a workspace, whose root sits in the directory above it.
_MEMBER = SCAN_ROOT / "ws/a"


class _ProjectLeftAsItIsTestCase(_PackageJsonFilesTestCase, NoUpdateTestCase):
    """Base for the tests of a package.json its package manager does not update, while its dependency is checked."""

    lock_file_name = "package-lock.json"

    def assert_left_as_it_is(  # noqa: PLR0913 — the patched mocks travel with the fixtures they are given
        self,
        get: Mock,
        mock_run: Mock,
        mock_glob: Mock,
        *,
        files: dict[Path, str],
        contents: str,
        npm_prefix: Path | None,
        message: LogMessage,
        **fields: object,
    ) -> None:
        """Assert that the run leaves the member's package.json as it is, logs why, and checks its stale dependency.

        npm reports the workspace root as its prefix and runs nothing else. Where the prefix is None, nothing runs.
        """
        self.files.replace_all(files)
        package_json = self.write_package_json(contents, _MEMBER)
        mock_glob.return_value = [package_json]
        mock_run.reset_mock()
        mock_run.side_effect = [] if npm_prefix is None else [Mock(stdout=f"{npm_prefix}\n")]
        get.return_value = self.registry_doc("2.0.11", STALE_DATE.isoformat())
        update_package_jsons()
        runs = [] if npm_prefix is None else [call(Command("npm", "prefix"), **SUBPROCESS_RUN_OPTIONS, cwd=_MEMBER)]
        self.assertEqual(mock_run.call_args_list, runs)
        self.assert_debug_logged_after_path(package_json, message, **fields)
        self.assert_stale_dependency_logged("clipboard", "2.0.11", Location(package_json, 4))


@no_vulnerabilities
@_patch_package_json_walk(SCAN_ROOT)
@patch("subprocess.run")
@patch("requests.Session.get")
class ProjectWithoutALockFileTest(_ProjectLeftAsItIsTestCase):
    """Unit tests for a package.json whose workspace root does not hold a lock file that Update-time reads."""

    def test_npm_not_run(self, get: Mock, mock_run: Mock, mock_glob: Mock):
        """Test that npm does not update a project whose workspace root does not hold a lock, and the checks run."""
        cases = {
            "no lock file": ({}, _MEMBER),
            "a lock file beside a workspace member alone": (self.lock_file(_MEMBER), _MEMBER.parent),
        }
        for case, (files, workspace_root) in cases.items():
            with self.subTest(case):
                self.assert_left_as_it_is(
                    get,
                    mock_run,
                    mock_glob,
                    files=files,
                    contents=self.clipboard_contents(),
                    npm_prefix=workspace_root,
                    message=message_of(Logger.no_lock_file),
                    manager="npm",
                    lock=Location(workspace_root / "package-lock.json"),
                )

    @kills.replace(
        package_manager_module._declares_member,
        "return not excluded and",
        "return True or",
        "a directory below a workspace root that does not declare it a member is updated against the root's lock",
        raises="StopIteration",
    )
    @kills.delete(
        package_manager_module._declares_member,
        "not excluded and ",
        "Update-time ignores a negated pattern of the root's workspaces, and runs bun against the root's lock",
        raises="StopIteration",
    )
    def test_bun_not_run(self, get: Mock, mock_run: Mock, mock_glob: Mock):
        """Test that bun does not update a project whose workspace root does not hold a lock, and the checks run."""
        root = _MEMBER.parent
        cases = {
            "declared in the packages of the root's workspaces object": (
                {root / "package.json": '{"packageManager": "bun@1.4.4", "workspaces": {"packages": ["a"]}}'},
                root,
            ),
            "excluded by a negated pattern of the root's workspaces": (
                {root / "package.json": '{"workspaces": ["*", "!a"]}', root / "bun.lock": ""},
                _MEMBER,
            ),
            "left out of the root's workspaces": (
                {root / "package.json": '{"workspaces": ["b"]}', root / "bun.lock": ""},
                _MEMBER,
            ),
        }
        for case, (files, workspace_root) in cases.items():
            with self.subTest(case):
                self.assert_left_as_it_is(
                    get,
                    mock_run,
                    mock_glob,
                    files=files,
                    contents=self.clipboard_contents(),
                    npm_prefix=None,
                    message=message_of(Logger.no_lock_file),
                    manager="bun",
                    lock=Location(workspace_root / "bun.lock"),
                )

    @kills.replace(
        package_manager_module._binary_lock_policy,
        "_LOG.binary_lock(workspace.root_file(), binary_lock, workspace.root / workspace.manager.lock_files[0])",
        "del binary_lock",
        "Update-time passes over a project locked in a binary bun.lockb without saying how to convert the lock",
    )
    @kills.replace(
        package_manager_module._Workspace.policy_for_a_project_left_as_it_is,
        "(binary_lock := self.root / manager.binary_lock)",
        "(binary_lock := path.parent / manager.binary_lock)",
        "a member whose workspace root holds a binary bun.lockb is said to lack a lock",
    )
    @kills.uncache(
        package_manager_module._binary_lock_policy,
        "a workspace locked in a binary bun.lockb is warned about again for each package.json it holds",
    )
    def test_bun_not_run_for_a_binary_lock(self, get: Mock, mock_run: Mock, mock_glob: Mock):
        """Test that bun does not update a project locked in a binary bun.lockb, warned about once, and checks run."""
        root_declaring_the_member = {_MEMBER.parent / "package.json": '{"workspaces": ["a"]}'}
        cases = {
            "beside the package.json": (_MEMBER, {}),
            "at the workspace root": (_MEMBER.parent, root_declaring_the_member),
        }
        for case, (lock_root, files) in cases.items():
            with self.subTest(case):
                self.files.replace_all({lock_root / "bun.lockb": "", **files})
                package_json = self.write_package_json(self.clipboard_contents(), _MEMBER)
                mock_glob.return_value = [path for path in self.files if path.name == "package.json"]
                mock_run.reset_mock()
                get.return_value = self.registry_doc("2.0.11", STALE_DATE.isoformat())
                update_package_jsons()
                mock_run.assert_not_called()
                self.assertEqual(len(self.records_of(message_of(Logger.binary_lock))), 1)
                self.assert_logged_among_others(
                    message_of(Logger.binary_lock),
                    location=Location(lock_root / "package.json"),
                    lock=Location(lock_root / "bun.lockb"),
                    text_lock=Location(lock_root / "bun.lock"),
                )
                pointers = self.records_of(
                    message_of(Logger.left_with_the_workspace_root), location=Location(package_json)
                )
                self.assertEqual(len(pointers), 0 if lock_root == _MEMBER else 1)
                self.assert_stale_dependency_logged("clipboard", "2.0.11", Location(package_json, 4), among_others=True)

    def test_pnpm_not_run(self, get: Mock, mock_run: Mock, mock_glob: Mock):
        """Test that pnpm does not update a project whose workspace root does not hold a lock, and the checks run."""
        cases = {
            "no lock file": ({}, _MEMBER),
            "a lock file beside a workspace member alone": (
                {_MEMBER.parent / "pnpm-workspace.yaml": "packages:\n  - a\n", _MEMBER / "pnpm-lock.yaml": ""},
                _MEMBER.parent,
            ),
        }
        for case, (files, workspace_root) in cases.items():
            with self.subTest(case):
                self.assert_left_as_it_is(
                    get,
                    mock_run,
                    mock_glob,
                    files=files,
                    contents=_PNPM_CLIPBOARD,
                    npm_prefix=None,
                    message=message_of(Logger.no_lock_file),
                    manager="pnpm",
                    lock=Location(workspace_root / "pnpm-lock.yaml"),
                )


@no_vulnerabilities
@_patch_package_json_walk(SCAN_ROOT)
@patch("subprocess.run")
@patch("requests.Session.get")
class FilesOfMoreThanOneManagerTest(_ProjectLeftAsItIsTestCase):
    """Unit tests for a package.json whose directory holds the files of more than one package manager."""

    @kills.force_false(
        update_package_jsons,
        "choice.names_more_than_one_manager()",
        "the manager whose file comes first runs, and may update a lock the project no longer uses",
    )
    def test_no_manager_runs(self, get: Mock, mock_run: Mock, mock_glob: Mock):
        """Test that no manager runs where the files of the package.json's project name two, which are warned about."""
        cases = {
            "npm's and bun's locks": (_MEMBER, ("package-lock.json", "bun.lock")),
            "npm's shrinkwrap file and pnpm's lock": (_MEMBER, ("npm-shrinkwrap.json", "pnpm-lock.yaml")),
            "pnpm's lock and bun's binary lock": (_MEMBER, ("pnpm-lock.yaml", "bun.lockb")),
            "pnpm's workspace file and bun's lock": (_MEMBER, ("pnpm-workspace.yaml", "bun.lock")),
            "pnpm's and yarn's locks": (_MEMBER, ("pnpm-lock.yaml", "yarn.lock")),
            "pnpm's and bun's locks at the workspace root": (_MEMBER.parent, ("pnpm-lock.yaml", "bun.lock")),
        }
        for case, (directory, names) in cases.items():
            with self.subTest(case):
                self.files.replace_all(dict.fromkeys((directory / name for name in names), ""))
                package_json = self.write_package_json(self.clipboard_contents(), _MEMBER)
                mock_glob.return_value = [package_json]
                mock_run.reset_mock()
                mock_run.return_value = Mock(stdout="", stderr="")
                get.return_value = self.registry_doc("2.0.11", STALE_DATE.isoformat())
                update_package_jsons()
                mock_run.assert_not_called()
                self.assert_logged_among_others(
                    message_of(Logger.files_of_more_than_one_manager),
                    location=Location(package_json),
                    files=tuple(Location(directory / name) for name in names),
                )
                self.assert_stale_dependency_logged("clipboard", "2.0.11", Location(package_json, 4), among_others=True)

    def test_a_package_json_without_dependencies_is_not_warned_about(self, get: Mock, mock_run: Mock, mock_glob: Mock):
        """Test that a package.json beside two managers' locks gets the usual debug line where it declares nothing."""
        self.files.replace_all({_MEMBER / "pnpm-lock.yaml": "", _MEMBER / "bun.lockb": ""})
        package_json = self.write_package_json('{"name": "fixture"}', _MEMBER)
        mock_glob.return_value = [package_json]
        update_package_jsons()
        mock_run.assert_not_called()
        get.assert_not_called()
        self.assert_logged_among_others(
            message_of(Logger.no_dependencies), location=Location(package_json), manager="pnpm", verb="install"
        )
        self.assert_no_warnings_logged()

    def test_the_manager_runs_where_one_is_named(self, get: Mock, mock_run: Mock, mock_glob: Mock):
        """Test that bun runs without a warning where a packageManager field or the files beside it name bun alone."""
        cases = {
            "a packageManager field beside pnpm's lock": (
                '"name": "app", "packageManager": "bun@1.4.4"',
                {_MEMBER / "pnpm-lock.yaml": "", _MEMBER / "bun.lock": _bun_lock()},
            ),
            "bun's text and binary locks": (
                '"name": "app"',
                {_MEMBER / "bun.lock": _bun_lock(), _MEMBER / "bun.lockb": ""},
            ),
        }
        for case, (name_field, files) in cases.items():
            with self.subTest(case):
                self.files.replace_all(files)
                contents = self.clipboard_contents().replace('"name": "app"', name_field)
                package_json = self.write_package_json(contents, _MEMBER)
                mock_glob.return_value = [package_json]
                mock_run.reset_mock()
                mock_run.return_value = Mock(stdout="")
                get.return_value = self.registry_doc("2.0.11", FRESH_DATE.isoformat())
                update_package_jsons()
                update = Command("bun", "update", "-r", _BUN_COOLDOWN_OPTION)
                mock_run.assert_called_once_with(update, **SUBPROCESS_RUN_OPTIONS, cwd=_MEMBER)
                self.assert_no_warnings_logged()


@no_vulnerabilities
@_patch_package_json_walk(_MEMBER)
@patch("subprocess.run")
@patch("requests.Session.get")
class WorkspaceRootOutsideTheScanTest(_ProjectLeftAsItIsTestCase):
    """Unit tests for a package.json whose workspace root lies outside the scan root."""

    def test_npm_not_run(self, get: Mock, mock_run: Mock, mock_glob: Mock):
        """Test that npm does not update a project whose workspace root lies outside the scan, and the checks run."""
        self.assert_left_as_it_is(
            get,
            mock_run,
            mock_glob,
            files=self.lock_file(_MEMBER.parent),
            contents=self.clipboard_contents(),
            npm_prefix=_MEMBER.parent,
            message=message_of(Logger.workspace_root_outside_the_scan),
            manager="npm",
            root=Location(_MEMBER.parent / "package.json"),
        )

    @kills.replace(
        package_manager_module._Workspace.policy_for_a_project_left_as_it_is,
        "return WORKSPACE_ROOT_OUTSIDE_THE_SCAN_POLICY",
        "return MARKER_POLICY",
        "a marker in a member whose workspace root lies outside the scan is reported as one npm decides",
    )
    def test_a_directive_steering_the_update_is_reported_as_deciding_nothing(
        self, get: Mock, mock_run: Mock, mock_glob: Mock
    ):
        """Test that each directive steering the update is reported as redundant, saying why."""
        workspace_root = _MEMBER.parent
        cases = {
            "ignore[update]": Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_LEFT,
            "allow[update<3]": Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_KEPT,
            "set[cooldown=30]": Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_LEFT,
        }
        for directive, reason in cases.items():
            with self.subTest(directive=directive):
                self.files.replace_all(self.lock_file(workspace_root))
                package_json = self.write_package_json(self.clipboard_contents(directive), _MEMBER)
                mock_glob.return_value = [package_json]
                mock_run.side_effect = [Mock(stdout=f"{workspace_root}\n")]
                get.return_value = self.registry_doc("2.0.11", FRESH_DATE.isoformat())
                update_package_jsons()
                self.assert_redundant_directive_logged(reason, "clipboard", Location(package_json, 4), directive)

    @kills.delete(
        package_manager_module.PackageManager.checked_catalogs,
        " if is_scanned(workspace.root) else Catalogs()",
        "a dependency of a catalog outside the scan is said to be checked at the catalog",
    )
    def test_a_warning_scope_on_a_catalog_dependency_is_reported_where_the_catalog_lies_outside_the_scan(
        self, _get: Mock, _mock_run: Mock, mock_glob: Mock
    ):
        """Test that an `ignore[yanked]` on a `catalog:` dependency is reported where the scan misses its catalog."""
        workspace_root = _MEMBER.parent
        cases = {
            "bun": {
                workspace_root / "bun.lock": "",
                workspace_root / "package.json": '{"workspaces": ["a"], "catalog": {"clipboard": "2.0.11"}}',
            },
            "pnpm": {
                workspace_root / "pnpm-workspace.yaml": "packages:\n  - a\ncatalog:\n  clipboard: 2.0.11\n",
                workspace_root / "pnpm-lock.yaml": "",
            },
        }
        for case, files in cases.items():
            with self.subTest(case):
                self.files.replace_all(files)
                package_json = self.write_package_json(self.clipboard_contents("ignore[yanked]", "catalog:"), _MEMBER)
                mock_glob.return_value = [package_json]
                update_package_jsons()
                location = Location(package_json, 4)
                self.assert_redundant_directive_logged(
                    Reason.CATALOG_ENTRY_NOT_FOUND, "clipboard", location, "ignore[yanked]"
                )

    def test_pnpm_not_run(self, get: Mock, mock_run: Mock, mock_glob: Mock):
        """Test that pnpm does not update a project whose workspace root lies outside the scan, and the checks run."""
        workspace_file = _MEMBER.parent / "pnpm-workspace.yaml"
        self.assert_left_as_it_is(
            get,
            mock_run,
            mock_glob,
            files={workspace_file: "packages:\n  - a\n", _MEMBER.parent / "pnpm-lock.yaml": ""},
            contents=_PNPM_CLIPBOARD,
            npm_prefix=None,
            message=message_of(Logger.workspace_root_outside_the_scan),
            manager="pnpm",
            root=Location(workspace_file),
        )


@no_vulnerabilities
@_patch_package_json_walk(SCAN_ROOT)
@patch("subprocess.run")
@patch("requests.Session.get")
class YarnVersionTest(_ProjectLeftAsItIsTestCase):
    """Unit tests for a package.json whose workspace root runs a yarn that cannot update it, or does not say which."""

    lock_file_name = "yarn.lock"

    def run_with_yarn_version(
        self, get: Mock, mock_run: Mock, mock_glob: Mock, contents: str, version: object = None
    ) -> Path:
        """Run the update of a package.json beside a yarn.lock, where `yarn --version` gives the answer."""
        package_json = self.write_package_json(contents)
        mock_glob.return_value = [package_json]
        mock_run.reset_mock()
        mock_run.side_effect = [Mock(stdout="1.22.22\n") if version is None else version]
        get.return_value = self.registry_doc("2.0.11", STALE_DATE.isoformat())
        update_package_jsons()
        self.assertEqual(mock_run.call_args_list, [_yarn_call(_YARN_VERSION)])
        return package_json

    @kills.replace(
        yarn_module,
        "_FIRST_YARN_WITH_A_COOLDOWN = (4, 10)",
        "_FIRST_YARN_WITH_A_COOLDOWN = (2,)",
        "a yarn before 4.10 is run with a cooldown variable it refuses, and fails",
        raises="StopIteration",
    )
    def test_yarn_not_run(self, get: Mock, mock_run: Mock, mock_glob: Mock):
        """Test that a yarn without a cooldown does not update the project, which is warned about, and checks run."""
        for case, version in {"yarn classic": "1.22.22", "a yarn before 4.10": "4.9.0"}.items():
            with self.subTest(case):
                answer = Mock(stdout=f"{version}\n")
                contents = self.clipboard_contents()
                package_json = self.run_with_yarn_version(get, mock_run, mock_glob, contents, answer)
                self.assert_logged_among_others(
                    message_of(Logger.yarn_without_a_cooldown),
                    location=Location(package_json),
                    directory=Location(SCAN_ROOT),
                    version=version,
                )
                self.assert_stale_dependency_logged("clipboard", "2.0.11", Location(package_json, 4), among_others=True)

    @kills.delete(
        process_module.tool_version,
        '_report_unrunnable(Command(executable, "--version"), outcome)',
        "Update-time passes over a yarn that the system cannot run without a word, and does not update it",
    )
    def test_a_yarn_the_system_cannot_run_is_reported_once(self, get: Mock, mock_run: Mock, mock_glob: Mock):
        """Test that a yarn whose interpreter is missing is reported once for the root and member it would run for."""
        root_contents = self.clipboard_contents().replace('"name": "app"', '"name": "app", "workspaces": ["a"]')
        self.files.replace_all({SCAN_ROOT / "yarn.lock": ""})
        root = self.write_package_json(root_contents)
        mock_glob.return_value = [root, self.write_package_json(self.clipboard_contents(), SCAN_ROOT / "a")]
        mock_run.side_effect = [FileNotFoundError(2, "No such file or directory")]
        get.return_value = self.registry_doc("2.0.11", STALE_DATE.isoformat())
        update_package_jsons()
        self.assertEqual(mock_run.call_args_list, [_yarn_call(_YARN_VERSION)])
        self.assert_error_logged(
            message_of(Logger.command_not_found), command=Command("yarn", "--version"), executable="yarn"
        )

    def test_a_workspace_is_warned_about_once_at_its_root(self, get: Mock, mock_run: Mock, mock_glob: Mock):
        """Test that a workspace of a yarn without a cooldown is warned about at its root, which each member names."""
        self.files.replace_all({SCAN_ROOT / "yarn.lock": ""})
        root = self.write_package_json(self.clipboard_contents().replace('"name": "app"', '"workspaces": ["a"]'))
        member = self.write_package_json(self.clipboard_contents(), SCAN_ROOT / "a")
        mock_glob.return_value = [root, member]
        mock_run.side_effect = [Mock(stdout="1.22.22\n")]
        get.return_value = self.registry_doc("2.0.11", STALE_DATE.isoformat())
        update_package_jsons()
        warning = message_of(Logger.yarn_without_a_cooldown)
        at_the_root = self.records_of(
            warning, location=Location(root), directory=Location(SCAN_ROOT), version="1.22.22"
        )
        self.assertEqual(self.records_of(warning), at_the_root)
        self.assertEqual(len(at_the_root), 1)
        self.assert_logged_among_others(
            message_of(Logger.left_with_the_workspace_root),
            location=Location(member),
            manager="yarn",
            root=Location(root),
        )

    @kills.replace(
        yarn_module._yarn_version_policy,
        "all(number.isdigit() for number in numbers)",
        "numbers",
        "a yarn that prints what is not a version stops the run",
        raises="ValueError: invalid literal for int() with base 10: 'yarn'",
    )
    @kills.delete(
        yarn_module._yarn_version_policy,
        '_LOG.tool_version_unknown("yarn", root, f"it printed {version}, which is not a version")',
        "Update-time passes over a project whose yarn prints what is not a version without saying why",
    )
    def test_yarn_not_run_where_its_version_is_unknown(self, get: Mock, mock_run: Mock, mock_glob: Mock):
        """Test that yarn does not run where it does not report its version, which is warned about, saying why."""
        parse_error = "Usage Error: Parse error when loading /.yarnrc.yml; please check it's proper Yaml"
        missing_release = "Error: Cannot find module '/.yarn/releases/yarn-4.18.1.cjs'"
        cases: dict[str, tuple[object, str]] = {
            "a .yarnrc.yml yarn cannot parse": (
                subprocess.CalledProcessError(1, "", output=f"{parse_error}\n", stderr=""),
                parse_error,
            ),
            "a yarnPath naming a missing release": (
                subprocess.CalledProcessError(1, "", output="", stderr=f"{missing_release}\n"),
                missing_release,
            ),
            "nothing printed": (Mock(stdout=""), "it did not print a version"),
            "what is not a version printed": (Mock(stdout="yarn\n"), "it printed yarn, which is not a version"),
        }
        for case, (answer, reason) in cases.items():
            with self.subTest(case):
                contents = self.clipboard_contents()
                package_json = self.run_with_yarn_version(get, mock_run, mock_glob, contents, answer)
                self.assert_logged_among_others(
                    message_of(Logger.tool_version_unknown),
                    executable="yarn",
                    directory=Location(SCAN_ROOT),
                    reason=reason,
                )
                self.assert_stale_dependency_logged("clipboard", "2.0.11", Location(package_json, 4), among_others=True)

    @kills.replace(
        marker_policy_module,
        "Reason.YARN_WITHOUT_A_COOLDOWN_LEFT, Reason.YARN_WITHOUT_A_COOLDOWN_KEPT",
        "Reason.BINARY_LOCK_LEFT, Reason.BINARY_LOCK_KEPT",
        "a marker in a project whose yarn cannot apply a cooldown is reported as redundant for another reason",
    )
    def test_a_directive_steering_the_update_is_reported_as_deciding_nothing(
        self, get: Mock, mock_run: Mock, mock_glob: Mock
    ):
        """Test that each directive steering the update is reported as redundant, saying why."""
        cases = {
            "ignore[update]": Reason.YARN_WITHOUT_A_COOLDOWN_LEFT,
            "allow[update<3]": Reason.YARN_WITHOUT_A_COOLDOWN_KEPT,
            "set[cooldown=30]": Reason.YARN_WITHOUT_A_COOLDOWN_LEFT,
        }
        for directive, reason in cases.items():
            with self.subTest(directive=directive):
                package_json = self.run_with_yarn_version(get, mock_run, mock_glob, self.clipboard_contents(directive))
                location = Location(package_json, 4)
                self.assert_redundant_directive_logged(reason, "clipboard", location, directive, among_others=True)


@no_vulnerabilities
@_patch_package_json_walk(SCAN_ROOT)
@patch("subprocess.run")
@patch("requests.Session.get")
class UpdateMarkerTest(_PackageJsonFilesTestCase, NoUpdateTestCase):
    """Unit tests for the markers that steer the update of a package.json dependency."""

    lock_file_name = "package-lock.json"

    def test_a_directive_steering_the_update_is_reported_as_deciding_nothing(
        self, get: Mock, mock_run: Mock, mock_glob: Mock
    ):
        """Test that each directive steering the update is reported as redundant, saying why, with or without a lock."""
        locked = self.lock_file(SCAN_ROOT)
        cases = {
            "ignore[update] on a range": (locked, "^2.0.11", "ignore[update]", Reason.MANAGER_RESOLVES_THE_VERSION),
            "ignore[update] on an exact pin": (locked, "2.0.11", "ignore[update]", Reason.MANAGER_RESOLVES_THE_VERSION),
            "bound": (locked, "^2.0.11", "allow[update<3]", Reason.BOUND_DECIDES_NOTHING),
            "cooldown": (locked, "^2.0.11", "set[cooldown=30]", Reason.COOLDOWN_PER_RUN),
            "ignore[update] without a lock file": ({}, "^2.0.11", "ignore[update]", Reason.NO_LOCK_FILE_LEFT),
            "bound without a lock file": ({}, "^2.0.11", "allow[update<3]", Reason.NO_LOCK_FILE_KEPT),
            "cooldown without a lock file": ({}, "^2.0.11", "set[cooldown=30]", Reason.NO_LOCK_FILE_LEFT),
        }
        for case, (files, spec, directive, reason) in cases.items():
            with self.subTest(case):
                self.files.replace_all(files)
                package_json = self.write_package_json(self.clipboard_contents(directive, spec))
                mock_glob.return_value = [package_json]
                mock_run.side_effect = _npm_runs(*_npm_no_update()) if files else [Mock(stdout=_NPM_PREFIX)]
                get.return_value = self.registry_doc("2.0.11", FRESH_DATE.isoformat())
                update_package_jsons()
                self.assert_redundant_directive_logged(reason, "clipboard", Location(package_json, 4), directive)

    @kills.replace(
        pnpm_workspace_module._catalog_policy,
        "return NO_LOCK_FILE_POLICY",
        "return MARKER_POLICY",
        "a marker in the catalog of a pnpm workspace without a lock file is judged as if pnpm ran for it",
    )
    @kills.replace(
        pnpm_workspace_module._catalog_policy,
        "if choice is not None and choice.names_more_than_one_manager():",
        "if choice is None:",
        "a marker in a pnpm catalog beside the files of another manager is judged as if pnpm ran for it",
    )
    def test_a_directive_steering_the_update_of_a_pnpm_catalog_entry_is_reported_as_deciding_nothing(
        self, get: Mock, mock_run: Mock, mock_glob: Mock
    ):
        """Test that an `ignore[update]` on a pnpm catalog's entry is reported as redundant, saying why."""
        workspace_file = SCAN_ROOT / "pnpm-workspace.yaml"
        pnpm_manifest = {"package.json": '{"packageManager": "pnpm@10.0.0"}'}
        cases: dict[str, tuple[dict[str, str], Reason]] = {
            "beside a lock file": ({"pnpm-lock.yaml": ""}, Reason.MANAGER_RESOLVES_THE_VERSION),
            "without a lock file": ({}, Reason.NO_LOCK_FILE_LEFT),
            "beside the files of another manager": ({"bun.lock": ""}, Reason.MORE_THAN_ONE_MANAGER_LEFT),
            "beside the files of another manager, and a package.json naming pnpm": (
                {"bun.lock": "", "pnpm-lock.yaml": ""} | pnpm_manifest,
                Reason.MANAGER_RESOLVES_THE_VERSION,
            ),
        }
        for case, (other_files, reason) in cases.items():
            with self.subTest(case):
                files = {workspace_file: "catalog:\n  clipboard: ^2.0.11  # update-time: ignore[update]\n"}
                self.files.replace_all(files | {SCAN_ROOT / name: text for name, text in other_files.items()})
                _find_workspace_files(mock_glob, workspace_file)
                mock_run.side_effect = _pnpm_runs(Mock(stdout="[]"), Mock(stdout=""), Mock(stdout="[]"))
                get.return_value = self.registry_doc("2.0.11", FRESH_DATE.isoformat())
                update_package_jsons()
                location = Location(workspace_file, 2)
                self.assert_redundant_directive_logged(reason, "clipboard", location, "ignore[update]")

    @kills.replace(
        update_package_json_module._yarn_catalog_policies,
        "policies.get(path.parent, MARKER_POLICY)",
        "MARKER_POLICY",
        "a marker in a .yarnrc.yml is judged as if yarn ran, where it does not",
    )
    def test_a_directive_steering_the_update_of_a_yarn_catalog_entry_is_reported_as_deciding_nothing(
        self, get: Mock, mock_run: Mock, mock_glob: Mock
    ):
        """Test that an `ignore[update]` on an entry of a .yarnrc.yml's catalog is reported as redundant, saying why."""
        yarnrc, package_json = SCAN_ROOT / ".yarnrc.yml", SCAN_ROOT / "package.json"
        manifest = '{"packageManager": "yarn@4.18.1", "dependencies": {"clipboard": "catalog:"}}'
        without_a_field = '{"dependencies": {"clipboard": "catalog:"}}'
        listing = _yarn_info()
        cases: dict[str, tuple[dict[str, str], list, Reason]] = {
            "beside a lock file": (
                {"yarn.lock": "", "package.json": manifest},
                _yarn_unmoved(listing),
                Reason.MANAGER_RESOLVES_THE_VERSION,
            ),
            "without a lock file": ({"package.json": manifest}, [], Reason.NO_LOCK_FILE_LEFT),
            "where yarn classic runs": (
                {"yarn.lock": "", "package.json": manifest},
                [Mock(stdout="1.22.22\n")],
                Reason.YARN_WITHOUT_A_COOLDOWN_LEFT,
            ),
            "beside the files of another manager": (
                {"yarn.lock": "", "bun.lock": "", "package.json": without_a_field},
                [],
                Reason.MORE_THAN_ONE_MANAGER_LEFT,
            ),
        }
        for case, (other_files, runs, reason) in cases.items():
            with self.subTest(case):
                files = {yarnrc: "catalog:\n  clipboard: ^2.0.11  # update-time: ignore[update]\n"}
                self.files.replace_all(files | {SCAN_ROOT / name: text for name, text in other_files.items()})
                _find_workspace_files(mock_glob, yarnrc, package_json)
                mock_run.side_effect = runs
                get.return_value = self.registry_doc("2.0.11", FRESH_DATE.isoformat())
                update_package_jsons()
                location = Location(yarnrc, 2)
                self.assert_redundant_directive_logged(
                    reason, "clipboard", location, "ignore[update]", among_others=True
                )

    def run_on_catalog_dependency(self, mock_glob: Mock, spec: str, root: dict[Path, str] | None = None) -> Path:
        """Run Update-time on a member declaring `clipboard` as the spec, and return the member's path.

        The files given hold the workspace root and its catalog, which is pnpm's where none are given.
        """
        pnpm_root = {SCAN_ROOT / "pnpm-workspace.yaml": "packages:\n  - a\ncatalog:\n  clipboard: 2.0.11\n"}
        self.files.replace_all(pnpm_root if root is None else root)
        package_json = self.write_package_json(self.clipboard_contents("ignore[yanked]", spec), SCAN_ROOT / "a")
        mock_glob.return_value = [package_json]
        update_package_jsons()
        return package_json

    @kills.replace(
        marker_policy_module._unchecked_scopes,
        "if declaration.catalog is not None:",
        "if declaration.catalog:",
        "the warning on a dependency of the unnamed catalog says the npm registry does not serve a release for it",
    )
    @kills.force_none(
        yarn_module,
        "lambda root: catalogs_of(CatalogsYaml.readable(root / _YARNRC))",
        "Update-time does not read a yarn workspace's catalogs, and says it does not find a dependency's entry",
    )
    def test_a_warning_scope_on_a_dependency_of_a_scanned_catalog_is_reported_as_checked_at_the_catalog(
        self, _get: Mock, _mock_run: Mock, mock_glob: Mock
    ):
        """Test that an `ignore[yanked]` on a `catalog:` dependency is reported where a scanned catalog holds it."""
        yarn_root = {
            SCAN_ROOT / "package.json": '{"packageManager": "yarn@4.18.1", "workspaces": ["a"]}',
            SCAN_ROOT / ".yarnrc.yml": "catalog:\n  clipboard: 2.0.11\n",
        }
        for case, root in {"pnpm": None, "yarn": yarn_root}.items():
            with self.subTest(case):
                location = Location(self.run_on_catalog_dependency(mock_glob, "catalog:", root), 4)
                reason = Reason.CHECKED_AT_THE_CATALOG
                self.assert_redundant_directive_logged(reason, "clipboard", location, "ignore[yanked]")

    @kills.replace(
        marker_policy_module.CheckedPackageJson.judged_by,
        "catalogs.location_of(declaration) is None",
        "not catalogs.entries",
        "Update-time says it checks a `catalog:typo` dependency at a catalog that the workspace does not hold",
    )
    def test_a_warning_scope_on_a_dependency_of_a_catalog_the_workspace_does_not_hold_is_reported(
        self, _get: Mock, _mock_run: Mock, mock_glob: Mock
    ):
        """Test that an `ignore[yanked]` on a `catalog:typo` dependency is reported, the workspace lacking it."""
        location = Location(self.run_on_catalog_dependency(mock_glob, "catalog:typo"), 4)
        self.assert_redundant_directive_logged(Reason.CATALOG_ENTRY_NOT_FOUND, "clipboard", location, "ignore[yanked]")

    @kills.replace(
        package_manager_module._binary_lock_policy,
        "return BINARY_LOCK_POLICY",
        "return NO_LOCK_FILE_POLICY",
        "a marker in a project locked in a binary bun.lockb is reported as one in a project without a lock",
    )
    def test_a_directive_steering_the_update_beside_a_binary_lock_is_reported_as_deciding_nothing(
        self, get: Mock, mock_run: Mock, mock_glob: Mock
    ):
        """Test that each directive steering the update of a project locked in a bun.lockb is reported as redundant."""
        cases = {"ignore[update]": Reason.BINARY_LOCK_LEFT, "allow[update<3]": Reason.BINARY_LOCK_KEPT}
        for directive, reason in cases.items():
            with self.subTest(directive=directive):
                self.files.replace_all({SCAN_ROOT / "bun.lockb": ""})
                package_json = self.write_package_json(self.clipboard_contents(directive))
                mock_glob.return_value = [package_json]
                get.return_value = self.registry_doc("2.0.11", FRESH_DATE.isoformat())
                update_package_jsons()
                mock_run.assert_not_called()
                location = Location(package_json, 4)
                self.assert_redundant_directive_logged(reason, "clipboard", location, directive, among_others=True)

    @kills.replace(
        marker_policy_module,
        "Reason.MORE_THAN_ONE_MANAGER_LEFT, Reason.MORE_THAN_ONE_MANAGER_KEPT",
        "Reason.NO_LOCK_FILE_LEFT, Reason.NO_LOCK_FILE_KEPT",
        "a marker in a project holding two managers' locks is reported as one in a project without a lock",
    )
    def test_a_directive_steering_the_update_beside_two_managers_files_is_reported_as_deciding_nothing(
        self, get: Mock, mock_run: Mock, mock_glob: Mock
    ):
        """Test that each directive steering the update of a project holding two managers' locks is redundant."""
        cases = {
            "ignore[update]": Reason.MORE_THAN_ONE_MANAGER_LEFT,
            "allow[update<3]": Reason.MORE_THAN_ONE_MANAGER_KEPT,
        }
        for directive, reason in cases.items():
            with self.subTest(directive=directive):
                self.files.replace_all({SCAN_ROOT / "pnpm-lock.yaml": "", SCAN_ROOT / "bun.lock": ""})
                package_json = self.write_package_json(self.clipboard_contents(directive))
                mock_glob.return_value = [package_json]
                get.return_value = self.registry_doc("2.0.11", FRESH_DATE.isoformat())
                update_package_jsons()
                mock_run.assert_not_called()
                location = Location(package_json, 4)
                self.assert_redundant_directive_logged(reason, "clipboard", location, directive, among_others=True)


@no_vulnerabilities
@_patch_package_json_walk(SCAN_ROOT)
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

    @kills.replace(
        update_package_json_module._report_undeclared_reference,
        "steers_the_update=False",
        "steers_the_update=True",
        "an `ignore[update]` on an undeclared dependency is said to hold the update back",
    )
    def test_a_marker_naming_an_entry_its_section_does_not_declare_is_reported(
        self, get: Mock, mock_run: Mock, glob: Mock
    ):
        """Test that a marker under a section that does not declare the name is recognised and reported as redundant."""
        stale = Marker(stale=Threshold(value=0))
        for case, (name, directive, marker) in {
            "undeclared": ("lodash", "set[stale-after=0]", stale),
            "declared in another section": ("is-number", "set[stale-after=0]", stale),
            "an ignore[update] holding nothing back": ("lodash", "ignore[update]", Marker(ignored_scopes=Scope.UPDATE)),
        }.items():
            with self.subTest(case=case):
                package_json = self.run_on_undeclared_markers(get, mock_run, glob, f'"{name}": "{directive}"')
                location = Location(package_json, 5)
                self.assert_redundant_directive_logged(Reason.UNDECLARED_DEPENDENCY, name, location, directive)
                self.assert_recognised_marker_logged(name, location, marker)
                self.assert_recognised_marker_spelled(f'{{"dependencies": {{"{name}": "{directive}"}}}}')
                self.assert_no_ignored_logged()

    @kills.delete(
        package_json_module.PackageJson._catalogs,
        " | marked",
        "a marker under a named catalog the file does not declare goes unreported",
    )
    @kills.replace(
        package_json_module.PackageJson.undeclared_references,
        "*section",
        "section[0]",
        "an undeclared catalog entry's marker is reported at the file rather than at its line",
    )
    def test_a_marker_naming_a_catalog_the_file_does_not_declare_is_reported(
        self, get: Mock, mock_run: Mock, glob: Mock
    ):
        """Test that a marker under a named catalog that the file does not declare is reported as redundant."""
        directive = "set[stale-after=0]"
        field = {"workspaces": {"catalogs": {"ghost": {"lodash": directive}}}}
        contents = json.dumps({"workspaces": {"catalog": {"clipboard": "^2.0.11"}}, "update-time": field}, indent=2)
        package_json = self.run_on_package_json(get, mock_run, glob, contents)
        location = Location(package_json, 11)
        self.assert_redundant_directive_logged(Reason.UNDECLARED_DEPENDENCY, "lodash", location, directive)

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

    @kills.delete(
        update_package_json_module._report_undeclared_reference,
        "marker.invalid_item is None and ",
        "an unreadable item on an undeclared dependency is reported as redundant as well as invalid",
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

    @kills.replace(
        package_json_module._marker,
        "if marker.raw else marker",
        "if directives else marker",
        "the Recognised line names a reference whose value in the field does not hold a directive",
    )
    def test_an_undeclared_marker_without_a_directive_is_not_reported(self, get: Mock, mock_run: Mock, glob: Mock):
        """Test that an undeclared dependency's marker without a directive is left unreported."""
        for case, value in {"empty": '""', "bare text": '"pinned on purpose"'}.items():
            with self.subTest(case=case):
                markers = f'"lodash": {value}, "left-pad": "set[stale-after=0]"'
                package_json = self.run_on_undeclared_markers(get, mock_run, glob, markers)
                self.assert_redundant_directive_logged(
                    Reason.UNDECLARED_DEPENDENCY, "left-pad", Location(package_json, 5), "set[stale-after=0]"
                )
                self.assert_none_logged(message_of(Logger.recognised_marker), "recognised marker", dependency="lodash")

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

    def test_an_ignore_update_on_an_exact_pin_is_not_reported_as_holding_the_update_back(
        self, get: Mock, mock_run: Mock, glob: Mock
    ):
        """Test that an `ignore[update]` on an exact pin is not reported as holding the update back."""
        package_json = self.run_on_marker(get, mock_run, glob, "ignore[update]", "2.0.11")
        marker = Marker(ignored_scopes=Scope.UPDATE)
        self.assert_recognised_marker_logged("clipboard", Location(package_json, 4), marker)
        self.assert_no_ignored_logged()
