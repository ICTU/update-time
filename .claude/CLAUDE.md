Consult the README to understand the project:

@../README.md

Consult the justfile to learn how to run tests and checks:

@../justfile

## Comments

Don't add comments or docstrings that justify or narrate a change; comments describe how the code behaves, not why an edit was made. Trivial changes get no comment.

## TDD

Develop test-first, in small steps, and stop after each step so I can steer:

1. **Pick a starting point.** For a new feature, propose at most five candidate tests and let me choose which to start with. Build a behaviour before its off-switch: don't test an `ignore` scope, a flag, or any other suppression until the thing it suppresses exists.
2. **Write the test.** Only the test — don't design or write the implementation yet. Follow the conventions of the nearest existing test for the same kind of behaviour.
3. **Predict the failure, then run `just test`.** Say how the test will fail, naming the exact error, before running it. Verify that only the new test fails, and that it fails as predicted. When anything else happens, analyse why before writing any code.
4. **Implement** the smallest change that makes the test pass, then run `just test` again.
5. **Refactor** production and test code, then run `just test` and `just check`.

A few rules that keep the cycle honest:

- Park test cases you are not writing yet in an explicit list instead of writing them all at once, and work through the list once the main path works.
- A test that pins down existing behaviour drives no code, so predict it passes and say so; it closes a gap in intent, not in behaviour.
- Coverage must stay at 100%. A gap after implementing points at a test case worth adding, not at a line worth excluding.
- Treat a failing existing test as a signal: work out whether its premise legitimately changed and say why, rather than patching the assertion to match the new output.
- `just check` prints `NOK` for a failing check, so don't count `OK` occurrences to conclude it passed: `NOK` contains `OK`.
