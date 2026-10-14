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

# Run a tool from the environment without syncing it first, since the malware check makes every sync ask a server.
# So every recipe running it depends on `install-py-dependencies`, which syncs where the environment is out of date.
uv_run := "uv run --quiet --no-sync"
fixit := uv_run + " fixit --quiet"
just_fmt := "just --unstable --fmt"
pyproject_fmt := uv_run + " pyproject-fmt --no-generate-python-version-classifiers"
python_m := uv_run + " python -m"
ruff := uv_run + " ruff --quiet"
troml := uv_run + " troml"
ty := uv_run + ' ty check --no-progress --error-on-warning --color=${_color:-auto}'
vulture := uv_run + " vulture --exclude .venv --min-confidence 0"
vulture_whitelist := "tools/vulture-whitelist.py"
prose_whitelist := "tools/prose-whitelist.txt"
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

# Release a new version. Pass -c/--check after the version (e.g. `just publish 1.2.3 --check`) for a dry run that leaves the working tree as it was.
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

# The variable `tests/mutation.py` reads to make the registered mutations stand aside. A test compares this spelling with the name that file gives it, so the two cannot drift apart.
mutation_checks_off := "_UPDATE_TIME_MUTATION_CHECKS_OFF"

# Switch the registered mutations off for a command, and tell whether a caller switched them off. Both spell the
# value 1 that `tests/mutation.py` reads.
switch_mutations_off := "env " + mutation_checks_off + "=1"
mutations_switched_off := '[ "${' + mutation_checks_off + ':-}" = 1 ]'

# Wrap a command in the spinner and the PASS or FAIL line. A recipe that chooses between commands cannot keep the pair in its body, so it builds the wrapped command instead.
progress(name, command) := start_progress() + " " + command + " " + end_progress(name)

# The registered mutations stand aside while coverage runs, so coverage is measured over the tests alone and a line that only a mutated re-run reaches shows up as a gap.
measured_run := switch_mutations_off + " " + coverage + " run -m unittest --quiet"

# A measured run must reach 100%. The text report applies that gate and lists the lines no test reaches. A named subset is skipped rather than measured, since it reaches too little of the tree to meet the gate and its report names every file it never imports.
coverage_command(tests) := if tests == "" { progress("test-coverage", measured_run + " && " + coverage + " report --show-missing") } else { skipped("test-coverage", "a named subset is not measured") }

# The tests carrying a kills decorator run again, unmeasured, against the mutations they register. A caller that switched the registered mutations off already, as `just mutate` does, leaves this pass nothing to add to the coverage pass, so it says so rather than running those tests twice. A named subset, which the coverage pass skips, runs here whole either way, labelled a plain `test` run when the registered mutations are off.
mutations_command(tests) := if tests == "" { 'if ' + mutations_switched_off + '; then ' + skipped("test-mutations", "the registered mutations are switched off") + '; else ' + mutations_run(tests) + "; fi" } else { mutations_run(tests) }

# Run the mutations pass's tests, labelled a plain `test` run when a caller switched the registered mutations off.
mutations_run(tests) := 'label=test-mutations; if ' + mutations_switched_off + '; then label=test; fi; ' + progress('"$label"', mutations_tests(tests))

# The tests the mutations pass runs. A full run loads the tests carrying a kills decorator alone, since the coverage pass runs every other test already.
mutations_tests(tests) := if tests == "" { uv_run + " python -c 'import unittest; from tests.mutation import KillsTestLoader; unittest.main(module=None, testLoader=KillsTestLoader())' --quiet" } else { python_m + " unittest --quiet " + named_tests(tests) }

# The tests named, each test package among them expanded into its test modules, since `unittest` does not run the tests in a package it is handed.
named_tests(tests) := "$(" + python_m + " tools.expand_tests " + tests + ")"

# Run the unit tests under coverage, with the registered mutations standing aside, so coverage is measured over the tests alone. A named subset is skipped rather than measured.
test-coverage *tests: install-py-dependencies install-nltk-data (coverage-pass tests)

