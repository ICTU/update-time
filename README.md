# Update-time - it's time to update your dependencies

[![PyPI](https://img.shields.io/pypi/v/update-time?logo=pypi&logoColor=white)](https://pypi.org/project/update-time/) [![Python versions](https://img.shields.io/pypi/pyversions/update-time?logo=python&logoColor=white)](https://pypi.org/project/update-time/) [![License](https://img.shields.io/pypi/l/update-time)](https://github.com/ICTU/update-time/blob/main/LICENSE)

Keeping dependencies up-to-date is an important aspect of software maintenance. Update-time is a command line tool that scans your repository for [dependencies](#-what-is-updated) and updates them to their latest versions. Where possible, it [pins](#-pinning) references — no more `latest` — and adds hashes. To protect against supply chain attacks, it applies a [cooldown](#-cooldown) period. And it warns you about [stale dependencies](#-stale-dependencies), [yanked versions](#-yanked-dependencies), and [vulnerable dependencies](#-vulnerable-dependencies).

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
                   [--ignore-vulnerability IDS] [--exclude-path PATHS]
                   [--allow-hash-drift] [--allow-floating-pin] [--force]
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
| ⚠️ | `WARNING` | what needs your attention: a [stale](#-stale-dependencies), [yanked](#-yanked-dependencies), or [vulnerable](#-vulnerable-dependencies) dependency, [hash drift](#hash-drift), a source it could not reach, a marker that is invalid, incorrect, or redundant |
| ❌ | `ERROR` | failures that stop an update, such as a package manager that is not installed |

### Workflow

The recommended workflow is to run Update-time on a dedicated branch, push it, and let CI do the verification:

1. Create a branch for the updates.
2. Run `update-time` in the root of your repository to update the dependencies in place.
3. Commit the changes and open a pull request.
4. Let your tests and checks run in CI to confirm nothing is broken before merging.

Because Update-time rewrites files in place, it expects to make updates inside a git repository, allowing for changes to be reverted. By default, Update-time refuses to run when the directory to scan is not inside a git repository; printing an error and exiting with a non-zero status without touching any files. Pass `--force` to override.

> [!NOTE]
> Being inside a repository only guarantees revertability relative to the last commit. Update-time does not check for uncommitted edits before running.

### Increasing rate limits

To raise API rate limits while updating, set the following environment variables before running Update-time:

- `GITHUB_TOKEN` — increases the GitHub API rate limit when updating GitHub Actions. The token only needs to read public release and commit data, so no specific scope is required: both a classic token with no scopes selected and a fine-grained token with default read-only access to public repositories work.
- `DOCKER_HUB_USERNAME` and `DOCKER_HUB_TOKEN` — authenticate to the Docker Hub API (both must be set) to increase its rate limit when updating Docker images.

## 🔄 Updating

Update-time scans your repository for version pins, moves each one to the newest version its source offers once that version is past the cooldown, and adds a hash pin wherever the reference can hold one.

### 📦 What is updated

Update-time updates the following types of dependencies, found in the listed files, and using the listed sources. Where a package manager manages the dependencies, Update-time works through that package manager. Other dependency types it updates itself:

| Dependency type | Files | Source | Updated by |
| :-------------- | :---- | :----- | :--------- |
| [Python dependencies](#python-dependencies) | `pyproject.toml`, `requirements.txt`, and PEP 723 inline script metadata (`# /// script` blocks in `*.py` files) | [PyPI](https://pypi.org) | Update-time for `requirements.txt`; for the other two, uv resolves the versions and Update-time writes the pins |
| [npm and pnpm dependencies](#npm-and-pnpm-dependencies) | `package.json` (and their lock files) | [npm registry](https://registry.npmjs.org) | delegated to npm or pnpm, whichever manages the `package.json` |
| [Node engine version](#node-engine-version-and-python-version) | `package.json` | the Node base image in the project's Dockerfile, or the latest [Node](https://hub.docker.com/_/node) release on Docker Hub | Update-time |
| [Python version](#node-engine-version-and-python-version) | `.python-version` | the Python base image in the project's Dockerfile, or the latest [Python](https://hub.docker.com/_/python) release on Docker Hub | Update-time |
| [Docker images](#docker-images) | Dockerfiles, CircleCI configs, `.gitlab-ci.yml`, Docker Compose files, Helm charts, and devcontainer configs | OCI registries ([Docker Hub](https://hub.docker.com), `ghcr.io`, `mcr.microsoft.com`, …) | Update-time |
| [GitHub Actions](#github-actions-and-pre-commit-hooks) | YAML files under `.github/` | [GitHub API](https://api.github.com) | Update-time |
| [Pre-commit hooks](#github-actions-and-pre-commit-hooks) | `.pre-commit-config.yaml` | [GitHub API](https://api.github.com) | Update-time |
| [jsDelivr npm URLs](#jsdelivr-npm-urls) | Sphinx config | [npm registry](https://registry.npmjs.org) | Update-time |

Dependency types that Update-time updates itself, it rewrites line by line: it picks the new version from the source and edits the reference in place. Dependency types it delegates, it hands to uv, npm, or pnpm, which resolves the versions itself and keeps the project's lock file in step where there is one. Update-time runs that package manager for you, so a delegated dependency is updated by the same `update-time` run as every other one. A Python dependency pinned with `==` is updated by both: uv resolves its new version, and Update-time writes that version into the `pyproject.toml` or the `# /// script` block. Two things follow for a delegated dependency: it takes no [marker](#-controlling-updates-and-warnings-per-reference), and the [cooldown](#-cooldown) is handed to the manager, which applies it per run rather than per reference.

Each type links to its own section under [Details per dependency type](#-details-per-dependency-type), which covers the files and dependencies it updates, and how pinning, the cooldown, staleness, yanks, vulnerabilities, and markers apply to it.

### 📌 Pinning

*Pinning* means specifying exactly what a reference should resolve to, rather than leaving that to whatever its source serves at the time. Two things can be pinned: the version a reference resolves to, and the artefact that the version resolves to.

A **version pin** names an exact version instead of something that floats: `python:3.14` instead of `python:latest`, `humanize==4.15.0` instead of `humanize>=4`. Its opposite is a **floating pin**, which leaves the version to the source: a channel such as `python:latest`, a range such as `humanize>=4` or npm's `^17.0.0`, and a branch such as the `main` in `actions/checkout@main`. Update-time updates version pins, and replaces the floating pin of an image reference with the version it currently serves (see [Floating image tags](#floating-image-tags)). The floating pin of any other reference is left as it is.

A **hash pin** adds a cryptographic hash of the artefact the version resolves to — an image digest, a commit SHA, or an integrity hash. The difference with a version pin is immutability: a version pin can be re-pointed under you, because a tag can be moved or re-pushed, while a hash pin can only match the one thing it was computed from. That is what protects against a supply chain attack, and Update-time strives to add a hash pin where possible.

Update-time works on both version pins and hash pins: it moves a version pin forward, taking the most precise spelling the source has for the version it lands on, and adds a hash pin to any reference that can hold one.

#### Floating image tags

A floating image tag pins no version: what a build pulls is whatever the registry serves under that tag on the day it runs. Update-time replaces it with the version and digest it serves at the time of the run, which is the image the build already pulls today:

```console
INFO Pinned python in Dockerfile:1 to 3.14.7@sha256:…
```

A reference that names no tag floats the same way: `FROM python` and `image: redis` ask for whatever their registry serves under `latest`. Update-time pins such a reference to the version and digest that tag resolves to, writing the tag after the image's name, so `FROM python` becomes `FROM python:3.14.7@sha256:4fad23465a06cc5149a541fbec6f87e234a64dc0550f6bfdd2d290d8f03240df`.

Registries name one image under several tags, so a floating tag shares its digest with the version tags of the same push: `python:latest` serves the same image as `python:3.14.7`, `python:3.14`, and `python:3`. Those are the versions the reference can be pinned to, and Update-time picks between them by keeping the labels of the tag first and its precision second:

1. A label the floating tag shares with a version tag is kept, so `node:trixie` lands on `26.7.0-trixie` rather than on `26.7.0`, and `python:slim` on `3.14.7-slim`. A label naming a channel is dropped — `latest`, `lts`, `stable`, and `edge` are labels of that kind — because a tag carrying one floats on: `node:lts-alpine` lands on `24-alpine` rather than on the `24-lts` that follows whichever 24 release is the LTS one. So is a label no version tag uses at all, since requiring it would leave nothing to pin to.
2. The most precise version wins, so `3.14.7` is preferred over `3.14` and `3`.
3. Then the most precise version of a variant the tag asked for, so `node:lts-alpine` lands on `24.19.0-alpine3.24` rather than on `24.19.0-alpine`.
4. The shortest name comes last, so `node:latest` lands on `26.7.0` rather than on `26.7.0-trixie`, and `amazoncorretto:latest` on `8` rather than on `8-al2023`. This prevents adopting a label the reference never asked for.

Because the pin names the image the reference already serves, the [cooldown](#-cooldown) holds nothing back: the image pinned is the one the project already runs. A [bound](#bounding-an-update) decides nothing either, so `allow[update<3.13]` on `python:latest` still pins the `3.14.7` that tag serves. From the next run on the reference is a version pin like any other, and is updated, bounded, and checked as one.

> [!IMPORTANT]
> A pin cannot follow a channel: `node:lts` pinned to `24.19.0` is a pin on the 24 line, and later runs move it to whatever version is newest, LTS or not.

Use a marker to keep a reference floating (see [Keeping a tag floating](#keeping-a-tag-floating)).

How the version tags sharing that image are found depends on the registry. Docker Hub lists the digest of every tag, so a single listing names them all. Another registry lists tag names only, so Update-time asks it for one tag's digest at a time, the newest version first, and stops at the first tag serving the same image. That resolves the common case in a handful of requests, and gives up once it has asked about the newest version tags without finding a match.

Four kinds of floating tag are left as they are:

- A tag whose image carries no version tag under the tag's own label, such as an image tagged only `dev` or `prod`.
- A tag on another registry whose image none of the version tags Update-time asked about serves.
- A tag whose registry serves no manifest for it, so what it serves cannot be read.
- A tag listed further down a large repository's tag list than Update-time reads.

Each is reported at `DEBUG`, in a line naming the reason the tag was left:

```console
DEBUG Floating tag acme/api:dev in docker-compose.yml:7 was left as it is: no tag naming a version serves the same image
DEBUG Floating tag acme/api:nightly in docker-compose.yml:7 was left as it is: its tag is not among the tags listed for the image
DEBUG Floating tag ghcr.io/acme/api:latest in Dockerfile:1 was left as it is: no tag naming a version among the newest examined serves the same image
DEBUG Floating tag ghcr.io/acme/api:edge in Dockerfile:1 was left as it is: the registry serves no manifest for its tag, so what that tag serves is unknown
```

#### Version precision

When a version pin is updated, and the source offers a more precise new version, that version is chosen. For example, when `python:3.12` is updated and both `python:3.13` and `python:3.13.0` are available, `python:3.13.0` is applied. Every kind of version pin gains precision this way: a `.python-version` entry of `3.12` becomes `3.13.2`, and `actions/checkout@v4` moves to the exact version that tag resolves to.

However, more precision is not guaranteed: if a newer version is less precise, it is still applied. For example, if `python:3.12.1` is the current version pin and `python:3.13` is available, but no `python:3.13.0`, the version pin moves to `python:3.13`.

Precision is only gained by updating, so a more precise spelling of the version a pin already names is left alone: `python:3.12` stays as it is when the newest matching tag is `3.12.0`, and so does `humanize==4.15` as long as `4.15.0` is the latest release.

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

Sometimes a reference already carries a hash pin and only what it points at has changed. Update-time warns about that and leaves the pin unchanged, so a changed target is never silently adopted (which would defeat the immutability a pin exists to provide). It takes three forms, one per kind of hash pin:

```console
WARNING Digest drift for python:3.14 in Dockerfile:1: pinned to sha256:… but the registry now serves sha256:…; the pin was left unchanged, verify the change is expected before updating the pin
WARNING Tag drift for actions/checkout@4.1.1 in .github/workflows/ci.yml:17: pinned to commit … but the tag now points at …; the pin was left unchanged, verify the tag was moved deliberately before updating the pin
WARNING Integrity hash mismatch for clipboard@2.0.11 in docs/conf.py:4: declares sha256-… but jsDelivr serves sha256-…; the hash was left unchanged, and since npm does not republish a version it is probably the declared hash that is wrong
```

*Digest drift* means an image tag was re-pushed (rebuilt) under the same name and version, so the registry now serves a different digest. A floating tag that already carries a digest is judged the same way. When the digest is the one its tag still serves, the reference is pinned to the version that serves it. When it differs, the tag was re-pointed after the reference was pinned, so the pin stands and the drift is warned about.

*Tag drift* means the version tag of a GitHub Action or pre-commit hook was moved onto another commit than the one the reference pins — a git tag is mutable, so whoever controls the repository can move it. This is what pinning to a commit SHA exists to catch: the pin keeps the reference on the commit it was pinned to whatever the tag does, which is the point, and the warning tells you that tag and pin have parted company.

An *integrity hash mismatch* means the hash a jsDelivr URL declares is not the one jsDelivr serves for the version the URL sits on. Unlike the other two this rarely means anything upstream changed, since npm does not allow a published version to be republished. More likely, the declared hash is wrong; mistyped, copied from another file, or tampered with. It is also the most urgent, because the browser silently refuses to load the script until the hash matches, which no build step catches.

A reference without hash pin has nothing that can drift, so a `requirements.txt` pin, a `.python-version` entry, and the Node engine version are not checked; nor are the dependencies Update-time updates through a package manager such as uv or npm.

To adopt the new value, opt the reference in with a marker (see [Controlling updates and warnings per reference](#-controlling-updates-and-warnings-per-reference)): an image reference then adopts the re-pushed digest, and a GitHub Action or pre-commit hook adopts the commit its tag was moved to. Alternatively, pass `--allow-hash-drift` to opt every reference in the scan in at once. A marker that holds the reference back wins over both, so a reference you deliberately froze is never re-pinned. Adopted drift is logged at `INFO`, like any other change.

An integrity hash mismatch is never adopted, whatever you opt in to: the whole point of the hash is to refuse content that doesn't match it, so Update-time reports it and leaves correcting it to you.

### ⏳ Cooldown

To avoid adopting releases that are too fresh to trust, Update-time honours a cooldown period during which newly published versions are not yet picked up. It defaults to **7 days** and can be changed with the `--cooldown` option, for example `update-time --cooldown 14`. A single reference can carry a cooldown of its own, which wins over whatever `--cooldown` says (see [Controlling updates and warnings per reference](#-controlling-updates-and-warnings-per-reference)). Who applies it depends on the dependency type:

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

Where a package manager applies it, Update-time hands the value to uv, npm, or pnpm, each of which takes it per run rather than per dependency, and each of which leaves a cooldown your project already configures in place. What that means per type is described under [Python dependencies](#python-dependencies) and [npm and pnpm dependencies](#npm-and-pnpm-dependencies).

## ⚠️ Warnings

Update-time also reports what updating cannot put right: a dependency whose newest release is years old, a version its maintainer withdrew, and a version a security advisory names. These warnings are informational, so they change no file and do not affect the exit status.

### 🕸️ Stale dependencies

Update-time warns when a dependency's newest release is older than a threshold, which may mean the project was abandoned or superseded. The threshold defaults to **365 days** and is set with `--stale-after DAYS`; pass `--stale-after 0` to disable the check. A single reference can carry a threshold of its own, which wins over whatever `--stale-after` says (see [Controlling updates and warnings per reference](#-controlling-updates-and-warnings-per-reference)). For example, a pin whose newest release came out well over a year ago is reported as:

```console
WARNING Stale dependency humanize in docs/requirements.txt:12: newest release 4.15.0 was published 512 days ago (> 365)
```

The date compared against the threshold is the publication date of the dependency's *newest* release. This way a project that has just published a release is never reported as stale, not even when that release is still within the [cooldown](#-cooldown) window. A reference can also be left out of the check altogether by a marker, which is a separate choice from holding back its updates (see [Controlling updates and warnings per reference](#-controlling-updates-and-warnings-per-reference)).

Every kind of dependency Update-time updates is checked for staleness, against the date its own source reports:

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

A yank means "stop using this": a release the maintainer withdrew because it was broken, botched, or insecure. An exact pin keeps installing one anyway, so Update-time warns when the version a dependency is pinned to has been yanked. The maintainer's reason is included when they gave one:

```console
WARNING Yanked dependency humanize in docs/requirements.txt:12: version 4.15.0 was yanked ("accidentally broke Python 3.10 support")
```

When no reason was given, the message reports `(reason not specified)` instead.

The warning is given only when the run leaves the reference on the yanked version. This happens when the replacement is still within the [cooldown](#-cooldown), a marker holds it back, a package manager left the pin where it was, or the yanked release is the newest one. A reference can also be left out of the check altogether by a marker (see [Controlling updates and warnings per reference](#-controlling-updates-and-warnings-per-reference)).

Which dependencies are checked follows from where a yank can be observed. PyPI reports one as [PEP 592](https://peps.python.org/pep-0592/) yank metadata. On npm there is no yank, but a per-version *deprecation* is the same signal, and is reported in the same wording as one. Where a withdrawal can be observed, that version is skipped when picking a new one, and a reference left on it is warned about:

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

Update-time looks the pinned version up in the [OSV](https://osv.dev) database, which aggregates GitHub's advisory database, PyPA's, and others, and warns when an advisory names that version as affected. It names the risk level, what the advisory says, and where to read the advisory in full:

```console
WARNING Vulnerable dependency django in docs/requirements.txt:12: version 3.2.0 has a critical vulnerability, "SQL Injection in Django" (GHSA-2gwj-7jmv-h26r, https://osv.dev/GHSA-2gwj-7jmv-h26r)
```

Several of the databases that OSV aggregates carry the same vulnerability, each under an identifier of its own, so OSV answers with an advisory per database. A vulnerability is warned about once, whichever of them reported it, because the advisories of one vulnerability name each other's identifiers and Update-time reads them as one. The databases rate a vulnerability independently, and only some rate it at all, so the warning names the advisory that rates it most severely, and the vulnerability is reported and filtered at that level.

The risk level is the one the advisory's reviewers gave it: `low`, `moderate`, `high`, or `critical`. Where they gave none, the level is derived from the advisory's CVSS base score, banded the same way, so `0.1` to `3.9` is low, `4.0` to `6.9` moderate, `7.0` to `8.9` high, and `9.0` to `10.0` critical. An advisory carrying both a CVSS v3 and a v4 vector is read at its v4 score, the newer of the two assessments. An advisory whose risk level Update-time cannot read at all is reported as `a vulnerability of unknown severity`. An advisory that gives no summary, which many do not, is reported without the quotation; its id and URL say which vulnerability it is.

A reference marked `# update-time: ignore[vulnerable]` is still looked up at OSV, and what OSV answers is silenced rather than warned about. It is looked up because the answer is the only thing that can tell you the marker has gone stale: a suppression outlives the vulnerability it was written for, and without the lookup there is nothing to compare it against. When the version turns out to have no vulnerability at all, Update-time reports the marker as holding nothing back:

```console
WARNING Redundant update-time directive ignore[vulnerable] for django in docs/requirements.txt:12: version 4.2.0 has no vulnerability
```

Run with `--log-level DEBUG` to see what the marker silenced. To silence one advisory rather than every one, name it in the marker (see [Silencing specific vulnerabilities](#silencing-specific-vulnerabilities)). A reference marked with a bare `# update-time: ignore` is not looked up at OSV at all, since that marker holds every check back and queries no source whatsoever. A reference frozen with `# update-time: ignore[update]` is checked and warned about as usual, so a pin you deliberately hold back keeps telling you that its version is vulnerable (see [Controlling updates and warnings per reference](#-controlling-updates-and-warnings-per-reference)).

The version checked is the one the run leaves the reference on: the new version when the reference moved, the current version when it did not. So a vulnerability the run updated away from is never reported, and one the run updated into always is. Only the version a dependency is pinned to is checked, since auditing the transitive dependencies in a lock file is what `uv audit`, `pip-audit`, and `npm audit` are for.

To silence one advisory across the whole scan, rather than on the one reference that carries a marker, pass `--ignore-vulnerability`, which takes a comma-separated list: `--ignore-vulnerability GHSA-2gwj-7jmv-h26r,CVE-2021-31542`. It names an advisory the way a marker does, so any identifier the vulnerability is known by will do, and what it silenced is logged at `DEBUG`. Where a reference's own marker silences the same advisory, the marker is the one reported.

Every risk level is warned about by default. To hear only about the more severe ones, raise the threshold with `--vulnerability-level`, for example `--vulnerability-level high`. A vulnerability whose risk level Update-time cannot read is warned about whatever the threshold is, since leaving the vulnerabilities nobody has rated out of the warnings would hide exactly the ones nobody has looked at.

Looking a pin up sends its package name and version to OSV. Pass `--vulnerability-level none` to switch the check off, which stops those requests altogether. A reference that sets a level of its own is still looked up though (see [Setting a risk level](#setting-a-risk-level)).

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

## 🎛️ Controlling updates and warnings per reference

Markers of the form `# update-time: <directive>` let you steer what happens to an individual reference — to hold it back, but also to bound how far it may move, or to opt it into behaviour that is off by default. A marker is a comment wherever the file can hold one, and a field where it cannot (see [Where to put a marker](#where-to-put-a-marker)). To stop Update-time from changing a specific reference, add an `# update-time: ignore` comment (all lower-case). You might do this because of a known incompatibility, a deferred migration, or to keep something reproducible. The reference is then left untouched and no registry or source is queried for it. You can add a reason after the marker, for example `# update-time: ignore (pinned until the 3.13 migration)`.

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

A marker is the `# update-time:` comment up to its last directive, and holds one or more directives, each a verb and the bracket that may follow it. Directives that use the same verb can be combined: `ignore[yanked, stale<90]` is equivalent to `ignore[yanked] ignore[stale<90]`. Free text after the last directive is a reason, which Update-time ignores. In a file that can hold no comment the directives are the value of an `update-time` field instead, and everything below reads the same (see [Where to put a marker](#where-to-put-a-marker)).

Zooming in on brackets:

```text
ignore[yanked, stale<90]
       └─┬──┘  └─┬─┘└┬┘
         │       │   └─ threshold: what the item sets for this reference, here 90 days
         │       └─ scope: what the item steers, here the staleness warning
         └─ item: a scope named alone holds it back outright, here to not warn about yanked versions
```

A bracket holds one or more items, separated by commas: the `ignore` bracket above holds the two items `yanked` and `stale<90`, and the `allow` bracket holds the single item `update<5`. An item names the scope it steers — the update, the cooldown, the hash drift, or one of the three warnings — and may set what that scope runs at.

| Term | What it is | Example |
| :--- | :--------- | :------ |
| Marker | the directives steering one reference: a `# update-time:` comment, up to its last directive, or a field's value | `# update-time: ignore[stale] allow[update<3.13]` |
| Directive | a verb and the bracket it may carry; a marker holds as many directives as you write | `ignore[stale<90]` |
| Verb | `ignore` drops what its items name and `allow` keeps it, so `ignore[stale<90]` and `allow[stale>=90]` say the same thing | `ignore`, `allow` |
| Bracket | what a directive's `[…]` holds: its items, separated by commas | `[yanked, stale<90]` |
| Item | one entry in a bracket: a scope, a scope with a threshold, a [bound](#bounding-an-update), or an [advisory](#silencing-specific-vulnerabilities) | `stale<90` |
| Scope | what an item steers: `update`, `cooldown`, `stale`, `yanked`, `vulnerable`, `hash-drift`, or `floating-pin` | `ignore[yanked]` steers the yank warning |
| Bare `ignore` | the verb with no bracket at all, which holds back every scope it can without naming one | `# update-time: ignore` |
| Reason | free text after the last directive, which Update-time keeps none of | `(pinned until the 3.13 migration)` |

A marker wins over the command-line option that sets the same thing, whatever that option is set to. An item that sets a value — a threshold, a cooldown, a risk level, or a bound — is written once per reference, and the result of pairing two, of either verb, is undefined.

### Holding a reference back

By default the marker holds a reference back from version updates, the [staleness](#-stale-dependencies) check, the [yank](#-yanked-dependencies) check, and the [vulnerability](#-vulnerable-dependencies) check. Add a bracketed scope to narrow it to just one:

| Marker | Version update | ⚠️ Staleness warning | 🚫 Yank warning | 🛡️ Vulnerability warning |
| :----- | :------------- | :---------------- | :----------- | :-------------------- |
| `# update-time: ignore` | held back | held back | held back | held back |
| `# update-time: ignore[update]` | held back | still checked | still checked | still checked |
| `# update-time: ignore[stale]` | applied | held back | still checked | still checked |
| `# update-time: ignore[yanked]` | applied | still checked | held back | still checked |
| `# update-time: ignore[vulnerable]` | applied | still checked | still checked | held back |

So `# update-time: ignore[update]` keeps a deliberately pinned reference frozen while still telling you when the project behind it has gone quiet or its version was withdrawn, `# update-time: ignore[stale]` silences a staleness warning you've acknowledged without freezing the version, and `# update-time: ignore[yanked]` does the same for a yank you have decided to live with. `# update-time: ignore[vulnerable]` silences the vulnerability warning for one you have assessed, while the reference keeps updating. A reason can still follow the scope, for example `# update-time: ignore[update] (pinned until the 3.13 migration)`.

#### Setting a staleness threshold

`ignore[stale]` silences the staleness warning altogether. To keep the warning but on a different schedule, give the scope a number of days: `# update-time: ignore[stale<90]` warns once that reference's newest release is more than 90 days old, and is a per-reference `--stale-after 90`. Use it for a critical dependency you want to hear about early, or for a low-churn library that shouldn't be flagged for years:

```text
humanize==4.15.0  # update-time: ignore[stale<90] (critical, warn early)
```

```dockerfile
# update-time: ignore[stale<1095]
FROM python:3.12
```

The threshold applies to the reference carrying it, and every other reference in the scan keeps the global one. It wins over `--stale-after`, `--stale-after 0` included, so disabling the check globally still leaves a reference with its own threshold checked. To disable the check for one reference, `ignore[stale<0]` does what `--stale-after 0` does globally, and `ignore[stale]` is the plainer way to spell it.

`allow` and `ignore` are complements here as elsewhere, so `allow[stale>=90]` sets the same 90-day threshold as `ignore[stale<90]`. Inverting the operator would warn while a release is fresh and go quiet once it is old, so neither `allow[stale<90]` nor `ignore[stale>=90]` sets a threshold. Update-time logs an inverted comparison at `WARNING` and holds nothing back, so the reference updates as usual and the global threshold applies to it:

```console
WARNING Incorrect 'stale>=90' in the update-time marker for python in Dockerfile:2: this comparison warns while a release is fresh and goes quiet once it is old, so it sets no threshold
```

A day count must be a whole number of days, so `ignore[stale<-5]` and `ignore[stale>=1.5]` are reported as invalid and leave the reference unchanged. An unreadable count is judged before the direction, so `ignore[stale>=1.5]` is reported as an unreadable count rather than as an inverted comparison. Where a reference carries both a threshold and a bare `ignore[stale]`, the `ignore[stale]` wins and the warning is suppressed whatever the threshold says.

Staleness is measured against the publication date of a dependency's newest release. Where the reference's own source reports no such date, Update-time reports the marker as holding nothing back, and the reference is updated as usual:

```console
WARNING Redundant update-time directive ignore[stale<90] for ghcr.io/astral-sh/uv in Dockerfile:2: this dependency's source reports no publication date to measure staleness against
```

Three kinds of reference get that warning. An image on a registry other than Docker Hub does, since only Docker Hub reports a push date, so the same marker on a Docker Hub image is left alone. So does a CircleCI machine-executor image, which no registry serves. And so does a runtime version that follows the project's Dockerfile, whether it is a `.python-version` entry or a Node engine. The staleness reported for it is the base image's, not its own. A bare `ignore[stale]` is reported there too, since it silences a warning those references never get.

#### Setting a cooldown period

The [cooldown](#-cooldown) holds back releases that are too fresh to trust. To put one reference on a different window from the rest, give a `cooldown` scope a number of days: `# update-time: ignore[cooldown<30]` drops update candidates published less than 30 days ago, and is a per-reference `--cooldown 30`. Use it for a dependency you have been burned by, or one you trust enough to adopt sooner than the rest:

```text
some-flaky-lib==2.1.0  # update-time: ignore[cooldown<30] (burned by 2.0.0)
```

```dockerfile
# update-time: ignore[cooldown<30]
FROM python:3.12
```

The cooldown applies to the reference carrying it, and every other reference in the scan keeps the global one. It wins over `--cooldown`. `allow` and `ignore` are complements here as elsewhere, so `allow[cooldown>=30]` sets the same 30-day window as `ignore[cooldown<30]`. To adopt new releases for one reference as soon as they ship, write `allow[cooldown>=0]` or `ignore[cooldown<0]`: a zero-day window holds nothing back, which is what `--cooldown 0` means globally.

Inverting the operator would adopt a release only while it is fresh and hold it back once it is old, so neither `allow[cooldown<30]` nor `ignore[cooldown>=30]` sets a cooldown. Update-time logs an inverted comparison at `WARNING` and holds nothing back, so the reference updates as usual and the global cooldown applies to it:

```console
WARNING Incorrect 'cooldown>=30' in the update-time marker for python in Dockerfile:2: this comparison adopts a release only while it is fresh and holds it back once it is old, so it sets no cooldown
```

A bare `ignore[cooldown]` can be understood in two ways: adopt at once, or never adopt at all. Rather than guess, Update-time reports it as invalid and leaves the reference unchanged. `allow[cooldown]` is reported the same way. Write `allow[cooldown>=0]` to adopt at once, and `ignore[update]` to freeze the reference. A day count must be a whole number of days, so `ignore[cooldown<-5]` and `ignore[cooldown<1.5]` are reported as invalid too.

The override reaches the dependencies whose cooldown Update-time enforces itself. It does nothing for the dependencies handed to uv, npm, or pnpm, which take a cooldown per run rather than per dependency (see [Cooldown](#-cooldown)). Where the reference's own source reports no publication date to measure a cooldown against, Update-time reports the marker as holding nothing back, and the reference is updated as usual:

```console
WARNING Redundant update-time directive ignore[cooldown<30] for python in .python-version:2: this dependency's source reports no publication date to measure a cooldown against
```

The same three kinds of reference get that warning as for staleness (see [Setting a staleness threshold](#setting-a-staleness-threshold)), and for the same reasons but one. A runtime version that follows the project's Dockerfile gets it, whether it is a `.python-version` entry or a Node engine, because its cooldown was already applied when the base image was updated. An image on a registry other than Docker Hub does too, since only Docker Hub reports a push date to measure a cooldown against, so the same marker on a Docker Hub image is left alone. So does a CircleCI machine-executor image, which no registry serves.

A `requirements.txt` requirement that pins no exact version is reported for a reason of its own, in its own words: PyPI dates its releases, but Update-time resolves no update for such a requirement, so a cooldown holds no release back.

#### Silencing specific vulnerabilities

`ignore[vulnerable]` silences every [vulnerability](#-vulnerable-dependencies) warning a reference gets. To silence just one of them — a vulnerability you have assessed and decided to live with — name the advisory after the scope:

```text
django==3.2.0  # update-time: ignore[vulnerable=GHSA-2gwj-7jmv-h26r] (assessed, we don't use the affected query API)
```

Any identifier the vulnerability is known by will do: OSV holds an advisory per database, each under an id of its own, so a marker naming the `CVE-…` silences a warning reported under the `GHSA-…`.

To silence a second advisory, add a second item: `# update-time: ignore[vulnerable=GHSA-2gwj-7jmv-h26r, vulnerable=CVE-2021-31542]`. The comma separates the bracket's items, so each identifier needs a `vulnerable=` of its own: `ignore[vulnerable=GHSA-…,CVE-…]` reads the second identifier as an item, reports it as invalid, and leaves the reference unchanged.

When none of the version's vulnerabilities answers to the identifier — the vulnerability was fixed by an update, or the identifier was mistyped — Update-time reports the marker as holding nothing back:

```console
WARNING Redundant update-time directive ignore[vulnerable=CVE-2022-28346] for django in docs/requirements.txt:12: version 4.2.0 has no such vulnerability
```

A marker naming several advisories is judged as one, so it is reported only once none of them names a vulnerability the version has.

Only `ignore` names an advisory here. `allow` naming one would keep that warning and drop the warning about every other advisory, which is not a rule the language offers, so `allow[vulnerable=GHSA-…]` is reported as an invalid item and the reference is left unchanged.

The reference keeps updating, and every other advisory affecting the version it lands on is still warned about, so a vulnerability that comes to light after you wrote the marker still reaches you. Run with `--log-level DEBUG` to see what the marker silenced. To silence an advisory wherever it turns up instead of on this one reference, pass `--ignore-vulnerability` (see [Vulnerable dependencies](#-vulnerable-dependencies)).

#### Setting a risk level

`ignore[vulnerable]` silences every vulnerability warning a reference gets. To keep the warnings but only from a given severity up, give the scope a risk level: `# update-time: ignore[vulnerable<high]` warns about that reference's `high` and `critical` vulnerabilities and stays quiet about its `low` and `moderate` ones, and is a per-reference `--vulnerability-level high`. Use it for a dependency whose milder advisories you have assessed and decided not to act on:

```text
django==3.2.0  # update-time: ignore[vulnerable<high] (assessed the moderate ones, acting on high and worse)
```

The level applies to the reference carrying it, and every other reference in the scan keeps the global one. It wins over `--vulnerability-level`, `--vulnerability-level none` included, so switching the check off globally still leaves a reference with its own level looked up at OSV and warned about. As with the global level, a vulnerability whose risk level Update-time cannot read is warned about whatever the level in force, since leaving the vulnerabilities nobody has rated out of the warnings would hide exactly the ones nobody has looked at.

When none of the version's vulnerabilities falls below the level, Update-time reports the marker as holding nothing back, since a level that silences nothing is one the reference no longer needs:

```console
WARNING Redundant update-time directive ignore[vulnerable<high] for django in docs/requirements.txt:12: version 4.2.0 has no vulnerability below high
```

`allow` and `ignore` are complements here as elsewhere, so `allow[vulnerable>=high]` sets the same level as `ignore[vulnerable<high]`. Inverting the operator would warn about the mild vulnerabilities and stay quiet about the severe ones, so neither `allow[vulnerable<high]` nor `ignore[vulnerable>=high]` sets a level. Update-time logs an inverted comparison at `WARNING` and holds nothing back, so the reference is warned about at the global level:

```console
WARNING Incorrect 'vulnerable>=high' in the update-time marker for django in docs/requirements.txt:12: this comparison warns about the mild vulnerabilities and stays quiet about the severe ones, so it sets no risk level
```

A level must be one of `low`, `moderate`, `high`, and `critical`, spelled in lower case, so `ignore[vulnerable<hgih]` is reported as invalid and leaves the reference unchanged. `none` is a value for `--vulnerability-level` rather than a level, so it is reported as invalid too: to switch the warning off for one reference, write `ignore[vulnerable]`. An unreadable level is judged before the direction, so `ignore[vulnerable>=hgih]` is reported as an unreadable level rather than as an inverted comparison.

#### Redundant markers

A yank can only be observed where the dependency's source reports one — of the references that accept a marker, `requirements.txt` pins and jsDelivr URLs (see [Yanked dependencies](#-yanked-dependencies)). On a Docker image, a GitHub Action, a pre-commit hook, a `.python-version` entry, or a Node engine the scope can never suppress anything, so Update-time logs it as redundant at `WARNING`:

```console
WARNING Redundant update-time directive ignore[yanked] for python in Dockerfile:2: this dependency's source has no yank concept
```

A `requirements.txt` requirement that pins no exact version is reported too: PyPI does report yanks, but a yank is about the version a reference is left on, and such a requirement pins none.

```console
WARNING Redundant update-time directive ignore[yanked] for humanize in docs/requirements.txt:12: this requirement pins no version to check for a yank
```

A vulnerability can only be reported where OSV holds advisories for the dependency — of the references that accept a marker, `requirements.txt` pins and jsDelivr URLs (see [Vulnerable dependencies](#-vulnerable-dependencies)). On a Docker image, a GitHub Action, a pre-commit hook, a `.python-version` entry, or a Node engine the scope can never suppress anything, so it is reported as redundant in all its forms:

```console
WARNING Redundant update-time directive ignore[vulnerable] for python in Dockerfile:2: this dependency's source reports no vulnerabilities
```

A requirement that pins no exact version is reported here too: an advisory is matched against a version, and such a requirement pins none.

```console
WARNING Redundant update-time directive ignore[vulnerable] for humanize in docs/requirements.txt:12: this requirement pins no version to check for a vulnerability
```

In all its forms, the `stale` scope is reported as redundant for a reference whose source reports no publication date to measure staleness against. [Setting a staleness threshold](#setting-a-staleness-threshold) names the three kinds of reference that get that warning.

An `allow[floating-pin]` is reported as redundant for a reference whose pin does not float, and for one whose update a marker holds back, since neither has anything to keep floating. [Keeping a tag floating](#keeping-a-tag-floating) shows both warnings.

A bare `# update-time: ignore` is never reported as redundant: it names no scope, so a warning would have no directive to name. A scope or item written beside it is reported though, so `# update-time: ignore ignore[yanked]` on a Docker image reports the `ignore[yanked]` as redundant.

#### Invalid markers

A scope Update-time does not recognise — a mistyped `ignore[stlae]`, say — is logged at `WARNING` as an invalid item:

```console
WARNING Invalid 'stlae' in the update-time marker for python in Dockerfile:2; leaving the reference unchanged
```

The reference is left as it is, because an item Update-time cannot read may have been meant to bound the update, so applying one would be guessing. The checks still run, since that item is never read as silencing a warning: an unreadable marker holds back what Update-time would write, never what it would tell you. Every item beside it that Update-time does read applies as written, so `ignore[stale, stlae]` still silences the staleness warning.

An `update-time` field Update-time cannot read is reported as an invalid item too. A field is read whole rather than item by item, so an unreadable one holds back every directive it carries, where an unreadable bracket item leaves the items beside it standing. The warning names where the marker would sit rather than the value that is wrong, because a field of the wrong shape holds no marker to quote:

```console
WARNING Invalid 'update-time.engines.node' in the update-time marker for node in package.json:3; leaving the reference unchanged
```

### Adopting hash drift

One further marker does the opposite of holding a reference back. `# update-time: allow[hash-drift]` opts an already-pinned reference *into* adopting what it now points at, so a re-pushed image tag's new digest, or the commit a moved version tag points at, is pinned instead of only warned about (see [Hash drift](#hash-drift)). It follows the same placement rules as the other markers, and the global `--allow-hash-drift` flag applies it to every reference at once. `ignore[hash-drift]` is the opposite and the default, so a reference carrying it keeps its pin exactly as one carrying no marker at all, in a run passing `--allow-hash-drift` as well. Where an `ignore` (or `ignore[update]`) marker also applies, that wins and the reference is left untouched.

### Keeping a tag floating

`# update-time: allow[floating-pin]` keeps a reference's [floating image tag](#floating-image-tags) as it is, where Update-time would otherwise replace it with the version and digest it serves. Use it for a reference you want to follow a channel, such as an image you rebuild from `latest` on purpose. Run with `--log-level DEBUG` to see what the marker held back, which names the version the tag resolves to, so a marker you no longer need shows what dropping it would pin. Where the tag serves another digest than the reference records, the [hash drift](#hash-drift) is reported instead:

```console
DEBUG Keeping the floating tag python:latest in Dockerfile:2: it resolves to 3.14.7@sha256:… (update-time: allow[floating-pin])
```

A reference that names no tag is kept as it is in the same way, and reported by its name alone, since it has no tag to name after it.

A reference kept floating is still checked for [hash drift](#hash-drift). Where it already records a digest and its tag now serves another, the drift is warned about, and a reference opted into drift adopts the new digest while its tag stays as it is. It follows the same placement rules as the other markers, and the global `--allow-floating-pin` flag keeps every reference in the scan floating at once. `ignore[floating-pin]` is the opposite and the default, so a reference carrying it is pinned exactly as one carrying no marker at all. It is pinned in a run passing `--allow-floating-pin` as well. Where an `ignore` (or `ignore[update]`) marker also applies, that wins and the reference is left untouched, tag and all.

An `allow[floating-pin]` on a reference whose pin does not float keeps nothing floating, so Update-time reports it as redundant and updates the reference as usual:

```console
WARNING Redundant update-time directive allow[floating-pin] for python in Dockerfile:2: this reference's pin does not float
```

A reference held back by an `ignore` or an `ignore[update]` keeps nothing floating either, since a reference that is never pinned keeps its tag whatever the directive beside the hold-back asks for. That is reported whatever the tag says, so a floating tag gets it too:

```console
WARNING Redundant update-time directive allow[floating-pin] for python in Dockerfile:2: this reference's update is held back, so its tag is never pinned
```

A floating tag Update-time could not resolve is not reported, since that tag does float. The `DEBUG` line naming why it was left as it is covers that case (see [Floating image tags](#floating-image-tags)).

### Bounding an update

A bound lets a reference keep updating while ruling out the jump you are not ready for: name the versions it may move to, or the level of update it may not make.

#### Bounding how far a reference may update

`ignore[update]` freezes a reference at its current version. Sometimes you want the middle ground: keep receiving updates *within a range* while blocking a jump you're not ready for — for example, keep getting `python:3.12` patch releases but hold off on `3.13` until you've migrated. Add a [PEP 440](https://peps.python.org/pep-0440/) version specifier directly after `update` inside the brackets, either to allow or ignore updates: `# update-time: allow[update<specifier>]` **keeps only** the updates whose version satisfies the specifier, and `# update-time: ignore[update<specifier>]` **drops** the updates whose version satisfies it (the plain `ignore[update]` is the drop-everything case).

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

Update-time filters the candidate versions *before* the highest is picked, so a bounded reference still advances as far as the bound allows: when on `3.12.8`, `allow[update<3.13]` still adopts a freshly published `3.12.9`, it just never crosses into `3.13`.

`allow` and `ignore` are complements, which matters for ranges. For a one-sided bound the two are interchangeable — `allow[update<3.13]` and `ignore[update>=3.13]` express the same ceiling. For a *range* they are opposites: with versions `3.13` through `3.16` available, `allow[update>=3.13,<3.15]` keeps the reference *within* `[3.13, 3.15)` and picks `3.14`, whereas `ignore[update>=3.13,<3.15]` *excludes* that range and skips ahead to `3.16`.

Choose the operator deliberately. To keep `3.12` together with its patch releases while blocking `3.13`, use `<3.13`, `==3.12.*`, or `~=3.12.0`. Don't use `<=3.12` if you want to stay on `3.12`: since `3.12.1 > 3.12` in PEP 440, it also blocks `3.12.1`, which is rarely what "stay on 3.12" means.

#### Bounding by update level

A specifier bound names the version it must not reach, so it goes stale: after migrating to `3.13`, an `allow[update<3.13]` blocks every update until you rewrite the comment. A level-based bound states the policy instead, holding back or keeping updates by how significant they are:

| Directive | Effect | Complement |
| :-------- | :----- | :--------- |
| `ignore[major-update]` | minor and patch updates only | `allow[minor-update]` |
| `ignore[minor-update]` | patch updates only | `allow[patch-update]` |

Pick whichever verb reads best in context. Unlike a specifier bound, a level-based bound is anchored to the currently pinned version on every run, so it ratchets along as the reference advances: `ignore[minor-update]` on `python:3.12.1` blocks `3.13` today and, once you migrate the pin to `3.13`, blocks `3.14` — the comment never needs editing:

```dockerfile
# update-time: ignore[minor-update]
FROM python:3.12.1-bookworm-slim
```

The levels are positional, not semantic: they refer to the component's position in the version, not to the project's compatibility promises. Projects may ship breaking changes in releases that bump the *second* component, so "stay on Python 3.12" is `ignore[minor-update]` despite Python 3.13 shipping breaking changes (it removed 19 legacy modules from the standard library). The same caution applies to projects using calendar versioning. And as with specifier bounds, the level applies to a Docker tag's main version; a version embedded in the suffix (the `3.23` in `alpine3.23`) is unaffected by the bound. A component the current version doesn't have counts as zero, so `ignore[minor-update]` on `node:22` blocks `22.1`.

#### How a bound interacts with the other markers

A few rules govern how a bound — with a specifier or level-based — interacts with the other markers and checks:

- A bare `# update-time: ignore` (or `# update-time: ignore[update]` with no specifier) holds back *all* updates and wins over any bound on the same reference.
- A bound narrows updates only, not staleness. Staleness is always measured against the project's newest overall release; the bound doesn't come into play.
- The hash pin is still added or refreshed for whichever version the bound selects, exactly as without a bound.
- To combine a bound with another directive of the same verb (say, `allow[hash-drift]`), list both as comma-separated items in one bracket: `# update-time: allow[update<3.13, hash-drift]` or `# update-time: allow[minor-update, hash-drift]`. To combine directives of different verbs, list them after the `# update-time:` prefix, separated by a space: `# update-time: ignore[stale] allow[update<3.13]`. A reason can still follow the last directive.

Update-time logs a redundant bound at `WARNING`. That may happen in two ways:
- Either the bound **never has an effect**, so removing it would change nothing: the current version and every version above it satisfy the bound, for example `allow[update>=3.12]` on a `3.12` pin, or `allow[major-update]` on any pin (it allows every update, so it says nothing).
- Or the bound **blocks every update**, so it is just a frozen `ignore[update]` in disguise (use that instead if the freeze is intended): no version above the current one satisfies the bound, for example `ignore[update>=3.12]` on a `3.12` pin, or `ignore[patch-update]` on any pin.

A `requirements.txt` requirement that pins no exact version is reported whatever the bound says, `ignore[update]` included, since Update-time resolves no update for it to bound.

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

- **In an `update-time` field**, for a reference in a file that can hold no comment. A `package.json` is strict JSON, so its Node engine's marker names the reference it steers instead of sitting beside it, in a field mirroring the file's own structure:

  ```json
  "update-time": { "engines": { "node": "ignore" } }
  ```

Every reference but the Node engine takes its marker in a comment, in files of these kinds: Dockerfiles, Docker Compose and Helm manifests, CircleCI and GitLab CI configs, GitHub Actions workflows, `.pre-commit-config.yaml` files, `devcontainer.json` files, `requirements.txt` files, `.python-version` files, and the jsDelivr URLs in a Sphinx `conf.py`. Use a `#` comment everywhere except `devcontainer.json` (which is JSONC), where the marker goes in a `//` comment. An inline marker pins only its own line, so it never accidentally pins the reference on the line below it. Where one comment placement is safer than the other, the details per dependency type say so.

A dependency updated through uv, npm, or pnpm takes no marker. Opt one out with a version specifier instead, as described under [Python dependencies](#python-dependencies) and [npm and pnpm dependencies](#npm-and-pnpm-dependencies).

#### Confirming a marker was understood

Run with `--log-level DEBUG` to confirm a marker is recognised: every recognised marker is logged, and so is every update or warning it holds back, each on a line of its own. A marker Update-time recognised is reported as:

```console
DEBUG Recognised update-time marker ignore[stale] for python in Dockerfile:2
```

That line reports the marker itself, and says it was read and understood. No `Recognised` line at all means the marker was not read. The prefix and the verbs are case-sensitive, and a field marker is read by name, so a typo in any of them leaves the reference updated as usual. A typo inside the brackets is logged at `WARNING` as an invalid item instead (see [Invalid markers](#invalid-markers)).

What the marker held back is reported separately, in lines about the update or the warning rather than about the marker, each naming the directive it obeyed:

```console
DEBUG Ignoring the staleness warning for python in Dockerfile:2 (update-time: ignore[stale])
```

Such a line appears only when the marker actually held something back. An `ignore[yanked]` on a version that was never yanked produces no such line, and neither does a bound that keeps an update out.

## 📖 Details per dependency type

For each dependency type, this chapter answers the same questions: what files, dependencies, and versions are updated, how pinning, the cooldown, staleness, yanks, and vulnerabilities are handled, and how markers can be placed. Two sections cover a pair of types that behave alike, so there are fewer sections than types.

### Python dependencies

#### What files are updated?

Python files containing requirements are discovered by name, case-sensitively: `pyproject.toml`, `requirements.txt`, `requirements-<purpose>.txt` and `<purpose>-requirements.txt` (for example `requirements-dev.txt` and `dev-requirements.txt`), and any `.txt` file in a `requirements/` directory. Unrelated files such as `constraints.txt` or `requirements.in` are not touched.

In addition, any `*.py` file that carries a [PEP 723](https://peps.python.org/pep-0723/) inline script metadata block is updated: a `# /// script … # ///` comment block that declares the standalone script's dependencies. `*.py` files without such a block are left untouched and never invoke uv.

Compiled or hash-pinned requirements files, such as a `requirements.txt` generated by [pip-tools](https://github.com/jazzband/pip-tools) or `uv pip compile` are skipped entirely, because bumping a single pin without recompiling its transitive dependencies and hashes would corrupt the file. Regenerate these with your package manager instead. Update-time recognises compiled or hash-pinned files by their contents (an autogenerated header or `--hash=` lines) or by the existence of a sibling `.in` file.

#### What dependencies are updated?

Update-time cannot update individual git, VCS, and URL dependencies (for example `git+https://github.com/org/repo.git@v8.0.3.0`, direct URLs, and `-e`/editable installs) in both `requirements.txt` and `pyproject.toml` files. Update them manually.

In a PEP 723 inline script metadata block, only the pins in the `dependencies` array are updated; the `requires-python` value and any other inline-metadata fields are left untouched.

#### What versions are updated?

Only versions specified with an exact match are updated, that is dependency versions pinned with `==`. Looser version specifiers are left untouched, so you can pin a maximum version to opt a dependency out of automatic updates. In a `pyproject.toml` or an inline script metadata block, a new version for such a dependency is still reported. In a `requirements.txt` it is not reported at all, although the package behind it is still checked for staleness, as described under Stale dependencies below.

#### Pinning

Update-time adds no hash pin to a Python dependency. A `requirements.txt` pin carries one as a `--hash=` line, which has to hold for the file's transitive dependencies too, so a file that already has them is skipped entirely rather than partly rewritten. Dependencies in `pyproject.toml` are locked by uv, which records each distribution's hash in the `uv.lock` file it maintains; a PEP 723 script has no lock file, so its pins carry no hash either.

#### Cooldown

For a `requirements.txt` pin, Update-time enforces the cooldown itself, against the release's publication date on PyPI.

For `pyproject.toml` dependencies, Update-time applies the cooldown through uv's `exclude-newer` setting. It writes the setting into the workspace root's `pyproject.toml` under `[tool.uv]`, as a relative value such as `exclude-newer = "7 days"` tagged with a `managed by Update-time` comment. The setting then applies to every uv command in the project (`uv lock`, `uv add`, CI), not just to Update-time. Update-time keeps its own tagged value in step with `--cooldown`, and leaves a value you set yourself alone: an `exclude-newer` without the comment, or a `UV_EXCLUDE_NEWER` environment variable, stays as it is. Remove the comment to take ownership of the line.

For inline script metadata, Update-time also applies the cooldown through uv's `exclude-newer`, but passes it to `uv tree` on the command line rather than persisting it, since a standalone script has no lockfile to keep reproducible. The cutoff is derived from `--cooldown` on every run, so, unlike `pyproject.toml`, nothing is written into the `# /// script` block.

#### Stale dependencies

Every Python pin is checked against the newest release of its package on PyPI, whichever of the three file kinds declares it, and stale ones are reported. A requirement that pins no exact version is checked as well, wherever the file declares it, since the package name alone is enough to look the newest release up. Two kinds are left out, because PyPI serves no release to measure them against: a dependency that points at a URL or a git repository, and one uv resolves through a `[tool.uv] sources` entry, such as a path or a workspace member. A package published only as `.egg` files is left out too, since PyPI no longer accepts files named that way.

#### Yanked dependencies

Each exact pin a Python file declares is checked against [PEP 592](https://peps.python.org/pep-0592/)'s yank metadata on PyPI, whichever of the three file kinds it sits in, and a yanked release is skipped when picking a new version. The version checked is the one the file holds when the run ends, so a pin uv held back is warned about although PyPI has a newer release. A `requirements.txt` pin the run leaves on a yanked release is reported unless an `ignore[yanked]` marker silences that warning. A `pyproject.toml` or inline script metadata pin left on one is reported as well, but takes no marker to silence that warning. A dependency those files declare without an exact pin is not checked, since a yank is about the version a reference is left on and such a declaration names none.

#### Vulnerable dependencies

Each exact pin a Python file declares is checked against OSV's PyPI advisories, whichever of the three file kinds it sits in. A compiled or hash-pinned `requirements.txt` is the exception: Update-time skips it whole, so its pins are neither updated nor checked. The transitive dependencies those pins pull in are not checked. Reading a resolved dependency tree is what `uv audit` and `pip-audit` are for. A vulnerable `requirements.txt` pin is reported unless an `ignore[vulnerable]` marker silences that warning. A vulnerable `pyproject.toml` or inline script metadata pin is reported as well, but takes no marker to silence that warning. A dependency those files declare without an exact pin is not checked either, since an advisory is matched against a version and such a declaration names none.

#### Markers

A `requirements.txt` pin takes an inline marker on its own line, as in `humanize==4.15.0  # update-time: ignore`; see [Controlling updates and warnings per reference](#-controlling-updates-and-warnings-per-reference) for the directives and where they go. A requirement that pins no exact version takes one too. Staleness is the only check such a requirement gets, so only a `stale` directive holds anything back there: `humanize>=4  # update-time: ignore[stale<1095]` warns once that package's newest release is more than three years old. A `yanked` or `vulnerable` scope on such a requirement is reported as redundant, since both checks need the version it does not pin. So are a `cooldown` and a bound, which steer an update Update-time never resolves for it. Dependencies in `pyproject.toml` and inline script metadata take no marker. Opt one of those out by pinning it with a maximum or non-`==` specifier instead, for example `package<=3.12`.

### npm and pnpm dependencies

#### What files are updated?

Update-time looks for `package.json` files recursively from the starting path. The accompanying lock file is updated as well: `package-lock.json` for npm, `pnpm-lock.yaml` for pnpm.

#### What dependencies are updated?

Update-time delegates updating Node dependencies to the package manager used to manage the `package.json`. If that is npm, it runs `npm update --save --include=dev`; if it is pnpm, it runs `pnpm update`.

#### What versions are updated?

Both package managers update each dependency to the newest version that satisfies the range declared in the `package.json`, so a dependency declared as `"react": "^17.0.0"` receives `17.x` updates but is never bumped to `18`. This means you can declare an upper bound to opt a dependency out of major-version updates, and pin an exact version to opt it out of automatic updates entirely, just like with [Python dependencies](#python-dependencies). See the documentation of [npm update](https://docs.npmjs.com/cli/v12/commands/npm-update) and [pnpm update](https://pnpm.io/cli/update) for the finer points of how each manager resolves versions.

#### Pinning

Update-time adds no hash pin to an npm or pnpm dependency. The integrity hash of each resolved package lives in the lock file, which npm and pnpm maintain themselves and Update-time updates by running them.

#### Cooldown

For npm, Update-time passes the cooldown via npm's `min-release-age` option, also measured in days, which npm added in 11.10.0. Older npm versions ignore the option, so updates still run but without a cooldown. If your project already configures a cooldown in its `.npmrc` (`min-release-age` or `before`), Update-time leaves that in place instead of overriding it.

For pnpm, Update-time passes the cooldown via pnpm's `minimumReleaseAge` setting, converting the value to minutes (pnpm measures the age in minutes rather than days). If your project already configures `minimumReleaseAge` (in `pnpm-workspace.yaml`), Update-time leaves that in place instead of overriding it.

#### Stale dependencies

Each dependency is checked against its newest release on the npm registry, and stale ones are reported. Dependencies given as git, file, link, workspace, alias, or GitHub-shorthand references are skipped, since they don't resolve to a registry release.

#### Yanked dependencies

There is no yank on npm, and the per-version deprecation that plays the same role is handled by npm and pnpm themselves when they resolve an update, so Update-time doesn't check `package.json` dependencies for it.

#### Vulnerable dependencies

A `package.json` dependency is not checked. It declares a range rather than a version, and the version that range resolves to is recorded in the lock file, which Update-time does not read; auditing that lock file is what `npm audit` is for.

#### Markers

A `package.json` dependency takes no marker, because npm and pnpm update it rather than Update-time rewriting its lines. Opt one out by declaring an upper bound or an exact version instead, as described under What versions are updated? above.

### GitHub Actions and pre-commit hooks

Both resolve their versions from the same source — a GitHub repository's releases and tags — so they behave the same way except where the file format differs.

#### What files are updated?

For GitHub Actions, Update-time looks for `*.yml` and `*.yaml` files under the `.github/` folder, recursively, so both workflow files (`.github/workflows/*.yml`) and composite action definitions are covered.

For pre-commit hooks, it looks for `.pre-commit-config.yaml` files, recursively from the starting path. Pre-commit reads the file at the repository root, but a monorepo can carry one per sub-project, so every one found is updated.

#### What dependencies are updated?

In a workflow or action definition, the actions in the `uses:` references. Actions referenced by a branch (for example `@main`) or as a local action without an `@` don't resolve to a version, so they are not updated.

In a `.pre-commit-config.yaml`, the `rev:` of each hook repository hosted on GitHub. A `repo: local` or `repo: meta` entry has no `rev:` and is left untouched, as is a repository hosted outside GitHub. A `rev:` that names a branch rather than a version is not updated.

#### What versions are updated?

A reference given as a version tag — `@v4` or `@v4.1.1` for an action, `v4.5.0` for a `rev:` — is bumped to the latest version, and so is one already pinned to a commit SHA with a version comment (`@<sha> # v4.1.1`, or `rev: <sha>  # frozen: v4.5.0`). Often, the latest version is the latest GitHub release, but a version that was tagged without being published as a release counts too, so a repository that only tags its versions — or whose releases stopped while tagging continued — is still updated.

#### Pinning

An action referenced by version tag only is pinned to the commit SHA of the latest version, with the version added as a trailing comment: `uses: actions/checkout@v4` becomes `uses: actions/checkout@<sha> # v4.1.1`.

A `rev:` referenced by version tag only is pinned to the commit SHA the same way, with the version travelling in pre-commit's own `# frozen: <version>` comment convention: `rev: v4.5.0` becomes `rev: <sha>  # frozen: v4.5.0`. This is the same format `pre-commit autoupdate --freeze` produces and understands, so the config stays interoperable with pre-commit's own tooling. The tag's `v` prefix convention is kept in the comment, so a repository that tags without a `v` gets `# frozen: 4.5.0`.

An action referenced by a branch gets no pin, since it resolves to no version, and neither does a `rev:` that names a branch or that is already a bare commit SHA without a `# frozen:` comment. Once a reference is pinned, a version tag moved onto another commit is reported as tag drift (see [Hash drift](#hash-drift)).

#### Cooldown

The cooldown is measured against the release's publication date, or, for a version that was only tagged, the date of the commit it tags. A version whose commit date can't be fetched is skipped rather than adopted with the cooldown unchecked.

#### Stale dependencies

Staleness is measured against the repository's newest release, whatever the reference names: a version, a branch, or a commit.

#### Yanked dependencies

GitHub has no yank concept, so neither an action nor a hook `rev:` is checked for one, and an `ignore[yanked]` marker on either is reported as redundant.

#### Vulnerable dependencies

Neither is checked. OSV does hold advisories for actions, but their affected entries enumerate no versions, so asking OSV about a version of an action comes back empty however that version is spelled: silence there says nothing about whether the action is safe. A hook repository is not an OSV package at all. A `vulnerable` scope on either is reported as redundant.

#### Markers

Both take an inline marker on the reference's own line. In a `.pre-commit-config.yaml`, the marker follows the `# frozen:` comment on the `rev:` line when both are present:

```yaml
rev: <sha>  # frozen: v4.5.0  # update-time: ignore
```

### Node engine version and Python version

Both name the runtime a project runs on rather than a package it depends on, and both follow the project's Dockerfile where there is one, so that the runtime you develop against and the one you ship stay in step.

#### What files are updated?

For the Node engine version, Update-time looks for `package.json` files that specify a [Node engine](https://docs.npmjs.com/cli/v11/configuring-npm/package-json#engines).

For the Python version, it looks for `.python-version` files recursively from the starting path, so both a repository that pins its Python version at the root and a monorepo that pins one per package are covered. `.python-version` is the de facto standard for pinning a project's Python version, read by uv, pyenv, and GitHub's `setup-python` action, among others.

#### What dependencies are updated?

The Node engine version in the `package.json`: the `node` entry its `engines` section declares.

In a `.python-version` file, each entry that is a plain CPython version, `X.Y` or `X.Y.Z` (for example `3.12` or `3.12.6`), on a line of its own. A file may list several entries, one per line (pyenv reads more than one), each handled independently. Alternative implementations (`pypy3.10-7.3.12`, `miniconda3-…`), free-threaded and other variant suffixes (`3.13t`), prefixed forms (`cpython@3.12`, `>=3.10`), and the `system` sentinel are left untouched.

Other Python version pins are left untouched too: the `requires-python` value in `pyproject.toml` and in PEP 723 inline script metadata is not a `.python-version` entry and stays as it is.

#### What versions are updated?

The new version is taken from the matching base image in the project's Dockerfile, provided there is a Dockerfile in the same folder and its base image has a numeric version. When no Dockerfile declares one, the version is instead taken from the latest [Node](https://hub.docker.com/_/node) or [Python](https://hub.docker.com/_/python) release on Docker Hub.

The Node engine version is updated only when it contains a specific version (for example, `26.4`); a range or other non-numeric value is left untouched. A Node base image whose tag carries no version to sync to, such as `node:lts` or `node:22.x`, is the exception to following the Dockerfile: the engine is left alone rather than overridden with a mismatched concrete version.

A `.python-version` entry is moved forward to a fuller version. It adopts the image's version at the precision the tag provides, so `python:3.14.2-slim` yields `3.14.2` and a bare `python:3.14` yields `3.14`, and an entry already ahead of the image is left alone rather than downgraded. The Node engine goes the other way, since it declares the runtime the project ships: an engine ahead of its base image is brought back to the image's version. Docker Hub always names a full version, so an entry that follows Docker Hub gains precision it didn't have: both `3.12.6` and `3.12` become `3.13.2` (or whatever the latest is).

#### Pinning

Neither can carry a hash pin. Both name a version rather than one artefact — a version covers every build ever published for it — so there is no image digest, commit SHA, or integrity hash to add. Neither format has anywhere to put one either: a `package.json` entry names a version, and a `.python-version` line is a bare version. Update-time only moves them to a fuller version, which makes them more precise but verifies nothing.

#### Cooldown

A version taken from Docker Hub honours the cooldown through the Node or Python image tag's push date. A version that instead follows the project's Dockerfile needs no cooldown of its own, since it was already applied when the base image was updated, so a `cooldown` marker on such a reference is reported as redundant, whether it is a `.python-version` entry or a Node engine.

#### Stale dependencies

Both are indirect cases. When the version is derived from the project's Dockerfile, only the staleness of the base image is reported. Neither the entry nor the engine is checked itself then, and a `stale` marker on either is reported as redundant. A version taken from Docker Hub is checked against the newest Node or Python release there.

#### Yanked dependencies

Neither source has a yank concept, so neither is checked for one, and an `ignore[yanked]` marker on either is reported as redundant.

#### Vulnerable dependencies

Neither is checked: both name a version of a runtime rather than a release of a package, which is not something OSV matches an advisory against. A `vulnerable` scope on either is reported as redundant.

#### Markers

A `.python-version` entry takes a marker in either placement, but uv rejects an inline comment on a `.python-version` line, ignoring the entry and silently resolving a different Python, so the line-above form is the safer placement for a uv project:

```text
# update-time: ignore
3.12
```

The Node engine version takes a marker too, but not as a comment: `package.json` is strict JSON, which has nowhere to put one. Its marker goes in an `update-time` field instead, which mirrors the file's own structure, so the marker steering the engine is named under `engines` and `node`, as the engine itself is:

```json
{
  "engines": { "node": "22" },
  "update-time": { "engines": { "node": "allow[update<23]" } }
}
```

The field holds the directives a comment holds, without the `# update-time:` prefix that introduces them there. npm and pnpm keep a field they do not know, so the marker survives an `npm update` and a `pnpm update`. Both rewrite the file onto one key per line, so the field comes back spread over several lines rather than the one shown here. A field of the wrong shape is reported as an invalid item and leaves the engine as it is (see [Invalid markers](#invalid-markers)).

Update-time reads the marker while it updates the engine, so an engine it never updates keeps a marker nobody reads. Two kinds are never updated: an engine declared as a range, and an engine whose base image tag names no version to sync to, such as `node:lts`. A marker on either holds nothing back, and Update-time reports nothing such a marker gets wrong.

Either way a marker wins over a version derived from the Dockerfile, so a deliberately held-back development version is never dragged forward by an image update.

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

A Dockerfile's `FROM` is read the way Docker reads it: in upper or lower case, and only where it opens its line, so a `FROM` written in prose is left alone. Two kinds of `FROM` name no image a registry serves and are left alone as well: `FROM scratch`, which starts a stage from nothing, and a `FROM` naming one of the file's own build stages, which an earlier `FROM ... AS name` introduced.

#### What versions are updated?

When updating an image tag, Update-time keeps the non-numeric parts of the tag and only advances its version numbers. A tag such as `python:3.14.6-alpine3.23` has three parts: the label prefix `python`, the main version `3.14.6`, and the suffix `alpine3.23`. The label prefix (`python`) and the suffix's label (`alpine`) are preserved, so a variant is never swapped out: `python` never becomes `pypy`, `slim` never becomes `fat`, and `alpine` never becomes `debian`. Both the main version and a version embedded in the suffix are upgraded, independently or together, for example `3.14.6-alpine3.23` → `3.15.0-alpine3.24`. Neither axis is ever downgraded to adopt a newer value on the other.

A suffix without an embedded version (`bookworm-slim`, `windows`) is never updated.

A tag naming a channel in labels alone, such as `latest` or `trixie`, floats, and is replaced by the version tag serving the same image, keeping the labels the tag itself carries (see [Floating image tags](#floating-image-tags)). A reference that names no tag asks for `latest`, the tag a registry serves by default, so it floats too and is pinned the same way. A dated snapshot such as `debian:bookworm-20260803` names the day its image was built. It is updated to the newest snapshot under its label, so a reference on `bookworm-20240110` moves to `bookworm-20260803`. A repository can also name a snapshot by the date alone, as Alpine names one `20260805` beside its `3.24.1` release. Such a tag is updated to the newest snapshot as well. A reference on a release keeps following releases, and a reference on a snapshot keeps following snapshots.

#### Pinning

An image referenced by tag only gets the `@sha256:digest` of the (latest) tag appended, so the image is reproducible. This covers base images in Dockerfiles (`FROM image:tag`), CircleCI images, GitLab CI images, Docker Compose and Helm manifest images, and devcontainer base images and features. The image's registry is taken from the reference, so images on Docker Hub and on other OCI registries (`ghcr.io`, `mcr.microsoft.com`, …) are both resolved.

A floating tag is pinned to both at once: the version tag serving the image it currently resolves to, and that image's digest.

Two kinds of reference get no digest. An image whose tag Update-time cannot read is ignored: a reference through a `{{ ... }}` template or `${VAR}` variable substitution. A CircleCI machine-executor image (the `image:` under a `machine:` key, such as `ubuntu-2204:2024.01.1`) gets none either, since it is not a registry image.

Once an image is pinned, a tag re-pushed under the same name is reported as digest drift (see [Hash drift](#hash-drift)).

#### Cooldown

Pinning a floating tag adopts no newer image, so the cooldown holds it back not at all. A newer tag is adopted only once it is past the cooldown, provided the image is hosted on Docker Hub. Other registries (`ghcr.io`, `mcr.microsoft.com`, …) expose no publication date, so images there are updated without a cooldown, and a `cooldown` marker on one of them is reported as redundant. So is a `cooldown` marker on a CircleCI machine-executor image, which no registry serves.

#### Stale dependencies

Staleness is measured against the image's newest release, on Docker Hub only, since other registries expose no publication date. A `stale` marker on an image hosted elsewhere is therefore reported as redundant. So is one on a CircleCI machine-executor image, which no registry serves.

#### Yanked dependencies

An OCI registry has no yank concept, so an image is not checked for one, and an `ignore[yanked]` marker on an image reference is reported as redundant.

#### Vulnerable dependencies

An image is not checked, because OSV has no ecosystem for container images. Reporting the vulnerabilities of the packages inside an image is what an image scanner is for. A `vulnerable` scope on an image reference is reported as redundant.

#### Markers

In a Dockerfile the marker goes on the line above the `FROM`, since Dockerfiles don't allow inline comments. In the YAML formats — CircleCI, GitLab CI, Docker Compose, and Helm — it can go inline on the image's own line, and in a `devcontainer.json` it goes in a `//` comment.

### jsDelivr npm URLs

#### What files are updated?

Update-time looks for Sphinx configuration files (`conf.py`) under the `docs/` folder, recursively.

#### What dependencies are updated?

The jsDelivr npm URLs and their accompanying Subresource Integrity (`integrity`) hash. For example: `https://cdn.jsdelivr.net/npm/clipboard@2.0.11/dist/clipboard.min.js`.

#### What versions are updated?

The npm package version embedded in the URL is updated to the latest version on the npm registry, and the SRI hash is updated in step so the two stay consistent.

#### Pinning

A URL whose attribute dictionary declares no `integrity` entry gains one, so the browser verifies the script the CDN serves before running it. The hash is inserted in front of the entries the dictionary already has, and reported as a pin: `Pinned clipboard in docs/conf.py:4 to 2.0.11@sha256-…`.

A URL declared as a bare string, without an attribute dictionary, has nowhere to hold an integrity hash, so it stays without one. Adding a hash would mean rewriting the string into a `(url, {"integrity": …})` tuple, which is more than rewriting a line, so Update-time logs it at `INFO` and leaves it alone. Declare the URL as such a tuple to have it pinned.

A declared hash that doesn't match what jsDelivr serves is reported as an integrity hash mismatch, which is never adopted (see [Hash drift](#hash-drift)).

#### Cooldown

A newer version is adopted only once it is past the cooldown, measured against its publication date on the npm registry.

#### Stale dependencies

The URL's package is checked against its newest release on the npm registry.

#### Yanked dependencies

There is no yank on npm, but a per-version deprecation is the same signal, so it is reported in the same wording. A deprecated version is skipped when picking a new one, and a URL left on a deprecated version is warned about.

#### Vulnerable dependencies

The version in the URL is checked against OSV's npm advisories, and a URL the run leaves on a version an advisory names is warned about unless an `ignore[vulnerable]` marker silences that warning.

#### Markers

A jsDelivr URL takes an inline marker in a `#` comment on its own line in `conf.py`.

## 📮 Point of contact

Point of contact for this repository is [Frank Niessink](https://github.com/fniessink).
