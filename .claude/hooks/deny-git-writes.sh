#!/usr/bin/env bash
# Deny a git command that changes the index, HEAD, or a branch or tag (see the Git section of CLAUDE.md). The review
# worktrees are the exception: removing one and its branch, and detaching HEAD inside one, are allowed.

input=$(cat)
cwd=$(printf '%s' "$input" | jq -r '.cwd // ""')
# Heredoc bodies are text, not commands, so a probe that mentions `git reset` is not judged as running it.
command=$(printf '%s' "$input" | jq -r '.tool_input.command' |
    awk '/<<-?.?EOF/ {print; skip=1; next} skip && /^EOF$/ {skip=0; next} !skip')
review_worktree='/\.claude/worktrees/'

deny() {
    jq -nc --arg reason "Run only the git commands that read, such as status, diff, log, and show: $1 (see the Git section of CLAUDE.md)." \
        '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $reason}}'
    exit 0
}

# Judge each simple command on its own: split at `;`, `&`, and `|`, and drop a leading `(` or `$(`.
while IFS= read -r part; do
    [[ $part =~ ^git([[:space:]]|$) ]] || continue
    directory=$cwd
    [[ $part =~ [[:space:]]-C[[:space:]]+([^[:space:]]+) ]] && directory=${BASH_REMATCH[1]}
    # Strip `git` and its global options, leaving the subcommand and its arguments.
    rest=$(printf '%s' "$part" | sed -E 's/^git(([[:space:]]+-[Cc][[:space:]]+[^[:space:]]+)|([[:space:]]+--[a-z-]+(=[^[:space:]]*)?))*[[:space:]]*//')
    subcommand=${rest%%[[:space:]]*}
    arguments=" ${rest#"$subcommand"} "
    case $subcommand in
    add | rm | mv | reset | commit | push | pull | switch | restore | stash | merge | rebase | cherry-pick | revert | \
        clean | am | update-ref | update-index | symbolic-ref)
        deny "git $subcommand changes the index, HEAD, or a ref"
        ;;
    checkout)
        [[ $arguments == *" --detach "* && $directory =~ $review_worktree ]] && continue
        deny "git checkout changes HEAD or the files in the working tree"
        ;;
    branch)
        [[ $arguments =~ ^[[:space:]]+-D[[:space:]]+worktree-agent-[[:alnum:]]+[[:space:]]*$ ]] && continue
        [[ $arguments =~ [[:space:]](-[dDmMcCf]|--(delete|move|copy|force|set-upstream-to|unset-upstream|edit-description))([[:space:]=]|$) ||
            $arguments =~ [[:space:]][^-[:space:]] ]] && deny "git branch with a name or a flag creates, moves, or deletes a branch"
        ;;
    tag)
        [[ $arguments =~ ^[[:space:]]*((-l|--list)([[:space:]].*)?)?$ ]] || deny "git tag with a name creates or deletes a tag"
        ;;
    worktree)
        [[ $arguments =~ ^[[:space:]]*list([[:space:]]|$) ]] && continue
        [[ $arguments =~ ^[[:space:]]*remove[[:space:]] && $arguments =~ $review_worktree ]] && continue
        deny "git worktree changes the worktrees other than by removing a review worktree"
        ;;
    esac
done < <(printf '%s\n' "$command" | tr ';&|' '\n\n\n' | sed -E 's/^[[:space:]]*(\$\(|\()?[[:space:]]*//')
exit 0