# Run the unit tests against the mutations they register: every test carrying a kills decorator, or every test named, e.g. `just test-mutations tests.update_time.io.test_log`. A test package named runs every test module under it.
test-mutations *tests: install-py-dependencies install-nltk-data (mutations-pass tests)

# Run the unit tests, all of them or only the ones named, e.g. `just test tests.update_time.io.test_log` or `just test tests.update_time.io`. The two passes run at the same time, neither reading what the other writes.
test *tests: install-py-dependencies install-nltk-data (test-passes tests)

# The pass of `test-coverage`, without the installs it needs, so a nested `just` can run it after the outer one installed.
[env("PYTHONDEVMODE", "1")]
[env("PYTHONPATH", "src")]
[private]
coverage-pass *tests:
    {{ coverage_command(tests) }}

# The pass of `test-mutations`, without the installs it needs.
[env("PYTHONDEVMODE", "1")]
[env("PYTHONPATH", "src")]
[private]
mutations-pass *tests:
    {{ mutations_command(tests) }}

# Both passes at the same time, without the installs they need.
[parallel]
[private]
test-passes *tests: (coverage-pass tests) (mutations-pass tests)

# Check that a test guards a behaviour: break the code it names, run the tests, and restore the file. See `just help mutate`.
mutate file *command: install-py-dependencies
    # FORCE_COLOR is unset rather than emptied: a tool reads it as set whatever its value, and the probe pipes
    # what the command writes, so colour there would only stand between the words the probe reads back.
    env -u FORCE_COLOR {{ python_m }} tools.mutate "$@"

[private]
mutate-help:
    @echo "\nBreak FILE by replacing a snippet in it, run COMMAND (default: just test), and restore FILE whatever"
    @echo "happens. Reads the snippet to replace and its replacement from stdin, separated by a line holding only @@:"
    @echo "\n    just mutate src/pkg/module.py <<'EOF'"
    @echo "    the code the test names"
    @echo "    @@"
    @echo "    a broken version of it"
    @echo "    EOF"
    @echo "\nThe snippet's first line drops its leading indentation; later lines keep theirs as in the file."
    @echo "\nName a definition after the file to look for the snippet inside that definition alone, so a snippet the"
    @echo "file repeats needs no padding to tell one occurrence from another:"
    @echo "\n    just mutate tests/mutation.py:Mutation._mutated <<'EOF'"
    @echo "    ..."
    @echo "    EOF"
    @echo "\nExits 0 when the mutation was killed (a test failed, so it is guarded), 1 when it survived (nothing"
    @echo "guards it), 2 when the probe never ran (it says why, such as a snippet FILE does not hold exactly once),"
    @echo "3 when the run was killed but reported errors, which a stub that broke the file does as much as a guard"
    @echo "that raised, and 4 when COMMAND failed although every test passed. Exit 4 means no test killed the"
    @echo "mutation: another gate of COMMAND failed, such as the coverage gate of just test."
    @echo "\nTelling 3 from a kill needs the test count of a clean run, so reaching it runs COMMAND a second time"
    @echo "on the restored file. That costs as long again as the first run, and the probe says so before it starts."
    @echo "\nThe run has the registered mutations switched off, so a test whose own mutation names a line this"
    @echo "probe rewrote is not reported: the kill list holds the tests that failed on the mutation you gave it."
    @echo "\nA killed run ends by naming each test that killed it, a subTest case with its parameters. Read that"
    @echo "list rather than the outcome alone: a case of a table missing from it guards nothing its neighbours"
    @echo "don't, and two tests in it for a one-line change say one of them guards nothing the other doesn't."
    @echo "\nThat list judges the registration as much as the suite, so register a mutation with @kills only"
    @echo "where the tests it is registered on are the ones that kill it. One the rest of the suite kills as well"
    @echo "shows the suite reacting rather than that guard, so re-aim it at what the test's own name claims, or"
    @echo "leave it unregistered."

