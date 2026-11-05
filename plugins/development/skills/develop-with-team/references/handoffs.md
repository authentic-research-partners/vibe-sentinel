# Team handoffs

Use one compact assignment per worker. Resolve paths against the repository
root and include that root explicitly, especially for a fresh-context agent.
Read the selected role's bundled TOML and include its instructions when the
tool cannot select a registered role. State the actual model and effort
requested through the tool, not an inferred setting.

## Assignment

```text
Role and selection: role name; requested model; reasoning effort
Repository: absolute root
Scope: outcome to deliver and explicit exclusions
Instructions: applicable AGENTS.md paths; selected role instructions
Paths owned: files/directories this worker may edit
Other owners: shared or adjacent paths reserved to other workers
Contracts: agreed interfaces, data shapes, errors, and pending decisions
Acceptance: observable conditions that complete this assignment
Checks: relevant commands to run; required integration checks owned by lead
Context: current facts, relevant files, and assumptions to verify
Blockers: known missing inputs, permissions, or dependencies
Return: changed paths, behavior, checks with results, remaining blockers
```

Use `gpt-6.1-sol` with `medium` effort for `sentinel-docs`. Implementation
and QA use `gpt-6-astra` with the effort selected for their assignment.

An ownership list coordinates edits; it grants no extra filesystem or action
permissions. A worker needing another path or contract change tells the lead
before editing it. Assign concrete file subsets when a directory has several
owners. Avoid copying an entire conversation when the contract and relevant
evidence suffice.

## Worker return

```text
Scope completed: outcome and acceptance criteria satisfied
Paths changed: owned paths actually edited
Contracts: retained contracts or proposed changes requiring integration
Checks: exact command, working directory, outcome, and relevant failure detail
Not checked: omitted checks and why
Blockers and uncertainty: evidence needed or decisions still open
Next step: specific integration or review action
```

Keep `passed`, `failed`, and `not run` distinct. Include exit status or test
counts when useful; do not substitute "looks good" for execution. If another
worker or the lead changes the candidate after a check, identify which
results need revalidation.

## Independent QA return

Use the worker return above and attach findings in this form:

```text
Severity: critical / high / medium / low, with concrete impact
Location: path and line(s) in the reviewed candidate
Trigger: inputs or sequence that exposes the problem
Evidence: observed result or traceable implementation behavior
Expected: acceptance criterion or contract being violated
Consequence: what fails for the user or downstream component
Verification: reproducer/check run, or clearly marked unexecuted reasoning
```

Report no findings when none are supported. Distinguish an unavailable check
from a defect, and identify the candidate reviewed so later changes do not
inherit a review they never received. Review starts read-only; the lead may
reassign a fix, but anyone who authors that fix cannot be its sole independent
reviewer.

## Resumable task note

When authorized, use a visible repository note at a task-appropriate path
chosen by the lead, honoring any existing project convention. Record that
path in the conversation. Keep it concise and update it at meaningful
handoffs rather than after every tool call. Do not include credentials,
private environment values, or raw model transcripts.

```text
Task and scope: user request, intended result, exclusions
Candidate: repository, branch if present, relevant working-tree changes
Checklist: completed items with evidence; active and pending items
Ownership: roles, owned paths, current assignments, available capacity
Contracts and decisions: agreed behavior and reason for material choices
Checks: commands, outcomes, candidate they covered, checks still required
Findings: open QA items and assigned owners
Attempts: failed approaches and the evidence they produced
Blockers: missing evidence, capability, authorization, or user decision
Next action: concrete first step for the next lead
```

The note is a handoff, not an authority source. Reconcile it with the actual
tree and current user instructions before continuing. Mark uncertain or
stale evidence explicitly. Do not assume agent identifiers remain usable
across chats; assignments must be understandable from their scope and paths.
