# Update-time - it's time to update your dependencies

Keeping dependencies up-to-date is an important aspect of software maintenance. Update-time is a command line tool that scans your repository for dependencies and updates them to their latest versions. It looks at the files you already have — `pyproject.toml`, hand-written `requirements.txt` files, `package.json`, Dockerfiles, GitHub Actions workflows, CircleCI configs, GitLab CI configs, Docker Compose and Helm manifests, devcontainer configs, and jsDelivr URLs — and rewrites the pinned versions in place. To avoid adopting freshly published releases that may still be buggy, it applies a cooldown period (see [Cooldown](#cooldown) below).

## Usage

Run Update-time without installing it using [uvx](https://docs.astral.sh/uv/):

```console
uvx update-time
```

Or install it as a [uv tool](https://docs.astral.sh/uv/concepts/tools/) so it's always available on your `PATH`:

```console
uv tool install update-time
update-time
```

Update-time has a small command-line interface. Run `update-time -h`/`--help` to see all options, `update-time -V`/`--version` to print the version, `update-time --cooldown DAYS` to override the default cooldown period (see [Cooldown](#cooldown) below), and `update-time --log-level LEVEL` to set how much is logged (one of `DEBUG`, `INFO`, `WARNING`, `ERROR`; defaults to `INFO`). Available new versions are logged at `INFO`, so use `--log-level WARNING` to see only genuine problems, or `--log-level DEBUG` to also see which files are checked. All logging goes to standard error, leaving standard output for the `--version` and `--help` text. Running `update-time` with no options in the root folder of a repository updates all supported dependencies.

By default Update-time scans the current directory. Pass a positional `PATH` to scan another directory instead — `update-time ../other-project` — without having to `cd` there first. `PATH` defaults to the current directory, so existing invocations are unchanged, and the paths in the log are reported relative to it. If `PATH` does not exist or is not a directory, Update-time exits with status `2`.

To hold part of the tree back from the scan, pass `--exclude-path` a comma-separated list of directories relative to the scan root — `update-time --exclude-path vendor,packages/legacy`. Every file under an excluded directory is skipped by every updater. This is useful when several repositories are checked out under a shared root, or for a vendored or generated subtree you don't want touched. The excluded directories are matched by relative path, not by name: `--exclude-path vendor` excludes `vendor/` at the root but not `sub/vendor/`. The list extends the always-ignored `build`, `node_modules`, `__pycache__`, and hidden (dot-prefixed) folders rather than replacing them. A listed directory that doesn't exist is not an error (it is logged at `WARNING`), but an absolute path, or one that escapes the scan root (`../…`), is rejected with exit status `2`. Run with `--log-level DEBUG` to see each excluded directory logged.

Update-time exits with status `0` when it ran (whether or not it changed any files), `2` when the command-line arguments are invalid, and a non-zero status when an updater could not complete. The exit status does not indicate whether anything was updated — inspect the diff (or the `INFO`-level log) for that.

The recommended workflow is to run Update-time on a dedicated branch, push it, and let CI do the verification:

1. Create a branch for the updates.
2. Run `update-time` in the root of your repository to update the dependencies in place.
3. Commit the changes and open a pull request.
4. Let your tests and checks run in CI to confirm nothing is broken before merging.

To raise API rate limits while updating, set the following environment variables before running Update-time:

- `GITHUB_TOKEN` — increases the GitHub API rate limit when updating GitHub Actions. The token only needs to read public release and commit data, so no specific scope is required: both a classic token with no scopes selected and a fine-grained token with default read-only access to public repositories work.
- `DOCKER_HUB_USERNAME` and `DOCKER_HUB_TOKEN` — authenticate to the Docker Hub API (both must be set) to increase its rate limit when updating Docker images.

## What is updated

Update-time runs a set of updater scripts, each responsible for one kind of dependency. The file-rewriting scripts run concurrently where it's safe to do so; `package.json` engine and dependency updates run sequentially because they touch the same files.

| Dependency | Files | Source |
| ---------- | ----- | ------ |
| Python dependencies pinned with `==` | `pyproject.toml` | [PyPI](https://pypi.org) |
| Python dependencies pinned with `==` | hand-written `requirements*.txt`, `requirements/*.txt` | [PyPI](https://pypi.org) |
| npm and pnpm dependencies | `package.json` (and `package-lock.json` / `pnpm-lock.yaml`) | [npm registry](https://registry.npmjs.org) |
| Node engine version | `package.json` | the Node base image in the project's Dockerfile |
| Dockerfile base images (tag + digest) | `Dockerfile`, `*.Dockerfile`, `Dockerfile.*` | OCI registries ([Docker Hub](https://hub.docker.com), `ghcr.io`, `mcr.microsoft.com`, …) |
| CircleCI images (tag + digest) | CircleCI YAML configs | OCI registries ([Docker Hub](https://hub.docker.com), `ghcr.io`, `mcr.microsoft.com`, …) |
| GitLab CI images (tag + digest) | `.gitlab-ci.yml` | OCI registries ([Docker Hub](https://hub.docker.com), `ghcr.io`, `mcr.microsoft.com`, …) |
| Docker Compose and Helm images (tag + digest) | Compose files and Helm folder | OCI registries ([Docker Hub](https://hub.docker.com), `ghcr.io`, `mcr.microsoft.com`, …) |
| Devcontainer image and features (tag + digest) | `.devcontainer/devcontainer.json`, `.devcontainer.json` | OCI registries (`ghcr.io`, `mcr.microsoft.com`, [Docker Hub](https://hub.docker.com), …) |
| GitHub Action versions (SHA + tag) | workflow YAML files | [GitHub releases API](https://api.github.com) |
| jsDelivr npm URLs (version + SRI hash) | Sphinx config | [npm registry](https://registry.npmjs.org) |

Only versions specified with an exact match (`==` for Python, a concrete `tag` — optionally already pinned as `tag@sha256:digest` — for images) are updated; looser version specifiers are left untouched, so you can pin a maximum version to opt a dependency out of automatic updates. Where available, Update-time prints the changelog entries between the current and new version so you can review what changed.

In `requirements.txt` files only exact `==` pins are updated. The following are left untouched:

- **Git, VCS, and URL dependencies** (e.g. `git+https://github.com/org/repo.git@v8.0.3.0`, direct URLs, and `-e`/editable installs) — these are not registry versions, so Update-time does not bump their refs; update them manually.
- **Compiled or hash-pinned files** — a `requirements.txt` generated by [pip-tools](https://github.com/jazzband/pip-tools) or `uv pip compile` (recognised by an autogenerated header, a sibling `.in` file, or `--hash=` lines) is skipped entirely, because bumping a single pin without recompiling its transitive dependencies and hashes would corrupt the file. Regenerate these with your compiler instead.

References that are not yet pinned are pinned automatically:

- **Docker images** referenced by tag only — base images in Dockerfiles (`FROM image:tag`), CircleCI images, GitLab CI images, Docker Compose / Helm manifest images, and devcontainer base images and features — get the `@sha256:digest` of the (latest) tag appended, so the image is reproducible. The image's registry is taken from the reference, so images on Docker Hub and on other OCI registries (`ghcr.io`, `mcr.microsoft.com`, …) are both resolved; the cooldown, however, only applies to Docker Hub, since the OCI protocol exposes no publication date. Images without a concrete version tag are ignored: references through a template (`{{ ... }}`) or variable substitution (`${VAR}`), and tagless base images such as `FROM scratch` or stage references. CircleCI machine-executor images (the `image:` under a `machine:` key, such as `ubuntu-2204:2024.01.1`) are also left alone, since they are not registry images.
- **GitHub Actions** referenced by version tag only (e.g. `uses: actions/checkout@v4`) are pinned to the commit SHA of the latest version, with the version added as a trailing comment (e.g. `uses: actions/checkout@<sha> # v4.1.1`). Actions referenced by a branch (e.g. `@main`) are left untouched because they don't resolve to a version.

## Excluding a reference from updates

To stop Update-time from changing a specific reference — because of a known incompatibility, a deferred migration, or to keep something reproducible — add an `# update-time: ignore` comment (all lower-case). The reference is then left untouched and no registry or source is queried for it. You can add a reason after the marker, for example `# update-time: ignore (pinned until the 3.13 migration)`.

The marker can be placed two ways:

- **Inline**, on the reference's own line (in YAML files, `requirements.txt`, and — with a `//` comment — `devcontainer.json`):

  ```yaml
  image: python:3.12  # update-time: ignore
  ```

  ```text
  humanize==4.15.0  # update-time: ignore
  ```

  ```jsonc
  "ghcr.io/devcontainers/features/node:1": {}  // update-time: ignore
  ```

- **On the line directly above** the reference. Use this form in Dockerfiles, which don't allow inline comments:

  ```dockerfile
  # update-time: ignore
  FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
  ```

This works for every reference Update-time rewrites line by line: Dockerfiles, Docker Compose and Helm manifests, CircleCI and GitLab CI configs, GitHub Actions workflows, `devcontainer.json` files, and `requirements.txt` files. Use a `#` comment everywhere except `devcontainer.json` (which is JSONC), where the marker goes in a `//` comment. An inline marker pins only its own line, so it never accidentally pins the reference on the line below it.

Run with `--log-level DEBUG` to see each ignored reference logged and confirm a marker is recognised. Since the marker is case-sensitive, a typo (or wrong case) simply produces no such log and the reference is updated as usual.

For `pyproject.toml` and `package.json` — which are updated through uv, npm, and pnpm rather than line by line — the marker does not apply. Opt a dependency out there by pinning it with a maximum or non-`==` version specifier instead (for example `package<=3.12`). The marker likewise has no effect on jsDelivr URLs, which are rewritten through a whole-file substitution rather than the line-by-line engine; there is currently no way to exclude a specific jsDelivr URL from updates.

## Cooldown

To avoid adopting releases that are too fresh to trust, Update-time honours a cooldown period during which newly published versions are not yet picked up. It defaults to **7 days** and can be changed with the `--cooldown` option, for example `update-time --cooldown 14`. How the cooldown is applied depends on the dependency type:

- **Docker images, GitHub Actions, `requirements.txt` dependencies, and jsDelivr npm URLs** — Update-time enforces the cooldown itself, based on each image tag's push date and each release's publication date.
- **npm dependencies** — Update-time passes the cooldown to `npm` via npm's `min-release-age` option (also measured in days), which npm added in 11.10.0; older npm versions ignore the option, so updates still run but without a cooldown. If your project already configures a cooldown in its `.npmrc` (`min-release-age` or `before`), Update-time leaves that in place instead of overriding it.
- **pnpm dependencies** — Update-time passes the cooldown to `pnpm` via pnpm's `minimumReleaseAge` setting, converting the value to minutes (pnpm measures the age in minutes rather than days). If your project already configures `minimumReleaseAge` (in `pnpm-workspace.yaml`), Update-time leaves that in place instead of overriding it.
- **`pyproject.toml` dependencies** — Update-time applies the cooldown through [uv](https://docs.astral.sh/uv/)'s `exclude-newer` setting, which it writes into your `pyproject.toml` under `[tool.uv]` (as a relative value such as `exclude-newer = "7 days"`, tagged with a `managed by Update-time` comment). It writes this to the workspace root, so a plain `uv sync --locked` keeps working afterwards without having to repeat the setting on the command line. Because the value lives in `[tool.uv]`, the cooldown then applies to every uv command in the project (`uv lock`, `uv add`, CI), not just to Update-time. Update-time keeps its own (commented) value in step with `--cooldown`, but never touches a value you set yourself: if your `pyproject.toml` already sets `exclude-newer` (without the marker comment), or the `UV_EXCLUDE_NEWER` environment variable is set, Update-time leaves that in place instead. Remove the marker comment to take ownership of the line and stop Update-time from changing it.

## Point of contact

Point of contact for this repository is [Frank Niessink](https://github.com/fniessink).
