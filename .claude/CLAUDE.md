Consult the README to understand the project:

@../README.md

Consult the justfile to learn how to run tests and checks:

@../justfile

## Git

I create the branches, commit, and push. Never do any of that, and never offer to. A piece of work is finished when you report it with the tests and checks green, not when you propose a commit.

Run `git status` before you edit, whenever you come back to the tree. I may have committed or switched branch meanwhile, and an edit onto a tree that moved is lost without a word.

Never run a command that throws away work I have not committed: `git checkout <path>`, `git restore`, `git stash`. Undo your own experiment by editing the file back, or run it on a copy.

## Running and rewriting code

Run every script with `just py`, which puts the package on the path, or with `uv run` where no recipe fits. Never use the system interpreter. This holds for a heredoc that edits one file as much as for a tree-wide rewrite.

Rename a Python name with `just rename`, never by substitution. It resolves the name against each module's scopes, so it leaves the same word alone in a docstring, a help string, a parameter, and a local. It renames module-level names. For a method or an attribute it rewrites the call sites but not the definition, so rename that by hand.

Any other bulk rewrite can destroy uncommitted work, whether you write a script or a `sed` one-liner. Follow these rules:

- Name the files to rewrite. Don't glob the tree.
- Make the substitution idempotent, so you can re-run it after a crash halfway through.
- Say how many occurrences you expect before you write anything. `grep -c` counts lines, not occurrences.
- Read the diff with `git diff` on the files you named. Grepping for the old name shows that it is gone and nothing about what replaced it.
- Replace whole lines. A target that is part of a line rewrites the rest of that line too, and matching once is no protection: the `Edit` tool accepts any fragment that matches once.
- Watch the edges. A regex can start at the delimiter you meant and end at a later one you didn't. A substring matches a longer line that starts the same way. A substitution that opens a bracket has to close it, and one that drops a name has to add the new name in the same pair.

## Code review

Review code against these criteria, whether on its own or in the TDD refactor step:

- **Comments**: A comment says what this code does now. It never says where the increment is heading, and never repeats the signature. Delete a sentence that narrates or justifies an edit, that walks through the steps in order, that says what the code is not, or that restates a parameter, a return type, or a default. Delete one that says what the return value is for: the summary already names it. Delete one that says what a caller does, or that names a concept from a layer further out. A test's docstring says which case the test pins; the reason for the behaviour belongs in the code under test and in the README. Keep a contrast only when the reader has to act on the difference. When a signature changes, the summary usually needs one word, not a new sentence. A docstring that matches the one beside it was copied rather than checked, so read both.
- **Duplication**: Look for the same decision taken in more than one place, not for repeated lines. A rule every module has to remember is duplication too, so state it once, where nobody can forget it. A helper that callers have to remember to call can be forgotten as well, so prefer a check that reads the code itself. In tests, turn repeated setup or a repeated assertion into a named helper.
- **Reuse**: Look for an existing type, test helper, or fixture before you write a new one. Watch for three misses: a fixture's value spelled out as a literal, a mixin's setup redone inline, and the same builder written in two modules instead of in the shared one.
- **Complexity**: A function holds one decision. Watch for nesting, for a flag parameter that makes one function do two things, and for a long parameter list. When a docstring needs several sentences for the control flow, the code does too much.
- **Missing abstractions**: Values that always travel together want a type. A sequence of calls that callers have to make in the right order wants a name. Raw strings, tuples, and dicts standing in for a domain concept are the usual smell, whether that type exists already or still has to be written.
- **Visibility**: A class, function, method, or constant without a leading underscore claims callers outside its own module or class. Check that it has them. A name made public for a call site that has since changed is how that claim goes stale.
- **Failure paths**: For every call that can fail — a request, a parse, a subprocess — check what happens when it does, and mock it so that it fails the same way. A mock that answers where the real thing raises lets a test pass without the branch it is named for. Log every exception you catch, or the failure is invisible. An exception that escapes aborts the whole run over one bad reference. A write that fails partway has to leave the file as it was.
- **Cost**: The work here is network requests, so count them. Watch for a request added inside a per-reference loop, and for a call path that bypasses a cache the old one used.
- **Readability**: Use the domain vocabulary that the code and the README establish. Use it in code, docstrings, log messages, and tests alike, and never coin a second word for a concept one of them already names. Prefer an early return to a nested conditional. Say what a test asserts in its method name. Keep implementation terms out of anything the user reads. When a behaviour is described in more than one place, change every place, not only the one you are in.
- **Prose**: Write plainly — in docstrings, comments, log messages, CLI help, the README, and your replies to me. One sentence, one claim. Put the subject first and the verb right after it. Split a sentence instead of nesting a clause in it. Name the thing when `it` or `which` could point at two things. Don't stack negatives. Don't rank ("the least", "the only", "the one thing"). Give the concrete case, not the general rule. Don't skip a step in an explanation. Read each new sentence again, and rewrite it if it needs a second read.

