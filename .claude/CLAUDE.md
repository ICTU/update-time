Consult the README to understand the project:

@../README.md

Consult the justfile to learn how to run tests and checks:

@../justfile

## Git

I create the branches, commit, and push. Never do any of that, and never offer to: finishing a piece of work means reporting it with the tests and checks green, not proposing a commit.

## Code review

When reviewing code, whether standalone or as part of the TDD refactor step, pay attention to the following:

- **Comments**: don't add comments or docstrings that justify or narrate a change; comments describe how the code behaves, not why an edit was made. Trivial changes get no comment. A docstring describing an approach the code has moved on from is worse than none, so re-read the docstrings around a change and check they still describe what is there.
- **Duplication**: look for the same decision taken in more than one place, rather than for repeated lines. A rule every module has to remember to apply is duplication too: prefer stating it once, somewhere it cannot be forgotten. In tests, repeated setup or a repeated assertion is worth naming as a helper.
- **Complexity**: a function should hold one decision. Watch for nesting, for flag parameters that make one function do two things, and for long parameter lists. When a docstring needs several sentences to describe the control flow, the code is doing too much, rather than the docstring being too short.
- **Missing abstractions**: values that always travel together want a type, and a sequence of calls that callers must make in the right order wants a name. Raw strings, tuples, and dicts standing in for domain concepts are the usual smell; look for a type that already models the concept before adding one.
- **Readability**: use the domain vocabulary the README establishes, consistently across code, docstrings, log messages, and tests. Prefer an early return to a nested conditional, say what a test asserts in its method name, and keep implementation terms out of anything the user reads.

## TDD

Develop test-first, in small steps, and hand back after each step so I can steer. Steps 3, 4, and 5 each end a turn: report the red run and stop, report the green run and stop, report the refactor and stop. Running the cycle end to end and reporting it afterwards leaves me nothing to steer.

1. **Pick a starting point.** For a new feature, propose at most five candidate tests and let me choose which to start with. When the test to start with is already prescribed — by the issue, or by me — say that is why you are skipping the proposal, rather than skipping it silently. Build a behaviour before its off-switch: don't test an opt-out, a flag, or any other suppression until the thing it suppresses exists.
2. **Write the test.** Only the test — don't design or write the implementation yet. Follow the conventions of the nearest existing test for the same kind of behaviour.
3. **Predict the failure, then run `just test`.** Say how the test will fail, naming the exact error, before running it. Run the whole suite, not just the new test's module, so that "only the new test fails" covers the whole suite. Verify that only the new test fails, and that it fails as predicted. When anything else happens, analyse why before writing any code.
4. **Implement** the smallest change that makes the test pass, then run `just test` again.
5. **Refactor** production and test code, then run `just test` and `just check`. The cycle ends here, not at step 4: don't report the work finished with this step still outstanding, and don't wait to be asked for it.

A few rules that keep the cycle honest:

- Park test cases you are not writing yet in an explicit list instead of writing them all at once, and work through the list once the main path works. One test starts a cycle even when two look like one step, say the same defect in two modules: fixing both on one branch is not a reason to write both tests at once.
- A test that pins down existing behaviour drives no code, so predict it passes and say so; it closes a gap in intent, not in behaviour.
- A refactor that moves the mechanism a test relies on can leave the test green but vacuous. Stub the mechanism out, confirm the test fails, and restore it.
- Coverage must stay at 100%, which `just test` already enforces, so a passing run needs no separate coverage command. A gap after implementing points at a test case worth adding, not at a line worth excluding.
- Treat a failing existing test as a signal: work out whether its premise legitimately changed and say why, rather than patching the assertion to match the new output.
- Assert the actual value first, as in `assertEqual(actual, expected)`. Both orders pass locally, but the reverse is flagged in CI, so it costs a round-trip to find out.
- `just check` prints `NOK` for a failing check, so don't count `OK` occurrences to conclude it passed: `NOK` contains `OK`.

## Documentation

- README.md is generated: edit `docs/README.md.in` and regenerate with `just readme`. An edit to README.md itself is silently lost on the next run.
- A user-visible change gets a changelog entry under `[Unreleased]`, one line naming the behaviour and linking the issue. The detail belongs in the README, not in the changelog.

## Self-improvement

At the end of a session, review how the session went against this file and fold what you learn back into it.

- Initiate this yourself when the work looks finished, with the tests and checks green and nothing left to steer, instead of waiting to be asked. When I take the session somewhere else instead, let it go.
- Review before editing, and report the review: which guidelines were followed, which were deviated from, and what actually happened in each case. Be factual and brief; state the deviation and move on.
- Separate the two causes, because they call for different edits. A guideline that was clear and simply not followed needs sharpening, so it cannot be read past a second time; a guideline that turned out to be silent on a case needs a line of its own.
- Ground every edit in something that happened this session. Don't add best practice that nothing in the session called for.
- Keep this file short: rewrite an existing line rather than adding one, and delete lines that events have overtaken. It is read in full at the start of every session, so its length is a cost paid every time.
- Make the edits, then report each one with the moment in the session that prompted it, so I can drop the ones I disagree with.
- This file holds how we work. What the code does belongs in the code, its docstrings, the README, and the changelog, so don't move project facts in here.
