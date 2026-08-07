# Vulnerability warnings — specification

Update-time warns about known vulnerabilities in the dependencies it scans, using the [OSV](https://osv.dev) database, and can be told to hold updates back until a vulnerability is gone. This document specifies the behaviour and slices it into increments. It is a working document: what lands goes in the README and the changelog, and this file is deleted when the last increment does.

## 1. Vocabulary

- **Vulnerability**: a known security defect in a specific version of a dependency, as recorded by an OSV **advisory** (`GHSA-…`, `CVE-…`, `PYSEC-…`; one advisory carries several such ids as **aliases**).
- **Vulnerable dependency**: a dependency the run leaves pinned to a version an advisory names as affected. The wording follows the existing *stale dependency* and *yanked dependency*, so the four checks read alike.
- **Risk level**: the advisory's severity, one of `low`, `moderate`, `high`, `critical`, or `unknown` when it carries no severity we can read.
- **Suppression**: a marker or command-line option that holds back the warning.
- **Block**: a marker or command-line option that drops vulnerable versions from the update candidates.

## 2. What is checked

Update-time checks the version each dependency is pinned to *after* the run has updated it — the new version when the reference moved, the current version when it did not. This is the rule the yank check already uses, and it collapses the "vulnerabilities in current versions" and "vulnerabilities in new versions" features into one: a vulnerability the run updated away from is never reported, and a vulnerability the run updated *into* always is.

| Dependency type | Checked | Ecosystem | Where the version comes from |
| :-------------- | :------ | :-------- | :--------------------------- |
| `requirements.txt` pins | yes | PyPI | the pin Update-time rewrote |
| `pyproject.toml` dependencies | yes | PyPI | the `==` pins uv settled on, read by the pass that already checks their staleness |
| PEP 723 inline script metadata | yes | PyPI | as above |
| jsDelivr npm URLs | yes | npm | the version in the URL |
| `package.json` dependencies | no | npm | declared as ranges; the resolved versions live in the lock file, which Update-time does not read |
| Docker images | no | — | OSV has no ecosystem for container images |
| GitHub Actions | no | GitHub Actions | OSV *has* advisories for actions, but its version matching returns nothing for them (see §3) |
| Pre-commit hooks | no | — | a hook repository is not an OSV package |
| Node engine version, `.python-version` | no | — | a runtime version, not a package release |

Only the version a dependency is pinned to is checked. Transitive dependencies are out of scope: those resolve in a lock file, and auditing a lock file is what `uv audit`, `pip-audit`, and `npm audit` are for.

## 3. The source

[OSV](https://osv.dev) aggregates GitHub's advisory database, PyPA's, and others; it needs no account, no token, and no rate-limit credentials, which keeps it in line with the sources Update-time already uses. Everything below was probed against the live API while writing this spec.

- **Detection** is `POST https://api.osv.dev/v1/querybatch` with a list of `{package: {name, ecosystem}, version}` queries. The response holds only advisory ids per query, so a clean dependency costs nothing beyond its slot in the batch.
- **Details** are `POST https://api.osv.dev/v1/query` for one `{package, version}`, which returns the full records — summary, aliases, severity — for every advisory affecting that version in one request. Preferred over `GET /v1/vulns/{id}` per advisory, which costs one request each (`django@3.2.0` has 56).
- **Name normalisation** is done by OSV: `Django` and `django` both return 56 advisories for `3.2.0`, as do `Jinja2` and `jinja2`. Scoped npm names (`@babel/traverse`) work as-is.
- **Withdrawn advisories** carry a `withdrawn` timestamp and are skipped.
- **GitHub Actions** advisories exist (`tj-actions/changed-files` has GHSA-mrrh-fwg8-r2c3, affecting `<= 45.0.7`) but their affected entries enumerate no versions and OSV cannot order Action versions: querying `45.0.0`, `v45.0.0`, `45`, and `46.0.0` all return zero. Matching would mean parsing `database_specific.last_known_affected_version_range` ourselves. Out of scope; revisit if OSV starts matching.
- **Failure** is handled as every other source is: `io/fetch` turns a timeout or a non-OK status into a logged `WARNING` and the run continues without vulnerability data for those dependencies.

Package names and pinned versions leave the machine when the check runs. The check can be switched off entirely (§6), which also covers a sealed network.

### Risk levels

An advisory reviewed by GitHub carries `database_specific.severity` (`LOW`, `MODERATE`, `HIGH`, `CRITICAL`), which is used directly. When it is absent but a CVSS vector is present (`severity: [{type: CVSS_V3, score: "CVSS:3.1/…"}]`), the level is derived from the base score using GitHub's bands: `0.1–3.9` low, `4.0–6.9` moderate, `7.0–8.9` high, `9.0–10.0` critical. Deriving a base score from a vector needs a CVSS implementation; whether to add the `cvss` package or compute it locally is settled by trying both in increment 1.

When neither is present the level is `unknown`. An unknown level is never below a threshold: it is always warned about, and a level-based block never skips it (a block by advisory id still does). Ordering is `low < moderate < high < critical`; `unknown` is outside the order.

## 4. What is reported

One line per vulnerability, at `WARNING`, naming the version, the level, the summary, and the advisory:

```console
WARNING Vulnerable dependency django in requirements.txt:12: version 3.2.0 has a critical vulnerability, "SQL Injection in Django" (GHSA-2gwj-7jmv-h26r, https://osv.dev/GHSA-2gwj-7jmv-h26r)
```

A dependency in `pyproject.toml` or a PEP 723 block has no line number, exactly as its staleness warning has none, so its location is the file alone. An advisory without a readable severity reports `has a vulnerability of unknown severity`.

Like the staleness and yank warnings, this one is informational: it changes no file and does not affect the exit status, which keeps meaning "the run finished" (0 success, 1 error, 2 bad argument).

## 5. Controlling it per reference

The marker language gains two scopes. `vulnerable` steers the warning; `vulnerable-update` steers which versions may be adopted. Both follow the language's existing rules: `allow` keeps what it names, `ignore` drops it, and the two are exact complements.

| Marker | Effect |
| :----- | :----- |
| `ignore[vulnerable]` | no vulnerability is warned about for this reference |
| `ignore[vulnerable=GHSA-…]` | this advisory is not warned about; any of its aliases spells it |
| `ignore[vulnerable<high]` | vulnerabilities below `high` are not warned about; `allow[vulnerable>=high]` is the same rule |
| `ignore[vulnerable-update]` | no version with any known vulnerability is adopted |
| `ignore[vulnerable-update=GHSA-…]` | no version carrying this advisory is adopted |
| `ignore[vulnerable-update>=high]` | no version carrying a `high` or worse vulnerability is adopted; `allow[vulnerable-update<high]` is the same rule |

`allow[vulnerable]` and `allow[vulnerable-update]` are the explicit spellings of the default, and are no-ops like `allow[update]`.

The operator runs the other way between the two scopes for the same intent, because `ignore` names what it *drops*: `ignore[vulnerable<high]` drops the warnings below `high`, while `ignore[vulnerable-update>=high]` drops the updates at `high` and above. The README documents both with the intent spelled out, as it does for `stale` and `cooldown`.

Handling of a malformed item follows what those scopes already do:

- An inverted comparison — `ignore[vulnerable>=high]`, which would report the trivial vulnerabilities and stay quiet about the serious ones, or `ignore[vulnerable-update<high]`, which would refuse a moderate version and adopt a critical one — is logged at `WARNING` as incorrect, holds nothing back, and leaves the global setting in force.
- An unreadable level (`ignore[vulnerable<hgih]`) is logged at `WARNING` as an invalid item and the reference is left unchanged, like any other unreadable bracket item.
- Use one directive per scope per reference; pairing two is undefined, as it is for `stale` and `cooldown`.

Interaction with the markers that already exist:

- A bare `# update-time: ignore` holds the vulnerability warning back too, making `vulnerable` a fourth scope alongside `update`, `stale`, and `yanked`. It keeps its property of querying no source at all, so it also reports no redundancy (§7).
- `ignore[update]` freezes the version but keeps the reference checked, so a deliberately pinned dependency still tells you it is vulnerable.
- A version bound (`allow[update<3.13]`) and a block are different filters over the same candidates and compose: a candidate must satisfy both. This is not the "one bound per reference" case, which is about pairing two *version* bounds.
- A `vulnerable` or `vulnerable-update` scope on a reference whose source reports no vulnerabilities — a Docker image, a GitHub Action, a pre-commit hook, a `.python-version` entry — is reported as redundant, the way `ignore[yanked]` already is there.

Placement is unchanged: inline on the reference's line, or on the line above. `pyproject.toml` and PEP 723 dependencies take no marker, because uv updates them; suppress those run-wide instead.

## 6. Controlling it run-wide

| Option | Meaning |
| :----- | :------ |
| `--warn-vulnerability-level LEVEL` | warn only at or above this level; `none` switches the check off entirely, making no request at all. Default `low` |
| `--ignore-vulnerability ID[,ID…]` | never warn about these advisories, in any dependency; any alias spells one |
| `--block-vulnerability-level LEVEL` | never adopt a version carrying a vulnerability at or above this level. Default `none` |
| `--block-vulnerability ID[,ID…]` | never adopt a version carrying one of these advisories |

Blocking is opt-in: out of the box Update-time updates to the newest eligible version and warns when it is vulnerable, leaving the decision to the reader, as the staleness and yank checks do.

A marker wins over the command line, as it does for `--cooldown` and `--stale-after`: a reference with its own level threshold keeps it whatever the global one says, `--warn-vulnerability-level none` included. Where a reference carries no marker, the global setting applies. Suppression by id and suppression by level compose: an advisory is reported when it is at or above the level in force *and* is not suppressed by id.

The block options reach the dependencies whose version Update-time picks itself. They do nothing for the dependencies uv, npm, and pnpm resolve, which choose their own versions; those are warned about but never held back.

## 7. Reporting a suppression or block that no longer holds anything back

A suppression outlives the vulnerability it was written for, so a marker that suppresses nothing is reported at `WARNING`, reusing the existing redundant-marker wording:

```console
WARNING Redundant update-time marker ignore[vulnerable=CVE-2022-28346] for django in requirements.txt:12: version 4.2.0 has no such vulnerability, so the marker holds nothing back
WARNING Redundant update-time marker ignore[vulnerable<high] for django in requirements.txt:12: version 4.2.0 has no vulnerability below high, so the marker holds nothing back
WARNING Redundant update-time marker ignore[vulnerable-update>=high] for django in requirements.txt:12: no candidate version carries a high or worse vulnerability, so the marker holds nothing back
WARNING Redundant update-time marker ignore[vulnerable] for python in Dockerfile:2: this dependency's source reports no vulnerabilities, so the marker holds nothing back
```

This is what makes a marked reference worth querying even when its warning is suppressed: without the query there is nothing to compare the suppression against. A bare `# update-time: ignore` is the exception, since it queries nothing.

What a marker did hold back is logged at `DEBUG`, as every other marker's effect is:

```console
DEBUG Ignoring the vulnerability warning for django in requirements.txt:12 (update-time: ignore[vulnerable=GHSA-2gwj-7jmv-h26r])
DEBUG Skipping version 5.0.0 of django in requirements.txt:12: it has a high vulnerability GHSA-… (update-time: ignore[vulnerable-update>=high])
```

Reporting the same redundancy for the *global* options needs a run-wide tally, which the current design cannot produce: the updaters run as separate subprocesses and none of them sees the whole scan. That is increment 9, and it is optional.

## 8. Cost

Requests are what this feature spends, so it is designed around two shapes:

- **Warning**: one `querybatch` request per file that holds checkable pins, sent after the file has been rewritten, plus one `query` request per pin that turns out to be affected. A repository with no vulnerable pins costs one request per file, whatever the number of dependencies in it.
- **Blocking**: the decision has to be made before a version is picked, inside the per-candidate walk, so it costs one `query` request per candidate examined — one for a reference that adopts the newest version, one more for each candidate a block rejects. This keeps the check ecosystem-agnostic: evaluating a package's whole advisory set locally instead would need PEP 440 ordering for PyPI (available) and semver ordering for npm (not available; npm advisories carry SEMVER ranges and enumerate no versions).

The block therefore only costs anything for the references that opted into it. When the check is off (`--warn-vulnerability-level none` with no block in force), no request is made.

## 9. Increments

Each increment ends with the README and the changelog describing exactly what landed.

1. **Warn about a vulnerable `requirements.txt` pin.** The OSV source, the ecosystem mapping, the level derivation, the per-file batch pass after the rewrite, the warning message, and `--warn-vulnerability-level` (including `none`, which is also the off switch for the increments that follow).
2. **Extend the warning to the remaining checked types**: jsDelivr npm URLs, and the `pyproject.toml` and PEP 723 pins, through the pass that already reports their staleness.
3. **Suppress one advisory**: `ignore[vulnerable=ID]` with alias matching, and `--ignore-vulnerability`.
4. **Suppress by risk level**: `ignore[vulnerable<LEVEL]` and `allow[vulnerable>=LEVEL]`, with the inverted-comparison and invalid-level reporting.
5. **Report a suppression that holds nothing back**, including the scope on a source that reports no vulnerabilities.
6. **Block one advisory**: `ignore[vulnerable-update=ID]`, and `--block-vulnerability`.
7. **Block by risk level**: `ignore[vulnerable-update>=LEVEL]` and `allow[vulnerable-update<LEVEL]`, and `--block-vulnerability-level`.
8. **Report a block that holds nothing back.**
9. *(optional)* **Report a global suppression or block that holds nothing back**, which needs a run-wide tally across the updater subprocesses.

Increment 1 carries `--warn-vulnerability-level` rather than deferring it to increment 4 with its marker, so that the check can be switched off from the first release it ships in.

## 10. Risks and things to settle

1. **CVSS derivation.** Whether to add the `cvss` package or compute the base score locally is settled by trying both in increment 1 and reporting what each costs.
2. **A result that changes under you.** With blocking on, the version a run picks depends on an advisory database that changes daily. This is inherent, and is why blocking is opt-in; the README says so.
3. **OSV rate limits.** OSV publishes no quota for these endpoints. If a large scan hits one, it surfaces as an unreachable source and the run continues; measure before assuming a batch size is safe.
4. **GitHub Actions coverage.** The advisories that matter most for supply-chain attacks are the ones we cannot match today (§3). Worth revisiting, and worth saying in the README that actions are not checked, so nobody reads silence as safety.
5. **A vulnerability with no fix.** When a block leaves no candidate, the reference stays where it is and the warning about its version still fires, which is the intended outcome and needs no separate message.
