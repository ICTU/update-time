# Log messages

Update-time ends each message it logs in the message's name, such as `(new-version)`. Where the message runs over several lines, the name ends its first line. In a terminal, the name links to the message's entry below. Each entry shows a sample of the message and says what it means.

## DEBUG

### archived-dependency-ignored

```console
DEBUG Ignoring the archival warning for aioredis in docs/requirements.txt:12 (update-time: ignore[archived]) (archived-dependency-ignored)
```

A reference's `ignore[archived]` silenced the warning that its dependency is archived. See [Silencing a warning](../README.md#silencing-a-warning).

### build-arguments-substituted

```console
DEBUG Reference python:$PY in Dockerfile:2 is python:3.14 with its build arguments substituted; it does not get a digest, since a digest would overrule a --build-arg (build-arguments-substituted)
```

A Dockerfile `FROM` names build arguments, so Update-time substituted the defaults the `ARG` lines declare. It does not add a digest, since a digest would overrule a `--build-arg`. See [Docker images › What dependencies are updated?](../README.md#what-dependencies-are-updated-5).

### changelog-not-served

```console
DEBUG Could not fetch the changelog at https://example.com/CHANGELOG.md: HTTP 404 Not Found (changelog-not-served)
```

A project publishes the URL of its changelog, but that URL answers with an HTTP error, so Update-time looks for the changes elsewhere, such as in the project's GitHub releases.

### command-line-arguments

```console
DEBUG Command-line arguments: --cooldown 14 --log-level DEBUG (command-line-arguments)
```

The arguments Update-time was started with, quoted as a shell reads them.

### command-run

```console
DEBUG Running YARN_NPM_MINIMAL_AGE_GATE=10080 yarn up -R --mode=update-lockfile left-pad in . (command-run)
```

Update-time runs a command, such as a package manager's update, in the directory the line names. The line names the variables Update-time adds to the command's environment.

### commit-comment-names-no-ref

```console
DEBUG Commit SHA of actions/checkout in .github/workflows/ci.yml:17 was left as it is: its comment pinned does not name a branch or tag of the repository (commit-comment-names-no-ref)
```

A GitHub Action or pre-commit hook pins a full commit SHA with a one-word comment, and the repository has no branch or tag of that name, so Update-time leaves the reference as it is. See [GitHub Actions and pre-commit hooks › Pinning](../README.md#pinning-3).

### compiled-file-without-source

```console
DEBUG Skipping requirements-lock.txt: a compiled or hash-pinned requirements file that does not name a requirements file as its source (compiled-file-without-source)
```

A compiled or hash-pinned requirements file does not name a requirements file as its source. Update-time does not have a source to update, so it skips the file. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### cooldown-set-by-project

```console
DEBUG Not applying the cooldown of 7 days to package.json: bun minimumReleaseAge sets one (cooldown-set-by-project)
```

A package manager's own settings already set a cooldown for a file, or the header of a compiled requirements file records a cooldown that Update-time does not manage. So Update-time does not hand the package manager its own. The line names the setting. See [Cooldown](../README.md#-cooldown).

### credentials

```console
DEBUG Credentials: GITHUB_TOKEN set, DOCKER_HUB_USERNAME set, DOCKER_HUB_TOKEN not set (credentials)
```

Which of the environment variables that raise rate limits are set. The line does not show their values. See [Increasing rate limits](../README.md#increasing-rate-limits).

### file-checked

```console
DEBUG Checking if there are updates for docs/requirements.txt (file-checked)
```

Update-time checks a file it found for dependencies to update.

### floating-tag-kept

```console
DEBUG Keeping the floating tag python:latest in Dockerfile:2: it resolves to 3.14.7@sha256:… (update-time: allow[floating-pin]) (floating-tag-kept)
```

A reference's `allow[floating-pin]` keeps a floating image tag as it is. The line names what Update-time would pin the reference to without the marker. See [Keeping a pin floating](../README.md#keeping-a-pin-floating).

### floating-tag-left-as-is

```console
DEBUG Floating tag acme/api:dev in docker-compose.yml:7 was left as it is: no tag naming a version serves the same image (floating-tag-left-as-is)
```

Update-time leaves a floating image tag as it is, and the line says why, such as that it does not find a tag naming a version that serves the same image. See [Floating image tags](../README.md#floating-image-tags).

### github-rate-limit-headers-unreadable

```console
DEBUG Could not read GitHub's rate-limit headers: 'X-RateLimit-Reset' (github-rate-limit-headers-unreadable)
```

GitHub says its rate limit is used up, but its headers do not say what the limit is or when it resets, so Update-time cannot report the used-up limit.

### held-back-by-cooldown

```console
DEBUG Holding back version 4.38.3 of github/codeql-action/init in .github/workflows/ci.yml:5: published 2026-10-08 13:01 UTC, within the cooldown of 7 days until 2026-10-15 13:01 UTC (held-back-by-cooldown)
```

The cooldown holds back a newer version that was published too recently. The line names the date the cooldown ends for it. See [Cooldown](../README.md#-cooldown).

### held-back-by-marker-cooldown

```console
DEBUG Holding back version 3.15.0 of python in Dockerfile:2: published 2026-10-08 13:01 UTC, within the cooldown of 30 days until 2026-11-07 13:01 UTC (update-time: set[cooldown=30]) (held-back-by-marker-cooldown)
```

A reference's `set[cooldown=…]` holds back a newer version that was published too recently. The line names the date the cooldown ends for it. See [Setting a cooldown period](../README.md#setting-a-cooldown-period).

### image-not-looked-up

```console
DEBUG Reference acme/api:1.2.3 in docker-compose.yml:4 was left as it is: the Compose file builds this image (image-not-looked-up)
```

Update-time does not ask a registry about a reference's image, and the line says why, such as that the Compose file builds the image. See [Docker images › What dependencies are updated?](../README.md#what-dependencies-are-updated-5).

### left-with-workspace-root

```console
DEBUG Not running yarn for packages/ui/package.json: the warning about its workspace root package.json says why (left-with-workspace-root)
```

A package manager does not run for a member of a workspace, for the reason the warning about the workspace root gives. See [npm, pnpm, yarn, and bun dependencies › What files are updated?](../README.md#what-files-are-updated-1).

### marker-recognised

```console
DEBUG Recognised update-time marker ignore[yanked] for humanize in docs/requirements.txt:12 (marker-recognised)
```

Update-time read a reference's marker. Where no `invalid-marker-item` warning sits beside it, it also understood the marker. See [Confirming a marker was understood](../README.md#confirming-a-marker-was-understood).

### maven-not-run

```console
DEBUG Not running Maven over sub/pom.xml: Maven cannot read the reactor this pom is part of (maven-not-run)
```

Update-time does not run Maven over a pom, because Maven cannot read the reactor the pom is part of. An earlier Maven run could not read the reactor, or a pom of the reactor does not parse. The error about the reactor says which. See [Maven dependencies › What files are updated?](../README.md#what-files-are-updated-2).

### network-settings

```console
DEBUG Network settings: proxies https=http://proxy.example.com:3128, except for localhost; the default CA bundle (network-settings)
```

The proxies, the hosts they skip, and the CA bundle that Update-time connects with.

### new-version-not-reported

```console
DEBUG Not reporting a new version for com.google.guava:guava in pom.xml:12: Maven moved the property guava.version to 33.5.0-jre for the other artefacts naming it, and Maven Central does not serve this artefact (new-version-not-reported)
```

Maven moved a property that several artefacts name. Maven Central does not serve this artefact, so Update-time reports the new version for the other artefacts alone.

### no-dependencies-declared

```console
DEBUG Not running uv for pyproject.toml: it does not declare dependencies for uv to resolve (no-dependencies-declared)
```

A file does not declare dependencies for the package manager to install or resolve, so Update-time does not run the package manager for it. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated) and [npm, pnpm, yarn, and bun dependencies › What files are updated?](../README.md#what-files-are-updated-1).

### no-lock-file

```console
DEBUG Not running npm for web/package.json: it does not have a lock file, and npm would write web/package-lock.json (no-lock-file)
```

A project does not have a lock file, and the package manager would write one, so Update-time does not run it. To have Update-time update the project, run the package manager's install once. See [npm, pnpm, yarn, and bun dependencies › What files are updated?](../README.md#what-files-are-updated-1).

### no-release-tagged

```console
DEBUG Reference actions/checkout in .github/workflows/ci.yml:17 was left as it is: the repository does not tag a release to update to (no-release-tagged)
```

The repository of a GitHub Action or pre-commit hook does not tag a release to update to, so Update-time leaves the reference as it is. See [GitHub Actions and pre-commit hooks › What versions are updated?](../README.md#what-versions-are-updated-3).

### not-a-uv-project

```console
DEBUG Not running uv for legacy/pyproject.toml: it does not have a [tool.uv] table, and legacy/uv.lock does not exist (not-a-uv-project)
```

A `pyproject.toml` has neither a `[tool.uv]` table nor a `uv.lock` beside its workspace root, so Update-time does not run uv for it. To have Update-time update the project, run `uv lock` once. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### path-excluded

```console
DEBUG Excluding vendor from the scan (--exclude-path) (path-excluded)
```

`--exclude-path` excludes a directory from the scan. See [Getting started](../README.md#getting-started).

### project-marked-unmanaged

```console
DEBUG Not running uv for vendored/pyproject.toml: the [tool.uv] table of vendored/pyproject.toml sets managed = false (project-marked-unmanaged)
```

The `[tool.uv]` table of the workspace root sets `managed = false`, so Update-time does not run uv for the `pyproject.toml`. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### ref-kept

```console
DEBUG Keeping the branch actions/checkout@main in .github/workflows/ci.yml:17: it resolves to 4.3.0@… (update-time: allow[floating-pin]) (ref-kept)
```

A reference's `allow[floating-pin]` keeps a branch, or a tag such as `stable-2024`, as it is. The line names what Update-time would pin the reference to without the marker. See [Keeping a pin floating](../README.md#keeping-a-pin-floating).

### repository-not-served

```console
DEBUG Reference spark-rm-base:3.5.1 in Dockerfile:2 was left as it is: the registry does not serve the image's repository (repository-not-served)
```

The registry does not serve the repository of a reference's image, so Update-time leaves the reference as it is. That happens for an image the project builds itself, a mistyped image name, and a private image. See [Docker images › What dependencies are updated?](../README.md#what-dependencies-are-updated-5).

### run-started

```console
DEBUG Running Update-time v0.0.61 on macOS-26.1-arm64 with Python 3.14.2 (run-started)
```

The version of Update-time, and the operating system and Python version it runs on.

### source-outside-scan

```console
DEBUG Leaving docs/requirements.txt and its source vendor/requirements.in as they are: the scan leaves vendor/requirements.in out (source-outside-scan)
```

The scan leaves out a source of a compiled requirements file, so Update-time leaves both the compiled file and its source as they are. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### stale-dependency-ignored

```console
DEBUG Ignoring the staleness warning for humanize in docs/requirements.txt:12 (update-time: set[stale-after=0]) (stale-dependency-ignored)
```

A reference's `set[stale-after=…]` silenced the staleness warning that the run's threshold gives. See [Setting a staleness threshold](../README.md#setting-a-staleness-threshold).

### tool-version

```console
DEBUG Version of yarn in .: 4.18.1 (tool-version)
```

The version that an executable, such as yarn, reports in a directory.

### update-held-back

```console
DEBUG Ignoring updates for humanize in docs/requirements.txt:12 (update-time: ignore[update]) (update-held-back)
```

A reference's `ignore[update]` holds its update back. On a pom, so does a bound that holds back every version, such as one naming `34.1rc1`. See [Holding a reference back](../README.md#holding-a-reference-back) and [Maven dependencies › Markers](../README.md#markers-2).

### updated-by-workspace-root

```console
DEBUG The bun run for the workspace root package.json updates packages/ui/package.json (updated-by-workspace-root)
```

The package manager's run for the workspace root updates a member of the workspace, so the member does not get a run of its own. See [npm, pnpm, yarn, and bun dependencies › What versions are updated?](../README.md#what-versions-are-updated-1).

### version-listing-not-served

```console
DEBUG Could not fetch the version listing at https://repo1.maven.org/maven2/nl/example/internal-lib/ (HTTP 404) (version-listing-not-served)
```

Maven Central does not serve a version listing for an artefact, as for one a project resolves from a mirror or a private repository. So the cooldown holds nothing back for the artefact, and Update-time does not check it for staleness. See [Maven dependencies › Cooldown](../README.md#cooldown-2).

### versions-held-back-by-bound

```console
DEBUG Holding back the versions [34,) of com.google.guava:guava in pom.xml:12 (update-time: ignore[update>=34]) (versions-held-back-by-bound)
```

A bound in a reference's marker holds back the versions the line names, written as a Maven range. See [Seeing what a marker did](../README.md#seeing-what-a-marker-did).

### versions-held-back-by-requires-python

```console
DEBUG Holding back the versions of python in .python-version:1 that the requires-python >=3.10,<3.12 of pyproject.toml:2 excludes (versions-held-back-by-requires-python)
```

The project's `requires-python` holds a `.python-version` entry back from the Python versions it excludes. See [Node engine version and Python version › What versions are updated?](../README.md#what-versions-are-updated-4).

### versions-plugin-setting-wins

```console
DEBUG The versions plugin's rulesUri wins over Update-time for pom.xml, so Maven may adopt versions of another variant, versions inside the cooldown, and versions a marker holds back (versions-plugin-setting-wins)
```

A setting of the versions plugin's configuration, such as a `rulesUri`, wins over what Update-time tells the plugin. The line says what Maven may adopt as a result. See [Maven dependencies › What versions are updated?](../README.md#what-versions-are-updated-2).

### versions-plugin-setting-wins-at-line

```console
DEBUG The versions plugin's rulesUri, set on line 12 of org.example:parent:1.0, wins over Update-time for pom.xml, so Maven may adopt versions of another variant, versions inside the cooldown, and versions a marker holds back (versions-plugin-setting-wins-at-line)
```

A setting of the versions plugin's configuration, such as a `rulesUri`, wins over what Update-time tells the plugin. The line names the pom and the line that set it, and what Maven may adopt as a result. See [Maven dependencies › What versions are updated?](../README.md#what-versions-are-updated-2).

### vulnerability-ignored-by-option

```console
DEBUG Ignoring the GHSA-2gwj-7jmv-h26r vulnerability warning for django in docs/requirements.txt:12 (--ignore-vulnerability GHSA-2gwj-7jmv-h26r) (vulnerability-ignored-by-option)
```

`--ignore-vulnerability` silenced the warning about an advisory. See [Vulnerable dependencies](../README.md#️-vulnerable-dependencies).

### vulnerable-dependency-ignored

```console
DEBUG Ignoring the GHSA-2gwj-7jmv-h26r vulnerability warning for django in docs/requirements.txt:12 (update-time: ignore[vulnerable=GHSA-2gwj-7jmv-h26r]) (vulnerable-dependency-ignored)
```

A reference's marker silenced the warning about an advisory: an `ignore[vulnerable=…]` names it, or a `set[vulnerability-level=…]` sets a level above its risk level. See [Silencing specific vulnerabilities](../README.md#silencing-specific-vulnerabilities) and [Setting a risk level](../README.md#setting-a-risk-level).

### workspace-root-outside-scan

```console
DEBUG Not running uv for pyproject.toml: its uv workspace root ../pyproject.toml lies outside the scan root (workspace-root-outside-scan)
```

The workspace root of a project lies outside the scan root, and the package manager would write its lock there, so Update-time does not run it. Update-time still checks the dependencies. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated) and [npm, pnpm, yarn, and bun dependencies › What files are updated?](../README.md#what-files-are-updated-1).

### workspaces-updated-together

```console
DEBUG Updating the uv workspaces of lib/pyproject.toml and app/pyproject.toml together: their projects source each other by path (workspaces-updated-together)
```

Projects source each other by path, so Update-time updates their uv workspaces together, and a pin they share moves to the same version. See [Python dependencies › What versions are updated?](../README.md#what-versions-are-updated).

### yanked-dependency-ignored

```console
DEBUG Ignoring the yank warning for humanize in docs/requirements.txt:12 (update-time: ignore[yanked]) (yanked-dependency-ignored)
```

A reference's `ignore[yanked]` silenced the warning that its version was yanked. See [Silencing a warning](../README.md#silencing-a-warning).

## INFO

### bare-url-not-pinned

```console
INFO Cannot pin clipboard in docs/conf.py:4: the URL is declared as a bare string, so it has no attribute dictionary to hold an integrity hash (bare-url-not-pinned)
```

A Sphinx `conf.py` declares a jsDelivr URL as a bare string, which has nowhere to hold an integrity hash, so Update-time leaves the URL as it is. Declare the URL as a `(url, {"integrity": …})` tuple to have it pinned. See [jsDelivr npm URLs › Pinning](../README.md#pinning-6).

### compiled-file-cooldown-set

```console
INFO Set uv exclude-newer to '7 days' for docs/requirements.txt, and recorded in docs/requirements.in that Update-time manages it (compiled-file-cooldown-set)
```

Update-time passed its cooldown to the tool that recompiles a compiled requirements file. It also wrote a line at the top of the file's first source, saying that Update-time manages that cooldown. Remove that line to set the cooldown yourself. See [Python dependencies › Cooldown](../README.md#cooldown).

### compiled-file-left-as-is

```console
INFO Leaving docs/requirements.txt as it is: Update-time updates the pins in docs/requirements.in, but does not recompile it, as its header records another file as its output (compiled-file-left-as-is)
```

Update-time updated the pins in the sources of a compiled requirements file, but did not recompile the file itself. The line says why. Recompile the file yourself to bring its pins in line with its sources. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### digest-drift-adopted

```console
INFO Adopted digest drift for python:3.14 in Dockerfile:1: re-pinned from sha256:… to sha256:… (update-time: allow[hash-drift]) (digest-drift-adopted)
```

The image tag now serves another digest than the reference pins. The reference's `allow[hash-drift]` marker adopts the drift, so Update-time re-pinned the reference to the new digest. See [Adopting hash drift](../README.md#adopting-hash-drift).

### new-version

```console
INFO New version available for internal-lib in pyproject.toml:2: 1.3.0 (new-version)
```

Update-time updated the reference to a newer version. Below the line, it shows what the dependency's changelog says changed in that version. Where it does not find a changelog, or showed the same changes earlier in the run, a note says so instead. See [What is updated](../README.md#-what-is-updated).

### pin-with-environment-marker-left-as-is

```console
INFO Leaving humanize in pyproject.toml:3 as it is: Update-time does not update a pin with an environment marker (pin-with-environment-marker-left-as-is)
```

A `pyproject.toml` or a script's inline metadata pins a dependency with an environment marker, such as `; python_version < "3.13"`. Update-time does not update such a pin, so update it yourself. See [Python dependencies › What versions are updated?](../README.md#what-versions-are-updated).

### pinned-to-hash

```console
INFO Pinned python in Dockerfile:1 to 3.14.7@sha256:… (pinned-to-hash)
```

Update-time pinned the reference to the version and the hash it resolves to: an image digest, a commit SHA, or an integrity hash. See [Pinning](../README.md#-pinning).

### pinned-to-version

```console
INFO Pinned python in Dockerfile:2 to 3.14.7 (pinned-to-version)
```

Update-time pinned a floating reference to the version it resolves to, without a hash, since the reference has no place for one. A Dockerfile `FROM` that names build arguments is such a reference: a digest would overrule a `--build-arg`. See [Docker images › Pinning](../README.md#pinning-5).

### range-rewritten

```console
INFO npm rewrote the range of minimatch in dev/package.json:5 from ^3.1.2 to ^3.1.5; the version in the lock file is unchanged at 3.1.5 (range-rewritten)
```

The package manager wrote the version it resolves into the range the `package.json` or the catalog declares. The version in the lock file stayed the same. See [npm, pnpm, yarn, and bun dependencies › What versions are updated?](../README.md#what-versions-are-updated-1).

### range-rewritten-without-locked-version

```console
INFO npm rewrote the range of minimatch in dev/package.json:5 from ^3.0.0 to ^3.1.5; the lock file did not record a version before (range-rewritten-without-locked-version)
```

The package manager wrote the version it resolves into the declared range. The lock file did not record a version of the dependency before the run, so the line does not name a locked version. See [npm, pnpm, yarn, and bun dependencies › What versions are updated?](../README.md#what-versions-are-updated-1).

### ref-drift-adopted

```console
INFO Adopted tag drift for actions/checkout@4.1.1 in .github/workflows/ci.yml:17: re-pinned from commit … to … (update-time: allow[hash-drift]) (ref-drift-adopted)
```

The tag, branch, or ref a GitHub Action or pre-commit hook names now points at another commit than the reference pins. The reference's `allow[hash-drift]` marker adopts the drift, so Update-time re-pinned the reference to the new commit. See [Adopting hash drift](../README.md#adopting-hash-drift).

### uv-cooldown-set

```console
INFO Set uv exclude-newer to '7 days' in pyproject.toml to apply the cooldown (uv-cooldown-set)
```

Update-time wrote its cooldown into the `[tool.uv]` table of the workspace root's `pyproject.toml`, as an `exclude-newer` tagged `managed by Update-time`. Remove the tag to take ownership of the setting. See [Python dependencies › Cooldown](../README.md#cooldown).

### version-moved-back

```console
INFO uv moved humanize in docs/requirements.in:2 back from 4.16.0 to 4.11.0 (version-moved-back)
```

A lock file or a compiled requirements file held a version younger than the cooldown, and the package manager moved it back to the newest version older than the cooldown. See [Cooldown](../README.md#-cooldown).

## WARNING

### affected-versions-unreadable

```console
WARNING Could not read the affected versions of advisory GHSA-1111-2222-3333 (Invalid version: 'unknown'), so the vulnerability check passes over that part of the advisory (affected-versions-unreadable)
```

A GitHub Actions advisory states affected versions Update-time cannot read, such as `unknown`. The vulnerability check passes over that part of the advisory, so it may miss a version the advisory names. See [GitHub Actions and pre-commit hooks › Vulnerable dependencies](../README.md#vulnerable-dependencies-3).

### archived-dependency

```console
WARNING Archived dependency aioredis in docs/requirements.txt:12: the project was archived (archived-dependency)
```

The dependency's source declares it archived: its maintainer does not expect to publish another release. Update-time still updates it to its newest release. To keep using it without the warning, mark it `ignore[archived]`. See [Archived dependencies](../README.md#️-archived-dependencies).

### archived-flag-missing

```console
WARNING Could not read whether GitHub archived actions/checkout: its repository endpoint answered without the archived flag (archived-flag-missing)
```

GitHub answered about a repository without saying whether it is archived, so Update-time does not check the dependency for archival.

### binary-bun-lock

```console
WARNING Not running bun for package.json: it locks its dependencies in the binary bun.lockb, which Update-time cannot read; run bun install --save-text-lockfile to convert it to bun.lock (binary-bun-lock)
```

The project locks its dependencies in the binary `bun.lockb` bun wrote before version 1.2, which Update-time cannot read, so it does not run bun. Update-time still checks the dependencies. Convert the lock with the command the line names. See [npm, pnpm, yarn, and bun dependencies › What files are updated?](../README.md#what-files-are-updated-1).

### bun-lock-unreadable

```console
WARNING Could not read bun.lock to find the versions it locks (it does not hold the JSON bun writes) (bun-lock-unreadable)
```

Update-time could not read a `bun.lock`, so it does not report the versions bun moved in it. The line names the error. See [npm, pnpm, yarn, and bun dependencies › What versions are updated?](../README.md#what-versions-are-updated-1).

### bunfig-unreadable

```console
WARNING Could not read bunfig.toml to find whether it sets a cooldown for bun (Expected ']' at the end of a table declaration (at line 1, column 9)) (bunfig-unreadable)
```

Update-time could not read a `bunfig.toml`, so it does not take the file to set a cooldown, and passes bun its own. The line names the error. See [npm, pnpm, yarn, and bun dependencies › Cooldown](../README.md#cooldown-1).

### catalogs-unreadable

```console
WARNING Could not read pnpm-workspace.yaml to find the catalogs it holds ('utf-8' codec can't decode byte 0xff in position 0: invalid start byte) (catalogs-unreadable)
```

Update-time could not read a `pnpm-workspace.yaml` or a `.yarnrc.yml`, so it does not check the entries of the catalogs the file holds. The line names the error. See [npm, pnpm, yarn, and bun dependencies › What files are updated?](../README.md#what-files-are-updated-1).

### command-stderr

```console
WARNING pnpm update -r wrote to stderr: (command-stderr)
 WARN  deprecated inflight@1.0.6
```

A command Update-time ran wrote to stderr. The lines below the message hold what it wrote. The command decides whether that is an error, a warning, or a notice.

### compiled-source-unsupported

```console
WARNING Skipping requirements-dev.txt: its source requirements.txt is itself compiled, which Update-time does not support (compiled-source-unsupported)
```

A source that a compiled requirements file names is itself compiled, which Update-time does not support, so it skips the compiled file. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### cvss-vector-unreadable

```console
WARNING Could not score the CVSS vector of advisory GHSA-1111-2222-3333 (unknown metric 'XX'), so its risk level is unknown (cvss-vector-unreadable)
```

Update-time could not score an advisory's CVSS vector, so it reports the vulnerability at unknown severity, unless another advisory of the same vulnerability gives it a risk level. It warns about such a vulnerability at every `--vulnerability-level`. See [Vulnerable dependencies](../README.md#️-vulnerable-dependencies).

### declaration-not-found

```console
WARNING Could not find where package.json declares node, leaving it unchanged (declaration-not-found)
```

A `package.json` declares a Node engine, but Update-time does not find the line declaring it, so it leaves the engine as it is. A file declaring its `engines` section twice causes this. See [Node engine version and Python version › What dependencies are updated?](../README.md#what-dependencies-are-updated-4).

### digest-drift

```console
WARNING Digest drift for python:3.14 in Dockerfile:1: pinned to sha256:… but the registry now serves sha256:…; the pin was left unchanged, verify the change is expected before updating the pin (digest-drift)
```

The image tag now serves another digest than the reference pins, so the image was re-pushed under the same tag. Update-time leaves the pin as it is. Verify the change is expected before you update the pin, or mark the reference `allow[hash-drift]` to adopt drift. See [Hash drift](../README.md#hash-drift).

### effective-pom-unreadable

```console
WARNING Could not read the effective pom of pom.xml, so the cooldown and the checks passed over any group or artifact the pom leaves to its parent, and the vulnerability check passed over any version the pom leaves to another pom or to Maven; perhaps a --quiet in the project's Maven config hid where Maven wrote it (effective-pom-unreadable)
```

Update-time could not read the effective pom Maven wrote for a pom. So the cooldown and the checks pass over what the pom leaves to its parent pom or to Maven. The line says what may cause this.

### effective-pom-without-input-locations

```console
WARNING The effective pom of pom.xml lacks the input locations Update-time reads, so the cooldown and the checks passed over any group or artifact the pom leaves to its parent, and the vulnerability check passed over any version the pom leaves to another pom or to Maven; the project's help plugin configuration sets verbose to false (effective-pom-without-input-locations)
```

The effective pom Maven wrote for a pom lacks the input locations Update-time reads. So the cooldown and the checks pass over what the pom leaves to its parent pom or to Maven. The line names the configuration that causes this.

### excluded-path-not-found

```console
WARNING Path vendor passed to --exclude-path does not exist (excluded-path-not-found)
```

A directory passed to `--exclude-path` does not exist, so it excludes nothing. The option matches a directory by its path relative to the scan root, so check that path. See [Getting started](../README.md#getting-started).

### forced-outside-git-repository

```console
WARNING Running outside a git repository (/home/user/project) because --force was given; changes are made in place and cannot be reverted (forced-outside-git-repository)
```

`--force` made Update-time run outside a git repository, so there is no commit to revert its changes to. See [Workflow](../README.md#workflow).

### http-error

```console
WARNING Could not fetch https://pypi.org/pypi/humanize/json: HTTP 503 Service Unavailable (http-error)
```

A source answered a request with an HTTP error, so Update-time goes on without the answer. The line names the URL and the status.

### incompatible-python-version

```console
WARNING Incompatible Python version python in .python-version:1: version 3.13.2 falls outside the requires-python >=3.10,<3.12 of pyproject.toml:2, so uv refuses to run for the project (incompatible-python-version)
```

A `.python-version` entry names a version outside the project's `requires-python`, so uv refuses to run for the project. Update-time never moves an entry to a lower version, so correct the entry or the `requires-python` yourself. See [Node engine version and Python version › What versions are updated?](../README.md#what-versions-are-updated-4).

### integrity-hash-mismatch

```console
WARNING Integrity hash mismatch for clipboard@2.0.11 in docs/conf.py:4: declares sha256-… but jsDelivr serves sha256-…; the hash was left unchanged, and since npm does not republish a version it is probably the declared hash that is wrong (integrity-hash-mismatch)
```

A jsDelivr URL declares another integrity hash than jsDelivr serves for its version. npm does not republish a version, so the declared hash is probably wrong. The browser refuses to load the file until the hash matches, so correct it yourself. See [Hash drift](../README.md#hash-drift).

### integrity-hash-not-resolved

```console
WARNING Could not resolve the integrity hash for clipboard 2.0.12 (dist/clipboard.min.js), leaving it unchanged (integrity-hash-not-resolved)
```

Update-time could not get the integrity hash of the file a jsDelivr URL points at, in a newer version. A new version without its hash would fail the browser's check, so the URL stays on its version. See [jsDelivr npm URLs › What versions are updated?](../README.md#what-versions-are-updated-6).

### invalid-file

```console
WARNING Skipping .github/workflows/ci.yml: it is not valid YAML (invalid-file)
```

A file does not parse as the format the line names, such as YAML or TOML, so Update-time skips it.

### invalid-json

```console
WARNING Could not read https://pypi.org/pypi/humanize/json: it is not valid JSON (invalid-json)
```

A source answered a request with a body that is not JSON, so Update-time goes on without the answer.

### invalid-marker-item

```console
WARNING Invalid 'stlae' in the update-time marker for python in Dockerfile:2 (invalid-marker-item)
```

Update-time cannot read an item of a reference's marker, such as a mistyped scope. It drops that item and applies the others, so an item meant to hold the update back lets the update through. See [Invalid markers](../README.md#invalid-markers).

### invalid-pom

```console
WARNING Could not read the pom at https://repo1.maven.org/maven2/com/google/guava/guava/33.5.0-jre/guava-33.5.0-jre.pom: it is not valid XML (invalid-pom)
```

Maven Central served a pom that is not valid XML, so Update-time cannot read from it the GitHub repository of the dependency, and does not check the dependency for archival. See [Maven dependencies › Archived dependencies](../README.md#archived-dependencies-2).

### node-engine-unreadable

```console
WARNING Could not read package.json to find the Node engine it declares ('utf-8' codec can't decode byte 0xff in position 0: invalid start byte) (node-engine-unreadable)
```

Update-time could not read a `package.json`, so it does not find the Node engine the file may declare. The line names the error.

### package-json-unreadable

```console
WARNING Could not read package.json to find the lines declaring its dependencies ('utf-8' codec can't decode byte 0xff in position 0: invalid start byte) (package-json-unreadable)
```

Update-time could not read a `package.json`, so it skips the file. The line names the error.

### package-manager-declaration-unreadable

```console
WARNING Could not read package.json to find the package manager it names ('utf-8' codec can't decode byte 0xff in position 0: invalid start byte) (package-manager-declaration-unreadable)
```

Update-time could not read a `package.json`, so it does not know which package manager the file's `packageManager` field may name. The line names the error. See [npm, pnpm, yarn, and bun dependencies › What files are updated?](../README.md#what-files-are-updated-1).

### package-manager-unsupported

```console
WARNING Skipping pyproject.toml: poetry is not supported, only uv (package-manager-unsupported)
```

A package manager Update-time does not support manages a file, so Update-time skips the file. The line names the package manager, and the ones Update-time supports.

### pinned-version-unreadable

```console
WARNING Could not read '4.15.0-final', the version docs/requirements.txt pins humanize to; not upgrading it (pinned-version-unreadable)
```

A compiled requirements file pins a package to a version that is not PEP 440. Update-time cannot judge a bound against it, so it does not upgrade the package when it recompiles the file. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### publication-date-unreadable

```console
WARNING Could not read the publication date 'yesterday' that Maven Central lists for com.google.guava:guava 33.5.0-jre, so the cooldown holds this version back (publication-date-unreadable)
```

Maven Central lists a publication date for a version that Update-time cannot read. Update-time cannot tell whether the version is older than the cooldown, so the cooldown holds it back. See [Maven dependencies › Cooldown](../README.md#cooldown-2).

### redundant-bound

```console
WARNING Redundant update-time directive allow[update>=3.12] for python in Dockerfile:2: this bound never has an effect at version 3.12 (redundant-bound)
```

A bound in a reference's marker never has an effect at the reference's version, or blocks every update. Remove the bound to silence this warning, or write `ignore[update]` where you mean to hold every update back. See [A bound](../README.md#a-bound).

### redundant-directive

```console
WARNING Redundant update-time directive ignore[yanked] for python in Dockerfile:2: this dependency's source does not record a yank (redundant-directive)
```

A directive in a reference's marker decides nothing for that reference. The line says why. Remove the directive to silence this warning. See [Redundant markers](../README.md#redundant-markers).

### redundant-ignore-archived

```console
WARNING Redundant update-time directive ignore[archived] for humanize in docs/requirements.txt:12: the project is not archived (redundant-ignore-archived)
```

An `ignore[archived]` silences nothing, since the source does not archive the dependency. The directive may have outlived what it was written for. Remove the directive to silence this warning. See [The warning scopes](../README.md#the-warning-scopes).

### redundant-ignore-vulnerable

```console
WARNING Redundant update-time directive ignore[vulnerable=CVE-2022-28346] for django in docs/requirements.txt:12: version 4.2.0 has no such vulnerability (redundant-ignore-vulnerable)
```

An `ignore[vulnerable=…]` names an advisory, but the version does not have a vulnerability that answers to it. An update fixed the vulnerability, or the identifier is mistyped. Remove the directive to silence this warning. See [The vulnerable scope](../README.md#the-vulnerable-scope).

### redundant-ignore-yanked

```console
WARNING Redundant update-time directive ignore[yanked] for humanize in docs/requirements.txt:12: version 4.15.0 was not yanked (redundant-ignore-yanked)
```

An `ignore[yanked]` silences nothing, since the reference pins a version that was not yanked. The directive may have outlived what it was written for. Remove the directive to silence this warning. See [The warning scopes](../README.md#the-warning-scopes).

### redundant-stale-after-as-default

```console
WARNING Redundant update-time directive set[stale-after=90] for humanize in docs/requirements.txt:12: newest release 4.15.0 was published 30 days ago, which the threshold of 90 days and the default of 365 days judge alike (redundant-stale-after-as-default)
```

A `set[stale-after=…]` and the default threshold judge the newest release alike: both warn about it, or neither does. Remove the directive to silence this warning, or set a threshold that judges otherwise. See [The warning scopes](../README.md#the-warning-scopes).

### redundant-stale-after-as-option

```console
WARNING Redundant update-time directive set[stale-after=90] for humanize in docs/requirements.txt:12: newest release 4.15.0 was published 30 days ago, which the threshold of 90 days and --stale-after 180 judge alike (redundant-stale-after-as-option)
```

A `set[stale-after=…]` sets a threshold of its own, and `--stale-after` sets the run's. Both thresholds judge the newest release alike: both warn about it, or neither does. Remove the directive to silence this warning, or set a threshold that judges otherwise. See [The warning scopes](../README.md#the-warning-scopes).

### redundant-vulnerability-level-as-default

```console
WARNING Redundant update-time directive set[vulnerability-level=high] for django in docs/requirements.txt:12: the risk level high and the default level low report the same vulnerabilities in version 4.2.0 (redundant-vulnerability-level-as-default)
```

A `set[vulnerability-level=…]` reports the same vulnerabilities in the version as the default level does. Remove the directive to silence this warning. See [The vulnerable scope](../README.md#the-vulnerable-scope).

### redundant-vulnerability-level-as-option

```console
WARNING Redundant update-time directive set[vulnerability-level=high] for django in docs/requirements.txt:12: the risk level high and --vulnerability-level moderate report the same vulnerabilities in version 4.2.0 (redundant-vulnerability-level-as-option)
```

A `set[vulnerability-level=…]` reports the same vulnerabilities in the version as the run's `--vulnerability-level` does. Remove the directive to silence this warning. See [The vulnerable scope](../README.md#the-vulnerable-scope).

### ref-drift

```console
WARNING Tag drift for actions/checkout@4.1.1 in .github/workflows/ci.yml:17: pinned to commit … but the tag now points at …; the pin was left unchanged, verify the tag was moved deliberately before updating the pin (ref-drift)
```

The tag, branch, or ref a GitHub Action or pre-commit hook names now points at another commit than the reference pins. Update-time leaves the pin as it is. Verify the move before you update the pin, or mark the reference `allow[hash-drift]` to adopt drift. See [Hash drift](../README.md#hash-drift).

### request-failed

```console
WARNING Could not fetch https://pypi.org/pypi/humanize/json: Connection refused (request-failed)
```

A request failed before a source could answer, for example because the connection failed, so Update-time goes on without the answer. The line names the error.

### request-timed-out

```console
WARNING Could not fetch https://pypi.org/pypi/humanize/json: the request timed out (request-timed-out)
```

A source did not answer a request in time, so Update-time goes on without the answer.

### requires-python-ignored

```console
WARNING Ignoring the requires-python of pyproject.toml:2: it is not a version specifier (requires-python-ignored)
```

A project's `requires-python` is not a version specifier, so Update-time updates the `.python-version` entry as if the project declared none. See [Node engine version and Python version › What versions are updated?](../README.md#what-versions-are-updated-4).

### requires-python-unreadable

```console
WARNING Could not read pyproject.toml to find the requires-python it declares ('utf-8' codec can't decode byte 0xff in position 0: invalid start byte) (requires-python-unreadable)
```

Update-time could not read a `pyproject.toml`, so it updates the `.python-version` entry as if the project did not declare a `requires-python`. The line names the error. See [Node engine version and Python version › What versions are updated?](../README.md#what-versions-are-updated-4).

### script-unreadable

```console
WARNING Could not read tool.py to find the inline script metadata it carries ('utf-8' codec can't decode byte 0xff in position 0: invalid start byte) (script-unreadable)
```

Update-time could not read a `*.py` file, so it does not find the inline script metadata the file may carry. It goes on with the other files. The line names the error. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### several-package-managers

```console
WARNING Not running a package manager for test/node/esm/package.json: test/node/esm/pnpm-lock.yaml and test/node/esm/bun.lockb name different ones; set the packageManager field to the one to run (several-package-managers)
```

The files of a directory name more than one package manager, as a `pnpm-lock.yaml` beside a `bun.lockb` does. Update-time cannot tell which one the project uses, so it runs none of them, and still checks the dependencies. Set the `packageManager` field to the one to run. See [npm, pnpm, yarn, and bun dependencies › What files are updated?](../README.md#what-files-are-updated-1).

### source-not-found

```console
WARNING Could not find docs/dev.in, a source docs/requirements.txt names; leaving docs/requirements.txt as it is (source-not-found)
```

A compiled requirements file names a source that does not exist, so Update-time leaves the compiled file as it is. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### stale-dependency

```console
WARNING Stale dependency humanize in docs/requirements.txt:12: newest release 4.15.0 was published 512 days ago (> 365) (stale-dependency)
```

The dependency's newest release is older than the staleness threshold, which may mean the project was abandoned or superseded. Set a threshold of its own with `set[stale-after=DAYS]`, or silence the warning with `set[stale-after=0]`. See [Stale dependencies](../README.md#️-stale-dependencies).

### tool-settings-unreadable

```console
WARNING Could not read from pip's settings whether they set an uploaded-prior-to for docs/requirements.txt ([Errno 2] No such file or directory: 'pip'); applying the cooldown (tool-settings-unreadable)
```

pip or yarn did not report its settings, so Update-time cannot tell whether they set a cooldown, and applies its own. The line gives the reason. See [Python dependencies › Cooldown](../README.md#cooldown).

### tool-version-unknown

```console
WARNING Could not determine the version of yarn in .: Usage Error: Parse error when loading .yarnrc.yml; please check it's proper Yaml (tool-version-unknown)
```

A tool did not report its version in a directory, such as a yarn whose `.yarnrc.yml` does not parse, so Update-time does not run it there. The line gives the tool's reason. See [npm, pnpm, yarn, and bun dependencies › What files are updated?](../README.md#what-files-are-updated-1).

### uv-cooldown-not-set

```console
WARNING Could not set uv exclude-newer in pyproject.toml to apply the cooldown: its tool, or the uv in it, is not a table (uv-cooldown-not-set)
```

The `tool` of a `pyproject.toml`, or the `uv` in it, is a value other than a table, so Update-time cannot write uv's `exclude-newer` there to apply the cooldown. See [Python dependencies › Cooldown](../README.md#cooldown).

### uv-settings-unreadable

```console
WARNING Could not read from uv's settings whether the project sets an exclude-newer for docs/requirements.txt; applying the cooldown (uv-settings-unreadable)
```

uv did not report its settings, so Update-time cannot tell whether the project sets an `exclude-newer`, and applies its own cooldown. See [Python dependencies › Cooldown](../README.md#cooldown).

### vulnerable-dependency

```console
WARNING Vulnerable dependency django in docs/requirements.txt:12: version 3.2.0 has a critical vulnerability, "SQL Injection in Django" (GHSA-2gwj-7jmv-h26r, https://osv.dev/GHSA-2gwj-7jmv-h26r) (vulnerable-dependency)
```

The run leaves the reference on a version that an advisory names as affected. The line names the risk level, what the advisory says, and where to read it. Update the dependency, or silence the advisory with `ignore[vulnerable=ID]` once you have assessed it. See [Vulnerable dependencies](../README.md#️-vulnerable-dependencies).

### yanked-dependency

```console
WARNING Yanked dependency humanize in docs/requirements.txt:12: version 4.15.0 was yanked ("accidentally broke Python 3.10 support") (yanked-dependency)
```

The maintainer withdrew the version the reference pins, and the run leaves the reference on it. The line quotes the maintainer's reason where they gave one. To live with the yank, mark the reference `ignore[yanked]`. See [Yanked dependencies](../README.md#-yanked-dependencies).

### yarn-without-cooldown

```console
WARNING Not running yarn for package.json: the yarn that runs in . is version 1.22.22, which cannot apply a cooldown; run yarn set version stable to switch the project to a yarn that can (yarn-without-cooldown)
```

The yarn that runs at the workspace root is older than 4.10 and cannot apply a cooldown, so Update-time does not run it. Update-time still checks the dependencies. Switch the project with the command the line names. See [npm, pnpm, yarn, and bun dependencies › What files are updated?](../README.md#what-files-are-updated-1).

## ERROR

### branch-drift-not-checked

```console
ERROR Could not check branch drift for actions/checkout@main in .github/workflows/ci.yml:17: GitHub did not compare the pinned commit with …, the branch's newest commit older than the cooldown (HTTP 403, API rate limit exceeded); the pin was left unchanged (branch-drift-not-checked)
```

Update-time could not ask GitHub whether a branch moved on from the commit the reference pins, so it left the pin as it is. The line gives GitHub's reason, such as a used-up rate limit. See [GitHub Actions and pre-commit hooks › Pinning](../README.md#pinning-3).

### command-failed

```console
ERROR yarn up -R --mode=update-lockfile left-pad failed: (command-failed)
➤ YN0000: · Yarn 4.18.1
➤ YN0000: ┌ Resolution step
➤ YN0001: │ RequestError: getaddrinfo ENOTFOUND registry.yarnpkg.com
➤ YN0000: └ Completed
➤ YN0000: · Failed with errors in 0s 75ms
```

A command Update-time ran exited with an error. The lines below the message hold what the command wrote, which says what went wrong.

### command-not-found

```console
ERROR Could not run pnpm update -r: is pnpm installed? (command-not-found)
```

Update-time could not run a command, because the command's executable is not on the path. Install the tool, or put it on the path, and run Update-time again.

### command-not-run

```console
ERROR Could not run npm update: [Errno 13] Permission denied: 'npm' (command-not-run)
```

The operating system refused to run a command for another reason than a missing executable, such as a missing permission. The line names the error the system reported.

### commit-sha-not-fetched

```console
ERROR Could not fetch commit SHA for actions/checkout v4.3.0 (HTTP 403, API rate limit exceeded): https://github.com/actions/checkout/releases/tag/v4.3.0 (commit-sha-not-fetched)
```

Update-time could not fetch from GitHub the commit a release's tag points at, so it cannot pin the reference to that release. The line gives the reason and links the release. See [GitHub Actions and pre-commit hooks › Pinning](../README.md#pinning-3).

### compiled-file-not-recompiled

```console
ERROR Could not recompile docs/requirements.txt: uv pip compile failed (compiled-file-not-recompiled)
```

The tool that compiled a requirements file, uv or pip-compile, failed to recompile it. Update-time logs the tool's own error above this line. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### compiler-not-on-path

```console
ERROR Could not recompile docs/requirements.txt: uv is not on the path (compiler-not-on-path)
```

The tool that compiled a requirements file, uv or pip-compile, is not on the path, so Update-time did not recompile the file. Install the tool and run Update-time again. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### declarations-changed

```console
ERROR Could not tell what changed in pom.xml: it declared 12 dependencies and plugins before the update and 11 after it (declarations-changed)
```

Maven rewrote a pom so that it declares more or fewer dependencies and plugins than before. Update-time then cannot pair the declarations up, so it reports nothing for the pom. Review the pom's diff. See [Maven dependencies › What files are updated?](../README.md#what-files-are-updated-2).

### dependencies-not-updated

```console
ERROR Could not update the dependencies in tool.py: uv lock failed (dependencies-not-updated)
```

uv could not lock a `pyproject.toml` or a script, so Update-time left its pins and its lock as they were. Update-time logs uv's error above this line. A pin on a version younger than the cooldown, or a registry uv cannot reach, causes this. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### dependencies-not-updated-together

```console
ERROR Could not update the dependencies in lib/pyproject.toml: they move together with app/pyproject.toml, which uv could not lock (dependencies-not-updated-together)
```

The project sources another project by path, or that project sources it, so uv locks them together. uv could not lock the other project, so Update-time gave both back the text they held before the run. The error about the other project says why uv could not lock it. See [Python dependencies › What versions are updated?](../README.md#what-versions-are-updated).

### drift-commit-date-unknown

```console
ERROR Could not determine from GitHub the date of commit … that actions/checkout@4.1.1 in .github/workflows/ci.yml:17 now points at (HTTP 403, API rate limit exceeded), so the cooldown can't be verified; not adopting the drift (drift-commit-date-unknown)
```

A GitHub Action or pre-commit hook names a tag or branch that now points at another commit. GitHub did not say when that commit was made. Update-time cannot check the cooldown, so it does not adopt the drift. See [GitHub Actions and pre-commit hooks › Cooldown](../README.md#cooldown-3).

### drift-push-date-unknown

```console
ERROR Could not determine from Docker Hub when python:3.14 in Dockerfile:2 was pushed (HTTP 429), so the cooldown can't be verified; not adopting the drift (drift-push-date-unknown)
```

The image tag now serves another digest than the reference pins, but Docker Hub did not say when it was pushed. Update-time cannot check the cooldown, so it does not adopt the drift. See [Docker images › Cooldown](../README.md#cooldown-5).

### file-not-restored

```console
ERROR Could not give uv.lock back the text it held before the run ([Errno 13] Permission denied: 'uv.lock') (file-not-restored)
```

After an update failed, Update-time could not give a file it rewrote back the text the file held before the run. The line names the error. Revert the file yourself. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### file-not-written

```console
ERROR Could not write pyproject.toml ([Errno 13] Permission denied: 'pyproject.toml'), so the dependencies uv locks together with it are not updated (file-not-written)
```

Update-time could not write a `pyproject.toml` or a script, so it left the dependencies uv locks together with it as they were. The line names the error, such as a missing permission. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### github-rate-limit-used-up

```console
ERROR GitHub's API rate limit of 60 requests is used up until 2026-10-17 15:42 UTC; set GITHUB_TOKEN to raise it (github-rate-limit-used-up)
```

The run used up the rate limit GitHub gives a client without a token, so Update-time cannot ask GitHub anything until the time the line names. Set `GITHUB_TOKEN` to raise the limit. See [Increasing rate limits](../README.md#increasing-rate-limits).

### github-token-rate-limit-used-up

```console
ERROR GitHub's API rate limit of 5000 requests for GITHUB_TOKEN is used up until 2026-10-17 15:42 UTC (github-token-rate-limit-used-up)
```

The run used up the rate limit GitHub gives `GITHUB_TOKEN`, so Update-time cannot ask GitHub anything until the time the line names. See [Increasing rate limits](../README.md#increasing-rate-limits).

### invalid-xml-after-update

```console
ERROR Could not read pom.xml after updating it: it is not valid XML (invalid-xml-after-update)
```

Maven rewrote a pom into XML that does not parse, so Update-time cannot read what changed. Review the pom's diff, and revert the pom where Maven broke it. See [Maven dependencies › What files are updated?](../README.md#what-files-are-updated-2).

### lock-not-read

```console
ERROR Could not read uv.lock ([Errno 13] Permission denied: 'uv.lock'), so the dependencies it locks are not updated (lock-not-read)
```

Update-time could not read the `uv.lock` of a workspace, or the lock of a script, so it left the workspace or the script as it was. The line names the error. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### lock-not-removed

```console
ERROR Could not remove tool.py.lock, which the run created ([Errno 13] Permission denied: 'tool.py.lock') (lock-not-removed)
```

The run created a lock for a script, or for a `pyproject.toml` without a `uv.lock`, and Update-time could not remove that lock afterwards. The line names the error. Remove the lock yourself. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated).

### package-manager-not-on-path

```console
ERROR Could not update the dependencies in pyproject.toml: uv is not on the path (package-manager-not-on-path)
```

The package manager that updates a file's dependencies, such as uv, npm, or Maven, is not on the path, so Update-time did not run it. Update-time still checks the dependencies. Install the package manager and run Update-time again. See [Python dependencies › What files are updated?](../README.md#what-files-are-updated), [npm, pnpm, yarn, and bun dependencies › What files are updated?](../README.md#what-files-are-updated-1), and [Maven dependencies › What files are updated?](../README.md#what-files-are-updated-2).

### reactor-pom-unparsable

```console
ERROR Update-time cannot read the reactor in . since sub/pom.xml does not parse, so Update-time does not run Maven over its 118 other poms (reactor-pom-unparsable)
```

A pom of a Maven reactor does not parse, so Maven cannot read the reactor. Update-time does not run Maven over the reactor's other poms, but still checks them. Fix the pom the line names. See [Maven dependencies › What files are updated?](../README.md#what-files-are-updated-2).

### reactor-unreadable

```console
ERROR Maven cannot read the reactor in ., so Update-time does not run Maven over the 50 poms left in it (reactor-unreadable)
```

Maven can no longer read a reactor it read earlier in the run, although the latest Maven run that read the reactor changed none of its poms. Maven's own error says why it cannot read the reactor. Update-time does not run Maven over the poms left in the reactor, but still checks them. See [Maven dependencies › What files are updated?](../README.md#what-files-are-updated-2).

### reactor-unreadable-as-found

```console
ERROR Maven cannot read the reactor in . as the scan found it, so Update-time does not run Maven over its 118 other poms (reactor-unreadable-as-found)
```

Maven cannot read a reactor before Update-time changed any of its poms, so the project's poms break it as they are. Maven's own error says why it cannot read the reactor. Update-time does not run Maven over the reactor's other poms, but still checks them. See [Maven dependencies › What files are updated?](../README.md#what-files-are-updated-2).

### reactor-unreadable-since-last-update

```console
ERROR Maven cannot read the reactor in . since Update-time updated dubbo-dependencies-bom/pom.xml; revert that pom and hold the update that broke the reactor back with a marker, or fix what Maven reports (reactor-unreadable-since-last-update)
```

Maven cannot read a reactor since Update-time updated the pom the line names. Maven found this in its run over the reactor's last pom, so the line does not count the poms left. Revert that pom and hold the update that broke the reactor back with a marker, or fix what Maven reports. See [Maven dependencies › What files are updated?](../README.md#what-files-are-updated-2).

### reactor-unreadable-since-update

```console
ERROR Maven cannot read the reactor in . since Update-time updated dubbo-dependencies-bom/pom.xml, so Update-time does not run Maven over the 50 poms left in it; revert that pom and hold the update that broke the reactor back with a marker, or fix what Maven reports (reactor-unreadable-since-update)
```

Maven cannot read a reactor since Update-time updated the pom the line names, so Update-time does not run Maven over the poms left in it. Revert that pom and hold the update that broke the reactor back with a marker, or fix what Maven reports. See [Maven dependencies › What files are updated?](../README.md#what-files-are-updated-2).

### ref-commit-not-fetched

```console
ERROR Could not fetch the commit of the ref actions/checkout@main in .github/workflows/ci.yml:17, so it was left as it is (HTTP 403, API rate limit exceeded) (ref-commit-not-fetched)
```

Update-time could not fetch the commit a branch, a tag such as `stable-2024`, or a short commit SHA points at, so it left the reference as it is. The line gives the reason, such as a used-up rate limit. For a pinned branch, the reason can also be that the branch's newest commit older than the cooldown lies beyond the commits Update-time examines. See [GitHub Actions and pre-commit hooks › Pinning](../README.md#pinning-3).

### release-metadata-not-fetched

```console
ERROR Could not fetch from PyPI the metadata of humanize 4.16.0 (HTTP 503), so its yank state and publication date are unknown; humanize stays on 4.15.0 rather than trying the versions in between (release-metadata-not-fetched)
```

PyPI did not serve the metadata of a newer release, so Update-time cannot tell whether it was yanked, or when it was published. The pin stays on its version, rather than moving to a version in between. See [Python dependencies › What versions are updated?](../README.md#what-versions-are-updated).

### run-stopped

```console
ERROR SIGINT (Ctrl-C) stopped Update-time before it finished; review the files it changed, since a file it was writing may be incomplete (run-stopped)
```

A signal, such as Ctrl-C, stopped the run. A file Update-time was writing may be incomplete, so review the changes, or revert them and run Update-time again. See [Workflow](../README.md#workflow).

### tag-push-date-unknown

```console
ERROR Could not determine from Docker Hub when python:3.15.0 was pushed (HTTP 429), so the cooldown can't be verified; python stays on 3.14.6 rather than trying the tags in between (tag-push-date-unknown)
```

Docker Hub did not say when a newer image tag was pushed, so Update-time cannot check the cooldown. The reference stays on its tag, rather than moving to a tag in between. See [Docker images › Cooldown](../README.md#cooldown-5).

### update-stopped

```console
ERROR Could not finish updating Maven dependencies: SIGTERM stopped it (update-stopped)
```

A signal stopped the updater of one dependency type before it finished, so that type's update may be partly done. Review its changes, or revert them and run Update-time again. See [Workflow](../README.md#workflow).

### version-commit-date-unknown

```console
ERROR Could not determine from GitHub when actions/checkout v4.4.0 was committed (HTTP 403, API rate limit exceeded), so the cooldown can't be verified; actions/checkout stays on 4.3.0 rather than trying the versions in between (version-commit-date-unknown)
```

GitHub did not say when the commit of a newer version was made, so Update-time cannot check the cooldown. The reference stays on its version, rather than moving to a version in between. See [GitHub Actions and pre-commit hooks › Cooldown](../README.md#cooldown-3).
