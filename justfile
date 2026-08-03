set guards
set positional-arguments
set quiet
set unstable # for user-defined functions

_default:
    @just --list

# List all recipes, or show usage (options and arguments) for one recipe, e.g. `just help test`.
help recipe="":
    @if [ -z "{{ recipe }}" ]; then just --list; \
    else just --usage {{ recipe }}; \
    if just --show {{ recipe }}-help > /dev/null 2>&1; then just {{ recipe }}-help; fi; fi

# Enable uv's malware check on every sync (it can't be enabled via pyproject.toml). The experimental-feature
# warning is suppressed by the --quiet flag on `uv sync`. See https://astral.sh/blog/uv-audit.
export UV_MALWARE_CHECK := "1"

uv_run := "uv run --quiet"
fixit := uv_run + " fixit --quiet"
just_fmt := "just --unstable --fmt"
pyproject_fmt := uv_run + " pyproject-fmt --no-generate-python-version-classifiers"
ruff := uv_run + " ruff --quiet"
troml := uv_run + " troml"
ty := uv_run + ' ty check --no-progress --error-on-warning --color=${_color:-auto}'
vulture := uv_run + " vulture --exclude .venv --min-confidence 0"
vulture_whitelist := "tools/vulture-whitelist.py"
coverage := uv_run + " coverage"

# === Build and publish ===

# Check that the version is well-formed (x.y.z or vx.y.z), greater than the current version, and not already tagged.
# Runs first, before the slow test and check recipes, so a bad version argument fails fast.
[private]
check-version version:
    #!/usr/bin/env bash
    set -euo pipefail
    new_version="{{ version }}"; new_version="${new_version#v}"  # Accept both x.y.z and vx.y.z
    uv run --quiet python -c '
    import pathlib, re, sys, tomllib
    from packaging.version import Version
    new = sys.argv[1]
    re.fullmatch(r"\d+\.\d+\.\d+", new) or sys.exit(f"Error: version {new!r} must be of the form x.y.z or vx.y.z")
    current = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"]
    Version(new) > Version(current) or sys.exit(f"Error: new version {new} must be greater than current version {current}")
    ' "$new_version"
    if git rev-parse -q --verify "refs/tags/v$new_version" >/dev/null 2>&1 || git ls-remote --exit-code --tags origin "refs/tags/v$new_version" >/dev/null 2>&1; then
        echo "Error: tag v$new_version already exists" >&2; exit 1
    fi

# Check that the repository is in a releasable state: on the main branch, clean, in sync with origin, and with an
# [Unreleased] section in the changelog to roll over.
[private]
check-repo:
    #!/usr/bin/env bash
    set -euo pipefail
    [ "$(git branch --show-current)" = main ] || { echo "Error: releases must be made from the main branch" >&2; exit 1; }
    [ -z "$(git status --porcelain)" ] || { echo "Error: the working tree has uncommitted changes; commit or stash them before releasing" >&2; exit 1; }
    grep -q '^## \[Unreleased\]$' CHANGELOG.md || { echo "Error: CHANGELOG.md has no '## [Unreleased]' section to release" >&2; exit 1; }
    git fetch --quiet
    [ "$(git rev-parse @)" = "$(git rev-parse '@{u}')" ] || { echo "Error: the main branch is not in sync with origin; push or pull first" >&2; exit 1; }

# Release a new version. Pass -c/--check after the version (e.g. `just publish 1.2.3 --check`) for a dry run that
# rehearses the build and upload without committing, tagging, or pushing, and restores the working tree afterwards.
publish version *flags: (check-version version) check-repo test check
    #!/usr/bin/env bash
    set -euo pipefail
    new_version="{{ version }}"; new_version="${new_version#v}"  # Accept both x.y.z and vx.y.z
    dry_run=false
    case " $* " in *" -c "* | *" --check "*) dry_run=true ;; esac
    # On a dry run, restore the files we are about to touch whenever the recipe exits:
    [ "$dry_run" = true ] && trap 'git checkout --quiet -- pyproject.toml CHANGELOG.md uv.lock' EXIT
    # Bump pyproject.toml and roll over the changelog's [Unreleased] section (the version was validated above):
    uv run --quiet python -c '
    import datetime, pathlib, re, sys
    new = sys.argv[1]
    pyproject = pathlib.Path("pyproject.toml")
    pyproject.write_text(re.sub(r"(?m)^version = \".*\"$", f"version = \"{new}\"", pyproject.read_text(), count=1))
    changelog = pathlib.Path("CHANGELOG.md")
    today = datetime.date.today().isoformat()
    changelog.write_text(changelog.read_text().replace("## [Unreleased]", f"## [Unreleased]\n\nNo changes yet.\n\n## {new} - {today}", 1))
    ' "$new_version"
    uv lock --quiet  # Resync the lock file to the new version
    rm -rf build dist
    uv build
    # Read the PyPI token from .pypirc; passed to uv publish in both modes so a dry run does not prompt for credentials:
    pypi_token="$(uvx python -c "import configparser, pathlib; c = configparser.ConfigParser(); c.read(pathlib.Path('.pypirc').expanduser()); print(c['pypi']['password'])")"
    if [ "$dry_run" = true ]; then
        uv publish --dry-run --token "$pypi_token"
        echo "Dry run for v$new_version: build and upload validated; nothing committed, tagged, or pushed; working tree restored."
    else
        git commit pyproject.toml CHANGELOG.md uv.lock --message "Release v$new_version"
        uv publish --token "$pypi_token"
        git tag --annotate "v$new_version" --message "Release v$new_version"
        git push --follow-tags
        echo "Published Update-time v$new_version"
    fi