# Run Python with the package importable, to probe how it behaves. See `just help py`.
[env("PYTHONPATH", "src")]
py *args: install-py-dependencies
    {{ uv_run }} python "$@"

[private]
py-help:
    @echo "\nRun Python with src on the PYTHONPATH, so the package can be imported without installing it. Pass a"
    @echo "snippet with -c, or a lone - to read a script from stdin:"
    @echo "\n    just py -c \"from update_time.sources.github import github_to_raw as raw; print(raw('https://github.com/org/repo/blob/main/CHANGELOG.md'))\""
    @echo "\n    just py - <<'EOF'"
    @echo "    from update_time.sources.npmjs import get_changes"
    @echo "    print(get_changes('react-grid-layout', '2.2.4'))"
    @echo "    EOF"
    @echo "\nARGS goes to the interpreter untouched, so a script path and its arguments work too."

# Report where a name is called, and how many places call it. See `just help callers`.
callers name: install-py-dependencies
    {{ python_m }} tools.callers "$@"

[private]
callers-help:
    @echo "\nReport every call this repository makes to NAME, one per line, and the number of them, so the size of"
    @echo "a change to the name is counted rather than guessed:"
    @echo "\n    just callers assert_new_version_logged"
    @echo "\nThe calls are read off the syntax rather than matched as text, so a call whose arguments sit on their"
    @echo "own lines counts once, at the line it starts on, and the line defining the name counts not at all."
    @echo "\nA name nothing defines fails the run, so a report of no call sites says the name exists and nothing"
    @echo "calls it, rather than leaving a misspelling to read the same way. A file whose Python does not parse"
    @echo "fails the run by name, so a count is never quietly short."

# === Run checks ===

# Run a Python check.
[private]
py-check name check: install-py-dependencies
    {{ start_capture() }} {{ check }} {{ end_capture(name) }}

# Run ty to type check Python code.
[private]
ty: (py-check "ty" f"{{ ty }} {{ code }}")

# Run mypy to type check Python code.
[private]
mypy: (py-check "mypy" f"{{ uv_run }} mypy {{ code }}")

# The Python files under the code folders, as git pathspecs.
code_globs := replace_regex(code, '(\S+)', "'$1/*.py'")

# Lint the files with fixit, after checking the local fixit rules with their own test cases.
fixit_command(files) := fixit + " test .tools.fixit_rules && " + fixit + " lint " + files

# Run fixit to lint Python code.
[private]
fixit: (py-check "fixit" fixit_command(code))

# Run fixit over the Python files that changed since it last passed.
[private]
fixit-if-changed: install-py-dependencies
    {{ lint_if_changed("fixit", "tools/fixit_rules.py pyproject.toml uv.lock justfile", "fixit-files") }}

[private]
fixit-files *files:
    {{ start_capture() }} {{ fixit_command('"$@"') }} {{ end_capture("fixit") }}

# Run ruff to lint and check the formatting of Python code.
[private]
ruff: (py-check "ruff" f"{{ ruff }} format --check {{ code }} && {{ ruff }} check {{ code }}")

# Run pyproject-fmt to check the formatting of pyproject.toml files.
[private]
pyproject-fmt: (py-check "pyproject-fmt" f"{{ pyproject_fmt }} --check pyproject.toml")

# Run troml to the check the classifiers in pyproject.toml files.
[private]
troml: (py-check "troml" f"{{ troml }} check")

# nltk's model-artifact APIs touch files outside the roots they are given (CVE-2026-81726). nltk is a development
# dependency, so no release of Update-time carries it, and no nltk release fixes it yet. The two auditors name the
# advisory differently, and only uv audit drops the ignore of its own accord once a fix is published.
nltk_advisory_for_pip_audit := "PYSEC-2026-3740"
nltk_advisory_for_uv_audit := "GHSA-8mgp-746c-j5xp"

