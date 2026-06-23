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

export COVERAGE_RCFILE := justfile_directory() + "/.coveragerc"
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
vulture_whitelist := ".vulture-whitelist.py"
coverage := uv_run + " coverage"

# === Build and publish ===

# Build and publish the distribution packages.
publish:
    rm -rf build dist
    uv build
    uv publish --token `uvx python -c "import configparser, pathlib; c = configparser.ConfigParser(); c.read(pathlib.Path('.pypirc').expanduser()); print(c['pypi']['password'])"`
    git tag v`uvx python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"`
    git push --tags

# === Run tests ===

# Run the unit tests.
[env("PYTHONDEVMODE", "1")]
[env("PYTHONPATH", "src")]
test *tests: install-py-dependencies
    # Show a spinner while running; suppress output unless the run fails; with coverage, also write the reports (xml fails if coverage is too low, but only after the text and HTML reports have been generated).
    {{ start_progress() }} {{ coverage }} run -m unittest --quiet {{ tests }} && {{ coverage }} report --fail-under=0 && {{ coverage }} html --quiet --fail-under=0 && {{ coverage }} xml --quiet {{ end_progress("test") }}

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

# Run fixit to lint Python code.
[private]
fixit: (py-check "fixit" f"{{fixit}} lint {{code}}")

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
    {{ start_capture() }} {{ uv_run }} yamllint --strict -c .yamllint -f {{ when_color("colored", "auto") }} . {{ end_capture("yamllint") }}

# Run zizmor to audit GitHub Action workflows.
[private]
zizmor:
    {{ start_capture() }} {{ uv_run }} zizmor --no-progress --quiet .github/workflows {{ end_capture("zizmor") }}

# Check the justfile for correct formatting.
[private]
check-justfile:
    {{ start_capture() }} {{ just_fmt }} --check --color=$_color {{ end_capture("check-justfile") }}

# Run the quality checks
[parallel]
check: ty mypy fixit ruff pyproject-fmt troml pip-audit uv-audit bandit vulture codespell check-justfile yamllint zizmor

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

# === CI ===

# Run SonarCloud prerequisites
_sonarcloud: test
    {{ coverage }} xml # SonarCloud needs a Cobertura compatible XML coverage report
    {{ uv_run }} python -m xmlrunner discover --output-file build/xunit.xml  # SonarCloud needs a JUnit compatible XML report

# Run everything in CI.
_ci: _sonarcloud check

# === Folders ===

exists(path) := path_exists(invocation_directory() + "/" + path)
src_folder := if exists("src") == "true" { "src" } else { "" }
tests_folder := if exists("tests") == "true" { "tests" } else { "" }
code := if trim(src_folder + " " + tests_folder) == "" { ".?*.py" } else { src_folder + " " + tests_folder }

# === Output functions ===

# Prefix and suffix that wrap a command (such as a check): `{{ start_capture() }} <cmd> {{ end_capture(name) }}` captures stdout+stderr, prints `<recipe-name> [<folder>/ ]OK` or `NOK`, and replays the captured output on failure.
folder_prefix(folder) := if folder == "" { "" } else if folder == "." { "" } else { " " + trim_end_match(folder, "/") + "/" }

# Pick a tool-flag value based on `$_color` set by `start_capture`. Useful for tools whose color flag values aren't `auto`/`always`/`never` (e.g. bandit's `screen`/`txt`, yamllint's `colored`/`auto`).
when_color(yes, no) := f'$([ "$_color" = always ] && echo {{yes}} || echo {{no}})'

start_capture() := f'_color=auto; [ -t 1 ] && { _color=always; export FORCE_COLOR=1; }; output=$({'
end_capture(name) := f'; } 2>&1) || { printf "%s%s {{RED}}NOK{{NORMAL}}\n%s\n" {{name}} "$output"; exit 1; }; printf "%s%s {{GREEN}}OK{{NORMAL}}\n" {{name}}'

# Like start_capture/end_capture, but for slow commands (e.g. tests): run them in the background and animate a spinner while they run. The spinner only shows for an unlabelled run on a terminal (a direct, interactive `just test`); labelled runs (a parallel `ci` or fan-out) skip it, so it never smears into their atomic OK/NOK lines.
start_progress() := f'if [ -t 1 ]; then spin=1; else spin=; fi; tmp=$(mktemp); trap "rm -f $tmp" EXIT; { '
end_progress(name) := f'; } > "$tmp" 2>&1 & pid=$!; sp="|/-\\"; while kill -0 "$pid" 2>/dev/null; do [ -n "$spin" ] && printf "\r%c" "$sp"; sp="${sp#?}${sp%???}"; sleep 0.1; done; [ -n "$spin" ] && printf "\r"; wait "$pid" && printf "%s%s {{GREEN}}OK{{NORMAL}}\n" {{name}} || { printf "%s%s {{RED}}NOK{{NORMAL}}\n%s\n" {{name}} "$(cat "$tmp")"; exit 1; }'
