---
name: develop-with-team
description: Coordinate project development with an Astra lead, relevant implementation specialists, and independent QA. Use when the user requests this team workflow or delegates a development task to the Sentinel team; ordinary Sentinel scans and runtime plugin setup use the runtime plugin instead.
---

# Develop with the Sentinel team

Use this development workflow to deliver the user's scoped change. Invoking
this skill authorizes delegation within that task. It does not authorize
installs, configuration changes, commits, publishing, deployment, or operations
outside the task's existing permissions. The development team is separate from
the Vibe Sentinel runtime plugin and its model endpoint.

## Establish the team

Use these model and effort preferences and load the selected
role's definition from `../../agents/<role>.toml`, relative to this skill's
directory, before assigning work:

| Role | Model | Effort | Assignment |
| --- | --- | --- | --- |
| `sentinel-lead` | `gpt-6-astra` | high | Scope, contracts, checklist, ownership, integration |
| `sentinel-backend` | `gpt-6-astra` | medium | Python, services, persistence, API, CLI; high for complex changes |
| `sentinel-node` | `gpt-6-astra` | medium | Node tooling and integration; high for module resolution or workspace changes |
| `sentinel-ui` | `gpt-6-astra` | medium | User interface and interaction changes |
| `sentinel-qa` | `gpt-6-astra` | high | Independent verification and review |
| `sentinel-docs` | `gpt-6.1-sol` | medium | Documentation of implemented behavior |

The bundled TOML files are role definitions, not proof that Codex registered
custom agents. Choose the assignment's model and effort first, including any
implementation effort increase to high warranted by its complexity. If the
current environment exposes a registered role, verify its model and instructions
against the bundled definition and its effort against the assignment. Use a supported effort
override when the assignment needs high and the registered role defaults to
medium. If that override is unavailable, or the role is not registered, use
the available subagent tool with explicit
`model` and `reasoning_effort` for the assignment: `gpt-6.1-sol` with `medium`
for documentation, or `gpt-6-astra` with the selected effort for implementation
and QA. Copy the role's `developer_instructions` into the assignment, together
with the task handoff. For tools that disallow overrides with inherited conversation
history, use a fresh context (`fork_turns: "none"`) and supply the necessary
context explicitly. A role name in a message does not select a model.

Do not silently substitute another model, omit an unsupported effort setting,
or claim that a requested selection was applied without tool support. Report
the blocked assignment and the missing capability. The active root chat's
model and effort are selected by the user in Codex; a prompt cannot change
them. If the root does not meet the requested Astra/high lead settings, make
that limitation explicit before development proceeds. If its active settings
cannot be inspected, report them as unverifiable rather than inventing a
model or claiming the requested settings are active. Never change global
model settings as a side effect of this skill.

## Lead the work

The active lead owns delivery. The root may act as lead when configured for
that role; do not spawn another lead merely to duplicate coordination.

1. Read the project's applicable instructions and inspect the working tree.
   Identify the requested result, existing user changes, and required checks.
   Maintain a concrete checklist with acceptance criteria, marking an item
   complete only after its work and required verification finish.
2. Define cross-component contracts before splitting implementation. Name
   data shapes, public interfaces, error behavior, or ownership boundaries
   that two workers must agree on. Keep assumptions visible and resolve only
   the uncertainties that affect the next assignment.
3. Assign only relevant roles. Use the assignment format in
   [references/handoffs.md](references/handoffs.md) whenever delegating. Give
   each writer explicit paths and one bounded outcome. Keep shared files
   with one owner and serialize dependent work. Workers propose contract
   changes to the lead before changing a shared interface.
4. Fit the schedule to the available agent slots, including the root. Queue
   work when slots are full and retain or free capacity for QA after the
   implementation is ready. Workers return to the lead rather than spawning
   their own teams. Reuse a relevant worker for follow-ups when appropriate.
5. Review worker handoffs and the actual diff before integration. Preserve
   unrelated user changes, reconcile contract changes explicitly, and resolve
   overlapping edits before asking QA to review a stable candidate.

Do not dispatch all six roles by default. A backend-only change may need a
lead, backend worker, and QA; docs or UI workers need a concrete task before
they consume a slot. A lead doing implementation still needs an independent
QA agent for this team workflow.

## Verify and converge

Give `sentinel-qa` the acceptance criteria, relevant project instructions,
candidate paths, and contract decisions. Ask for an independent reading of
the implementation and tests, not endorsement of an implementer's claims.
QA reports actionable findings with severity, file/line evidence, a concrete
trigger and consequence, plus the checks actually run. An unexplored concern
is an uncertainty, not a confirmed defect.

The lead assigns justified fixes to the owning worker, then asks QA to
recheck affected behavior. Do not weaken tests, remove coverage, or relax
project gates to obtain a green result. Changing an expectation requires
evidence that the intended contract changed. Follow the project's mandatory
checks; use targeted checks during iteration, and run required integration
checks on the final combined tree. Report a pass only when that command ran
successfully against the relevant candidate. A missing backend or tool is a
verification limit, never a clean scan; do not start user-managed servers to
remove that limit without authorization.

Bound retries by evidence. After two unsuccessful fix-and-recheck attempts
at the same issue, the lead stops that loop, records the attempts and current
diagnosis, and changes approach only with a concrete new hypothesis. If the
next step requires unavailable evidence, expanded scope, or user choice,
report the blocker while completing independent authorized work. Do not
restart unchanged commands or spawn replacement agents indefinitely.

## Resume and finish

Keep the current checklist, ownership, decisions, and verification evidence
in the conversation. For work that needs a durable handoff, use an existing
user-authorized task note or a routine task note inside the repository when
that write is within the requested scope. Follow the task-note format in
[references/handoffs.md](references/handoffs.md). Do not create hidden global
state or install persistence machinery. If a note is outside authorized
scope, leave the resumable handoff in the conversation instead.

On resuming, read the handoff and current project instructions, inspect the
working tree, and confirm which work and checks still match it. Do not count
stale checks or remembered worker activity as current evidence. Finish with
the delivered behavior, verification results, and remaining blockers or
limits, including the task note's path when one was used.