# Run pip-audit to check Python dependencies for known security vulnerabilities.
[private]
pip-audit: install-py-dependencies
    req=$(mktemp); trap "rm -f $req" EXIT; \
    {{ start_capture() }} uv export --quiet --color never --no-emit-local --format requirements-txt > $req && \
    {{ uv_run }} pip-audit --requirement $req --disable-pip --progress-spinner off \
    --ignore-vuln {{ nltk_advisory_for_pip_audit }} {{ end_capture("pip-audit") }}

# Run uv audit to check Python dependencies for known security vulnerabilities.
[private]
uv-audit: install-py-dependencies
    {{ start_capture() }} uv audit --locked --quiet --ignore-until-fixed {{ nltk_advisory_for_uv_audit }} {{ end_capture("uv-audit") }}

# Run bandit to check Python code for security vulnerabilities.
[private]
bandit: (py-check "bandit" bandit_command(code))

# Run bandit over the Python files that changed since it last passed.
[private]
bandit-if-changed: install-py-dependencies
    {{ lint_if_changed("bandit", "pyproject.toml uv.lock justfile", "bandit-files") }}

[private]
bandit-files *files:
    {{ start_capture() }} {{ bandit_command('"$@"') }} {{ end_capture("bandit") }}

# Check the files with bandit for security issues.
bandit_command(files) := uv_run + " bandit --configfile pyproject.toml --quiet --recursive --format " + when_color("screen", "txt") + " " + files

# Run vulture to check for dead Python code.
[private]
vulture: (py-check "vulture" f"{{ vulture }} {{ code }} {{ vulture_whitelist }}")

# Run codespell to check for common misspellings.
[private]
codespell: (py-check "codespell" f"{{ uv_run }} codespell")

# Run vale to check the prose for style. `vale sync` fetches the styles `.vale.ini` names, on every run and over the
# network, so this check needs a connection as `pip-audit` and `uv-audit` do.
[private]
vale: (py-check "vale" f"{{ uv_run }} vale sync && {{ uv_run }} vale --no-wrap --glob '*.md*' {{ prose }}")

# Run yamllint to lint YAML files such as workflow definitions.
[private]
yamllint: install-py-dependencies
    {{ start_capture() }} {{ uv_run }} yamllint --strict -c tools/yamllint.yml -f {{ when_color("colored", "auto") }} . {{ end_capture("yamllint") }}

# Run zizmor to audit GitHub Action workflows.
[private]
zizmor: install-py-dependencies
    {{ start_capture() }} {{ uv_run }} zizmor --no-progress --quiet .github/workflows {{ end_capture("zizmor") }}

# Check the justfile for correct formatting.
[private]
check-justfile:
    {{ start_capture() }} {{ just_fmt }} --check --color=$_color {{ end_capture("check-justfile") }}

# Check that README.md and the log-output screenshot are what regenerating them produces.
[private]
check-readme-is-up-to-date: (py-check "check-readme-is-up-to-date" f"{{ python_m }} tools.generate_readme --check")

# Check that the README documents every dependency type the same way, and that its internal links resolve.
[private]
check-readme-structure: install-py-dependencies
    {{ start_capture() }} {{ python_m }} tools.readme_structure_check docs/README.md.in {{ end_capture("check-readme-structure") }}

# Check the readability of the prose in the code and the documentation.
[private]
check-readability: install-nltk-data
    {{ start_capture() }} {{ python_m }} tools.readability_check --check-whitelist {{ prose_whitelist }} {{ code }} {{ prose }} {{ end_capture("check-readability") }}

# Run the quality checks. Run one by name for a quicker loop, e.g. `just ruff` or `just mypy`.
[parallel]
check: fixit bandit pip-audit uv-audit zizmor unconditional-checks

# The quality checks that run whatever changed.
[parallel]
[private]
unconditional-checks: ty mypy ruff pyproject-fmt troml vulture codespell check-justfile check-readme-is-up-to-date check-readme-structure check-readability vale yamllint

