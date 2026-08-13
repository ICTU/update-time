# Changelog

All notable changes to *Update-time* will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and this project will adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) soon.

## [Unreleased]

### Added

- Warn about a `stale` marker on a reference whose source reports no publication date to measure staleness against, instead of accepting it in silence. Closes [#221](https://github.com/ICTU/update-time/issues/221).

### Fixed

- Name the directive a redundant-marker warning is about, instead of every `ignore` directive on the reference's line, so a directive beside it that holds plenty back is no longer reported as holding nothing back. Part of [#221](https://github.com/ICTU/update-time/issues/221).
- Name a `vulnerable` scope set with `allow`, such as `allow[vulnerable>=high]`, in the warning reporting it as redundant. Such a warning previously named no directive at all. Part of [#221](https://github.com/ICTU/update-time/issues/221).

## 0.0.27 - 2026-08-13

### Added

- Warn about a pin left on a yanked release in a `pyproject.toml` or a PEP 723 inline script metadata block, as Update-time already does for a `requirements.txt` pin. Closes [#234](https://github.com/ICTU/update-time/issues/234).
- Warn about a `cooldown` marker on a reference whose source reports no publication date to measure a cooldown against, instead of accepting it in silence. Closes [#205](https://github.com/ICTU/update-time/issues/205).

## 0.0.26 - 2026-08-12

### Changed

- Remove the column that named the Update-time module each log line came from. Closes [#230](https://github.com/ICTU/update-time/issues/230).
- Report a dependency that uv, npm, or pnpm updates at the line declaring it, instead of at the file: the new version available for it, and the staleness and vulnerability warnings it gets. Closes [#218](https://github.com/ICTU/update-time/issues/218).

### Fixed

- Report every pin of a dependency that a `pyproject.toml` or PEP 723 inline script metadata file pins more than once, instead of only the last one, so no pin's available new version, staleness, or vulnerability goes unreported. Part of [#218](https://github.com/ICTU/update-time/issues/218).
- Update a `pyproject.toml` or PEP 723 inline script metadata dependency whose name is spelled with a `_` or a `.`, such as `typing_extensions`, instead of reporting a new version for it and then leaving its pin unchanged. Part of [#218](https://github.com/ICTU/update-time/issues/218).
- Read a GitHub repository URL in three more spellings: git's scp-like `git@github.com:owner/repo` form, npm's `github:owner/repo` host shorthand, and npm's bare `owner/repo` shorthand. A pre-commit hook whose repository uses the scp-like form is updated instead of being silently left alone. The changelog of a package whose npm `repository` uses any of the three is found instead of being reported missing. Closes [#224](https://github.com/ICTU/update-time/issues/224).
- Look for the changelog of a Python dependency under every project URL PyPI publishes, matching their labels as [PEP 753](https://peps.python.org/pep-0753/) prescribes, and never read a GitHub sponsors URL as a repository. Closes [#225](https://github.com/ICTU/update-time/issues/225).

## 0.0.25 - 2026-08-10

### Added

- Warn when the version a Python dependency or a jsDelivr URL is left on has a known vulnerability, according to the [OSV](https://osv.dev) database. Closes [#210](https://github.com/ICTU/update-time/issues/210).

## 0.0.24 - 2026-08-07

### Fixed

- Report the changelog of a dependency whose repository URL is an ssh URL, instead of reporting that no changelog is available. Closes [#206](https://github.com/ICTU/update-time/issues/206).

## 0.0.23 - 2026-08-05

### Added

- Warn when the version tag a GitHub Action or pre-commit hook is pinned to points at another commit than the pinned one. Part of [#179](https://github.com/ICTU/update-time/issues/179).
- Warn when the Subresource Integrity hash a jsDelivr URL declares is not the one jsDelivr serves for that version. Part of [#179](https://github.com/ICTU/update-time/issues/179).
- Adopt the commit a moved tag points at, instead of only warning about it, when a GitHub Action or pre-commit hook is marked `# update-time: allow[hash-drift]` or `--allow-hash-drift` is passed. Part of [#179](https://github.com/ICTU/update-time/issues/179).
- Override the stale-after period for a single reference with `# update-time: ignore[stale<90]`. Closes [#192](https://github.com/ICTU/update-time/issues/192).
- Override the cooldown period for a single reference with `# update-time: ignore[cooldown<30]`. Closes [#193](https://github.com/ICTU/update-time/issues/193).

### Changed

- Rename the digest-drift opt-in to `# update-time: allow[hash-drift]` and `--allow-hash-drift`, so it names what an image digest, a commit SHA, and an integrity hash have in common. Part of [#179](https://github.com/ICTU/update-time/issues/179).
- Warn about an `# update-time:` marker Update-time cannot read, such as a mistyped `ignore[stlae]` or a bracket that is never closed, instead of silently freezing the reference or dropping the bound it holds. Closes [#195](https://github.com/ICTU/update-time/issues/195).

### Fixed

- Leave a file's line endings alone when updating a reference in it: CRLF endings are no longer rewritten to LF, and a file without a final newline no longer gains one. Part of [#181](https://github.com/ICTU/update-time/issues/181).
- Apply a very large cooldown, from `--cooldown` or an `# update-time: ignore[cooldown<DAYS]` marker, instead of aborting the run with an error. Part of [#193](https://github.com/ICTU/update-time/issues/193).

## 0.0.22 - 2026-07-28

### Added

- Warn when the version a dependency is pinned to has been yanked from PyPI, or deprecated on npm, and hold the warning back with an `# update-time: ignore[yanked]` marker. Closes [#147](https://github.com/ICTU/update-time/issues/147).
- Honour `# update-time:` markers on the jsDelivr URLs in a Sphinx config, both inline and on the line above. Part of [#177](https://github.com/ICTU/update-time/issues/177).
- Pin a jsDelivr URL that declares no Subresource Integrity hash, by inserting one into the attribute dictionary that accompanies it. Closes [#177](https://github.com/ICTU/update-time/issues/177).

### Changed

- Report a recognised `# update-time:` marker as `Recognised update-time marker ...` instead of `Applying update-time marker ...`, so the message says the marker was understood rather than implying it had an effect.

### Fixed

- Log at log-level DEBUG when an `# update-time: ignore[stale]` or `# update-time: ignore[yanked]` marker holds a warning back. Closes [#169](https://github.com/ICTU/update-time/issues/169).
- Attribute the staleness, yank, redundant-bound, and GitHub pin log records to the updater that triggered them, so the source position in the log names the updater like every other record. Closes [#171](https://github.com/ICTU/update-time/issues/171).
- Warn when an `# update-time: ignore[yanked]` marker is on a reference whose source has no yank concept, so it can never hold anything back. Closes [#172](https://github.com/ICTU/update-time/issues/172).
- Report the log messages about a jsDelivr URL at the URL's line (`docs/conf.py:3`) instead of at the file only. Part of [#177](https://github.com/ICTU/update-time/issues/177).

## 0.0.21 - 2026-07-22

### Added

- Append the line number of a reference to the file location in the log (`docs/requirements.txt:42`), styled as one unit, so clicking it in a supporting terminal jumps to the line. Closes [#164](https://github.com/ICTU/update-time/issues/164).
- Update the CPython version pinned in `.python-version` files, following the Python base image in the project's Dockerfile or the latest [Python](https://hub.docker.com/_/python) release on Docker Hub. Closes [#142](https://github.com/ICTU/update-time/issues/142).
- Update the hook versions pinned in `.pre-commit-config.yaml` files: bump each GitHub-hosted hook's `rev:` to its latest version, pin an unpinned tag to a commit SHA using pre-commit's own `# frozen:` comment convention, and apply the cooldown and staleness check like any other dependency. Closes [#144](https://github.com/ICTU/update-time/issues/144).

### Changed

- Update the Node engine to the latest [Node](https://hub.docker.com/_/node) release on Docker Hub when no Dockerfile declares a numeric Node base image to derive it from, instead of logging an error. Part of [#156](https://github.com/ICTU/update-time/issues/156).

### Fixed

- Echo `# update-time:` markers back to the user exactly as written in the file — the whole marker, its `ignore` directive when an update is held back, and its `allow` directives when digest drift is adopted — so the log lines match what the user typed. Closes [#153](https://github.com/ICTU/update-time/issues/153).

## 0.0.20 - 2026-07-20

### Added

- Hold back major or minor updates with level-based bounds: `# update-time: ignore[minor-update]` on a `python:3.12` pin keeps `3.12` patch updates coming while blocking `3.13`. Unlike `ignore[update>=3.13]` it re-anchors to the pinned version on every run, so the comment survives migrations unedited. The `allow` complements (`allow[minor-update]`, `allow[patch-update]`) work too. Closes [#145](https://github.com/ICTU/update-time/issues/145).
- Support GitHub versions that are tagged but not released: a version that was tagged without being published as a GitHub release is now an update candidate too, with the publication date for the cooldown taken from the tagged commit. Closes [#143](https://github.com/ICTU/update-time/issues/143).

### Fixed

- Keep an image tag's spelling when the registry lists the same version under another name: a reference pinned to e.g. `node:22.15.0` was sometimes rewritten to the alias tag `node:22.15` — the same version, spelled shorter — when no newer version was eligible.

## 0.0.19 - 2026-07-16

### Added

- Bound how far a line-based reference may update by adding a version specifier to the `update` scope. For example, `# update-time: allow[update<3.13]` limits updates to those smaller than 3.13, so a `python:3.12.x` pin keeps getting `3.12` patches without jumping to `3.13`. Closes [#58](https://github.com/ICTU/update-time/issues/58).
- Update exact `==` pins in the [PEP 723](https://peps.python.org/pep-0723/) inline script metadata (`# /// script` blocks) of standalone `*.py` files, using uv, the same way `pyproject.toml` dependencies are updated. Closes [#29](https://github.com/ICTU/update-time/issues/29).
- Refuse to run when the scanned directory is not inside a git repository, so Update-time's in-place rewrites always have a `git restore` safety net. Pass `--force` to run anyway. Closes [#81](https://github.com/ICTU/update-time/issues/81).
- Colour the dependency name in bold white in the log output, so it stands out from the surrounding message when skimming a run. Closes [#128](https://github.com/ICTU/update-time/issues/128).

### Fixed

- Keep pnpm-managed `package.json` dependencies within their declared version ranges, as npm-managed dependencies already were, instead of bumping every dependency to the newest release regardless of the declared range. Closes [#135](https://github.com/ICTU/update-time/issues/135).
- Colour a `sha256:` digest as a single token in the log output. Rich's automatic highlighting previously matched only a fragment of a digest — reading e.g. `a256:a4fd` as an IPv6 address — so the digest came out partly coloured and partly plain.

## 0.0.18 - 2026-07-10

### Added

- Opt in to adopting image digest drift. When an already-pinned image tag has been re-pushed under the same version, Update-time still warns by default, but a new `# update-time: allow[digest-drift]` marker (or the global `--allow-image-digest-drift` flag) now makes it adopt the new digest instead. Closes [#120](https://github.com/ICTU/update-time/issues/120).

### Fixed

- Update and pin base images in Dockerfile `FROM` lines that carry a `--platform=…` flag (e.g. `FROM --platform=$BUILDPLATFORM python:3.14`), common in multi-arch builds. Previously such lines didn't match and were silently left un-updated and un-pinned. The `--platform=…` flag is left untouched. Closes [#90](https://github.com/ICTU/update-time/issues/90).

## 0.0.17 - 2026-07-09

### Added

- Warn when a dependency's newest release is older than a threshold, surfacing pins on abandoned or long-unmaintained projects. The threshold defaults to 365 days and is set with the new `--stale-after DAYS` option. Closes [#116](https://github.com/ICTU/update-time/issues/116).
- Scope the `# update-time: ignore` marker with an optional `[update]` or `[stale]` suffix: `ignore[update]` holds back the version update but still warns when the dependency is stale, and `ignore[stale]` silences the staleness warning while still updating. A bare `ignore` holds back both.
- Discover and update requirements files named `<purpose>-requirements.txt` (e.g. `dev-requirements.txt`), in addition to the already recognized `requirements.txt`, `requirements-<purpose>.txt` (e.g. `requirements-dev.txt`), and `requirements/*.txt`. Closes [#114](https://github.com/ICTU/update-time/issues/114).

## 0.0.16 - 2026-07-07

### Added

- Upgrade a version number embedded in an image tag's suffix. A pin such as `python:3.14.6-alpine3.23` now advances its `alpine3.23` to a newer `alpine3.24` (on its own or together with a bump of the main version, e.g. to `3.15.0-alpine3.24`) instead of keeping the suffix version forever. Closes [#107](https://github.com/ICTU/update-time/issues/107).
- Warn when an already-pinned image tag has been re-pushed with a different digest. If a reference such as `python:3.14.6@sha256:…` is already at the latest version but the registry now serves a different digest for that tag, Update-time logs a warning and leaves the pin unchanged, rather than silently adopting the new digest. Closes [#110](https://github.com/ICTU/update-time/issues/110).

### Fixed

- In the pathological case where an unpinned `uses:` reference in a GitHub Actions workflow was newer than the repo's latest eligible release, the reference would get pinned to the newer version with the older release's commit SHA. Closes [#104](https://github.com/ICTU/update-time/issues/104).

## 0.0.15 - 2026-07-05

### Added

- Accept an optional positional `PATH` argument to scan a directory other than the current one (`update-time ../other-project`), instead of having to `cd` there first. Closes [#86](https://github.com/ICTU/update-time/issues/86).
- Add a `--exclude-path` option to exclude a comma-separated list of directories, relative to the scan root, from the walk (`update-time --exclude-path vendor,packages/legacy`). Closes [#84](https://github.com/ICTU/update-time/issues/84).

### Fixed

- Keep uv-managed projects reproducible after an update: `uv sync --locked` no longer fails with "Resolving despite existing lockfile due to removal of global exclude newer". Update-time now writes its cooldown into `[tool.uv] exclude-newer` in `pyproject.toml` instead of passing it to uv only on the command line, so uv applies the same cooldown on every command. Closes [#94](https://github.com/ICTU/update-time/issues/94).

## 0.0.14 - 2026-07-03

### Added

- Recognise the `update-time: ignore` marker in `.devcontainer/devcontainer.json` and `.devcontainer.json`, written as a `//` comment since these files are JSONC.

### Changed

- Send all diagnostics — the new-version report as well as warnings and errors — to standard error instead of standard output, so stdout stays clean for the `--version`/`--help` output and the whole run is redirectable in one stream (`update-time 2> run.log`).

### Fixed

- Report a user-facing error for a non-integer `--cooldown` value; the message now reads `invalid days value: 'abc'` instead of leaking an internal identifier.
- Stop logging routine `npm`/`pnpm outdated` (and `list`) output as a failure. These commands exit non-zero as a normal "there are updates" signal, and pnpm additionally prints deprecation warnings to stderr, which Update-time surfaced as a misleading `WARNING Error running ...` on every run. Their stderr is now logged only on a genuine failure (when the command produced no usable output); action commands (`npm`/`pnpm update`, `uv lock`) still warn when they actually fail.

## 0.0.13 - 2026-07-02

### Added

- Update `package.json` files managed by [pnpm](https://pnpm.io) using pnpm, instead of skipping them, keeping both `package.json` and `pnpm-lock.yaml` in sync. The cooldown is applied via pnpm's `minimumReleaseAge`; yarn and bun are still skipped with a warning. Closes [#47](https://github.com/ICTU/update-time/issues/47).
- Update the base `image` and each `features` entry in `.devcontainer/devcontainer.json` and `.devcontainer.json`, bumping each OCI reference to its latest tag and pinning it with the digest. Devcontainers that build from a `dockerfile` or `dockerComposeFile` are updated as part of that Dockerfile or Compose file. Closes [#49](https://github.com/ICTU/update-time/issues/49).
- Update Dockerfiles named `*.Dockerfile` or `Dockerfile.*` (e.g. `python.Dockerfile`, `Dockerfile.dev`), not only files named exactly `Dockerfile`.

### Fixed

- Resolve images on registries that reject a host-prefixed repository path, such as `mcr.microsoft.com`, which previously returned a 404. The registry host is now dropped from the repository path for every registry.
- Apply the cooldown to jsDelivr npm URLs, which previously adopted a freshly published version immediately; Update-time now picks the latest version published outside the cooldown window. Closes [#68](https://github.com/ICTU/update-time/issues/68).
- Compute the jsDelivr Subresource Integrity hash for the file referenced in the URL rather than the package's default entry point, which previously crashed on packages whose default file jsDelivr doesn't list (e.g. `mathjax`). When the referenced file's hash can't be resolved, the reference is left unchanged and a warning is logged instead of crashing. Closes [#69](https://github.com/ICTU/update-time/issues/69).

## 0.0.12 - 2026-06-30

### Changed

- Log an unsupported package manager (pnpm/yarn/bun for `package.json`, Poetry/PDM for `pyproject.toml`) at the `WARNING` level instead of `INFO`, so the skipped dependency set stands out.

### Added

- Resolve image versions on registries other than Docker Hub (e.g. `ghcr.io`, `mcr.microsoft.com`, `gcr.io`, `quay.io`), wherever Update-time finds image references (Dockerfiles, Docker Compose / Helm manifests, CircleCI, GitLab CI). This reverses the earlier "skip non-Docker Hub images" behavior. The cooldown still only applies to Docker Hub, because the OCI protocol exposes no publication date. Closes [#48](https://github.com/ICTU/update-time/issues/48).
- Update image tags whose version carries a non-numeric label prefix, such as `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` (bumped to `python3.13-bookworm-slim` and digest-pinned), preserving the prefix and suffix. Previously these tags were left unchanged. Closes [#55](https://github.com/ICTU/update-time/issues/55).
- Leave a reference unchanged when its line carries an `# update-time: ignore` comment, so an update can be pinned deliberately. It works for Dockerfiles, Docker Compose / Helm manifests, CircleCI, GitLab CI, GitHub Actions, and `requirements.txt`, inline or as a comment on the line directly above, and each ignored reference is logged at `DEBUG`. Closes [#56](https://github.com/ICTU/update-time/issues/56).

## 0.0.11 - 2026-06-29

### Fixed

- Don't warn about CircleCI machine-executor images (the `image:` under a `machine:` key, such as `ubuntu-2204:2024.01.1`). These have no registry to query and are now recognised and left unchanged, instead of being looked up on Docker Hub and logged as unfetchable on every run.
- Skip `package.json` files managed by a package manager other than npm (pnpm, yarn, bun) instead of running npm against them, which would write a stray `package-lock.json` and leave the real lockfile out of sync.
- Skip `pyproject.toml` files managed by a Python dependency manager other than uv (Poetry, PDM) instead of running uv against them, which would write a stray `uv.lock` alongside the real lockfile.

## 0.0.10 - 2026-06-29

### Added

- Apply Update-time's cooldown to `pyproject.toml` dependency updates via uv's `exclude-newer`, so freshly published releases are held back for both the pins and the `uv.lock`. If the project already sets `exclude-newer` (or the `UV_EXCLUDE_NEWER` environment variable), Update-time leaves it untouched. Closes [#36](https://github.com/ICTU/update-time/issues/36).
- Apply Update-time's cooldown to npm dependency updates via npm's `min-release-age`. If the project already configures a cooldown in its `.npmrc`, Update-time leaves it untouched. npm applies the option from version 11.10.0 onwards; older versions update without a cooldown. Closes [#37](https://github.com/ICTU/update-time/issues/37).

### Fixed

- Include the file being updated in the "New version available" and "Pinned" messages, so it is clear which file each change applies to.

## 0.0.9 - 2026-06-29

### Changed

- Resolve the latest Docker Hub tag by listing tag names once and fetching metadata only for the chosen tag, instead of paginating through every tag's metadata. For heavily-tagged images such as `node` (~9,000 tags) this cuts the requests per image from ~90 to a handful. Closes [#34](https://github.com/ICTU/update-time/issues/34).

## 0.0.8 - 2026-06-28

### Added

- Add a `--log-level` option (`DEBUG`, `INFO`, `WARNING`, or `ERROR`) to control how much is logged. Default log level is `INFO`.

### Changed

- Log available new versions at the `INFO` level instead of `WARNING`, so the `WARNING` level is reserved for genuinely unexpected situations. The per-file "Checking ..." progress now logs at `DEBUG`.

## 0.0.7 - 2026-06-28

### Added

- Add support for updating exact pins (`package==version`) in hand-written `requirements.txt` files (also `requirements*.txt` and `requirements/*.txt`) against PyPI. Compiled or hash-pinned files (generated by pip-compile or `uv pip compile`) are left untouched. Closes [#21](https://github.com/ICTU/update-time/issues/21).

### Changed

- Skip image references hosted on registries other than Docker Hub up front instead of querying Docker Hub and logging a 404 for each on every run. Images referenced with an explicit Docker Hub host (`docker.io/...`, `index.docker.io/...`) are updated. Closes [#24](https://github.com/ICTU/update-time/issues/24).

### Fixed

- Don't rewrite `package.json` when there are no dependency updates. npm normalizes specs such as `git+https://...` URLs to the `github:` shorthand whenever it saves the manifest, which previously produced a spurious diff even when no version changed; the original file is now restored unless a registry version was actually updated. Closes [#27](https://github.com/ICTU/update-time/issues/27).

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
