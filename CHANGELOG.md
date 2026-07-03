# Changelog

All notable changes to *Update-time* will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and this project will adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) soon.

## [Unreleased]

### Added

- Recognise the `update-time: ignore` marker in `.devcontainer/devcontainer.json` and `.devcontainer.json`. Since these files are JSONC, the marker is written in a `//` comment (`// update-time: ignore`), either inline on the reference's line or as a standalone comment on the line directly above it.

### Changed

- Send all diagnostics — the new-version report as well as warnings and errors — to standard error instead of standard output. Update-time's real output is the files it rewrites in place, so nothing it logs belongs on stdout; keeping stdout empty makes the whole run redirectable in one stream (`update-time 2> run.log`) and leaves stdout clean for the `--version`/`--help` output, so e.g. `update-time -V` can be captured without log lines mixed in.

### Fixed

- Report a user-facing error for a non-integer `--cooldown` value. The message previously leaked an internal identifier (`invalid non_negative_int value: 'abc'`); it now reads `invalid days value: 'abc'`.
- Stop logging routine `npm`/`pnpm outdated` (and `list`) output as a failure. These commands exit non-zero as a normal "there are updates" signal, and pnpm additionally prints deprecation `[WARN]`s to stderr (e.g. `The "pnpm" field in package.json is no longer read by pnpm`) — which Update-time surfaced as a misleading `WARNING Error running ...` on every run, wrongly suggesting the update had failed. Their stderr is now logged only when the command produced no usable output (a genuine failure), and the message reads `<command> wrote to stderr: ...` — neutral about severity, since the tool's own output already labels it (`[ERROR]`, `[WARN]`, a notice, …). Action commands (`npm`/`pnpm update`, `uv lock`) still warn when they actually fail.

## 0.0.13 - 2026-07-02

### Added