# The exit status `run_if_changed` gives where it skips a command. A command it runs that exits with this status makes
# the tool exit with 1, so the failure never reads as a skip.
skipped_status := "99"

# Run the command where the inputs changed since it last passed, keeping their fingerprints under the step's name, and
# say the step was skipped where they did not. A command running a nested `just` passes it `--no-deps` where the
# recipe needs none of its dependencies, since this `just` installed them already.
# The tool reads the standard library alone, so it runs outside the project's environment, which it may be installing.
if_changed(name, inputs, command) := 'status=0; uv run --no-project --quiet python -m tools.run_if_changed --skipped-status ' + skipped_status + ' --record ' + name + ' --inputs ' + inputs + ' -- ' + command + ' || status=$?; if [ "$status" -eq ' + skipped_status + ' ]; then ' + skipped(name, "its inputs did not change since it last passed") + '; else exit "$status"; fi'

# Lint the Python files that changed since the linter last passed, reading them whole, as a linter reads its comments.
# A change to another input lints every Python file.
lint_if_changed(name, inputs, recipe) := if_changed(name, inputs + " --pass-changed " + code_globs + " --whole-files", "just --no-deps " + recipe)

# Run the tests where their inputs changed since they last passed.
[private]
test-if-changed: install-py-dependencies install-nltk-data
    {{ if_changed("test", "'*.py' justfile pyproject.toml uv.lock .python-version README.md docs/README.md.in 'src/*.xml'", "just test-passes") }}

# Audit the Python dependencies where their inputs changed since they last passed. The advisory database is not an
# input, so `just check` and CI report a new advisory against an unchanged lock, since they audit on every run.
[private]
pip-audit-if-changed: install-py-dependencies
    {{ if_changed("pip-audit", "uv.lock justfile", "just --no-deps pip-audit") }}

# The same for uv audit.
[private]
uv-audit-if-changed: install-py-dependencies
    {{ if_changed("uv-audit", "uv.lock justfile", "just --no-deps uv-audit") }}

# Audit the workflows where their inputs changed since they last passed.
[private]
zizmor-if-changed: install-py-dependencies
    {{ if_changed("zizmor", "'.github/workflows/*' uv.lock justfile", "just --no-deps zizmor") }}

# Run the checks, with fixit, the audits, and the tests where their inputs changed, at the same time; none reads what
# another writes.
[parallel]
[private]
test-and-check: test-if-changed fixit-if-changed bandit-if-changed pip-audit-if-changed uv-audit-if-changed zizmor-if-changed unconditional-checks

# Format the code, then run the checks, with fixit, the audits, and the tests where their inputs changed: what a TDD step
# ends with.
verify: format test-and-check

# === Fix issues ===

# Rename a module-level name, a method, or an attribute, and every reference to it, in the files named. See `just help rename`.
rename old new +files: install-py-dependencies
    {{ python_m }} tools.rename "$@"

[private]
rename-help:
    @echo "\nRename OLD to NEW in FILES, resolving the name against each module's scopes, so the same word in a"
    @echo "docstring, a help string, or an f-string is left alone, as is a parameter or a local of that name."
    @echo "A keyword argument spelled like the name is rewritten, though, so read the diff:"
    @echo "\n    just rename release_metadata _release_metadata src/update_time/sources/pypi.py"
    @echo "\nA name defined in one module and used in another is renamed only where the files named cover both, so"
    @echo "name every file that refers to it, and spell OLD as the fully qualified name, since a bare name reaches"
    @echo "the definition alone across modules. The recipe fails, renaming nothing, when none of FILES is a module"
    @echo "the qualified OLD names, so name the file that defines it as well. A module-private name takes the bare"
    @echo "form, the qualified one resolving to nothing. The recipe fails when a module-level name survives in a file"
    @echo "it was given, so a rename that reached only some of them is caught rather than left on disk. A rename that"
    @echo "landed reports the prose that still mentions the old name in backticks, wherever in the repository it"
    @echo "sits, since a rename rewrites none of it: the same word is a parameter or a local elsewhere, and means"
    @echo "something else there."
    @echo "\nSpell a method or an attribute as module.Class.name. Every file renames each attribute, keyword argument,"
    @echo "method, and class attribute of that name, whatever it belongs to, so an override in a subclass is renamed"
    @echo "with the definition. A function or a local of that name is left alone, so a member rename runs no"
    @echo "check for the old name surviving:"
    @echo "\n    just rename tests.update_time.helpers.LoggingTestCase.old_name new_name FILES"
    @echo "\nRead the diff afterwards, as with any rewrite."

