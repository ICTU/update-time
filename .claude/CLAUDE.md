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

Develop test-first, in small steps, and hand back control after each step so I can steer.

While doing TDD, keep a numbered list of candidate tests (T1, T2, ...) and their implementation status (todo/pass/fail). No tests are removed from the list until the session ends, unless I explicitly tell you to remove a candidate test from the list. The session ends when all tests pass (so no tests todo or fail).

For a new feature, bug fix, task, or increment, prepare the cycle by adding at most five candidate tests to the list and let me choose which to start with.

Each cycle consists of the following three steps:

1. **Red**: Show the list, then write the candidate test — don't design or write the implementation yet. Check the new test against existing tests first: when one already covers the case, add the assertion that pins it to that test instead of writing a near-duplicate. Predict the outcome of running the test, then run `just test`. If you predict the test will fail, say how the test will fail, naming the exact error, before running it. If you predict the test will succeed, say why the test will succeed. Run the whole suite, not just the new test's module, so that "only the new test fails" covers the whole suite. If the new test is predicted to fail, verify that only the new test fails, and that it fails as predicted. If you predicted the new test to succeed, verify that the whole suite succeeds. When anything else happens, analyse why and report to me. Hand back control.
2. **Green**. If the test failed, design and implement the smallest change that makes the test pass, then run `just test` again. Smallest counts what the change leaves behind: a hack the next cycle has to throw away isn't smaller, so when you pick the structural change over it, say what you rejected. If the test succeeded, whether a refactor moved the mechanism it relies on or it pins down behaviour that already works, check that the test is not vacuous. Stub the mechanism out, confirm the test fails, and restore it. After `just test` passes, also run `just check` and fix any issue reported. Hand back control.
3. **Refactor**. Review and refactor production and test code: review the whole diff against the code review criteria above, report the findings as a numbered list (R1, R2, ...), then work through them one at a time, running `just test` and `just check` after each. A fix a linter flagged is not that review. The cycle ends here, not at step 2: don't report the work finished with this step still outstanding, and don't wait to be asked for it. Hand back control.

A few rules that keep the cycle honest:

1. New tests follow the conventions of the nearest existing test for the same kind of behaviour, unless that test breaks a rule below.
2. Build a behaviour before its off-switch: don't test an opt-out, a flag, or any other suppression until the thing it suppresses exists.
3. Don't settle for describing what does or doesn't happen in a docstring: an assertion is checked on every run, a docstring claim is checked by nobody.
4. A test that pins down existing behaviour drives no code, so predict it passes and say so; it closes a gap in intent, not in behaviour.
5. Coverage must stay at 100%, which `just test` already enforces, so a passing run needs no separate coverage command. A gap after implementing points at a test case worth adding, not at a line worth excluding. It also bounds what a step may leave behind: write a second test in the same step when the chosen one mocks the collaborator the new code lives on, rather than ending the step with an uncovered method or none at all.
6. Treat a failing existing test as a signal: work out whether its premise legitimately changed and say why, rather than patching the assertion to match the new output.
7. Assert the actual value first, as in `assertEqual(actual, expected)`. Both orders pass locally, but the reverse is flagged in CI, so it costs a round-trip to find out.
8. `just check` prints `NOK` for a failing check, so don't count `OK` occurrences to conclude it passed: `NOK` contains `OK`.
9. Settle a "could we do X?" design question by trying X and reporting what the checker or the suite says, not by reasoning about it: the tools name the exact errors, and the count tells us how much the alternative would cost. This holds for questions you mean to ask me too — try it first, so the question reaches me with numbers instead of an estimate.
10. Add a missing candidate test to the list as soon as you identify one, at whatever step you are in.

## Documentation

- README.md is generated: edit `docs/README.md.in` and regenerate with `just readme`. An edit to README.md itself is silently lost on the next run.
- A user-visible change gets a changelog entry under `[Unreleased]`, one line naming the behaviour and linking the issue. The detail belongs in the README, not in the changelog.

## Self-improvement

At the end of a session, review how the session went against this file and fold what you learn back into it.

- Propose this yourself when the work looks finished, with the tests and checks green and nothing left to steer, instead of waiting for me to think of it — but I decide when it runs, so don't start it unasked.
- Review before editing, and report the review: which guidelines were followed, which were deviated from, and what actually happened in each case. Be factual and brief; state the deviation and move on.
- Name the cause of each deviation, because different causes call for different edits: a guideline that was clear and simply not followed needs sharpening, so it cannot be read past a second time; a guideline that turned out to be silent on the case needs a line of its own.
- Ground every edit in something that happened this session. Don't add best practice that nothing in the session called for.
- Keep this file short: rewrite an existing line rather than adding one, and delete lines that events have overtaken. It is read in full at the start of every session, so its length is a cost paid every time.
- Make the edits, then report each one with the moment in the session that prompted it, so I can drop the ones I disagree with.
- This file holds how we work. What the code does belongs in the code, its docstrings, the README, and the changelog, so don't move project facts in here.
