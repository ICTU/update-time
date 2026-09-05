# Update-time - it's time to update your dependencies

[![PyPI](https://img.shields.io/pypi/v/update-time?logo=pypi&logoColor=white)](https://pypi.org/project/update-time/) [![Python versions](https://img.shields.io/pypi/pyversions/update-time?logo=python&logoColor=white)](https://pypi.org/project/update-time/) [![License](https://img.shields.io/pypi/l/update-time)](https://github.com/ICTU/update-time/blob/main/LICENSE)

Keeping dependencies up-to-date is an important aspect of software maintenance. Update-time is a command line tool that scans your repository for [dependencies](#-what-is-updated) and updates them to their latest versions. Where possible, it [pins](#-pinning) references — no more `latest` — and adds hashes. To protect against supply chain attacks, it applies a [cooldown](#-cooldown) period. And it warns you about [stale dependencies](#-stale-dependencies), [yanked versions](#-yanked-dependencies), [vulnerable dependencies](#-vulnerable-dependencies), and [archived dependencies](#-archived-dependencies).

Update-time rewrites the files in place and logs what it did:

![Update-time's colour-coded log output](https://raw.githubusercontent.com/ICTU/update-time/main/docs/log-output.svg)

<details>
<summary>The same output as text</summary>

```console
[09:14:03] INFO     New version available for humanize in docs/requirements.txt:12: 4.15.0
                    Changed in 4.15.0
                    - Fantastic new features
                    - A few bugs squashed
           INFO     Pinned python in Dockerfile:1 to
                    3.14.6@sha256:9f2c1e7bd4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4
           INFO     New version available for actions/checkout in .github/workflows/ci.yml:17: 4.3.0
                    No changelog available!
           WARNING  Stale dependency left-pad in package.json:24: newest release 1.3.0 was published
                    512 days ago (> 365)
           WARNING  Vulnerable dependency django in docs/requirements.txt:12: version 3.2.0 has a
                    critical vulnerability, "SQL Injection in Django" (GHSA-2gwj-7jmv-h26r,
                    https://osv.dev/GHSA-2gwj-7jmv-h26r)
```

</details>

## ☰ Table of contents

- [⚡ Usage](#-usage)
  - [Getting started](#getting-started)
  - [Workflow](#workflow)
  - [Increasing rate limits](#increasing-rate-limits)
- [🔄 Updating](#-updating)
  - [📦 What is updated](#-what-is-updated)
  - [📌 Pinning](#-pinning)
  - [⏳ Cooldown](#-cooldown)
- [⚠️ Warnings](#-warnings)
  - [🕸️ Stale dependencies](#-stale-dependencies)
  - [🚫 Yanked dependencies](#-yanked-dependencies)
  - [🛡️ Vulnerable dependencies](#-vulnerable-dependencies)
  - [🗄️ Archived dependencies](#-archived-dependencies)
- [🎛️ Controlling updates and warnings per reference](#-controlling-updates-and-warnings-per-reference)
  - [The anatomy of a marker](#the-anatomy-of-a-marker)
  - [Holding a reference back](#holding-a-reference-back)
  - [Adopting hash drift](#adopting-hash-drift)
  - [Keeping a tag floating](#keeping-a-tag-floating)
  - [Bounding an update](#bounding-an-update)
  - [Writing a marker](#writing-a-marker)
- [📖 Details per dependency type](#-details-per-dependency-type)
  - [Python dependencies](#python-dependencies)
  - [npm and pnpm dependencies](#npm-and-pnpm-dependencies)
  - [GitHub Actions and pre-commit hooks](#github-actions-and-pre-commit-hooks)
  - [Node engine version and Python version](#node-engine-version-and-python-version)
  - [Docker images](#docker-images)
  - [jsDelivr npm URLs](#jsdelivr-npm-urls)
- [📮 Point of contact](#-point-of-contact)

## ⚡ Usage

### Getting started

Run Update-time without installing it using [uvx](https://docs.astral.sh/uv/):

```console
uvx update-time
```

Or install it as a [uv tool](https://docs.astral.sh/uv/concepts/tools/) so it's always available on your `PATH`:

```console
uv tool install update-time
update-time
```

<details>
<summary>Running <code>update-time -h</code> shows the full command-line interface</summary>

```console
$ update-time -h
usage: update-time [-h] [-V] [--cooldown DAYS] [--stale-after DAYS]
                   [--vulnerability-level {low,moderate,high,critical,none}]
                   [--ignore-vulnerability IDS] [--ignore-archived]
                   [--exclude-path PATHS] [--allow-hash-drift]
                   [--allow-floating-pin] [--force]
                   [--log-level {DEBUG,INFO,WARNING,ERROR}]
                   [PATH]

Scan the PATH for pinned dependencies and update them to their latest
versions, rewriting the pinned versions in place. Looks at pyproject.toml,
requirements.txt, PEP 723 inline script metadata, package.json, .python-
version, Dockerfiles, CircleCI configs, .gitlab-ci.yml, Docker Compose files,
Helm charts, devcontainer configs, YAML files under .github/, .pre-commit-
config.yaml, and Sphinx config. A cooldown period holds back releases that are
too fresh to trust.

positional arguments:
  PATH                  the directory to scan recursively for dependencies to
                        update; paths in the log are reported relative to it
                        (default: the current directory)

options:
  -h, --help            show this help message and exit
  -V, --version         show program's version number and exit
  --cooldown DAYS       number of days to hold back a newly published version,
                        except for references that set a cooldown of their own
                        with an # update-time: ignore[cooldown<DAYS] marker
                        (default: 7)
  --stale-after DAYS    warn when a dependency's newest release is older than
                        this many days; 0 disables the check, except for
                        references that set a threshold of their own with an #
                        update-time: ignore[stale<DAYS] marker (default: 365)
  --vulnerability-level {low,moderate,high,critical,none}
                        warn about a known vulnerability in the version a
                        dependency is pinned to when the advisory's risk level
                        is at least this severe; a vulnerability whose risk
                        level cannot be read is always warned about. Pass none
                        to switch the check off, which queries the advisory
                        database not at all, except for references that set a
                        level of their own with an # update-time:
                        ignore[vulnerable<LEVEL] marker (default: low)
  --ignore-vulnerability IDS
                        comma-separated list of advisories to never warn
                        about, wherever in the scan they turn up, for example
                        GHSA-2gwj-7jmv-h26r,CVE-2021-31542. An advisory can be
                        named by any of the identifiers it is known by. To
                        silence one for a single reference instead, mark that
                        reference with an # update-time: ignore[vulnerable=ID]
                        marker
  --ignore-archived     switch the archival check off, so no dependency is
                        warned about as archived. To silence the warning for a
                        single reference instead, mark that reference with an
                        # update-time: ignore[archived] marker
  --exclude-path PATHS  comma-separated list of directories, relative to the
                        scan root, to exclude from the scan, for example
                        vendor,packages/legacy. Every file under an excluded
                        directory is skipped, on top of the always-ignored
                        build, node_modules, __pycache__, and hidden folders.
                        Directories are matched by relative path, not by name:
                        --exclude-path vendor excludes vendor/ at the root but
                        not sub/vendor/. Directories that don't exist are
                        ignored and logged at log-level WARNING. Absolute
                        paths, or paths that escape the scan root (../…), are
                        rejected. Run with --log-level DEBUG to see excluded
                        directories
  --allow-hash-drift    when an already-pinned image tag has been re-pushed,
                        or a pinned version tag has been moved to another
                        commit, adopt the new digest or commit instead of only
                        warning; equivalent to marking every reference with #
                        update-time: allow[hash-drift] (an # update-time:
                        ignore marker still wins)
  --allow-floating-pin  keep every floating image tag in the scan as it is,
                        instead of pinning it to the version and digest it
                        currently serves; equivalent to marking every
                        reference with # update-time: allow[floating-pin] (an
                        # update-time: ignore[floating-pin] marker still pins
                        that reference)
  --force               run even when not inside a git repository (changes are
                        made in place and cannot be reverted)
  --log-level {DEBUG,INFO,WARNING,ERROR}
                        the minimum severity of messages to log; available new
                        versions are logged at INFO (default: INFO)

Update-time exits with status 0 when it ran successfully, 1 when an error
prevented it from finishing, and 2 when any command-line argument was invalid,
including a PATH that is not inside a git repository (unless --force is
passed). Exit status does not indicate whether anything was updated. Inspect
the diff or the INFO-level log for that.
```

</details>

Update-time logs at four levels. `--log-level` sets the lowest one shown, which by default is `INFO`.

| | Level | What is logged |
| :-: | :---- | :------------- |
| 🔍 | `DEBUG` | what Update-time is doing: each file it checks, each directory `--exclude-path` skips, each [marker](#-controlling-updates-and-warnings-per-reference) it recognises, and everything a marker or `--ignore-vulnerability` held back |
| ℹ️ | `INFO` | what Update-time changed: a version updated, a [hash pinned](#-pinning) |
| ⚠️ | `WARNING` | what needs your attention: a [stale](#-stale-dependencies), [yanked](#-yanked-dependencies), [vulnerable](#-vulnerable-dependencies), or [archived](#-archived-dependencies) dependency, [hash drift](#hash-drift), a source it could not reach, a marker that is invalid, incorrect, or redundant |
| ❌ | `ERROR` | failures that stop an update, such as a package manager that is not installed |

### Workflow

The recommended workflow is to run Update-time on a dedicated branch, push it, and let CI do the verification:

1. Create a branch for the updates.
2. Run `update-time` in the root of your repository to update the dependencies in place.
3. Commit the changes and open a pull request.
4. Let your tests and checks run in CI to confirm nothing is broken before merging.

Update-time rewrites files in place, so it expects to run inside a git repository, where you can revert its changes. By default, Update-time refuses to run when the directory to scan sits outside a git repository. It then prints an error, exits with a non-zero status, and touches no files. Pass `--force` to override.

> [!NOTE]
> Being inside a repository only guarantees revertability relative to the last commit. Update-time does not check for uncommitted edits before running.

### Increasing rate limits

To raise API rate limits while updating, set the following environment variables before running Update-time:

- `GITHUB_TOKEN` — increases the GitHub API rate limit when updating GitHub Actions. The token only needs to read public release and commit data, so it needs no specific scope. A classic token with no scopes selected works. So does a fine-grained token with default read-only access to public repositories.
- `DOCKER_HUB_USERNAME` and `DOCKER_HUB_TOKEN` — authenticate to the Docker Hub API (both must be set) to increase its rate limit when updating Docker images.

## 🔄 Updating

Update-time scans your repository for version pins. It moves each pin to the newest version its source offers, once that version is past the cooldown. It also adds a hash pin wherever the reference can hold one.

### 📦 What is updated

Update-time updates the following types of dependencies, found in the listed files, and using the listed sources. Where a package manager manages the dependencies, Update-time works through that package manager. Update-time updates the other dependency types itself:

| Dependency type | Files | Source | Updated by |
| :-------------- | :---- | :----- | :--------- |
| [Python dependencies](#python-dependencies) | `pyproject.toml`, `requirements.txt`, and PEP 723 inline script metadata (`# /// script` blocks in `*.py` files) | [PyPI](https://pypi.org) | Update-time for `requirements.txt`. For the other two, uv resolves the versions and Update-time writes the pins |
| [npm and pnpm dependencies](#npm-and-pnpm-dependencies) | `package.json` (and their lock files) | [npm registry](https://registry.npmjs.org) | delegated to npm or pnpm, whichever manages the `package.json` |
| [Node engine version](#node-engine-version-and-python-version) | `package.json` | the Node base image in the project's Dockerfile, or the latest [Node](https://hub.docker.com/_/node) release on Docker Hub | Update-time |
| [Python version](#node-engine-version-and-python-version) | `.python-version` | the Python base image in the project's Dockerfile, or the latest [Python](https://hub.docker.com/_/python) release on Docker Hub | Update-time |
| [Docker images](#docker-images) | Dockerfiles, CircleCI configs, `.gitlab-ci.yml`, Docker Compose files, Helm charts, and devcontainer configs | OCI registries ([Docker Hub](https://hub.docker.com), `ghcr.io`, `mcr.microsoft.com`, …) | Update-time |
| [GitHub Actions](#github-actions-and-pre-commit-hooks) | YAML files under `.github/` | [GitHub API](https://api.github.com) | Update-time |
| [Pre-commit hooks](#github-actions-and-pre-commit-hooks) | `.pre-commit-config.yaml` | [GitHub API](https://api.github.com) | Update-time |
| [jsDelivr npm URLs](#jsdelivr-npm-urls) | Sphinx config | [npm registry](https://registry.npmjs.org) | Update-time |

Update-time rewrites the dependency types it updates itself line by line: it picks the new version from the source and edits the reference in place. It hands the types it delegates to uv, npm, or pnpm, which resolves the versions itself. That package manager also keeps the project's lock file in step where there is one. Update-time runs the package manager for you, so the same `update-time` run updates a delegated dependency and every other one. Both update a Python dependency pinned with `==`: uv resolves the new version, and Update-time writes it into the `pyproject.toml` or the `# /// script` block. This is needed because uv has no [command to upgrade dependencies](https://github.com/astral-sh/uv/issues/6794).

Two things to note for a delegated dependency. It takes no [marker](#-controlling-updates-and-warnings-per-reference). And Update-time hands the [cooldown](#-cooldown) to the manager, which applies it per run rather than per reference.

Each dependency type links to its own section under [Details per dependency type](#-details-per-dependency-type). That section covers the files and dependencies the type updates. It also covers how pinning, the cooldown, staleness, yanks, vulnerabilities, archival, and markers apply to it.

### 📌 Pinning

*Pinning* means specifying exactly what a reference should resolve to, rather than leaving that to whatever its source serves at the time. Two things can be pinned: the version a reference resolves to, and the artefact that the version resolves to.

A **version pin** names an exact version instead of something that floats: `python:3.14` instead of `python:latest`, `humanize==4.15.0` instead of `humanize>=4`. Its opposite is a **floating pin**, which leaves the version to the source. A channel such as `python:latest`, a range such as `humanize>=4` or npm's `^17.0.0`, and a branch such as the `main` in `actions/checkout@main` each float. Update-time updates version pins, and replaces the floating pin of an image reference with the version it currently serves (see [Floating image tags](#floating-image-tags)). Update-time leaves the floating pin of any other reference as it is.

A **hash pin** adds a cryptographic hash of the artefact the version resolves to — an image digest, a commit SHA, or an integrity hash. The difference with a version pin is immutability. A version pin can be re-pointed under you, because a tag can be moved or re-pushed. A hash pin can only match the one thing it was computed from. That is what protects against a supply chain attack, and Update-time adds a hash pin wherever it can.

Update-time works on both version pins and hash pins. It moves a version pin forward, and takes the most precise spelling the source has for the version it lands on. It adds a hash pin to any reference that can hold one.

#### Floating image tags

A floating image tag pins no version: what a build pulls is whatever the registry serves under that tag on the day it runs. Update-time replaces it with the version and digest it serves at the time of the run, which is the image the build already pulls today:

```console
INFO Pinned python in Dockerfile:1 to 3.14.7@sha256:…
```

A reference that names no tag at all floats the same way: `FROM python` and `image: redis` ask for whatever their registry serves under `latest`. Update-time pins such a reference to the version and digest that the tag resolves to, writing the tag after the image's name, so `FROM python` becomes `FROM python:3.14.7@sha256:4fad23465a06cc5149a541fbec6f87e234a64dc0550f6bfdd2d290d8f03240df`.

Registries name one image under several tags, so a floating tag shares its digest with the version tags of the same push. For example, `python:latest` serves the same image as `python:3.14.7`, `python:3.14`, and `python:3`. Update-time can pin the reference to any of those versions. It picks between them using these rules:

1. Update-time keeps a label the floating tag shares with a version tag, so `node:trixie` lands on `26.7.0-trixie` rather than on `26.7.0`, and `python:slim` on `3.14.7-slim`. It drops a label naming a channel, since a tag carrying one keeps floating. `latest`, `lts`, `stable`, and `edge` are labels of that kind. So `node:lts-alpine` lands on `24-alpine` rather than on the `24-lts` that follows whichever 24 release is the LTS one. Update-time also drops a label no version tag uses at all, since requiring that label would leave nothing to pin to.
2. The most precise version wins, so `3.14.7` is preferred over `3.14` and `3`.
3. Then the most precise version of a variant the tag asked for, so `node:lts-alpine` lands on `24.19.0-alpine3.24` rather than on `24.19.0-alpine`.
4. The shortest name comes last, so `node:latest` lands on `26.7.0` rather than on `26.7.0-trixie`, and `amazoncorretto:latest` on `8` rather than on `8-al2023`. This prevents adopting a label the reference never asked for.

The pin names the image the reference already serves, so the [cooldown](#-cooldown) holds nothing back: the image pinned is the one the project already runs. A [bound](#bounding-an-update) decides nothing either, so `allow[update<3.13]` on `python:latest` still pins the `3.14.7` that tag serves. From the next run on, the reference is a version pin like any other. Update-time updates, bounds, and checks it as one.

> [!IMPORTANT]
> Once pinned, a reference no longer follows a channel. Later runs move `node:lts` pinned to `24.19.0` to whatever version is newest, LTS or not. To keep the channel, mark the reference `# update-time: allow[floating-pin]` (see [Keeping a tag floating](#keeping-a-tag-floating)). To keep the pin under 25, add a bound. From the next run on, `ignore[major-update]` or `allow[update<25]` holds it there (see [Bounding an update](#bounding-an-update)).

Update-time leaves four kinds of floating tag as they are:

- A tag whose image carries no version tag under the tag's own label, such as an image tagged only `dev` or `prod`.
- A tag on a registry other than Docker Hub, where none of the version tags Update-time checks serves the same image.
- A tag whose registry serves no manifest for it — a private image Update-time cannot authenticate to, or a registry it could not reach — so it cannot read the digest that tag serves
- A tag listed further down a large repository's tag list than Update-time reads.

Update-time reports each of them at `DEBUG`, in a line naming the reason the tag was left:

```console
DEBUG Floating tag acme/api:dev in docker-compose.yml:7 was left as it is: no tag naming a version serves the same image
DEBUG Floating tag acme/api:nightly in docker-compose.yml:7 was left as it is: its tag is not among the tags listed for the image
DEBUG Floating tag ghcr.io/acme/api:latest in Dockerfile:1 was left as it is: no tag naming a version among the newest examined serves the same image
DEBUG Floating tag ghcr.io/acme/api:edge in Dockerfile:1 was left as it is: the registry serves no manifest for its tag, so what that tag serves is unknown
```

#### Version precision

When Update-time updates a version pin, and the source offers a more precise new version, it chooses that version. For example, Update-time applies `python:3.13.0` when it updates `python:3.12` and both `python:3.13` and `python:3.13.0` are available. Every kind of version pin gains precision this way. A `.python-version` entry of `3.12` becomes `3.13.2`, and `actions/checkout@v4` moves to the exact version that tag resolves to.

However, more precision is not guaranteed: Update-time still applies a newer version that is less precise. For example, if `python:3.12.1` is the current version pin and `python:3.13` is available, but no `python:3.13.0`, the version pin moves to `python:3.13`.

A pin only gains precision by updating, so Update-time does not rewrite a pin into a more precise spelling of the version it already names. `python:3.12` stays as it is when the newest matching tag is `3.12.0`. So does `humanize==4.15` as long as `4.15.0` is the latest release.

#### Which dependencies get a hash pin

What Update-time adds depends on what the reference can hold:

| Dependency type | What Update-time adds |
| :-------------- | :-------------------- |
| [Python dependencies](#python-dependencies) | nothing: a pin names a version, and the hashes live in uv's lock file |
| [npm and pnpm dependencies](#npm-and-pnpm-dependencies) | nothing: the integrity hashes live in the package manager's lock file |
| [Node engine version](#node-engine-version-and-python-version) | nothing: it names a Node version, not an artefact that could be hashed |
| [Python version](#node-engine-version-and-python-version) | nothing: it names a Python version, not an artefact that could be hashed |
| [Docker images](#docker-images) | the `@sha256:digest` of the tag, appended to the reference |
| [GitHub Actions](#github-actions-and-pre-commit-hooks) | the commit SHA of the version, with the version in a trailing comment |
| [Pre-commit hooks](#github-actions-and-pre-commit-hooks) | the commit SHA of the version, with the version in a `# frozen:` comment |
| [jsDelivr npm URLs](#jsdelivr-npm-urls) | the SRI `integrity` hash of the file the URL points at |

Each type's own section explains further which of its references can hold a hash pin.

#### Hash drift

Sometimes a reference already carries a hash pin, and only what it points at changed. Update-time warns about that and leaves the pin unchanged. It never silently adopts a changed target, which would defeat the immutability a pin exists to provide. Hash drift takes three forms, one per kind of hash pin:

```console
WARNING Digest drift for python:3.14 in Dockerfile:1: pinned to sha256:… but the registry now serves sha256:…; the pin was left unchanged, verify the change is expected before updating the pin
WARNING Tag drift for actions/checkout@4.1.1 in .github/workflows/ci.yml:17: pinned to commit … but the tag now points at …; the pin was left unchanged, verify the tag was moved deliberately before updating the pin
WARNING Integrity hash mismatch for clipboard@2.0.11 in docs/conf.py:4: declares sha256-… but jsDelivr serves sha256-…; the hash was left unchanged, and since npm does not republish a version it is probably the declared hash that is wrong
```

*Digest drift* means an image tag was re-pushed (rebuilt) under the same name and version, so the registry now serves a different digest. Update-time judges a floating tag that already carries a digest the same way. When the digest is the one its tag still serves, Update-time pins the reference to the version that serves it. When it differs, the tag was re-pointed after the reference was pinned, so the pin stands and Update-time warns about the drift.

*Tag drift* means someone moved the version tag of a GitHub Action or pre-commit hook onto another commit than the one the reference pins. A git tag is mutable, so whoever controls the repository can move it. This is what pinning to a commit SHA exists to catch. The pin keeps the reference on the commit it was pinned to, whatever the tag does. The warning tells you that the tag and the pin now name different commits.

An *integrity hash mismatch* means the hash a jsDelivr URL declares is not the one jsDelivr serves for the version the URL sits on. Unlike the other two this rarely means anything upstream changed, since npm does not allow a published version to be republished. More likely, the declared hash is wrong: mistyped, copied from another file, or tampered with. It is also the most urgent, because the browser silently refuses to load the script until the hash matches, which no build step catches.

A reference without hash pin has nothing that can drift. So Update-time checks no `requirements.txt` pin, no `.python-version` entry, and no Node engine version. It checks none of the dependencies it updates through a package manager such as uv or npm either.

To adopt the new value, opt the reference in with a marker (see [Controlling updates and warnings per reference](#-controlling-updates-and-warnings-per-reference)). An image reference then adopts the re-pushed digest, and a GitHub Action or pre-commit hook adopts the commit its tag was moved to. Alternatively, pass `--allow-hash-drift` to opt every reference in the scan in at once. A marker that holds the reference back wins over both, so a reference you deliberately froze is never re-pinned. Update-time logs adopted drift at `INFO`, like any other change.

Update-time never adopts an integrity hash mismatch, whatever you opt in to. The whole point of the hash is to refuse content that doesn't match it. So Update-time reports the mismatch and leaves correcting it to you.

### ⏳ Cooldown

To avoid adopting releases that are too fresh to trust, Update-time honours a cooldown period. It does not yet adopt a version published inside that period. The cooldown defaults to **7 days**. Change it with the `--cooldown` option, for example `update-time --cooldown 14`. A single reference can carry a cooldown of its own, which wins over whatever `--cooldown` says (see [Controlling updates and warnings per reference](#-controlling-updates-and-warnings-per-reference)). Who applies it depends on the dependency type:

| Dependency type | Applied by | Measured against |
| :-------------- | :--------- | :--------------- |
| [Python dependencies](#python-dependencies) | Update-time for `requirements.txt`, uv for the rest | the release's publication date on PyPI |
| [npm and pnpm dependencies](#npm-and-pnpm-dependencies) | npm and pnpm | the release's age on the npm registry |
| [Node engine version](#node-engine-version-and-python-version) | Update-time, or already applied via the Dockerfile | the Node image tag's push date |
| [Python version](#node-engine-version-and-python-version) | Update-time, or already applied via the Dockerfile | the Python image tag's push date |
| [Docker images](#docker-images) | Update-time, on Docker Hub only | the image tag's push date |
| [GitHub Actions](#github-actions-and-pre-commit-hooks) | Update-time | the release's publication date, or the tagged commit's date |
| [Pre-commit hooks](#github-actions-and-pre-commit-hooks) | Update-time | the release's publication date, or the tagged commit's date |
| [jsDelivr npm URLs](#jsdelivr-npm-urls) | Update-time | the release's publication date on the npm registry |

Where Update-time applies the cooldown itself, it holds back every version published inside the window.

Where a package manager applies the cooldown, Update-time hands the value to uv, npm, or pnpm. Each of them takes the value per run rather than per dependency. Each of them also leaves a cooldown your project already configures in place. [Python dependencies](#python-dependencies) and [npm and pnpm dependencies](#npm-and-pnpm-dependencies) describe what that means per dependency type.

## ⚠️ Warnings

Update-time also reports what an update cannot fix:

- a dependency whose newest release is years old
- a version its maintainer withdrew
- a version a security advisory names
- a dependency its maintainer archived

These warnings are informational, so they change no file and do not affect the exit status.

### 🕸️ Stale dependencies

Update-time warns when a dependency's newest release is older than a threshold. That may mean the project was abandoned or superseded. The threshold defaults to **365 days**, and `--stale-after DAYS` sets it. Pass `--stale-after 0` to disable the check. A single reference can carry a threshold of its own, or leave the check out altogether, and either wins over whatever `--stale-after` says (see [Controlling updates and warnings per reference](#-controlling-updates-and-warnings-per-reference)). For example, Update-time reports a pin whose newest release is well over a year old as:

```console
WARNING Stale dependency humanize in docs/requirements.txt:12: newest release 4.15.0 was published 512 days ago (> 365)
```

The date compared against the threshold is the publication date of the dependency's *newest* release. So Update-time never reports a project that just published a release as stale, not even when that release is still within the [cooldown](#-cooldown) window.

Update-time checks every kind of dependency it updates for staleness, against the date its own source reports:

| Dependency type | Measured against |
| :-------------- | :--------------- |
| [Python dependencies](#python-dependencies) | the newest release of the package on PyPI |
| [npm and pnpm dependencies](#npm-and-pnpm-dependencies) | the newest release on the npm registry, for the dependencies that resolve to one |
| [Node engine version](#node-engine-version-and-python-version) | the base image it follows, or the newest Node release on Docker Hub when no Dockerfile declares one |
| [Python version](#node-engine-version-and-python-version) | the base image it follows, or the newest Python release on Docker Hub when no Dockerfile declares one |
| [Docker images](#docker-images) | the image's newest release, and on Docker Hub only, since other registries expose no publication date |
| [GitHub Actions](#github-actions-and-pre-commit-hooks) | the newest release of the action's repository |
| [Pre-commit hooks](#github-actions-and-pre-commit-hooks) | the newest release of the hook's repository |
| [jsDelivr npm URLs](#jsdelivr-npm-urls) | the newest release of the package on the npm registry |

### 🚫 Yanked dependencies

A yank means "stop using this": a release the maintainer withdrew because it was broken, botched, or insecure. An exact pin keeps installing one anyway, so Update-time warns when the version a dependency is pinned to was yanked. Update-time includes the maintainer's reason when they gave one:

```console
WARNING Yanked dependency humanize in docs/requirements.txt:12: version 4.15.0 was yanked ("accidentally broke Python 3.10 support")
```

When no reason was given, the message reports `(reason not specified)` instead.

Update-time gives the warning only when the run leaves the reference on the yanked version. That happens when the replacement is still within the [cooldown](#-cooldown), or when a marker holds the update back. It also happens when a package manager left the pin where it was, or when the yanked release is the newest one. To silence the warning itself, mark the reference `# update-time: ignore[yanked]` (see [Controlling updates and warnings per reference](#-controlling-updates-and-warnings-per-reference)).

Which dependencies are checked follows from where a yank can be observed. PyPI reports one as [PEP 592](https://peps.python.org/pep-0592/) yank metadata. On npm there is no yank, but a per-version *deprecation* is the same signal, and Update-time reports it in the same wording as a yank. Where a withdrawal can be observed, Update-time skips that version when picking a new one, and warns about a reference left on it:

| Dependency type | Yank check |
| :-------------- | :--------- |
| [Python dependencies](#python-dependencies) | `requirements.txt`, `pyproject.toml`, and inline script metadata pins, against PyPI's yank metadata |
| [npm and pnpm dependencies](#npm-and-pnpm-dependencies) | none: npm and pnpm handle deprecated versions themselves |
| [Node engine version](#node-engine-version-and-python-version) | none: its source has no yank concept |
| [Python version](#node-engine-version-and-python-version) | none: its source has no yank concept |
| [Docker images](#docker-images) | none: its source has no yank concept |
| [GitHub Actions](#github-actions-and-pre-commit-hooks) | none: its source has no yank concept |
| [Pre-commit hooks](#github-actions-and-pre-commit-hooks) | none: its source has no yank concept |
| [jsDelivr npm URLs](#jsdelivr-npm-urls) | against the npm registry's deprecation of the pinned version |

### 🛡️ Vulnerable dependencies

Update-time queries the [OSV](https://osv.dev) database about the pinned version. OSV aggregates GitHub's advisory database, PyPI's advisory database, and others. Update-time warns when an advisory names that version as affected. The warning names the risk level, what the advisory says, and where to read the advisory in full:

```console
WARNING Vulnerable dependency django in docs/requirements.txt:12: version 3.2.0 has a critical vulnerability, "SQL Injection in Django" (GHSA-2gwj-7jmv-h26r, https://osv.dev/GHSA-2gwj-7jmv-h26r)
```

Several of the databases that OSV aggregates carry the same vulnerability, each under an identifier of its own. So OSV answers with an advisory per database. Update-time warns about a vulnerability once, whichever of them reported it. The advisories of one vulnerability name each other's identifiers, and Update-time reads them as one. The databases rate a vulnerability independently, and only some rate it at all. So the warning names the advisory that rates it most severely, and Update-time reports and filters the vulnerability at that level.

The risk level is the one the advisory's reviewers gave it: `low`, `moderate`, `high`, or `critical`. Where they gave none, Update-time derives the level from the advisory's CVSS base score: `0.1` to `3.9` is low, `4.0` to `6.9` moderate, `7.0` to `8.9` high, and `9.0` to `10.0` critical.

Update-time reads an advisory carrying both a CVSS v3 and a v4 vector at its v4 score, the newer of the two assessments. It reports an advisory whose risk level it cannot read at all as `a vulnerability of unknown severity`. It reports an advisory that gives no summary, which many do not, without the quotation. That advisory's id and URL say which vulnerability it is.

Update-time still checks a reference marked `# update-time: ignore[vulnerable]`, and silences what it finds rather than warning about it. The check is what can tell you the marker went stale: a suppression outlives the vulnerability it was written for. When the version has no vulnerability at all, Update-time reports the marker as holding nothing back:

```console
WARNING Redundant update-time directive ignore[vulnerable] for django in docs/requirements.txt:12: version 4.2.0 has no vulnerability
```

Run with `--log-level DEBUG` to see what the marker silenced. To silence one advisory rather than every one, name it in the marker (see [Silencing specific vulnerabilities](#silencing-specific-vulnerabilities)). To skip the check altogether, mark the reference `# update-time: ignore`, which holds every check back and queries no source. Update-time still checks a reference frozen with `# update-time: ignore[update]`, so a pin you deliberately hold back keeps telling you its version is vulnerable.

Update-time checks the version the run leaves the reference on. So it warns about a vulnerability the run updated into, and not about one the run updated away from. It checks that version alone. Auditing the transitive dependencies in a lock file is what `uv audit`, `pip-audit`, and `npm audit` are for.

To silence one advisory across the whole scan, rather than on the one reference that carries a marker, pass `--ignore-vulnerability`. The option takes a comma-separated list: `--ignore-vulnerability GHSA-2gwj-7jmv-h26r,CVE-2021-31542`. It names an advisory the way a marker does, so any identifier the vulnerability is known by will do. Update-time logs what the option silenced at `DEBUG`. Where a reference's own marker silences the same advisory, the marker is the one reported.

Update-time warns about every risk level by default. To hear only about the more severe ones, raise the threshold with `--vulnerability-level`, for example `--vulnerability-level high`. Update-time warns about a vulnerability whose risk level it cannot read, whatever the threshold is. Leaving the vulnerabilities nobody rated out of the warnings would hide exactly the ones nobody looked at.

A query sends the pin's package name and version to OSV. Pass `--vulnerability-level none` to switch the check off, which stops those requests altogether. Update-time still queries OSV about a reference that sets a level of its own (see [Setting a risk level](#setting-a-risk-level)).

Which dependencies are checked follows from what OSV can match a pinned version against:

| Dependency type | Vulnerability check |
| :-------------- | :------------------ |
| [Python dependencies](#python-dependencies) | `requirements.txt`, `pyproject.toml`, and inline script metadata pins, against OSV's PyPI advisories |
| [npm and pnpm dependencies](#npm-and-pnpm-dependencies) | none: those dependencies are declared as ranges and resolved in the lock file, which Update-time does not read |
| [Node engine version](#node-engine-version-and-python-version) | none: it names a runtime version rather than a package release |
| [Python version](#node-engine-version-and-python-version) | none: it names a runtime version rather than a package release |
| [Docker images](#docker-images) | none: OSV has no ecosystem for container images |
| [GitHub Actions](#github-actions-and-pre-commit-hooks) | none: OSV holds advisories for actions but matches no version against them, so silence here is not safety |
| [Pre-commit hooks](#github-actions-and-pre-commit-hooks) | none: a hook repository is not an OSV package |
| [jsDelivr npm URLs](#jsdelivr-npm-urls) | the version in the URL, against OSV's npm advisories |

### 🗄️ Archived dependencies

An archived dependency is one its maintainer declared finished: expect no further release. Update-time warns when a dependency's source declares it archived, and `--ignore-archived` switches that check off for the whole run. For example, Update-time reports an archived project and an archived repository as:

```console
WARNING Archived dependency aioredis in docs/requirements.txt:12: the project was archived
WARNING Archived dependency actions/setup-ruby in .github/workflows/ci.yml:17: the repository was archived
```

The clause after the colon names what the source archived: PyPI archives a project, GitHub a repository. The warning quotes a reason where the source publishes one.

An archived dependency is still updated to its newest release, if one is available.

Which dependencies are checked follows from where an archival declaration can be read:

| Dependency type | Archival check |
| :-------------- | :------------- |
| [Python dependencies](#python-dependencies) | the project status PyPI publishes for the package a Python file declares |
| [npm and pnpm dependencies](#npm-and-pnpm-dependencies) | none: the npm registry publishes no archival signal |
| [Node engine version](#node-engine-version-and-python-version) | none: it follows a Docker image, and no registry publishes an archival signal |
| [Python version](#node-engine-version-and-python-version) | none: it follows a Docker image, and no registry publishes an archival signal |
| [Docker images](#docker-images) | none: no OCI registry publishes an archival signal |
| [GitHub Actions](#github-actions-and-pre-commit-hooks) | whether GitHub reports the action's repository as archived |
| [Pre-commit hooks](#github-actions-and-pre-commit-hooks) | whether GitHub reports the hook's repository as archived |
| [jsDelivr npm URLs](#jsdelivr-npm-urls) | none: the npm registry publishes no archival signal |

Switching the check off also stops Update-time asking GitHub for the repository metadata of each action and hook, which it reads for this check alone. It still fetches that repository's releases and tags, which it needs for other reasons. On PyPI the archival answer rides on a request another check makes anyway, so there switching the check off saves nothing while that check still runs.

`--stale-after 0` does not switch the archival check off. The two checks share one lookup, so a run with the staleness check off still asks the sources above about the projects they report on. Switch both off and that lookup is skipped. A reference the run updates keeps its project all the same, read from what the update itself fetched. A marker silences the warning rather than the query, and only a bare `# update-time: ignore` skips both.

## 🎛️ Controlling updates and warnings per reference

Markers of the form `# update-time: <directive>` let you steer what happens to an individual reference. You can hold a reference back with one. You can also bound how far it may move, or opt it into behaviour that is off by default. A marker is a comment wherever the file can hold one, and a field where it cannot (see [Where to put a marker](#where-to-put-a-marker)).

To stop Update-time from changing a specific reference, add an `# update-time: ignore` comment (all lower-case). You might do this because of a known incompatibility, a deferred migration, or to keep something reproducible. Update-time then leaves the reference untouched, and queries no registry or source for it. You can add a reason after the marker, for example `# update-time: ignore (pinned until the 3.13 migration)`.

> [!WARNING]
> As long as Update-time is alpha (version `0.0.X`), marker syntax and semantics are subject to change without deprecation notice or migration support.

### The anatomy of a marker

The rest of this chapter names the parts of a marker, so here is one taken apart:

```text
humanize==4.15.0  # update-time: ignore[yanked, stale<90] allow[update<5] (until 5.0)
                  └────────────────────── marker ───────────────────────┘ └ reason ─┘
                                 └───── directives ─────┘ └─ directive ─┘
                                 └verb┘└─── bracket ────┘ └verb┘└bracket┘
```

A marker is the `# update-time:` comment up to its last directive. It holds one or more directives, each a verb and the bracket that may follow it. Directives that use the same verb can be combined: `ignore[yanked, stale<90]` is equivalent to `ignore[yanked] ignore[stale<90]`. Free text after the last directive is a reason, which Update-time ignores. In a file that can hold no comment, the directives are the value of an `update-time` field instead. Everything below reads the same (see [Where to put a marker](#where-to-put-a-marker)).

Zooming in on brackets:

```text
ignore[yanked, stale<90]
       └─┬──┘  └─┬─┘└┬┘
         │       │   └─ threshold: what the item sets for this reference, here 90 days
         │       └─ scope: what the item steers, here the staleness warning
         └─ item: a scope named alone holds it back outright, here to not warn about yanked versions
```

A bracket holds one or more items, separated by commas. The `ignore` bracket above holds the two items `yanked` and `stale<90`, and the `allow` bracket holds the single item `update<5`. An item names the scope it steers: the update, the cooldown, the hash drift, or one of the warnings. It may also set a value for that scope, as `stale<90` sets 90 days for the staleness warning.

| Term | What it is | Example |
| :--- | :--------- | :------ |
| Marker | the directives steering one reference: a `# update-time:` comment, up to its last directive, or a field's value | `# update-time: ignore[stale] allow[update<3.13]` |
| Directive | a verb and the bracket it may carry. A marker holds one or more directives | `ignore[stale<90]` |
| Verb | `ignore` drops what its items name and `allow` keeps it, so `ignore[stale<90]` and `allow[stale>=90]` say the same thing | `ignore`, `allow` |
| Bracket | what a directive's `[…]` holds: its items, separated by commas | `[yanked, stale<90]` |
| Item | one entry in a bracket: a scope, a scope with a threshold, a [bound](#bounding-an-update), or an [advisory](#silencing-specific-vulnerabilities) | `stale<90` |
| Scope | what an item steers: `update`, `cooldown`, `stale`, `yanked`, `vulnerable`, `archived`, `hash-drift`, or `floating-pin` | `ignore[yanked]` steers the yank warning |
| Bare `ignore` | the verb with no bracket at all, which holds back every scope it can without naming one | `# update-time: ignore` |
| Reason | free text after the last directive, which Update-time ignores | `(pinned until the 3.13 migration)` |

A marker wins over the command-line option that sets the same thing, whatever that option is set to. Set a threshold, a cooldown, a risk level, or a bound once per reference. Setting one twice, with either verb, has an undefined result.

### Holding a reference back

A bare `# update-time: ignore` holds a reference back from version updates and from the [staleness](#-stale-dependencies), [yank](#-yanked-dependencies), [vulnerability](#-vulnerable-dependencies), and [archival](#-archived-dependencies) checks. Add a bracket to narrow it to one or more scopes:

| Marker | Version update | ⚠️ Staleness warning | 🚫 Yank warning | 🛡️ Vulnerability warning | 🗄️ Archival warning |
| :----- | :------------- | :---------------- | :----------- | :-------------------- | :------------------ |
| `# update-time: ignore` | held back | held back | held back | held back | held back |
| `# update-time: ignore[update]` | held back | still checked | still checked | still checked | still checked |
| `# update-time: ignore[stale]` | applied | held back | still checked | still checked | still checked |
| `# update-time: ignore[yanked]` | applied | still checked | held back | still checked | still checked |
| `# update-time: ignore[vulnerable]` | applied | still checked | still checked | held back | still checked |
| `# update-time: ignore[archived]` | applied | still checked | still checked | still checked | held back |

So `# update-time: ignore[update]` keeps a deliberately pinned reference frozen. It still tells you when the project behind the reference went quiet, or when its version was withdrawn. `# update-time: ignore[stale]` silences a staleness warning you acknowledged, without freezing the version. `# update-time: ignore[yanked]` does the same for a yank you decided to live with.

`# update-time: ignore[vulnerable]` silences the vulnerability warning for one you assessed, while the reference keeps updating. `# update-time: ignore[archived]` silences the archival warning for a dependency you decided to keep using. To silence it for every dependency in the scan, pass `--ignore-archived` (see [Archived dependencies](#-archived-dependencies)). A reason can still follow the scope, for example `# update-time: ignore[update] (pinned until the 3.13 migration)`.

#### Setting a staleness threshold

`ignore[stale]` silences the staleness warning altogether. To keep the warning but on a different schedule, give the scope a number of days. `# update-time: ignore[stale<90]` warns once that reference's newest release is more than 90 days old, and is a per-reference `--stale-after 90`. Use it for a critical dependency you want to hear about early, or for a low-churn library that shouldn't be flagged for years:

```text
humanize==4.15.0  # update-time: ignore[stale<90] (critical, warn early)
```

```dockerfile
# update-time: ignore[stale<1095]
FROM python:3.12
```

The threshold applies to the reference carrying it, and every other reference in the scan keeps the global one. It wins over `--stale-after`, `--stale-after 0` included, so disabling the check globally still leaves a reference with its own threshold checked. To disable the check for one reference, use `ignore[stale]`.

`allow` and `ignore` are complements here as elsewhere, so `allow[stale>=90]` sets the same 90-day threshold as `ignore[stale<90]`. Inverting the operator would warn while a release is fresh, and go quiet once it is old. So neither `allow[stale<90]` nor `ignore[stale>=90]` sets a threshold. Update-time logs an inverted comparison at `WARNING` and holds nothing back, so the reference updates as usual and the global threshold applies to it:

```console
WARNING Incorrect 'stale>=90' in the update-time marker for python in Dockerfile:2: this comparison warns while a release is fresh and goes quiet once it is old, so it sets no threshold
```

A day count must be a whole number of days, so Update-time reports `ignore[stale<-5]` and `ignore[stale>=1.5]` as invalid and leaves the reference unchanged. Where a reference carries both a threshold and a bare `ignore[stale]`, the `ignore[stale]` wins and silences the warning whatever the threshold says.

Staleness is measured against the publication date of a dependency's newest release. Where the reference's own source reports no such date, Update-time reports the marker as holding nothing back. It updates the reference as usual:

```console
WARNING Redundant update-time directive ignore[stale<90] for ghcr.io/astral-sh/uv in Dockerfile:2: this dependency's source reports no publication date to measure staleness against
```

Three kinds of reference get that warning. An image on a registry other than Docker Hub does, since only Docker Hub reports a push date. So Update-time does not report the same marker on a Docker Hub image. A CircleCI machine-executor image gets the warning too, since no registry serves it. And so does a runtime version that follows the project's Dockerfile, whether it is a `.python-version` entry or a Node engine. The staleness reported for it is the base image's, not its own.

Update-time reports a bare `ignore[stale]` on those references too, since it silences a warning they never get.

#### Setting a cooldown period

The [cooldown](#-cooldown) holds back releases that are too fresh to trust. To put one reference on a different window from the rest, give a `cooldown` scope a number of days. `# update-time: ignore[cooldown<30]` drops update candidates published less than 30 days ago, and is a per-reference `--cooldown 30`. Use it for a dependency that burned you before, or one you trust enough to adopt sooner than the rest:

```text
some-flaky-lib==2.1.0  # update-time: ignore[cooldown<30] (burned by 2.0.0)
```

```dockerfile
# update-time: ignore[cooldown<30]
FROM python:3.12
```

The cooldown applies to the reference carrying it, and every other reference in the scan keeps the global one. It wins over `--cooldown`. `allow` and `ignore` are complements here as elsewhere, so `allow[cooldown>=30]` sets the same 30-day window as `ignore[cooldown<30]`. To adopt new releases for one reference as soon as they ship, write `allow[cooldown>=0]` or `ignore[cooldown<0]`. A zero-day window holds nothing back, which is what `--cooldown 0` means globally.

Inverting the operator would adopt a release only while it is fresh, and hold it back once it is old. So neither `allow[cooldown<30]` nor `ignore[cooldown>=30]` sets a cooldown. Update-time logs an inverted comparison at `WARNING` and holds nothing back, so the reference updates as usual and the global cooldown applies to it:

```console
WARNING Incorrect 'cooldown>=30' in the update-time marker for python in Dockerfile:2: this comparison adopts a release only while it is fresh and holds it back once it is old, so it sets no cooldown
```

You can read a bare `ignore[cooldown]` in two ways: adopt at once, or never adopt at all. Rather than guess, Update-time reports it as invalid and leaves the reference unchanged. It reports `allow[cooldown]` the same way. Write `allow[cooldown>=0]` to adopt at once, and `ignore[update]` to freeze the reference. A day count must be a whole number of days, so Update-time reports `ignore[cooldown<-5]` and `ignore[cooldown<1.5]` as invalid too.

The override reaches the dependencies whose cooldown Update-time enforces itself. It does nothing for the dependencies handed to uv, npm, or pnpm, which take a cooldown per run rather than per dependency (see [Cooldown](#-cooldown)). Where the reference's own source reports no publication date to measure a cooldown against, Update-time reports the marker as holding nothing back. It updates the reference as usual:

```console
WARNING Redundant update-time directive ignore[cooldown<30] for python in .python-version:2: this dependency's source reports no publication date to measure a cooldown against
```

The same three kinds of reference get that warning as for staleness (see [Setting a staleness threshold](#setting-a-staleness-threshold)), and for the same reasons but one. A runtime version that follows the project's Dockerfile gets it, whether it is a `.python-version` entry or a Node engine. Update-time already applied its cooldown when it updated the base image. An image on a registry other than Docker Hub gets the warning too. Only Docker Hub reports a push date to measure a cooldown against, so Update-time does not report the same marker on a Docker Hub image. A CircleCI machine-executor image gets the warning as well, since no registry serves it.

Update-time also reports a `cooldown` scope as redundant on a `requirements.txt` requirement that pins no exact version. PyPI dates its releases, but Update-time resolves no update for such a requirement, so a cooldown holds no release back.

#### Silencing specific vulnerabilities

`ignore[vulnerable]` silences every [vulnerability](#-vulnerable-dependencies) warning a reference gets. To silence just one of them — a vulnerability you assessed and decided to live with — name the advisory after the scope:

```text
django==3.2.0  # update-time: ignore[vulnerable=GHSA-2gwj-7jmv-h26r] (assessed, we don't use the affected query API)
```

Any identifier the vulnerability is known by will do. OSV holds an advisory per database, each under an id of its own. So a marker naming the `CVE-…` silences a warning reported under the `GHSA-…`.

To silence a second advisory, add a second item: `# update-time: ignore[vulnerable=GHSA-2gwj-7jmv-h26r, vulnerable=CVE-2021-31542]`. The comma separates the bracket's items, so each identifier needs a `vulnerable=` of its own. `ignore[vulnerable=GHSA-…,CVE-…]` reads the second identifier as an item. Update-time reports that item as invalid, and leaves the reference unchanged.

Sometimes none of the version's vulnerabilities answers to the identifier, because an update fixed the vulnerability or because the identifier was mistyped. Update-time then reports the marker as holding nothing back:

```console
WARNING Redundant update-time directive ignore[vulnerable=CVE-2022-28346] for django in docs/requirements.txt:12: version 4.2.0 has no such vulnerability
```

Update-time judges a marker naming several advisories together. It warns only when none of them matches a vulnerability the version has.

Only `ignore` names an advisory here. `allow` naming one would keep that warning and drop the warning about every other advisory, which is not a rule the language offers. So Update-time reports `allow[vulnerable=GHSA-…]` as an invalid item and leaves the reference unchanged.

The reference keeps updating, and Update-time still warns about every other advisory affecting the version it lands on. So a vulnerability found after you wrote the marker still reaches you. Run with `--log-level DEBUG` to see what the marker silenced. To silence an advisory wherever it appears, pass `--ignore-vulnerability` (see [Vulnerable dependencies](#-vulnerable-dependencies)).

#### Setting a risk level

`ignore[vulnerable]` silences every vulnerability warning a reference gets. To keep the warnings but only from a given severity up, give the scope a risk level. `# update-time: ignore[vulnerable<high]` warns about that reference's `high` and `critical` vulnerabilities, and stays quiet about its `low` and `moderate` ones. It is a per-reference `--vulnerability-level high`. Use it for a dependency where you act on `high` and `critical` alone. The level keeps silencing mild advisories published later, so it states a policy rather than a judgement on the advisories the version has today:

```text
django==3.2.0  # update-time: ignore[vulnerable<high] (we act on high and worse for this dependency)
```

The level applies to the reference carrying it, and every other reference in the scan keeps the global one. It wins over `--vulnerability-level`, `--vulnerability-level none` included. So a run that switches the check off globally still queries OSV about a reference with its own level, and still warns about it. As with the global level, Update-time warns about a vulnerability whose risk level it cannot read, whatever level is in force. Leaving the vulnerabilities nobody rated out of the warnings would hide exactly the ones nobody looked at.

When none of the version's vulnerabilities falls below the level, Update-time reports the marker as holding nothing back. A level that silences nothing is one the reference no longer needs:

```console
WARNING Redundant update-time directive ignore[vulnerable<high] for django in docs/requirements.txt:12: version 4.2.0 has no vulnerability below high
```

`allow` and `ignore` are complements here as elsewhere, so `allow[vulnerable>=high]` sets the same level as `ignore[vulnerable<high]`. Inverting the operator would warn about the mild vulnerabilities, and stay quiet about the severe ones. So neither `allow[vulnerable<high]` nor `ignore[vulnerable>=high]` sets a level. Update-time logs an inverted comparison at `WARNING` and holds nothing back, so it warns about the reference at the global level:

```console
WARNING Incorrect 'vulnerable>=high' in the update-time marker for django in docs/requirements.txt:12: this comparison warns about the mild vulnerabilities and stays quiet about the severe ones, so it sets no risk level
```

A level must be one of `low`, `moderate`, `high`, and `critical`, spelled in lower case. So Update-time reports `ignore[vulnerable<hgih]` as invalid and leaves the reference unchanged. `none` is a value for `--vulnerability-level` rather than a level, so Update-time reports it as invalid too. To switch the warning off for one reference, write `ignore[vulnerable]`.

#### Redundant markers

A yank can only be observed where the dependency's source reports one. Of the references that accept a marker, that means `requirements.txt` pins and jsDelivr URLs (see [Yanked dependencies](#-yanked-dependencies)). On a Docker image, a GitHub Action, a pre-commit hook, a `.python-version` entry, or a Node engine, the scope can never suppress anything. So Update-time logs it as redundant at `WARNING`:

```console
WARNING Redundant update-time directive ignore[yanked] for python in Dockerfile:2: this dependency's source has no yank concept
```

Update-time reports a `requirements.txt` requirement that pins no exact version too. PyPI does report yanks, but a yank is about the version a reference is left on, and such a requirement pins none.

```console
WARNING Redundant update-time directive ignore[yanked] for humanize in docs/requirements.txt:12: this requirement pins no version to check for a yank
```

A vulnerability can only be reported where OSV holds advisories for the dependency. Of the references that accept a marker, that means `requirements.txt` pins and jsDelivr URLs (see [Vulnerable dependencies](#-vulnerable-dependencies)). On a Docker image, a GitHub Action, a pre-commit hook, a `.python-version` entry, or a Node engine, the scope can never suppress anything. So Update-time reports it as redundant in all its forms:

```console
WARNING Redundant update-time directive ignore[vulnerable] for python in Dockerfile:2: this dependency's source reports no vulnerabilities
```

Update-time reports a requirement that pins no exact version here too. An advisory is matched against a version, and such a requirement pins none.

```console
WARNING Redundant update-time directive ignore[vulnerable] for humanize in docs/requirements.txt:12: this requirement pins no version to check for a vulnerability
```

Archival can only be observed where the dependency's source publishes an archival signal. Of the references that accept a marker, that means `requirements.txt` requirements, GitHub Actions, and pre-commit hooks (see [Archived dependencies](#-archived-dependencies)). On a Docker image, a jsDelivr URL, a `.python-version` entry, or a Node engine, the scope can never suppress anything. So Update-time reports it as redundant:

```console
WARNING Redundant update-time directive ignore[archived] for python in Dockerfile:2: this dependency's source publishes no archival signal
```

In all its forms, Update-time reports the `stale` scope as redundant for a reference whose source reports no publication date to measure staleness against. [Setting a staleness threshold](#setting-a-staleness-threshold) names the three kinds of reference that get that warning.

Update-time reports an `allow[floating-pin]` as redundant for a reference whose pin does not float, and for one whose update a marker holds back. Neither has anything to keep floating. [Keeping a tag floating](#keeping-a-tag-floating) shows both warnings.

Update-time never reports a bare `# update-time: ignore` as redundant. It names no scope, so a warning would have no directive to name. Update-time does report a scope or item written beside it, so `# update-time: ignore ignore[yanked]` on a Docker image reports the `ignore[yanked]` as redundant.

#### Invalid markers

A scope Update-time does not recognise — a mistyped `ignore[stlae]`, say — is logged at `WARNING` as an invalid item:

```console
WARNING Invalid 'stlae' in the update-time marker for python in Dockerfile:2; leaving the reference unchanged
```

Update-time leaves the reference as it is, because an item it cannot read may have been meant to bound the update. Applying an update would be guessing. The checks still run, since Update-time never reads that item as silencing a warning. An unreadable marker holds back what Update-time would write, never what it would tell you. Every item beside it that Update-time does read applies as written, so `ignore[cooldwn<30, stale]` still silences the staleness warning.

Update-time reports an `update-time` field it cannot read as an invalid item too. It reads a field whole rather than item by item. So an unreadable field holds back every directive it carries, where an unreadable bracket item leaves the items beside it standing. The warning names where the marker would sit rather than the value that is wrong. A field of the wrong shape holds no marker to quote:

```console
WARNING Invalid 'update-time.engines.node' in the update-time marker for node in package.json:3; leaving the reference unchanged
```

### Adopting hash drift

`# update-time: allow[hash-drift]` opts an already-pinned reference *into* adopting what it now points at (see [Hash drift](#hash-drift)). Update-time then pins a re-pushed image tag's new digest, or the commit a moved version tag points at, instead of only warning about it. The global `--allow-hash-drift` flag applies it to every reference at once.

`ignore[hash-drift]` is the opposite and the default. A reference carrying it keeps its pin exactly as one carrying no marker at all, in a run passing `--allow-hash-drift` as well. Where an `ignore` (or `ignore[update]`) marker also applies, that wins and Update-time leaves the reference untouched.

### Keeping a tag floating

`# update-time: allow[floating-pin]` keeps a reference's [floating image tag](#floating-image-tags) as it is. Update-time would otherwise replace that tag with the version and digest it serves. Use the marker for a reference you want to follow a channel, such as an image you rebuild from `latest` on purpose. Run with `--log-level DEBUG` to see what the marker held back. That line names the version the tag resolves to, so a marker you no longer need shows what dropping it would pin. Where the tag serves another digest than the reference records, Update-time reports the [hash drift](#hash-drift) instead:

```console
DEBUG Keeping the floating tag python:latest in Dockerfile:2: it resolves to 3.14.7@sha256:… (update-time: allow[floating-pin])
```

Update-time keeps a reference without a tag the same way. The `DEBUG` line then names the image alone, since the reference has no tag to name.

Update-time still checks a reference kept floating for [hash drift](#hash-drift). Where it already records a digest and its tag now serves another, Update-time warns about the drift. A reference opted into drift adopts the new digest, while its tag stays as it is.

The global `--allow-floating-pin` flag keeps every reference in the scan floating at once. `ignore[floating-pin]` is the opposite and the default, so Update-time pins a reference carrying it exactly as one carrying no marker at all. It pins that reference in a run passing `--allow-floating-pin` as well. Where an `ignore` (or `ignore[update]`) marker also applies, that wins and Update-time leaves the reference untouched, tag and all.

An `allow[floating-pin]` on a reference whose pin does not float keeps nothing floating, so Update-time reports it as redundant and updates the reference as usual:

```console
WARNING Redundant update-time directive allow[floating-pin] for python in Dockerfile:2: this reference's pin does not float
```

A reference held back by an `ignore` or an `ignore[update]` keeps nothing floating either. A reference that is never pinned keeps its tag, whatever the directive beside the hold-back asks for. Update-time reports the directive as redundant whatever the tag says, so even a floating tag gets the warning:

```console
WARNING Redundant update-time directive allow[floating-pin] for python in Dockerfile:2: this reference's update is held back, so its tag is never pinned
```

Update-time does not report a floating tag it could not pin, since that tag does float. It logs the reason at `DEBUG` instead (see [Floating image tags](#floating-image-tags)).

### Bounding an update

A bound lets a reference keep updating, while it blocks the jump you are not ready for. Name the versions the reference may move to, or the level of update it may not make.

#### Bounding how far a reference may update

`ignore[update]` freezes a reference at its current version. Sometimes you want the middle ground: updates *within a range*, without the jump you're not ready for. For example, keep the `python:3.12` patch releases, and avoid `3.13` until you migrate. Add a [PEP 440](https://peps.python.org/pep-0440/) version specifier directly after `update` inside the brackets, either to allow or ignore updates. `# update-time: allow[update<specifier>]` **keeps only** the updates whose version satisfies the specifier. `# update-time: ignore[update<specifier>]` **drops** the updates whose version satisfies it, and the plain `ignore[update]` is the drop-everything case.

For example, `allow[update<3.13]` keeps a `python` base image on its newest `3.12` release and never crosses into `3.13`:

```dockerfile
# update-time: allow[update<3.13]
FROM python:3.12.1-bookworm-slim
```

The same works inline, for image and action references — here `allow[update==7.*]` keeps Redis on its `7.x` line and `ignore[update>=5]` keeps `checkout` below `v5`:

```yaml
image: redis:7.2  # update-time: allow[update==7.*]
uses: actions/checkout@v4  # update-time: ignore[update>=5]
```

Update-time filters the candidate versions *before* it picks the highest one, so a bounded reference still advances as far as the bound allows. On `3.12.8`, `allow[update<3.13]` still adopts a freshly published `3.12.9`. It just never crosses into `3.13`.

`allow` and `ignore` are complements, which matters for ranges. For a one-sided bound the two are interchangeable — `allow[update<3.13]` and `ignore[update>=3.13]` express the same ceiling. For a *range* they are opposites. With versions `3.13` through `3.16` available, `allow[update>=3.13,<3.15]` keeps the reference *within* `[3.13, 3.15)` and picks `3.14`. `ignore[update>=3.13,<3.15]` *excludes* that range and skips to `3.16`.

Choose the operator deliberately. To keep `3.12` together with its patch releases while blocking `3.13`, use `<3.13`, `==3.12.*`, or `~=3.12.0`. Don't use `<=3.12` if you want to stay on `3.12`. Since `3.12.1 > 3.12` in PEP 440, it also blocks `3.12.1`, which is rarely what "stay on 3.12" means.

#### Bounding by update level

A specifier bound names the version it must not reach, so it goes stale. After you migrate to `3.13`, an `allow[update<3.13]` blocks every update until you rewrite the comment. A level-based bound states the policy instead, holding back or keeping updates by how significant they are:

| Directive | Effect | Complement |
| :-------- | :----- | :--------- |
| `ignore[major-update]` | minor and patch updates only | `allow[minor-update]` |
| `ignore[minor-update]` | patch updates only | `allow[patch-update]` |

Pick whichever verb reads best in context. Unlike a specifier bound, a level-based bound anchors to the currently pinned version on every run. So it ratchets along as the reference advances. `ignore[minor-update]` on `python:3.12.1` blocks `3.13` today, and blocks `3.14` once you migrate the pin to `3.13`. The comment never needs editing:

```dockerfile
# update-time: ignore[minor-update]
FROM python:3.12.1-bookworm-slim
```

The levels are positional, not semantic: they refer to the component's position in the version, not to the project's compatibility promises. Projects may ship breaking changes in releases that bump the *second* component. So "stay on Python 3.12" is `ignore[minor-update]`, although Python 3.13 shipped breaking changes: it removed 19 legacy modules from the standard library. The same caution applies to projects using calendar versioning.

And as with specifier bounds, the level applies to a Docker tag's main version. The bound does not affect a version embedded in the suffix, such as the `3.23` in `alpine3.23`. A component the current version doesn't have counts as zero, so `ignore[minor-update]` on `node:22` blocks `22.1`.

#### How a bound interacts with the other markers

A few rules govern how a bound — with a specifier or level-based — interacts with the other markers and checks:

- A bare `# update-time: ignore` (or `# update-time: ignore[update]` with no specifier) holds back *all* updates and wins over any bound on the same reference.
- A bound narrows updates only, not staleness. Staleness is always measured against the project's newest overall release, and the bound does not affect it.
- The hash pin is still added or refreshed for whichever version the bound selects, exactly as without a bound.
- To combine a bound with another directive of the same verb (say, `allow[hash-drift]`), list both as comma-separated items in one bracket: `# update-time: allow[update<3.13, hash-drift]` or `# update-time: allow[minor-update, hash-drift]`. To combine directives of different verbs, list them after the `# update-time:` prefix, separated by a space: `# update-time: ignore[stale] allow[update<3.13]`. A reason can still follow the last directive.

#### Redundant bounds

Update-time logs a redundant bound at `WARNING`. That may happen in two ways:
- Either the bound **never has an effect**, so removing it would change nothing. The current version and every version above it satisfy the bound. Examples are `allow[update>=3.12]` on a `3.12` pin, and `allow[major-update]` on any pin, which allows every update and so says nothing.
- Or the bound **blocks every update**, so it is a frozen `ignore[update]` in disguise. Use `ignore[update]` instead if you intend the freeze. No version above the current one satisfies the bound. Examples are `ignore[update>=3.12]` on a `3.12` pin, and `ignore[patch-update]` on any pin.

On a `requirements.txt` requirement that pins no exact version, Update-time reports every bound as redundant, `ignore[update]` included, since it resolves no update to bound.

### Writing a marker

Where a marker goes depends on the format of the file it sits in, and a run at `--log-level DEBUG` reports which markers Update-time read.

#### Where to put a marker

A marker is written in one of three places, and the file's format decides which:

- **Inline**, on the reference's own line (in YAML files, `requirements.txt`, `devcontainer.json`, `.python-version`, and Sphinx `conf.py` files):

  ```yaml
  image: python:3.12  # update-time: ignore
  ```

  ```text
  humanize==4.15.0  # update-time: ignore
  ```

  ```jsonc
  "ghcr.io/devcontainers/features/node:1": {}  // update-time: ignore
  ```

  ```python
  "https://cdn.jsdelivr.net/npm/clipboard@2.0.11/dist/clipboard.min.js",  # update-time: ignore
  ```

- **On the line directly above** the reference. Use this form in Dockerfiles, which don't allow inline comments:

  ```dockerfile
  # update-time: ignore
  FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
  ```

- **In an `update-time` field**, for a reference in a file that can hold no comment. A `package.json` is strict JSON. So its Node engine's marker names the reference it steers instead of sitting beside it, in a field mirroring the file's own structure:

  ```json
  "update-time": { "engines": { "node": "ignore" } }
  ```

Every reference but the Node engine takes its marker in a comment. Those files are Dockerfiles, Docker Compose and Helm manifests, CircleCI and GitLab CI configs, and GitHub Actions workflows. They also include `.pre-commit-config.yaml` files, `devcontainer.json` files, `requirements.txt` files, `.python-version` files, and the jsDelivr URLs in a Sphinx `conf.py`. Use a `#` comment everywhere except `devcontainer.json` (which is JSONC), where the marker goes in a `//` comment. An inline marker pins only its own line, so it never accidentally pins the reference on the line below it. Where one comment placement is safer than the other, the details per dependency type say so.

A dependency updated through uv, npm, or pnpm takes no marker. Opt one out with a version specifier instead, as described under [Python dependencies](#python-dependencies) and [npm and pnpm dependencies](#npm-and-pnpm-dependencies).

#### Confirming a marker was understood

Run with `--log-level DEBUG` to confirm a marker is recognised. Update-time logs every recognised marker, and every update or warning it holds back, each on a line of its own. Update-time reports a marker it recognised as:

```console
DEBUG Recognised update-time marker ignore[stale] for python in Dockerfile:2
```

That line reports the marker itself, and says it was read and understood. No `Recognised` line at all means the marker was not read. The prefix and the verbs are case-sensitive, and a field marker is read by name. So a typo in any of them leaves the reference updated as usual. Update-time logs a typo inside the brackets at `WARNING` as an invalid item instead (see [Invalid markers](#invalid-markers)).

Update-time reports what the marker held back separately, in lines about the update or the warning rather than about the marker. Each line names the directive it obeyed:

```console
DEBUG Ignoring the staleness warning for python in Dockerfile:2 (update-time: ignore[stale])
```

Such a line appears only when the marker actually held something back. An `ignore[yanked]` on a version that was never yanked produces no such line, and neither does a bound that blocks an update.

## 📖 Details per dependency type

For each dependency type, this chapter answers the same questions: what files, dependencies, and versions Update-time updates. It also says how pinning, the cooldown, staleness, yanks, vulnerabilities, and archival are handled, and where a marker can go. Two sections cover a pair of types that behave alike, so there are fewer sections than types.

### Python dependencies

#### What files are updated?

Update-time discovers Python files containing requirements by name, case-sensitively. Those are `pyproject.toml`, `requirements.txt`, `requirements-<purpose>.txt` and `<purpose>-requirements.txt` (for example `requirements-dev.txt` and `dev-requirements.txt`), and any `.txt` file in a `requirements/` directory. It touches no unrelated file, such as `constraints.txt` or `requirements.in`.

Update-time also updates any `*.py` file that carries a [PEP 723](https://peps.python.org/pep-0723/) inline script metadata block. That is a `# /// script … # ///` comment block declaring the standalone script's dependencies. A `*.py` file without such a block stays untouched and never invokes uv.

Update-time skips compiled or hash-pinned requirements files entirely, such as a `requirements.txt` generated by [pip-tools](https://github.com/jazzband/pip-tools) or `uv pip compile`. Bumping a single pin without recompiling its transitive dependencies and hashes would corrupt the file. Regenerate these with your package manager instead. Update-time recognises compiled or hash-pinned files by their contents, an autogenerated header or `--hash=` lines. It also recognises them by a sibling `.in` file.

#### What dependencies are updated?

Update-time reads the dependency arrays a `pyproject.toml` declares. Those are the `dependencies` array under `[project]`, one array per extra under `optional-dependencies`, and one per group under `dependency-groups`. It also reads uv's legacy `[tool.uv] dev-dependencies`, and the `requires` array under `[build-system]`. It leaves every other string the file holds alone, `[tool.uv] constraint-dependencies` and `override-dependencies` among them.

Update-time cannot update individual git, VCS, and URL dependencies, in a `requirements.txt` or a `pyproject.toml`. Examples are `git+https://github.com/org/repo.git@v8.0.3.0`, direct URLs, and `-e`/editable installs. Update them manually.

Update-time updates a dependency uv resolves through a `[tool.uv] sources` entry like any dependency in the file. Such an entry names a path, a workspace member, or another index. Here too uv resolves the dependency, and Update-time only writes the version uv reports. PyPI serves no release for it, though, so none of the warnings below applies to it.

In a PEP 723 inline script metadata block, Update-time updates only the pins in the `dependencies` array. It leaves the `requires-python` value and any other inline-metadata field untouched.

#### What versions are updated?

Update-time rewrites a dependency's declaration when it pins one exact version with `==`. What the declaration spells around the pin makes no difference. Update-time accepts an extra, an environment marker, and spaces around the operator, and keeps them as written when it rewrites the version. So it rewrites `django[argon2]==3.2.0`, `humanize==4.15.0; python_version < "3.13"`, and `humanize == 4.15.0` each to the new version.

Update-time leaves every other specifier untouched, so you can pin a maximum version to opt a dependency out of automatic updates. A `==` that names more than one version pins none either: `humanize==4.15.*` names a range, `humanize===nightly` is an arbitrary equality, and `humanize==4.15.0,!=4.15.1` combines two specifiers. Update-time leaves each of them alone.

Update-time still reports a new version for a dependency that pins no exact version, both in a `pyproject.toml` and in an inline script metadata block. In a `pyproject.toml` uv also applies it: uv resolves the specifier and records the version it picks in the `uv.lock` file. So `humanize>=4` moves from one release to the next without its line changing. A script has no lock file, so there Update-time reports the new version and nothing more. In a `requirements.txt` it reports no new version, because it resolves none for a requirement that pins no exact version. It still checks that requirement's package for staleness and archival, as described below.

#### Pinning

Update-time adds no hash pin to a Python dependency. A `requirements.txt` pin carries one as a `--hash=` line, which has to hold for the file's transitive dependencies too. So Update-time skips a file that already has them entirely, rather than rewriting it partly. Dependencies in `pyproject.toml` are locked by uv, which records each distribution's hash in the `uv.lock` file it maintains. A PEP 723 script has no lock file, so its pins carry no hash either.

#### Cooldown

For a `requirements.txt` pin, Update-time enforces the cooldown itself, against the release's publication date on PyPI.

For `pyproject.toml` dependencies, Update-time applies the cooldown through uv's `exclude-newer` setting. It writes the setting into the workspace root's `pyproject.toml` under `[tool.uv]`. The value is a relative one such as `exclude-newer = "7 days"`, tagged with a `managed by Update-time` comment. The setting then applies to every uv command in the project (`uv lock`, `uv add`, CI), not just to Update-time.

Update-time keeps its own tagged value in step with `--cooldown`. An `exclude-newer` without the `managed by Update-time` comment stays as it is, and so does a `UV_EXCLUDE_NEWER` environment variable. Remove the comment to take ownership of the line.

For inline script metadata, Update-time also applies the cooldown through uv's `exclude-newer`. It passes the value to `uv tree` on the command line rather than persisting it, since a standalone script has no lockfile to keep reproducible. Update-time derives the cutoff from `--cooldown` on every run, so, unlike `pyproject.toml`, it writes nothing into the `# /// script` block.

#### Stale dependencies

Update-time checks every Python pin against the newest release of its package on PyPI, whichever of the three file kinds declares it. It reports the stale ones. It checks a requirement that pins no exact version as well, wherever the file declares it. The package name alone is enough to find the newest release.

Update-time skips two kinds, because PyPI serves no release to measure them against. The first points at a URL or a git repository. The second is one uv resolves through a `[tool.uv] sources` entry, such as a path or a workspace member. Update-time skips a package published only as `.egg` files too, since PyPI no longer accepts files named that way.

#### Yanked dependencies

Update-time checks each exact pin a Python file declares against [PEP 592](https://peps.python.org/pep-0592/)'s yank metadata on PyPI. It checks the pin whichever of the three file kinds it sits in, and skips a yanked release when picking a new version. The version checked is the one the file holds when the run ends. So Update-time warns about a pin uv held back, although PyPI has a newer release.

Update-time reports a `requirements.txt` pin the run leaves on a yanked release, unless an `ignore[yanked]` marker silences that warning. It reports a `pyproject.toml` or inline script metadata pin left on a yanked release too, but that pin takes no marker to silence the warning. It does not check a dependency those files declare without an exact pin. A yank is about the version a reference is left on, and such a declaration names none. It skips a pin uv resolves through a `[tool.uv] sources` entry as well, since PyPI serves no release for it.

#### Vulnerable dependencies

Update-time checks each exact pin a Python file declares against OSV's PyPI advisories, whichever of the three file kinds it sits in. A compiled or hash-pinned `requirements.txt` is the exception: Update-time skips it whole, so its pins are neither updated nor checked. It does not check the transitive dependencies those pins require. Reading a resolved dependency tree is what `uv audit` and `pip-audit` are for.

Update-time reports a vulnerable `requirements.txt` pin, unless an `ignore[vulnerable]` marker silences that warning. It reports a vulnerable `pyproject.toml` or inline script metadata pin too, but that pin takes no marker to silence the warning. It does not check a dependency those files declare without an exact pin either. An advisory is matched against a version, and such a declaration names none. It skips a pin uv resolves through a `[tool.uv] sources` entry as well, since PyPI serves no release for it.

#### Archived dependencies

PyPI publishes a project status. Update-time reads it for each dependency a Python file declares, whether or not the dependency pins an exact version. Archival is a fact about the project, so the package's name alone is enough to find that status. A compiled or hash-pinned `requirements.txt` is the exception: Update-time skips it whole, so its requirements are neither updated nor checked.

Update-time skips two kinds of dependency as well, since neither names a PyPI project to read a status from. The first points at a URL or a git repository. The second is one uv resolves through a `[tool.uv] sources` entry, such as a path or a workspace member. Update-time reports a `requirements.txt` requirement whose project is archived, unless an `ignore[archived]` marker silences that warning. It reports a `pyproject.toml` or inline script metadata dependency too, but that dependency takes no marker to silence the warning.

#### Markers

Write a marker for a `requirements.txt` pin inline, on the pin's own line, as in `humanize==4.15.0  # update-time: ignore`. See [Controlling updates and warnings per reference](#-controlling-updates-and-warnings-per-reference) for the directives and where they go. Update-time checks a requirement that pins no exact version for staleness and archival. Such a requirement can carry a marker as well: `humanize>=4  # update-time: ignore[stale<1095]` warns once that package's newest release is more than three years old.

Update-time reports a `yanked` or `vulnerable` scope on a requirement that pins no exact version as redundant, since these checks need a pinned version. It does not update such a requirement, so it reports a `cooldown` and a bound as redundant too. Update-time reads no markers in `pyproject.toml` or inline script metadata. Opt a requirement in one of those out of updating by pinning it with a maximum or non-`==` specifier instead, for example `package<=3.12`.

### npm and pnpm dependencies

#### What files are updated?

Update-time looks for `package.json` files recursively from the starting path. The accompanying lock file is updated as well: `package-lock.json` for npm, `pnpm-lock.yaml` for pnpm.

#### What dependencies are updated?

Update-time delegates updating Node dependencies to the package manager that manages the `package.json`. If that is npm, it runs `npm update --save --include=dev`. If it is pnpm, it runs `pnpm update`.

#### What versions are updated?

Because Update-time delegates updating Node dependencies to a package manager, that package manager also chooses the version. Both npm and pnpm update a dependency to the newest version that satisfies the range declared in the `package.json`. So a dependency declared as `"react": "^17.0.0"` receives `17.x` updates, but never moves to `18`, and one pinned to an exact version stays where it is. See the documentation of [npm update](https://docs.npmjs.com/cli/v12/commands/npm-update) and [pnpm update](https://pnpm.io/cli/update) for the finer points of how each manager resolves versions.

#### Pinning

Update-time adds no hash pin to an npm or pnpm dependency. The integrity hash of each resolved package lives in the lock file, which npm and pnpm maintain themselves and Update-time updates by running them.

#### Cooldown

For npm, Update-time passes the cooldown via npm's `min-release-age` option, also measured in days, which npm added in 11.10.0. Older npm versions ignore the option, so updates still run but without a cooldown. If your project already configures a cooldown in its `.npmrc` (`min-release-age` or `before`), Update-time leaves that in place instead of overriding it.

For pnpm, Update-time passes the cooldown via pnpm's `minimumReleaseAge` setting, converting the value to minutes (pnpm measures the age in minutes rather than days). If your project already configures `minimumReleaseAge` (in `pnpm-workspace.yaml`), Update-time leaves that in place instead of overriding it.

#### Stale dependencies

Update-time checks each dependency against its newest release on the npm registry, and reports the stale ones. It skips dependencies given as git, file, link, workspace, alias, or GitHub-shorthand references, since they don't resolve to a registry release.

#### Yanked dependencies

There is no yank on npm. Instead, npm and pnpm handle the per-version deprecation that plays the same role, when they resolve an update. So Update-time doesn't check `package.json` dependencies for it.

#### Vulnerable dependencies

Update-time does not check a `package.json` dependency. It declares a range rather than a version, and the lock file records the version that range resolves to. Update-time does not read that lock file. Auditing it is what `npm audit` and `pnpm audit` are for.

#### Archived dependencies

The npm registry publishes no archival signal, so Update-time does not check a `package.json` dependency. What npmjs.com shows as a deprecated package is a deprecation on the release its `latest` tag points at. That says something about the release rather than about the project.

#### Markers

A `package.json` dependency takes no marker, because npm and pnpm update it rather than Update-time rewriting its lines. Opt one out by declaring an upper bound or an exact version instead, as described under What versions are updated? above.

### GitHub Actions and pre-commit hooks

Both resolve their versions from the same source: a GitHub repository's releases and tags. So they behave the same way, except where the file format differs.

#### What files are updated?

For GitHub Actions, Update-time looks for `*.yml` and `*.yaml` files under the `.github/` folder, recursively. That covers both workflow files (`.github/workflows/*.yml`) and composite action definitions.

For pre-commit hooks, it looks for `.pre-commit-config.yaml` files, recursively from the starting path. Pre-commit reads the file at the repository root, but a monorepo can carry one per sub-project, so Update-time updates every one it finds.

#### What dependencies are updated?

In a workflow or action definition, the actions in the `uses:` references. Actions referenced by a branch (for example `@main`), or as a local action without an `@`, don't resolve to a version. So Update-time does not update them.

In a `.pre-commit-config.yaml`, the `rev:` of each hook repository hosted on GitHub. A `repo: local` or `repo: meta` entry has no `rev:`, so Update-time leaves it untouched, as it leaves a repository hosted outside GitHub untouched. Update-time does not update a `rev:` that names a branch rather than a version.

#### What versions are updated?

Update-time moves a reference given as a version tag to the latest version. Such a tag is `@v4` or `@v4.1.1` for an action, and `v4.5.0` for a `rev:`. It also moves a reference already pinned to a commit SHA with a version comment, such as `@<sha> # v4.1.1` or `rev: <sha>  # frozen: v4.5.0`.

Often, the latest version is the latest GitHub release. But a version that was tagged without being published as a release counts too. So Update-time still updates a repository that only tags its versions, or whose releases stopped while tagging continued.

#### Pinning

Update-time pins an action referenced by version tag only to the commit SHA of the latest version, and adds the version as a trailing comment. So `uses: actions/checkout@v4` becomes `uses: actions/checkout@<sha> # v4.1.1`.

Update-time pins a `rev:` referenced by version tag only to the commit SHA the same way. The version travels in pre-commit's own `# frozen: <version>` comment convention, so `rev: v4.5.0` becomes `rev: <sha>  # frozen: v4.5.0`. This is the same format `pre-commit autoupdate --freeze` produces and understands, so the config stays interoperable with pre-commit's own tooling. Update-time keeps the tag's `v` prefix convention in the comment, so a repository that tags without a `v` gets `# frozen: 4.5.0`.

An action referenced by a branch gets no pin, since it resolves to no version. Neither does a `rev:` that names a branch, or one that is already a bare commit SHA without a `# frozen:` comment.

#### Cooldown

The cooldown is measured against the release's publication date, or, for a version that was only tagged, the date of the commit it tags. Update-time skips a version whose commit date it cannot fetch, rather than adopting it with the cooldown unchecked.

#### Stale dependencies

Staleness is measured against the repository's newest release, whatever the reference names: a version, a branch, or a commit.

#### Yanked dependencies

GitHub has no yank concept, so Update-time checks neither an action nor a hook `rev:` for one. It reports a `yanked` scope on either as redundant.

#### Vulnerable dependencies

Update-time checks neither. OSV does hold advisories for actions, but their affected entries enumerate no versions. So a question to OSV about a version of an action returns nothing, however that version is spelled. Silence there says nothing about whether the action is safe. A hook repository is not an OSV package at all. Update-time reports a `vulnerable` scope on either as redundant.

#### Archived dependencies

Update-time checks both against the `archived` flag GitHub publishes for a repository. Archival is a fact about the repository rather than about one of its versions. So Update-time reads the flag whatever the reference names: a version, a branch, or a commit. GitHub publishes no reason beside the flag, so the warning gives none. Update-time reports an action or a hook whose repository is archived, unless an `ignore[archived]` marker silences that warning.

#### Markers

Both take an inline marker on the reference's own line. In a `.pre-commit-config.yaml`, the marker follows the `# frozen:` comment on the `rev:` line when both are present:

```yaml
rev: <sha>  # frozen: v4.5.0  # update-time: ignore
```

### Node engine version and Python version

Both name the runtime a project runs on, rather than a package it depends on. Both follow the project's Dockerfile where there is one, so the runtime you develop against and the one you ship stay in step.

#### What files are updated?

For the Node engine version, Update-time looks for `package.json` files that specify a [Node engine](https://docs.npmjs.com/cli/v12/configuring-npm/package-json#engines).

For the Python version, it looks for `.python-version` files recursively from the starting path. That covers a repository that pins its Python version at the root, and a monorepo that pins one per package. `.python-version` is the de facto standard for pinning a project's Python version, read by uv, pyenv, and GitHub's `setup-python` action, among others.

#### What dependencies are updated?

The Node engine version in the `package.json`: the `node` entry its `engines` section declares.

In a `.python-version` file, each plain CPython version on a line of its own, `X.Y` or `X.Y.Z`. Examples are `3.12` and `3.12.6`. A file may list several entries, one per line (pyenv reads more than one), each handled independently. Update-time leaves the other entries untouched: alternative implementations (`pypy3.10-7.3.12`, `miniconda3-…`), free-threaded and other variant suffixes (`3.13t`), prefixed forms (`cpython@3.12`, `>=3.10`), and the `system` sentinel.

Update-time leaves other Python version pins untouched too. The `requires-python` value in `pyproject.toml` and in PEP 723 inline script metadata is not a `.python-version` entry, and stays as it is.

#### What versions are updated?

Update-time takes the new version from the matching base image in the project's Dockerfile. That needs a Dockerfile in the same folder, whose base image has a numeric version. When no Dockerfile declares one, Update-time takes the version from the latest [Node](https://hub.docker.com/_/node) or [Python](https://hub.docker.com/_/python) release on Docker Hub.

Update-time updates the Node engine version only when it contains a specific version, for example `26.4`. It leaves a range or other non-numeric value untouched. A Node base image whose tag carries no version to sync to, such as `node:lts` or `node:22.x`, is the exception to following the Dockerfile. Update-time then leaves the engine alone, rather than overriding it with a mismatched concrete version.

Update-time moves a `.python-version` entry forward to a fuller version. The entry adopts the image's version at the precision the tag provides, so `python:3.14.2-slim` yields `3.14.2` and a bare `python:3.14` yields `3.14`. Update-time leaves an entry already ahead of the image alone, rather than downgrading it.

The Node engine goes the other way, since it declares the runtime the project ships. Update-time brings an engine ahead of its base image back to the image's version. Docker Hub always names a full version, so an entry that follows Docker Hub gains precision it didn't have. Both `3.12.6` and `3.12` become `3.13.2`, or whatever the latest is.

#### Pinning

Neither can carry a hash pin. Both name a version rather than one artefact, and a version covers every build ever published for it. So there is no image digest, commit SHA, or integrity hash to add. Neither format has anywhere to put one either: a `package.json` entry names a version, and a `.python-version` line is a bare version. Update-time only moves them to a fuller version, which makes them more precise but verifies nothing.

#### Cooldown

A version taken from Docker Hub honours the cooldown through the Node or Python image tag's push date. A version that instead follows the project's Dockerfile needs no cooldown of its own, since Update-time already applied one when it updated the base image. So Update-time reports a `cooldown` marker on such a reference as redundant, whether it is a `.python-version` entry or a Node engine.

#### Stale dependencies

Both are indirect cases. When the version comes from the project's Dockerfile, Update-time reports the staleness of the base image alone. It checks neither the entry nor the engine itself then, and reports a `stale` marker on either as redundant. It checks a version taken from Docker Hub against the newest Node or Python release there.

#### Yanked dependencies

Neither source has a yank concept, so Update-time checks neither for one. It reports a `yanked` scope on either as redundant.

#### Vulnerable dependencies

Update-time checks neither. Both name a version of a runtime rather than a release of a package, which is not something OSV matches an advisory against. Update-time reports a `vulnerable` scope on either as redundant.

#### Archived dependencies

Update-time checks neither. Both take their version from a Docker base image or from Docker Hub, and no OCI registry publishes an archival signal. Update-time reports an `archived` scope on either as redundant.

#### Markers

A `.python-version` entry takes a marker in either placement. But uv rejects an inline comment on a `.python-version` line: it ignores the entry and silently resolves a different Python. So the line-above form is the safer placement for a uv project:

```text
# update-time: ignore
3.12
```

The Node engine version takes a marker too, but not as a comment: `package.json` is strict JSON, which has nowhere to put one. Its marker goes in an `update-time` field instead, which mirrors the file's own structure. The marker steering the engine therefore sits under `engines` and `node`, as the engine itself does:

```json
{
  "engines": { "node": "22" },
  "update-time": { "engines": { "node": "allow[update<23]" } }
}
```

The field holds the directives a comment holds, without the `# update-time:` prefix that introduces them there. npm and pnpm keep a field they do not know, so the marker survives an `npm update` and a `pnpm update`. Both rewrite the file onto one key per line, so the field appears spread over several lines rather than on the one shown here. Update-time reports a field of the wrong shape as an invalid item, and leaves the engine as it is (see [Invalid markers](#invalid-markers)).

Two kinds of engine are never updated: one declared as a range, and one whose base image tag names no version to sync to. `node:lts` is such a tag. A marker steers an update, so on either engine it has nothing to steer, and Update-time has nothing to report.

Either way a marker wins over a version derived from the Dockerfile. So an image update never drags a deliberately held-back development version forward.

### Docker images

#### What files are updated?

Update-time looks for these files, each with its own globs and in its own folder, searching recursively where the table says so:

| Files | Globs | Folder | Recursive |
| :---- | :---- | :----- | :-------: |
| Dockerfiles | `Dockerfile`, `*.Dockerfile`, `Dockerfile.*` | the scan root | ✅ |
| CircleCI configs | `*.yml`, `*.yaml` | `.circleci/` | ✅ |
| .gitlab-ci.yml | `.gitlab-ci.yml` | the scan root |  |
| Docker Compose files | `docker-compose*.yml` | the scan root | ✅ |
| Helm charts | `*.yml`, `*.yaml` | `helm/` | ✅ |
| devcontainer configs | `.devcontainer.json`, `.devcontainer/devcontainer.json`, `.devcontainer/*/devcontainer.json` | the scan root | ✅ |

#### What dependencies are updated?

| Files | Dependencies |
| :---- | :-----------  |
| Dockerfiles | Base images (`FROM` references) |
| CircleCI configs | Docker images (machine-executor images are left unchanged) |
| .gitlab-ci.yml | Docker images (`image:` references) |
| Docker Compose files | Service images (`image:` references) |
| Helm charts | Container images (`image:` references) |
| devcontainer configs | The base image and each feature |

Update-time reads a Dockerfile's `FROM` the way Docker reads it: in upper or lower case, and only where it opens its line. Neither `FROM scratch` nor a `FROM` naming one of the file's own build stages names an image a registry serves, so Update-time leaves both alone.

#### What versions are updated?

When updating an image tag, Update-time keeps the non-numeric parts of the tag and only advances its version numbers. A tag such as `python:3.14.6-alpine3.23` has three parts: the label prefix `python`, the main version `3.14.6`, and the suffix `alpine3.23`. Update-time preserves the label prefix and the suffix's label, so it never replaces a variant: `python` never becomes `pypy`, and `alpine` never becomes `debian`. It upgrades both the main version and a version embedded in the suffix, independently or together, for example `3.14.6-alpine3.23` → `3.15.0-alpine3.24`. A suffix without an embedded version (`bookworm-slim`, `windows`) is never updated, since it carries no version to advance. It never downgrades one axis to adopt a newer value on the other.

A tag naming a channel in labels alone, such as `latest` or `trixie`, floats. Update-time replaces it with the version tag serving the same image, and keeps the labels the tag itself carries (see [Floating image tags](#floating-image-tags)). A reference that names no tag asks for `latest`, the tag a registry serves by default. So it floats too, and Update-time pins it the same way.

A dated snapshot such as `debian:bookworm-20260803` names the day its image was built. Update-time updates it to the newest snapshot under its label, so a reference on `bookworm-20240110` moves to `bookworm-20260803`. A repository can also name a snapshot by the date alone, as Alpine names one `20260805` beside its `3.24.1` release. Update-time updates such a tag to the newest snapshot as well. A reference on a release keeps following releases, and a reference on a snapshot keeps following snapshots.

#### Pinning

Update-time appends the `@sha256:digest` of the (latest) tag to an image referenced by tag only, so the image is reproducible. This covers base images in Dockerfiles (`FROM image:tag`), CircleCI images, GitLab CI images, Docker Compose and Helm manifest images, and devcontainer base images and features. Update-time takes the image's registry from the reference, so it resolves images on Docker Hub and on other OCI registries (`ghcr.io`, `mcr.microsoft.com`, …) alike.

A floating tag is pinned to both at once: the version tag serving the image it currently resolves to, and that image's digest.

Two kinds of reference get no digest. Update-time ignores an image whose tag it cannot read: a reference through a `{{ ... }}` template or `${VAR}` variable substitution. A CircleCI machine-executor image (the `image:` under a `machine:` key, such as `ubuntu-2204:2024.01.1`) gets none either, since it is not a registry image.

#### Cooldown

Update-time adopts a newer tag only once it is past the cooldown, provided Docker Hub hosts the image. Other registries (`ghcr.io`, `mcr.microsoft.com`, …) expose no publication date. So Update-time updates images there without a cooldown, and reports a `cooldown` marker on one of them as redundant. It reports a `cooldown` marker on a CircleCI machine-executor image as redundant too, since no registry serves that image. Pinning a floating tag adopts no newer image, so the cooldown holds nothing back there (see [Floating image tags](#floating-image-tags)).

#### Stale dependencies

Staleness is measured against the image's newest release, on Docker Hub only, since other registries expose no publication date. Update-time therefore reports a `stale` marker on an image hosted elsewhere as redundant. It reports one on a CircleCI machine-executor image as redundant too, since no registry serves that image.

#### Yanked dependencies

An OCI registry has no yank concept, so Update-time does not check an image for one. It reports a `yanked` scope on an image reference as redundant.

#### Vulnerable dependencies

Update-time does not check an image, because OSV has no ecosystem for container images. Reporting the vulnerabilities of the packages inside an image is what an image scanner is for. Update-time reports a `vulnerable` scope on an image reference as redundant.

#### Archived dependencies

Update-time does not check an image, because no OCI registry publishes an archival signal. It reports an `archived` scope on an image reference as redundant.

#### Markers

In a Dockerfile the marker goes on the line above the `FROM`, since Dockerfiles don't allow inline comments. In the YAML formats — CircleCI, GitLab CI, Docker Compose, and Helm — it can go inline on the image's own line. In a `devcontainer.json` it goes in a `//` comment.

### jsDelivr npm URLs

#### What files are updated?

Update-time looks for Sphinx configuration files (`conf.py`) under the `docs/` folder, recursively.

#### What dependencies are updated?

The jsDelivr npm URLs and their accompanying Subresource Integrity (`integrity`) hash. For example: `https://cdn.jsdelivr.net/npm/clipboard@2.0.11/dist/clipboard.min.js`.

#### What versions are updated?

Update-time updates the npm package version embedded in the URL to the latest version on the npm registry. It updates the SRI hash in step, so the two stay consistent.

#### Pinning

A URL whose attribute dictionary declares no `integrity` entry gains one, so the browser verifies the script the CDN serves before running it. Update-time inserts the hash in front of the entries the dictionary already has, and reports it as a pin: `Pinned clipboard in docs/conf.py:4 to 2.0.11@sha256-…`.

A URL declared as a bare string, without an attribute dictionary, has nowhere to hold an integrity hash, so it stays without one. Adding a hash would mean rewriting the string into a `(url, {"integrity": …})` tuple, which is more than rewriting a line. So Update-time logs it at `INFO` and leaves it alone. Declare the URL as such a tuple to have it pinned.

#### Cooldown

Update-time adopts a newer version only once it is past the cooldown, measured against its publication date on the npm registry.

#### Stale dependencies

Update-time checks the URL's package against its newest release on the npm registry.

#### Yanked dependencies

There is no yank on npm, but a per-version deprecation is the same signal, so Update-time reports it in the same wording. It skips a deprecated version when picking a new one, and warns about a URL left on a deprecated version.

#### Vulnerable dependencies

Update-time checks the version in the URL against OSV's npm advisories. It warns about a URL the run leaves on a version an advisory names, unless an `ignore[vulnerable]` marker silences that warning.

#### Archived dependencies

Update-time does not check a jsDelivr URL, since the npm registry publishes no archival signal. It reports an `archived` scope on a URL as redundant.

#### Markers

A jsDelivr URL takes an inline marker in a `#` comment on its own line in `conf.py`.

## 📮 Point of contact

Point of contact for this repository is [Frank Niessink](https://github.com/fniessink).