# Format and lint-fix Python code, the part of `just fix` a quicker loop needs after an edit.
format: install-py-dependencies
    {{ ruff }} format {{ code }}
    {{ ruff }} check --fix {{ code }}

# Format Python code and report what the linter finds, fixing none of it: what to run after a single edit, since
# fixing deletes an import whose first use the next edit is about to add.
[private]
format-after-edit: install-py-dependencies
    {{ ruff }} format {{ code }}
    {{ ruff }} check {{ code }}

# Fix the quality issues that can be fixed automatically.
fix: install-py-dependencies
    {{ ty }} --fix {{ code }}
    {{ ruff }} format {{ code }}
    {{ ruff }} check --fix {{ code }}
    {{ fixit }} fix {{ code }}
    # Pyproject-fmt returns exit code 1 when pyproject.toml needs formatting, ignore it when formatting:
    {{ pyproject_fmt }} --no-print-diff pyproject.toml || true
    {{ troml }} suggest --fix
    {{ just_fmt }}

# === Whitelists ===

# Regenerate the whitelists the checks read: the dead code vulture passes over, and the sentences the prose check passes over.
update-whitelists: install-py-dependencies
    # Vulture writes out every finding, the ones this run introduced included, so read the diff. The prose whitelist
    # only drops the sentences the prose no longer holds, so a sentence that fails the check has to be rewritten.
    # Vulture returns exit code 3 when there is dead code, ignore it when writing the whitelist:
    {{ vulture }} --make-whitelist {{ code }} > {{ vulture_whitelist }} || true
    {{ python_m }} tools.readability_check --make-whitelist {{ prose_whitelist }} {{ code }} {{ prose }}

# === Install dependencies ===

# Fetch the nltk datasets the readability check reads. The unit tests read prose through them and refuse the
# network, so a fresh checkout has to have them before the tests run.
[private]
install-nltk-data: install-py-dependencies
    {{ start_capture() }} {{ python_m }} tools.readability_check --install-data {{ end_capture("install-nltk-data") }}

# Install Python dependencies from the lock file, where their inputs changed since they last synced. The malware check
# asks a server on every sync, so skipping an unchanged one saves a request each time.
[private]
install-py-dependencies:
    {{ if_changed("install-py-dependencies", "uv.lock pyproject.toml .venv/pyvenv.cfg justfile", "just --no-deps uv-sync") }}

# Sync the environment with the lock file.
[private]
uv-sync:
    {{ start_capture() }} uv sync --no-progress --locked --all-extras --all-groups {{ end_capture("install-py-dependencies") }}

# === Update dependencies ===

# Update direct and indirect dependencies. Set GITHUB_TOKEN, DOCKER_HUB_USERNAME, and DOCKER_HUB_TOKEN to prevent hitting rate limits. Without Maven on the path, the run reports an error for each pom.xml it finds and leaves that pom as it is.
update-dependencies: install-py-dependencies
    {{ uv_run }} src/update_time/updaters/update.py

alias update-deps := update-dependencies

# === Documentation ===

# Regenerate README.md from docs/README.md.in (fills in `update-time -h` and the log output; rewrites the screenshot).
readme: install-py-dependencies
    {{ python_m }} tools.generate_readme

# === CI ===