- Update `package.json` files managed by [pnpm](https://pnpm.io) using pnpm, instead of skipping them. The package manager is detected from the corepack `packageManager` field or a sibling `pnpm-lock.yaml`, and pnpm updates both `package.json` and `pnpm-lock.yaml` without ever writing a stray `package-lock.json`. Update-time's cooldown (the `--cooldown` value, default 7 days) is applied via pnpm's `minimumReleaseAge` setting (measured in minutes); if the project already configures `minimumReleaseAge`, Update-time leaves it untouched. yarn and bun are still skipped with a warning. Closes [#47](https://github.com/ICTU/update-time/issues/47).
- Update the base `image` and each `features` entry in `.devcontainer/devcontainer.json` and `.devcontainer.json`, bumping each OCI reference to its latest compatible tag and pinning it with the tag's digest. The references are resolved on any OCI registry (`ghcr.io`, `mcr.microsoft.com`, Docker Hub, …); since the OCI protocol exposes no publication date, the cooldown is not enforced for registries other than Docker Hub. Devcontainers that build from a `dockerfile` or `dockerComposeFile` are left to the Dockerfile and Compose updaters. The file is edited line by line, so its comments and trailing commas are preserved. Closes [#49](https://github.com/ICTU/update-time/issues/49).
- Update Dockerfiles named `*.Dockerfile` or `Dockerfile.*` (e.g. `python.Dockerfile`, `Dockerfile.dev`), not only files named exactly `Dockerfile`. This applies to both the base-image updater and the Dockerfile the Node-engine updater reads the Node version from.

### Fixed

- Resolve images on registries that reject a host-prefixed repository path, such as `mcr.microsoft.com`. The registry host is now dropped from the repository path for every registry (not only Docker Hub), so `mcr.microsoft.com/devcontainers/typescript-node` is queried as `.../v2/devcontainers/typescript-node/...` instead of a doubled path that returned a 404. This affected any such image reference, wherever the updaters find one.
- Apply the cooldown to jsDelivr npm URLs, which previously adopted a freshly published version immediately. Update-time now walks the available versions newest-first and picks the latest one published outside the cooldown window (using the npm registry's publication dates), consistent with the other version sources. Closes [#68](https://github.com/ICTU/update-time/issues/68).
- Compute the jsDelivr Subresource Integrity hash for the file referenced in the URL rather than the package's default entry point. The previous behavior crashed on packages whose default file jsDelivr doesn't list (e.g. `mathjax`), and could have written a hash that didn't match the referenced file. When the referenced file's hash can't be resolved (for example because it no longer exists in the newer version), the reference is now left unchanged and a warning is logged instead of crashing. Closes [#69](https://github.com/ICTU/update-time/issues/69).

## 0.0.12 - 2026-06-30

### Changed

- Log an unsupported package manager (pnpm/yarn/bun for `package.json`, Poetry/PDM for `pyproject.toml`) at the `WARNING` level instead of `INFO`, so the skipped dependency set stands out — for example when running with `--log-level WARNING`.

### Added

- Resolve image versions on registries other than Docker Hub (e.g. `ghcr.io`, `mcr.microsoft.com`, `gcr.io`, `quay.io`), wherever the existing updaters find image references (Dockerfiles, Docker Compose / Helm manifests, CircleCI, GitLab CI). The registry host is taken from the reference, auth is auto-discovered via the OCI `WWW-Authenticate` challenge (anonymous when the registry doesn't require it, Docker Hub credentials for Docker Hub when set), and the digest to pin is read from the image's OCI manifest. This reverses the earlier "skip non-Docker Hub images" behavior; references that genuinely don't resolve (CircleCI machine images, `${VAR}` substitutions, private images we can't authenticate for) are still left unchanged. Note that the cooldown still only applies to Docker Hub, because the OCI protocol exposes no publication date. Closes [#48](https://github.com/ICTU/update-time/issues/48).
- Update image tags whose version carries a non-numeric label prefix, such as `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` (bumped to `python3.13-bookworm-slim` and digest-pinned). The label prefix (e.g. `python`) and the suffix (e.g. `bookworm-slim`) are preserved, and a prefix never crosses to a different one (e.g. `python` is not replaced by `pypy`). Previously these tags were left unchanged because the version couldn't be parsed. Closes [#55](https://github.com/ICTU/update-time/issues/55).
- Leave a reference unchanged when its line carries an `# update-time: ignore` comment, so an update can be pinned deliberately (a known incompatibility, a deferred migration, reproducibility). It works across the line-based updaters (Dockerfiles, Docker Compose / Helm manifests, CircleCI, GitLab CI, GitHub Actions, `requirements.txt`), either inline on the reference's line (valid in YAML and requirements) or as a standalone comment on the line directly above it — the form to use in Dockerfiles, which reject inline comments. A pinned line is left untouched and triggers no registry or source lookup, and each ignored reference is logged at the `DEBUG` level so you can confirm a marker is recognised. Closes [#56](https://github.com/ICTU/update-time/issues/56).

## 0.0.11 - 2026-06-29

### Fixed

- Don't warn about CircleCI machine-executor images (the `image:` under a `machine:` key, such as `ubuntu-2204:2024.01.1`). These are not Docker Hub images and have no registry to query; they are now recognised by parsing the CircleCI YAML and left unchanged, instead of being looked up on Docker Hub and logged as unfetchable on every run.
- Skip `package.json` files managed by a package manager other than npm (pnpm, yarn, bun) instead of running npm against them. Running npm on, for example, a pnpm project would write a stray `package-lock.json` and leave `pnpm-lock.yaml` out of sync. The package manager is detected from the `packageManager` field or a sibling lockfile; only npm projects (and projects with no manager indicator) are updated.
- Skip `pyproject.toml` files managed by a Python dependency manager other than uv (Poetry, PDM) instead of running uv against them, which would write a stray `uv.lock` alongside the real lockfile. The manager is detected from a `[tool.poetry]`/`[tool.pdm]` section or a sibling lockfile; only uv projects (and projects with no manager indicator) are updated.

## 0.0.10 - 2026-06-29

### Added

- Apply Update-time's cooldown (the `--cooldown` value, default 7 days) to `pyproject.toml` dependency updates by passing it to uv's `exclude-newer` option, so freshly published releases are held back for both the `pyproject.toml` pins and the `uv.lock`. If the project already sets `exclude-newer` under `[tool.uv]` (or the `UV_EXCLUDE_NEWER` environment variable is set), Update-time leaves it untouched. Closes [#36](https://github.com/ICTU/update-time/issues/36).
- Apply Update-time's cooldown (the `--cooldown` value, default 7 days) to npm dependency updates via npm's `min-release-age` option, so freshly published npm releases are held back like Docker images, GitHub Actions, and `requirements.txt` dependencies already are. If the project already configures a cooldown in its `.npmrc` (`min-release-age` or `before`), Update-time leaves it untouched. npm applies `min-release-age` from version 11.10.0 onwards; older npm versions ignore the option and update without a cooldown. Closes [#37](https://github.com/ICTU/update-time/issues/37).

### Fixed

- Include the file being updated in the "New version available" and "Pinned" messages (e.g. "New version available for humanize in docs/requirements.txt: 4.15.0" and "Pinned redis in docker-compose.yml to 7.2.0@sha256:..."), so it is clear which file the change applies to now that the per-file "Checking ..." progress is logged at `DEBUG`.

## 0.0.9 - 2026-06-29

### Changed

- Resolve the latest Docker Hub tag by listing tag names once (via the registry's names-only endpoint) and then fetching metadata only for the chosen tag, instead of paginating through every tag's full metadata. For heavily-tagged images such as `node` (~9,000 tags) this cuts the number of requests per image from ~90 to a handful. Closes [#34](https://github.com/ICTU/update-time/issues/34).

## 0.0.8 - 2026-06-28

### Added

- Add a `--log-level` option (`DEBUG`, `INFO`, `WARNING`, or `ERROR`; default `INFO`) to control how much is logged.

### Changed

- Log available new versions at the `INFO` level instead of `WARNING`, so the `WARNING` level is reserved for genuinely unexpected situations. The per-file "Checking ..." progress now logs at `DEBUG`.

## 0.0.7 - 2026-06-28

### Added

- Add support for updating exact pins (`package==version`) in hand-written `requirements.txt` files (also `requirements*.txt` and `requirements/*.txt`) against PyPI. Compiled or hash-pinned files (generated by pip-compile or `uv pip compile`) are left untouched. Closes [#21](https://github.com/ICTU/update-time/issues/21).

### Changed

- Skip image references hosted on registries other than Docker Hub (e.g. `registry.gitlab.com/...`, `gcr.io/...`, `ghcr.io/...`) up front instead of querying Docker Hub and logging a 404 for each on every run. Images referenced with an explicit Docker Hub host (`docker.io/...`, `index.docker.io/...`) are recognised and updated. Closes [#24](https://github.com/ICTU/update-time/issues/24).

### Fixed

- Don't rewrite `package.json` when there are no dependency updates. npm normalizes specs such as `git+https://...` URLs to the `github:` shorthand whenever it saves the manifest, which previously produced a spurious diff even when no version changed; the original file is now restored unless a registry version was actually updated. Fixes [#27](https://github.com/ICTU/update-time/issues/27).

## 0.0.6 - 2026-06-27

### Fixed

- Don't crash when an `image:` reference is not on Docker Hub (for example a CircleCI machine image such as `ubuntu-2204`, or an image hosted on another registry); log it and leave the reference unchanged instead.
- Don't report an error when a Dockerfile's Node base image uses a non-numeric tag such as `node:lts`; warn that the Node engine version can't be derived and leave the engine in the `package.json` unchanged instead of failing.

## 0.0.5 - 2026-06-27

### Added

- Add support for updating Docker images (tag + digest) in GitLab CI (`.gitlab-ci.yml`) files. Closes [#20](https://github.com/ICTU/update-time/issues/20).

## 0.0.4 - 2026-06-24

### Added

- Add a command-line interface with `-h`/`--help` and `-V`/`--version` options.
- Add a `--cooldown` option to configure the cooldown period (in days) for Docker images and GitHub Actions, overriding the default of 7 days.

### Fixed

- Detect the Node base image when the `FROM node:...` line is not the first line of the Dockerfile, for example when it is preceded by comments or `ARG` directives.
- Log the npm version that was actually installed instead of the latest available version. These can differ when `npm update` holds back a release, for example because `min-release-age` is configured in the `.npmrc`.

## 0.0.3 - 2026-06-23

### Added

- Automatically pin Docker images that are referenced by tag only — Dockerfile base images, CircleCI images, and Docker Compose / Helm manifest images — by appending the `@sha256:digest` of the (latest) tag, instead of leaving them untouched.
- Automatically pin GitHub Actions that are referenced by version tag only (e.g. `@v4`) to the commit SHA of the latest version, adding the version as a trailing comment, instead of leaving them untouched.

### Fixed

- Don't say "Updating ..." when checking whether there are any updates because there may well be no updates.

## 0.0.2 - 2026-06-22

### Fixed

- Fix import error.

## 0.0.1 - 2026-06-22

### Added

- Copied the update scripts from [Quality-time](https://github.com/ICTU/quality-time).