## TDD

Develop test-first, in small steps, so I can steer.

Keep a numbered list of candidate tests (T1, T2, ...) with the status of each: todo, pass, or fail. Drop a test from the list only once every test on it passes, or when I tell you to drop it.

Prepare a cycle by adding at most five candidate tests to the list, then let me choose where to start. This holds for a feature, a bug fix, a task, and an increment alike.

Some work changes no behaviour, so no test drives it. Prepare that as a numbered list of refactorings instead, each of which ends green, and let me choose where to start.

Some increments are made of many items I want to steer, such as authored sentences or a list of findings. Prepare those the same way and review them in small batches. Never land them in one green.

A cleanup that follows from a tool's findings (SonarCloud, a linter) or from a review is a task like any other. Group the findings, say which of them change behaviour, and let me choose which ones a test has to drive. The list of findings is the increment, however many cycles it takes, so propose the increment review once the list is done, not after each finding.

Each cycle has three steps:

1. **Red**:
   - Show the list with each test's status, even when nothing on it changed. Once it holds more than five tests, one line for the passing tests and a row per test still to write is enough.
   - Write the candidate test. Don't design or write the implementation yet.
   - Check the new test against the existing ones first. When one already covers the case, add the assertion to that test instead of writing a near-duplicate.
   - Predict the outcome before you run `just test`: how the test will fail, naming the exact error, or why it will pass.
   - Run the whole suite, not only the new test's module. A test predicted to fail is the only one failing, and fails as predicted. A test predicted to pass leaves the suite green. When anything else happens, work out why and tell me.
2. **Green**:
   - If the test failed, design the smallest change that makes it pass, then run `just test` again.
   - Smallest counts what the change leaves behind. A hack that the next cycle throws away is not smaller, so say what you rejected when you pick the structural change instead.
   - Update every docstring and comment the change makes untrue. Do that now, not in the refactor step.
   - If the test passed, mutate the mechanism it relies on and confirm that it fails.
   - When the step adds or changes a check, run that check over the real tree as well. A check that passes its fixtures can still be blind to the real file, which someone wrote without sharing its assumptions. Read what it reports, then mutate that file: `just mutate <that file> just <check>` when `just check` runs the check, plain `just mutate <that file>` when the test suite does.
   - End the step with `just verify`, green. When the smallest change that passes the test leaves a check failing, the step is too small: widen it rather than ending red.
3. **Refactor**:
   - Review and refactor the production code and the tests. A fix that a linter flagged is not this review.
   - Review the whole diff against the criteria above, comments and docstrings included: the ones you wrote, and the ones that stayed the same while the code they describe changed. Ask of each whether it should exist at all, not only whether it is still true.
   - Review the call sites of anything whose signature, fields, or behaviour the step changed, and their docstrings. The diff doesn't show them.
   - Review the architecture. A step that adds a dependency between modules, or widens what a module exposes, wants a new or a tightened architecture test.
   - Work through the findings one at a time, running `just verify` after all findings have been fixed.
   - The cycle ends here, not at step 2. Say what you fixed and move on.

A few rules that keep the cycle honest:

