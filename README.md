# Update-time - it's time to update your dependencies

Keeping dependencies up-to-date is an important aspect of software maintenance. Update-time is a command line tool that scans your repository for dependencies and updates them to their latest versions. It looks at the files you already have — `pyproject.toml`, `package.json`, Dockerfiles, GitHub Actions workflows, CircleCI configs, GitLab CI configs, Docker Compose and Helm manifests, and jsDelivr URLs — and rewrites the pinned versions in place. To avoid adopting freshly published releases that may still be buggy, it applies a cooldown period (see [Cooldown](#cooldown) below).

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
| npm dependencies | `package.json` (and `package-lock.json`) | [npm registry](https://registry.npmjs.org) |
| Node engine version | `package.json` | the Node base image in the project's Dockerfile |
| Dockerfile base images (tag + digest) | `Dockerfile` | [Docker Hub](https://hub.docker.com) |
| CircleCI images (tag + digest) | CircleCI YAML configs | [Docker Hub](https://hub.docker.com) |
| GitLab CI images (tag + digest) | `.gitlab-ci.yml` | [Docker Hub](https://hub.docker.com) |
| Docker Compose and Helm images (tag + digest) | Compose files and Helm folder | [Docker Hub](https://hub.docker.com) |
| GitHub Action versions (SHA + tag) | workflow YAML files | [GitHub releases API](https://api.github.com) |
| jsDelivr npm URLs (version + SRI hash) | Sphinx config | [npm registry](https://registry.npmjs.org) |

Only versions specified with an exact match (`==` for Python, a concrete `tag` — optionally already pinned as `tag@sha256:digest` — for images) are updated; looser version specifiers are left untouched, so you can pin a maximum version to opt a dependency out of automatic updates. Where available, Update-time prints the changelog entries between the current and new version so you can review what changed.

References that are not yet pinned are pinned automatically:

- **Docker images** referenced by tag only — base images in Dockerfiles (`FROM image:tag`), CircleCI images, GitLab CI images, and Docker Compose / Helm manifest images — get the `@sha256:digest` of the (latest) tag appended, so the image is reproducible. Images without a concrete version tag are ignored: references through a template (`{{ ... }}`) or variable substitution (`${VAR}`), and tagless base images such as `FROM scratch` or stage references.
- **GitHub Actions** referenced by version tag only (e.g. `uses: actions/checkout@v4`) are pinned to the commit SHA of the latest version, with the version added as a trailing comment (e.g. `uses: actions/checkout@<sha> # v4.1.1`). Actions referenced by a branch (e.g. `@main`) are left untouched because they don't resolve to a version.

## Cooldown

To avoid adopting releases that are too fresh to trust, Update-time honours a cooldown period during which newly published versions are not yet picked up. Where the cooldown comes from depends on the dependency type:

- **Docker images and GitHub Actions** — Update-time enforces its own cooldown, based on each image tag's push date and each release's publication date. It defaults to **7 days** and can be changed with the `--cooldown` option, for example `update-time --cooldown 14`.
- **Python dependencies** — Update-time delegates the actual updating to [uv](https://docs.astral.sh/uv/), so the cooldown is whatever you configure for uv. Use the `exclude-newer` setting under `[tool.uv]` in your `pyproject.toml` to hold back recently released versions.
- **npm dependencies** — Update-time delegates the actual updating to `npm`, so the cooldown is whatever you configure for npm (for example via your `.npmrc`).

Because Python and npm updates are delegated, the built-in 7-day cooldown does **not** apply to them; configure those tools directly if you want a cooldown there.

## Point of contact

Point of contact for this repository is [Frank Niessink](https://github.com/fniessink).