# === Run tests ===

# A full run goes through coverage and must reach 100%: the text and HTML reports are written first, then `xml` applies the gate. A named subset cannot reach 100%, so it runs without coverage and leaves the reports from the last full run in place.
test_command(tests) := if tests == "" { coverage + " run -m unittest --quiet && " + coverage + " report --fail-under=0 && " + coverage + " html --quiet --fail-under=0 && " + coverage + " xml --quiet" } else { uv_run + " python -m unittest --quiet " + tests }

# Run the unit tests, all of them or only the ones named, e.g. `just test tests.update_time.io.test_log`.
[env("PYTHONDEVMODE", "1")]
[env("PYTHONPATH", "src")]
test *tests: install-py-dependencies
    # Show a spinner while running and suppress the output unless the run fails.
    {{ start_progress() }} {{ test_command(tests) }} {{ end_progress("test") }}

# === Run checks ===

# Run a Python check.
[private]
py-check name check: install-py-dependencies
    {{ start_capture() }} {{ check }} {{ end_capture(name) }}

# Run ty to type check Python code.
[private]
ty: (py-check "ty" f"{{ty}} {{code}}")

# Run mypy to type check Python code.
[private]
mypy: (py-check "mypy" f"{{uv_run}} mypy {{code}}")

# Run fixit to lint Python code, after checking the local fixit rules with their own test cases.
[private]
fixit: (py-check "fixit" f"{{fixit}} test .tools.fixit_rules && {{fixit}} lint {{code}}")

# Run ruff to lint and check the formatting of Python code.
[private]
ruff: (py-check "ruff" f"{{ruff}} format --check {{code}} && {{ruff}} check {{code}}")

# Run pyproject-fmt to check the formatting of pyproject.toml files.
[private]
pyproject-fmt: (py-check "pyproject-fmt" f"{{pyproject_fmt}} --check pyproject.toml")

# Run troml to the check the classifiers in pyproject.toml files.
[private]
troml: (py-check "troml" f"{{troml}} check")

# Run pip-audit to check Python dependencies for known security vulnerabilities.
[private]
pip-audit: install-py-dependencies
    req=$(mktemp); trap "rm -f $req" EXIT; \
    {{ start_capture() }} uv export --quiet --color never --no-emit-local --format requirements-txt > $req && \
    {{ uv_run }} pip-audit --requirement $req --disable-pip --progress-spinner off {{ end_capture("pip-audit") }}

# Run uv audit to check Python dependencies for known security vulnerabilities.
[private]
uv-audit: (py-check "uv-audit" f"uv audit --locked --quiet")

# Run bandit to check Python code for security vulnerabilities.
[private]
bandit: (py-check "bandit" f"{{uv_run}} bandit --configfile pyproject.toml --quiet --recursive --format {{when_color("screen", "txt")}} {{code}}")

# Run vulture to check for dead Python code.
[private]
vulture: (py-check "vulture" f"{{vulture}} {{code}} {{vulture_whitelist}}")

# Run codespell to check for common misspellings.
[private]
codespell: (py-check "codespell" f"{{uv_run}} codespell")

# Run yamllint to lint YAML files such as workflow definitions.
[private]
yamllint:
    {{ start_capture() }} {{ uv_run }} yamllint --strict -c tools/yamllint.yml -f {{ when_color("colored", "auto") }} . {{ end_capture("yamllint") }}

# Run zizmor to audit GitHub Action workflows.
[private]
zizmor:
    {{ start_capture() }} {{ uv_run }} zizmor --no-progress --quiet .github/workflows {{ end_capture("zizmor") }}

# Check the justfile for correct formatting.
[private]
check-justfile:
    {{ start_capture() }} {{ just_fmt }} --check --color=$_color {{ end_capture("check-justfile") }}

# Check that README.md and the log-output screenshot are what regenerating them produces.
[private]
check-readme: (py-check "check-readme" f"PYTHONPATH=src {{uv_run}} python -m docs.generate_readme --check")