# Run SonarCloud prerequisites
_sonarcloud: test
    {{ coverage }} xml # SonarCloud needs a Cobertura compatible XML coverage report
    # SonarCloud needs a JUnit compatible XML report. The registered mutations are not run, because `test-mutations` ran them already and this run is here for the report alone.
    {{ switch_mutations_off }} {{ python_m }} xmlrunner discover --output-file build/xunit.xml

# Run everything in CI
_ci: _sonarcloud check

# === Folders ===

# The folders holding Python code. `docs` holds the README template and the screenshot, so it is checked for
# prose but has no Python to check.
code := "src tests tools"

# The prose outside the Python code: the README's template, the Markdown files at the root, and the guidelines.
prose := "docs *.md .claude/CLAUDE.md"

# === Output functions ===

# Pick a tool-flag value based on `$_color` set by `start_capture`. Useful for tools whose color flag values aren't `auto`/`always`/`never` (e.g. bandit's `screen`/`txt`, yamllint's `colored`/`auto`).
when_color(yes, no) := f'$([ "$_color" = always ] && echo {{ yes }} || echo {{ no }})'

# Prefix and suffix that wrap a command (such as a check): `{{ start_capture() }} <cmd> {{ end_capture(name) }}` captures stdout+stderr, prints `<recipe-name> PASS` or `FAIL`, and replays the captured output on failure. These lines, the SKIP line, and the spinner go to stderr, so stdout holds only what a recipe's command writes, such as the output of `just py`. Neither token contains the other, so a run's outcome cannot be misread by matching on a substring. The word is coloured only on a terminal, so a run whose output is piped or captured is plain throughout and a reader of it needs no escape codes stripped.
start_capture() := f'_color=auto; _green=; _red=; _normal=; [ -t 2 ] && { _color=always; _green="{{ GREEN }}"; _red="{{ RED }}"; _normal="{{ NORMAL }}"; export FORCE_COLOR=1; }; output=$({'
end_capture(name) := f'; } 2>&1) || { status=$?; printf "%s ${_red}FAIL${_normal}\n%s\n" {{ name }} "$output" >&2; exit "$status"; }; printf "%s ${_green}PASS${_normal}\n" {{ name }} >&2'

# A line saying the step was skipped, and why. The word is coloured only on a terminal, as PASS and FAIL are.
skipped(name, reason) := f'if [ -t 2 ]; then _yellow="{{ YELLOW }}"; _normal="{{ NORMAL }}"; else _yellow=; _normal=; fi; printf "%s ${_yellow}SKIP${_normal} (%s)\n" {{ name }} "{{ reason }}" >&2'

# Like start_capture/end_capture, but for slow commands (e.g. tests): run them in the background and animate a spinner while they run. The spinner only shows on a terminal, and leaves the cursor at the start of its line, so a PASS line printed beside it overwrites it instead of being prefixed by it. `verify` prints such lines, running the tests and the checks at the same time.
start_progress() := f'if [ -t 2 ]; then spin=1; _green="{{ GREEN }}"; _red="{{ RED }}"; _normal="{{ NORMAL }}"; else spin=; _green=; _red=; _normal=; fi; tmp=$(mktemp); trap "rm -f $tmp" EXIT; { '
end_progress(name) := f'; } > "$tmp" 2>&1 & pid=$!; sp="|/-\\"; while kill -0 "$pid" 2>/dev/null; do [ -n "$spin" ] && printf "%c\r" "$sp" >&2; sp="${sp#?}${sp%???}"; sleep 0.1; done; [ -n "$spin" ] && printf " \r" >&2; wait "$pid"; status=$?; if [ "$status" -eq 0 ]; then count=$(grep -m1 "^Ran " "$tmp" | cut -d" " -f2); printf "%s ${_green}PASS${_normal} (%s tests)\n" {{ name }} "${count:-?}" >&2; else printf "%s ${_red}FAIL${_normal}\n%s\n" {{ name }} "$(cat "$tmp")" >&2; exit "$status"; fi'