1. Follow the conventions of the nearest existing test for the same kind of behaviour, unless that test breaks a rule below. Cover the cases it covers as well: a construct of the same shape has the same edges, so read them off that test before you choose where to start. Loop a table of cases with `subTest`, naming each case, rather than repeating an assertion or a helper call. Such a table reports one failure per failing case, so predict that many.
2. Build a behaviour before its off-switch. Don't test an opt-out, a flag, or any other suppression until the thing it suppresses exists.
3. Assert what happens and what doesn't. An assertion runs on every run; a claim in a docstring is checked by nobody.
   - A test that asserts nothing was found also passes when nothing was examined, so assert that something was.
   - Neither a test's name nor a green run is evidence of what the test guards. Settle that with `just mutate`, for a duplicate you would fold or delete as much as for anything else, and register the mutation on the test with `@kills`.
   - Call a stub that varies its answer by an argument with more than one value of that argument, or the test shows something other than what its name claims. The same holds for a fixture whose docstring names a case it does not create.
   - Pick the mutation from the regression the guard defends against, not from the nearest line to mutate. One that leaves the guard green says nothing about it. One that fails a dozen other tests says little more: it shows the suite reacting, not that guard.
   - When a stub quotes more than a handful of lines, look for a shorter form that isolates the same regression.
4. A test that pins existing behaviour drives no code. Predict that it passes and say so: it closes a gap in intent, not in behaviour. A step that only removes behaviour drives no test either, so don't offer one asserting that the removed thing is gone. Delete the tests that guarded it, and predict what the deletion leaves behind: the suite green, the count down by exactly the tests you deleted, and any name those tests were the last outside caller of now private.
5. Coverage stays at 100%, and `just test` enforces it, so you need no separate coverage command. A gap after implementing points at a test case worth adding, not at a line worth excluding. Never end a step with the new code uncovered: when the test you chose mocks the collaborator the new code lives on, add a second test in the same step that reaches it. Coverage omits one file, `tools/fixit_rules.py`. Pin each of its branches with the VALID and INVALID cases that `just fixit` runs.
6. Treat a failing existing test as a signal. Work out whether its premise legitimately changed and say why. Don't patch the assertion to match the new output.
7. A green run proves nothing by itself, so check that it ran what you think it ran.
   - Read the tail of `just test` and `just check` yourself. Don't grep or count their output: a filter that matches nothing exits non-zero and silently skips the rest of an `&&` chain.
   - A filter that prints nothing has told you nothing. Never read that silence as a pass.
   - One run prints all of its output, so read that run rather than starting another for a different slice of it.
   - Compare the test count whenever imports move or test methods are renamed. A module that fails to import, or a method whose new name collides with an existing one, drops tests without a word.
   - Never read a count off output you piped through `head`.
   - Run `just mutate` unpiped. Which tests failed is the evidence you are after, and only the bare form runs without an approval prompt.
8. Settle a "could we do X?" question by trying X, not by reasoning about it. Settle a "there is no X" the same way: a search you stopped is not a proof.
   - Report the errors the tools name, and what the alternative costs. Bring me numbers, whether the question is one you mean to put to me or one about your own first draft.
   - Try a library the project already depends on before you hand-roll one.
   - Try the simplest option that could work before you measure an elaborate one. Where both work, put the simpler one to me first.
   - Write a probe as a heredoc through `just py -`, not as a file to write and delete. A probe that has to type-check is the exception: put it under `src` or `tests`, where the checkers look, and take it out again once `just ty` has answered.
   - When only CI can decide, try each option locally and say what stays unverified.
   - Don't announce a comparison and then not run it.
   - A probe is evidence only when it would fail if the answer were the other way. A rule that matched nothing, or a probe whose signal the code under test swallows, passes exactly like one that holds.
9. Check a claim against the code or the tools before you state it. One run or a look at a sibling module usually settles one, such as "nothing covers this yet".
   - A review finding is a claim, and the most plausible findings are the ones to check hardest. Mutate the code the finding describes and say what that showed, or don't report it.
   - Anything an issue says is a claim too, whether you carry out its instruction or copy its sentence into the README. A spec says what was intended, not what got built.
   - So is the reason you give for an option you put to me, because I choose on that reason.
   - Measure a challenged claim. Don't argue it.