# Check prose for too complex sentences.
[private]
check-sentence-complexity:
    {{ start_capture() }} {{ uv_run }} --script tools/sentence_complexity_check.py {{ code }} {{ end_capture("check-sentence-complexity") }}

# Run the quality checks. Run one by name for a quicker loop, e.g. `just ruff` or `just mypy`.
[parallel]
check: ty mypy fixit ruff pyproject-fmt troml pip-audit uv-audit bandit vulture codespell check-justfile check-readme check-sentence-complexity yamllint zizmor

# === Fix issues ===

# Fix quality issues that can be fixed automatically
fix: install-py-dependencies
    {{ ty }} --fix {{ code }}
    {{ ruff }} format {{ code }}
    {{ ruff }} check --fix {{ code }}
    {{ fixit }} fix {{ code }}
    # Pyproject-fmt returns exit code 1 when pyproject.toml needs formatting, ignore it when formatting:
    {{ pyproject_fmt }} --no-print-diff pyproject.toml || true
    {{ troml }} suggest --fix
    # Vulture returns exit code 3 when there is dead code, ignore it when writing the whitelist:
    {{ vulture }} --make-whitelist {{ code }} > {{ vulture_whitelist }} || true
    {{ just_fmt }}

# === Install dependencies ===

# Install Python dependencies from the lock file.
[private]
install-py-dependencies:
    {{ start_capture() }} uv sync --no-progress --locked --all-extras --all-groups {{ end_capture("install-py-dependencies") }}

# === Update dependencies ===

# Update direct and indirect dependencies. Set GITHUB_TOKEN, DOCKER_HUB_USERNAME, and DOCKER_HUB_TOKEN to prevent hitting rate limits.
update-dependencies:
    {{ uv_run }} src/update_time/updaters/update.py

alias update-deps := update-dependencies

# === Documentation ===

# Regenerate README.md from docs/README.md.in (fills in `update-time -h` and the log output; rewrites the screenshot).
[env("PYTHONPATH", "src")]
readme:
    {{ uv_run }} python -m docs.generate_readme

# === CI ===

# Run SonarCloud prerequisites
_sonarcloud: test
    {{ coverage }} xml # SonarCloud needs a Cobertura compatible XML coverage report
    {{ uv_run }} python -m xmlrunner discover --output-file build/xunit.xml  # SonarCloud needs a JUnit compatible XML report

# Run everything in CI.
_ci: _sonarcloud check

# === Folders ===

code := "src tests docs"

# === Output functions ===

# Pick a tool-flag value based on `$_color` set by `start_capture`. Useful for tools whose color flag values aren't `auto`/`always`/`never` (e.g. bandit's `screen`/`txt`, yamllint's `colored`/`auto`).
when_color(yes, no) := f'$([ "$_color" = always ] && echo {{yes}} || echo {{no}})'

# Prefix and suffix that wrap a command (such as a check): `{{ start_capture() }} <cmd> {{ end_capture(name) }}` captures stdout+stderr, prints `<recipe-name> PASS` or `FAIL`, and replays the captured output on failure. Neither token contains the other, so a run's outcome cannot be misread by matching on a substring.
start_capture() := f'_color=auto; [ -t 1 ] && { _color=always; export FORCE_COLOR=1; }; output=$({'
end_capture(name) := f'; } 2>&1) || { printf "%s {{RED}}FAIL{{NORMAL}}\n%s\n" {{name}} "$output"; exit 1; }; printf "%s {{GREEN}}PASS{{NORMAL}}\n" {{name}}'

# Like start_capture/end_capture, but for slow commands (e.g. tests): run them in the background and animate a spinner while they run. The spinner only shows on a terminal (a direct, interactive `just test`); a parallel `ci` run isn't one, so it never smears into those atomic PASS/FAIL lines.
start_progress() := f'if [ -t 1 ]; then spin=1; else spin=; fi; tmp=$(mktemp); trap "rm -f $tmp" EXIT; { '
end_progress(name) := f'; } > "$tmp" 2>&1 & pid=$!; sp="|/-\\"; while kill -0 "$pid" 2>/dev/null; do [ -n "$spin" ] && printf "\r%c" "$sp"; sp="${sp#?}${sp%???}"; sleep 0.1; done; [ -n "$spin" ] && printf "\r"; wait "$pid" && { count=$(grep -m1 "^Ran " "$tmp" | cut -d" " -f2); printf "%s {{GREEN}}PASS{{NORMAL}} (%s tests)\n" {{name}} "${count:-?}"; } || { printf "%s {{RED}}FAIL{{NORMAL}}\n%s\n" {{name}} "$(cat "$tmp")"; exit 1; }'
