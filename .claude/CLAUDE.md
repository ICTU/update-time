Consult the README to understand the project:

@../README.md

Consult the justfile to learn how to run tests and checks:

@../justfile

## Git

I create the branches, commit, and push. Never do any of that, and never offer to: finishing a piece of work means reporting it with the tests and checks green, not proposing a commit. Never run a command that discards work I haven't committed either (`git checkout <path>`, `git restore`, `git stash`) unless I ask for it: undo your own experiment by editing the file back, or run the experiment on a copy.

A bulk rewrite can destroy uncommitted work, a `sed` or `perl -pi` one-liner as much as a script: run a script with the project's interpreter (`uv run`, never the system `python3`), name the files to rewrite explicitly rather than globbing the tree, and keep the substitution idempotent, so a crash halfway through can be re-run rather than undoing what already landed. Read the diff it produced before running anything else: a regex that spans a pair of delimiters can start at one you meant and end at a later one you didn't, rewriting every line in between.

## Code review

When reviewing code, whether standalone or as part of the TDD refactor step, pay attention to the following:

- **Comments**: a comment or docstring says what *this* code does, and only what the signature and the code don't already say. Delete a sentence that narrates or justifies an edit, that says what the code is not or is unlike, that restates a parameter, return type, or default, or that describes what a caller or a collaborator does. Keep a contrast only when the reader has to act on the difference. When a signature changes, the docstring usually needs a word in its summary, not a new sentence.
- **Duplication**: look for the same decision taken in more than one place, rather than for repeated lines. A rule every module has to remember to apply is duplication too: prefer stating it once, somewhere it cannot be forgotten. In tests, repeated setup or a repeated assertion is worth naming as a helper.
- **Reuse**: look for an existing type, test helper, or fixture before writing a new one. The misses to watch for: a fixture's value spelled out as a literal, a mixin's setup redone inline, and the same builder defined in two modules rather than in the shared one.
- **Complexity**: a function should hold one decision. Watch for nesting, for flag parameters that make one function do two things, and for long parameter lists. When a docstring needs several sentences to describe the control flow, the code is doing too much, rather than the docstring being too short.
- **Missing abstractions**: values that always travel together want a type, and a sequence of calls that callers must make in the right order wants a name. Raw strings, tuples, and dicts standing in for domain concepts are the usual smell.
- **Visibility**: a class, function, method, or constant without a leading underscore claims callers outside the module or class defining it, so check it has them. One made public for a call site that has since changed is the usual way that claim goes stale.
- **Failure paths**: for every call that can fail — a request, a parse, a subprocess — check what happens when it does. An exception caught without a log is invisible; one that escapes aborts the whole run over a single bad reference; and a write that fails partway must leave the file as it was.
- **Cost**: the work here is network requests, so count them. Watch for a request added inside a per-reference loop, and for a new call path that bypasses a cache the old one used.
- **Readability**: use the domain vocabulary the README establishes, consistently across code, docstrings, log messages, and tests. Prefer an early return to a nested conditional, say what a test asserts in its method name, and keep implementation terms out of anything the user reads. And when a behaviour is described in more than one place, change them all, not just the section you are in.
- **Prose**: read new prose back as prose — docstring, log message, CLI help, README. Lead with the case and name the actor (`If the source resolves a version equal to the current one, it is still returned`), rather than with the general rule or with what the code refrains from doing. A sentence that needs a second pass is a defect no linter catches, and it may be hiding a false claim rather than merely reading badly, so rewrite it instead of patching it. The usual causes are compression: a pronoun, a `which`, or a `then` with more than one possible antecedent, a stranded preposition, a verb dropped from the second half of a pair, an abstraction where naming the two concrete cases reads plainer.

## TDD

Develop test-first, in small steps, so I can steer.

While doing TDD, keep a numbered list of candidate tests (T1, T2, ...) and their implementation status (todo/pass/fail). No tests are removed from the list until the session ends, unless I explicitly tell you to remove a candidate test from the list. The session ends when all tests pass (so no tests todo or fail).

For a new feature, bug fix, task, or increment, prepare the cycle by adding at most five candidate tests to the list and let me choose which to start with. A cleanup driven by a tool's findings (SonarCloud, a linter) is a task like any other: group the findings, say which of them change behaviour, and let me choose which ones a test has to drive before you start fixing.

Each cycle consists of the following three steps:

1. **Red**: Show the list, then write the candidate test — don't design or write the implementation yet. Check the new test against existing tests first: when one already covers the case, add the assertion that pins it to that test instead of writing a near-duplicate. Predict the outcome before running `just test`: how the test will fail, naming the exact error, or why it will succeed. Run the whole suite, not just the new test's module, and verify the prediction against it: a test predicted to fail is the only one failing and fails as predicted, one predicted to succeed leaves the whole suite green. When anything else happens, analyse why and report to me.
2. **Green**. If the test failed, design and implement the smallest change that makes the test pass, then run `just test` again. Smallest counts what the change leaves behind: a hack the next cycle has to throw away isn't smaller, so when you pick the structural change over it, say what you rejected. Docstrings and comments the change makes untrue are part of the change: update them now, not in the refactor step. If the test succeeded, whether a refactor moved the mechanism it relies on or it pins down behaviour that already works, check that the test is not vacuous. Stub the mechanism out, confirm the test fails, and restore it. After `just test` passes, also run `just check` and fix any issue reported.
3. **Refactor**. Review and refactor production and test code: review the whole diff against the code review criteria above, comments and docstrings included — the ones you wrote, and the ones that stayed the same while the code they describe changed — and also, for anything whose signature, fields, or behaviour the step changed, its call sites and their docstrings, which the diff doesn't show. Review the architecture too: a step that adds a dependency between modules or widens what one exposes wants a new or tightened architecture test. Report the findings as a numbered list (R1, R2, ...), then work through them one at a time, running `just test` and `just check` after each. A fix a linter flagged is not that review. The cycle ends here, not at step 2: hand back only once the R-list is reported, so a hand-back without one is an unfinished cycle.

A few rules that keep the cycle honest:

1. New tests follow the conventions of the nearest existing test for the same kind of behaviour, unless that test breaks a rule below.
2. Build a behaviour before its off-switch: don't test an opt-out, a flag, or any other suppression until the thing it suppresses exists.
3. Assert what does and doesn't happen, rather than saying it in a docstring: an assertion is checked on every run, a docstring claim is checked by nobody.
4. A test that pins down existing behaviour drives no code, so predict it passes and say so; it closes a gap in intent, not in behaviour.
5. Coverage must stay at 100%, which `just test` already enforces, so a passing run needs no separate coverage command. A gap after implementing points at a test case worth adding, not at a line worth excluding. A step must not end with the new code uncovered: when the test you chose mocks the collaborator the new code lives on, add a second test in the same step that reaches that code. Code the gate doesn't reach, such as `tools/`, needs that care most: pin each branch with the tool's own test cases, since neither coverage nor the type checkers look there.
6. Treat a failing existing test as a signal: work out whether its premise legitimately changed and say why, rather than patching the assertion to match the new output.
7. Assert the actual value first, as in `assertEqual(actual, expected)`. The local fixit rule flags the reverse whenever it can tell the two values apart, and `just fix` swaps them.
8. A green run can prove nothing, so check it ran what you think: read the tail of `just test` and `just check` yourself instead of grepping or counting their output, since a filter that matches nothing exits non-zero and silently skips the rest of an `&&` chain. Compare the test count whenever imports move or test methods are renamed, since a module that fails to import, or a method whose new name collides with an existing one, drops tests without a word. The same goes for the output you quote from: never read a count off something you piped through `head`.
9. Settle a "could we do X?" design question by trying X and reporting what the checker or the suite says, not by reasoning about it: the tools name the exact errors, and the count tells us how much the alternative would cost. This holds for questions you mean to ask me too — try it first, so the question reaches me with numbers instead of an estimate. When the deciding tool only runs in CI, try each option locally anyway and hand me what the local tools say plus what stays unverified, rather than an estimate of what CI would say. And don't announce a comparison you then don't run.
10. Check a claim against the code before stating it — "nothing covers this yet", "it can't live there" — since a sibling module or an existing test usually settles it. A review finding is a claim too, and a challenged claim is one to check again, not to repeat.
11. Add a missing candidate test to the list as soon as you identify one, at whatever step you are in.
12. A refactor that changes behaviour is not a refactor, however unreachable the changed case looks, and a change that only *adds* behaviour counts — a decorator that fills in methods the class was missing changes what calling them does. Say so before making the change rather than after, and let me decide whether a test has to drive it first.

## Documentation

- README.md is generated: edit `docs/README.md.in` and regenerate with `just readme`. An edit to README.md itself is silently lost on the next run.
- A user-visible change gets a changelog entry under `[Unreleased]`, one line naming the behaviour and linking the issue. The detail belongs in the README, not in the changelog. When a change lands over several cycles, update both in the cycle that lands the behaviour they describe, so neither ever documents a state the code isn't in.

## Self-improvement

At the end of a session, review how the session went against this file and fold what you learn back into it.

- Propose this yourself when the work looks finished, with the tests and checks green and nothing left to steer, instead of waiting for me to think of it — but I decide when it runs, so don't start it unasked.
- Review before editing, and report the review: which guidelines were followed, which were deviated from, which went unused, what actually happened in each case, and any command you ran repeatedly outside `just`, since that is usually a recipe missing or broken. Be factual and brief; state the deviation and move on.
- Name the cause of each deviation, because different causes call for different edits: a guideline that was clear and simply not followed needs sharpening, so it cannot be read past a second time; a guideline that turned out to be silent on a case needs that case spelled out in it. Where the deviation can be caught mechanically, write a check instead — a fixit rule or a script under `tools/` — since the rules that hold are the ones a failing run enforces.
- Ground every edit in something that happened this session. Don't add best practice that nothing in the session called for.
- Keep this file short: rewrite an existing line rather than adding one, and propose dropping or merging the lines that go unused session after session. It is read in full at the start of every session, so its length is a cost paid every time.
- Make the edits, then report each one with the moment in the session that prompted it, so I can drop the ones I disagree with.
- This file holds how we work. What the code does belongs in the code, its docstrings, the README, and the changelog, so don't move project facts in here.