10. Add a missing candidate test to the list as soon as you find one, at whatever step you are in.
11. A refactor that changes behaviour is not a refactor, however unreachable the changed case looks. A change that only *adds* behaviour counts as well: a decorator that fills in methods the class was missing changes what calling them does. Say so before you make the change, not after, and let me decide whether a test has to drive it first.
12. A problem you hit and fixed yourself needs no narration, at whatever step it happens: a probe that misfired, a rewrite that overreached, a formatter that undid an edit. That holds for the cycle's report and for the session's edits as much as for the work itself. Note it, and bring it to the session's evaluation if it still matters.
13. An answer of mine may admit more than one reading. Name the readings you see and ask, rather than implementing the one you would pick: a message costs less than the cycle that undoes a guess. When I push back on one passage twice, we are working from different assumptions. Name yours and ask for mine, rather than rewriting the passage again.

When every candidate test passes, propose an increment review before you propose the self-improvement session. The increment is the one an issue lists, or the whole session where no issue lists one.

Review everything the increment changed, against the same criteria, and report it as its own numbered list. Read the files it touched end to end rather than its diff: a finding can span cycles and show up in no single one, such as a helper the second cycle duplicated, a module head grown long with constants, or a name that restates the line beside it.

A bug fix or a small diff gets that one review. An increment of several cycles gets a second review with fresh context, from subagents you give the criteria and the diff but not the reasoning that produced the code, because a review in the context that wrote the code misses what a fresh one catches. Tell the subagents to check each finding against the code before reporting it.

Merge both reviews into one numbered list. A finding only one review reached belongs in it as much as one they both reached, which you state once. Every finding sits in that list, so each can be referred to by its number, and the text around the list holds none. Say which findings change behaviour before I choose what to fix.

## Documentation

- README.md is generated. Edit `docs/README.md.in` and regenerate with `just readme`. An edit to README.md itself is lost on the next run, without a word. Its per-type headings are questions, so keep each section's sentences answering its own question.
- A change to what Update-time does gets a changelog entry under `[Unreleased]`: one line naming the behaviour and linking the issue. A change to the documentation alone gets none.
- The detail belongs in the README, not in the changelog. The README names the behaviour and shows the message it produces. It leaves out a worked example of what that message already shows.
- When a change lands over several cycles, update the README and the changelog once the behaviour has settled, and before you hand the increment back. Neither may document a state the code is not in. A command-line option is one such state: the cycle that adds it does everything its help promises, rather than accepting a value it then ignores.

## Self-improvement

At the end of a session, evaluate how the session went against this file, and fold what you learn back into it.

- Propose the evaluation yourself when the work looks finished, with the tests and checks green and nothing left to steer. Don't wait for me to think of it, and don't start it unasked: I decide when it runs.
- Interview me first, then add your own evaluation. Ask a handful of questions, one at a time. Each question names the moment it is about: a guideline you deviated from, a correction I made, a cycle that skipped a step. A command you ran repeatedly outside `just` is such a moment too, because it means a recipe is missing, broken, or one you didn't reach for. Leave your own verdict out of the questions, so the answers are mine. Close with an open question asking what else I noticed, and repeat it as long as I answer it with something.
- Then report your evaluation, briefly and factually: which guidelines were followed, which were deviated from, which went unused, and what happened in each case. Propose the edits from the main points of the interview and of your evaluation.
- Name the cause of each deviation, because different causes call for different edits. A guideline that was clear and simply not followed needs sharpening, so that nobody can read past it a second time. A guideline that turned out to be silent on a case needs that case spelled out in it.
- Write a check instead, where the deviation can be caught mechanically: a fixit rule, a script under `tools/`, or a hook in `.claude/settings.json` for a rule about which commands to run. The rules that hold are the ones a failing run enforces. A check that `just check` runs holds everywhere, so it replaces the line it enforces. A hook binds this harness alone, so it adds enforcement and the line stays.
- Ground every edit in something that happened this session. Don't add best practice that nothing in the session called for.
- Keep this file short. Rewrite an existing line rather than adding one, and propose dropping or merging the lines that go unused session after session. I read it in full at the start of every session, so its length costs me every time.
- Propose each edit with the moment in the session that prompted it, and wait for me to choose. Make the ones I accept, then run `just check`, which reads this file too.
- This file holds how we work. What the code does belongs in the code, its docstrings, the README, and the changelog, so don't move project facts in here.
