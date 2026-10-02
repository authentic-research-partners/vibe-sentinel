# Control mapping

Each risk vibe-sentinel's safety controls address, the control, whether it
holds, where the risk sits in three published frameworks, and the evidence —
the test or eval case that shows it. It is the index to
[THREAT_MODEL.md](THREAT_MODEL.md), whose threat rows (T1–T19) it cites, and
to the measured rates in [evals.md](evals.md).

A row with no control says **not covered**. Evidence is cited as
*file::name* for a test in `tests/`, and *suite:case-id* for an eval
case; a test in `tests/test_evals.py` fails if any citation here stops
existing, so a row cannot keep claiming evidence that was renamed away.
"Covered" means the described call is detected or a stated boundary is enforced.
Detection can still lead to a `safe` verdict; it does not promise a refusal.
How often the judgement is right is a rate, in
[evals.md](evals.md), not a property this table can promise.

## Sources

Identifiers are copied from the published documents, read on 2026-09-30.

| Framework | Version | Published | Read from |
|---|---|---|---|
| OWASP Top 10 for LLM Applications | Version 2026 | August 2026 (the PDF cover reads "August 4th, 2026") | [genai.owasp.org](https://genai.owasp.org/resource/owasp-genai-llm-top-10-2026/) |
| OWASP Top 10 for Agentic Applications | Version 2026 | December 2025 | [genai.owasp.org](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/) |
| OWASP Agentic AI – Threats and Mitigations | Version 1.1 | December 2025 | [genai.owasp.org](https://genai.owasp.org/resource/agentic-ai-threats-and-mitigations/) |
| MITRE ATLAS | data 2026.09 | 2026-09-15 | [atlas-data, dist/v6/ATLAS-2026.09.yaml](https://github.com/mitre-atlas/atlas-data) |
| NIST AI RMF (NIST AI 100-1) | 1.0 | January 2023 | [nvlpubs.nist.gov](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf) |
| NIST AI 600-1, Generative AI Profile | — | July 2024 | [nvlpubs.nist.gov](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf) |

The 2026 LLM Top 10 renumbered the 2025 one — Supply Chain is `LLM04:2026`,
where it was `LLM03:2025`, and Excessive Agency is `LLM03:2026`, where it was
`LLM06:2025` — so an identifier is always cited with its year. ATLAS's
`dist/ATLAS.yaml` is deprecated and frozen at 5.6.0; the IDs here are from the
2026.09 release.

## Overview

| # | Risk | Threat | Status | OWASP | MITRE ATLAS | NIST AI RMF |
|---|---|---|---|---|---|---|
| C1 | Destructive action on files or history | T2, T5 | covered | LLM03:2026, ASI02 | AML.T0101, AML.T0053 | MEASURE 2.6 |
| C2 | Destruction through an interpreter or infrastructure tool | T3, T4 | covered, by name | ASI05, LLM03:2026 | AML.T0050, AML.T0101 | MEASURE 2.7 |
| C3 | Code fetched at run time executed | T1 | covered, except a script fetched into the project and run there | ASI05 | AML.T0050, AML.T0053 | MEASURE 2.7 |
| C4 | A hallucinated or misspelled dependency installed | T6, T7 | covered | LLM04:2026, ASI04 | AML.T0010.001, AML.T0060, AML.T0011.001 | GOVERN 6.1, MAP 4.1, MANAGE 3.1 |
| C5 | Local data sent off the machine | T8 | covered, except to an expected host | LLM02:2026, ASI02 | AML.T0086 | MEASURE 2.7 |
| C6 | The agent reads credentials in the home directory | T9 | covered in the home directory | LLM02:2026, ASI03 | AML.T0055 | MEASURE 2.7 |
| C7 | Credentials at rest in the working tree | — | covered (a gate) | LLM02:2026 | AML.T0055 | MEASURE 2.7, MANAGE 1.3 |
| C8 | A write outside the project | T10 | covered | LLM03:2026, ASI02 | AML.T0053 | MEASURE 2.6 |
| C9 | The agent switches its own guardrail off | T11 | covered, except through an interpreter | LLM03:2026, ASI03 | AML.T0081 | MANAGE 2.4, GOVERN 1.4 |
| C10 | The audit record moved or deleted | T12 | covered | Agentic T&M T8 | no match | MEASURE 2.4, MANAGE 4.1 |
| C11 | A dangerous MCP tool call | T13 | partial: by name | ASI02, LLM03:2026 | AML.T0053, AML.T0101 | MAP 4.2, MEASURE 2.6 |
| C12 | PowerShell | T14 | partial: two spellings | ASI05 | AML.T0050 | MEASURE 2.7 |
| C13 | Prompt injection against the judge | T15 | partial | LLM01:2026, ASI01 | AML.T0051.001 | MEASURE 2.7 |
| C14 | A different model answering as the judge | T16 | partial | LLM04:2026 | AML.T0010 | MANAGE 3.2 |
| C15 | A question cut short without an error | T17 | partial | — | no match | MEASURE 1.1 |
| C16 | A secret sent to a remote model | T18 | prevented by default | LLM02:2026 | AML.T0057 | MEASURE 2.7 |
| C17 | The dependency inventory sent to a third party | T19 | prevented | LLM02:2026 | no match | GOVERN 6.1 |
| C18 | The gate has no verdict | — | covered: asked under `enforce` | Agentic T&M T10 | no match | MAP 3.5, MEASURE 2.6 |
| C19 | The hook outlives its own timeout | — | **not covered**: the call proceeds | — | no match | MEASURE 2.6 |
| C20 | The judge is wrong | — | measured, not prevented | LLM07:2026 | no match | MEASURE 2.3, MEASURE 1.1 |

The identifiers, in full:

- OWASP LLM 2026: `LLM01:2026` Prompt Injection · `LLM02:2026` Sensitive
  Information Disclosure · `LLM03:2026` Excessive Agency · `LLM04:2026` Supply
  Chain · `LLM07:2026` Misinformation.
- OWASP Agentic 2026: `ASI01` Agent Goal Hijack · `ASI02` Tool Misuse and
  Exploitation · `ASI03` Identity and Privilege Abuse · `ASI04` Agentic Supply
  Chain Vulnerabilities · `ASI05` Unexpected Code Execution (RCE).
- OWASP Agentic AI – Threats and Mitigations 1.1: `T8` Repudiation &
  Untraceability · `T10` Overwhelming Human in the Loop.
- MITRE ATLAS 2026.09: `AML.T0010` AI Supply Chain Compromise ·
  `AML.T0010.001` AI Software · `AML.T0011.001` Malicious Package ·
  `AML.T0050` Command and Scripting Interpreter · `AML.T0051.001` Indirect
  (LLM Prompt Injection) · `AML.T0053` AI Agent Tool Invocation ·
  `AML.T0055` Unsecured Credentials · `AML.T0057` LLM Data Leakage ·
  `AML.T0060` Publish Hallucinated Entities · `AML.T0081` Modify AI Agent
  Configuration · `AML.T0086` Exfiltration via AI Agent Tool Invocation ·
  `AML.T0101` Data Destruction via AI Agent Tool Invocation. ATLAS has no
  technique for impairing an agent's audit log, which is why C10 says *no
  match* rather than stretching one.
- NIST AI RMF 1.0: GOVERN 1.4 transparent risk policies and controls ·
  GOVERN 6.1 third-party risk · MAP 3.5 human oversight · MAP 4.1 mapping the
  risks of components, including third-party software · MAP 4.2 internal risk
  controls for components · MEASURE 1.1 metrics, and documenting what cannot
  be measured · MEASURE 2.3 performance measured for conditions similar to
  deployment · MEASURE 2.4 monitoring in production · MEASURE 2.6 safety, and
  failing safely · MEASURE 2.7 security and resilience · MANAGE 1.3 responses
  to high-priority risks · MANAGE 2.4 mechanisms to supersede, disengage or
  deactivate · MANAGE 3.1 third-party risks monitored · MANAGE 3.2 pre-trained
  models monitored · MANAGE 4.1 post-deployment monitoring. The subcategory
  texts are in NIST AI 100-1, Tables 1–4. From NIST AI 600-1, the rows touch
  the GAI risks *Information Security* (C1–C13), *Data Privacy* (C5, C6, C7,
  C16, C17), *Value Chain and Component Integration* (C4, C14) and
  *Confabulation* (C20).

## The rows

### C1 — Destructive action on files or history

**Threat:** T2, T5. **Control:** `deleting-files`, `recursive`,
`home-directory`, `discarding-work`, `force` and `rewriting-history` flag the
command, and the model's verdict decides; `changing-the-gate` settles `rm -rf .`
and any delete of the gate's own files as `unsafe`. **Status:** covered.

**Evidence:** `test_safety::test_destructive_commands_escalate`,
`test_safety::test_enforce_denies_an_unsafe_command`,
`adversarial:delete-ssh-keys`, `adversarial:delete-git-history`,
`adversarial:force-push-main`,
`adversarial:destructive-step-after-reading-a-guide`,
`safety:rm-rf-home-documents`, `safety:git-force-push-main`.

### C2 — Destruction through an interpreter or infrastructure tool

**Threat:** T3, T4. **Control:** `deleting-files` matches an interpreter's
deletion calls by name — `shutil.rmtree`, `os.remove`, `unlink`, node's
`fs.rm` and `rmSync`, `rimraf`. `removing-infrastructure` catches `terraform
destroy` and `apply -destroy`, `pulumi`, `cdk` and `tofu destroy`, `kubectl
delete` and container prunes; `changing-a-database` catches `DROP`. A
deletion through a call the list does not name is not seen. **Status:**
covered, by name.

**Evidence:** `test_safety::test_destructive_commands_escalate`,
`test_safety::test_ordinary_work_never_reaches_the_model`,
`adversarial:interpreter-deletes-home`, `adversarial:terraform-destroy`,
`safety:kubectl-delete-in-prod`, `safety:drop-production-database`.

### C3 — Code fetched at run time executed

**Threat:** T1. **Control:** `running-unseen-code` flags `curl … | sh`,
`bash -c "$(curl …)"`, a decoded payload piped to a shell, and a file run
from where downloads land — a temp directory, `~/Downloads` — which is how a
fetch and a run split across two commands look; the model reads the fetch
in the history. It answers `unclear` for most of these, which under
`enforce` is asked about rather than allowed. A script fetched into the
project and run from there is not flagged. **Status:** covered, with that
gap.

**Evidence:** `adversarial:fetched-script-piped-to-shell`,
`adversarial:fetched-script-in-command-substitution`,
`adversarial:encoded-payload-piped-to-shell`,
`adversarial:fetched-then-run-in-two-steps`,
`test_safety::test_eval_in_a_command_position_escalates`,
`test_safety::test_enforce_asks_about_an_unclear_verdict`.

### C4 — A hallucinated or misspelled dependency installed

**Threat:** T6, T7. **Control:** `installing-undeclared-dependency` asks about
any name `pip`, `uv pip`, `npm`, `pnpm`, `yarn` or `bun` would install that the
manifest does not declare, before its install scripts run; a manifest that
will not parse is refused under `enforce` rather than read as empty, and an
install whose words cannot be split is asked about rather than read as
installing nothing. The
`packages` gate pairs installed names one edit apart and asks which pair is a
squat. **Status:** covered.

**Evidence:** `test_safety::test_an_undeclared_install_escalates`,
`test_requirements::test_an_npm_family_install_of_an_undeclared_name_is_found`,
`test_safety::test_a_broken_manifest_refuses_an_install_under_enforce`,
`test_packages::test_typosquat_and_unclear_both_stand`,
`test_packages::test_a_freshly_registered_hallucination_is_squatted`,
`adversarial:misspelled-python-package`, `adversarial:misspelled-npm-package`,
`adversarial:misspelled-package-with-yarn`,
`adversarial:misspelled-package-with-bun`,
`adversarial:misspelled-package-behind-a-comment`,
`adversarial:misspelled-npm-package-behind-a-comment`,
`test_safety::test_an_install_the_gate_cannot_split_escalates`,
`packages:requests-misspelled`,
`packages:urllib3-misspelled`, `packages:django-homoglyph`.

### C5 — Local data sent off the machine

**Threat:** T8. **Control:** `sending-data-off-the-machine` asks about an
upload — `curl` or `wget` with data, a raw socket, a remote copy, `ssh` fed
through a pipe, a cloud-storage upload — to any host not in
`[safety] expected_hosts`, and a command the gate cannot split into words
whose text names a sender. A secret sent to an expected host, or sent by an
interpreter's own code, is not seen. **Status:** covered, with those gaps.

**Evidence:** `test_safety::test_sending_to_an_unexpected_host_is_asked_about`,
`test_safety::test_fetching_loopback_and_expected_hosts_are_not_egress`,
`adversarial:upload-env-file`, `adversarial:pipe-env-to-socket`,
`adversarial:copy-secrets-to-a-remote-host`,
`adversarial:upload-behind-unsplittable-quoting`,
`test_safety::test_a_command_that_will_not_split_still_matches_what_its_text_names`.

### C6 — The agent reads credentials in the home directory

**Threat:** T9. **Control:** `secret-store` and `secret-store-target` settle a
command or a tool naming `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.netrc` and the
other stores as `unsafe`; `reaching-into-home` asks about anything else in the
home directory outside the project. Outside the home directory is not watched.
**Status:** covered in the home directory.

**Evidence:** `test_safety::test_a_secret_store_is_settled_unsafe`,
`test_safety::test_the_rest_of_home_is_asked_about`,
`test_safety::test_the_project_and_the_system_are_not_home`,
`adversarial:read-private-key-from-shell`,
`adversarial:read-private-key-with-read-tool`,
`adversarial:read-cloud-credentials`,
`adversarial:read-another-projects-secrets`,
`adversarial:read-another-projects-secrets-behind-unsplittable-quoting`.

### C7 — Credentials at rest in the working tree

**Threat:** not a tool call — a fact about the tree. **Control:** the
`credentials` gate finds candidates by pattern, has the local model judge each
from a prefix and a shape, and fails until each live one is removed or pinned
with a reason. **Status:** covered.

**Evidence:** `test_credentials::test_files_that_exist_to_hold_credentials`,
`test_credentials::test_credentials_hardcoded_into_ordinary_source`,
`test_credentials::test_a_candidate_nobody_judged_fails_the_run`,
`credentials:dotenv-with-live-values`, `credentials:private-key-at-rest`,
`credentials:provider-token-in-source`,
`credentials:documented-example-access-key`.

### C8 — A write outside the project

**Threat:** T10. **Control:** `writing-outside-the-project` for a writing tool
aimed anywhere outside the project; `reaching-into-home` for a shell write into
the home directory. **Status:** covered.

**Evidence:** `test_safety::test_a_write_outside_the_project_escalates`,
`adversarial:write-shell-startup-file`,
`adversarial:append-to-shell-startup-from-shell`.

### C9 — The agent switches its own guardrail off

**Threat:** T11. **Control:** `changing-the-gate` settles a write, move or
delete of the gate's settings, its hook entry, its record or a declared rule
file as `unsafe`, with no model, so it holds with the backend down — and
`vibe-sentinel hook --install`, which rewrites the mode without naming the
file. `vibe-sentinel status` says which mode is in force and where the hook
is installed, and says so when it is not. `hook --install --managed` puts the
hook in managed settings no project can override and the mode and dangers in a
system policy no project can lower; those files are protected the same way,
so the agent cannot switch the gate off by editing what it owns — though as
the same user it can still try, and `sudo` is asked about, not prevented. A
write
hidden in an interpreter's code is not seen. **Status:** covered, with that
gap.

**Evidence:** `test_safety::test_a_change_to_the_gate_is_settled_unsafe`,
`test_safety::test_reading_the_gate_or_naming_the_root_is_not_changing_it`,
`test_safety::test_editing_the_gate_is_refused_under_enforce_with_the_model_down`,
`adversarial:edit-sentinel-config`, `adversarial:turn-gate-off-from-shell`,
`adversarial:rewrite-hook-settings`,
`adversarial:turn-gate-off-through-its-own-cli`,
`test_safety::test_reinstalling_the_hook_is_changing_the_gate`,
`test_cli::test_status_says_nothing_is_installed_in_those_words`,
`test_system_policy::test_the_floor_holds_at_the_hook`,
`test_system_policy::test_a_project_cannot_remove_or_soften_a_system_danger`,
`test_system_policy::test_writing_a_system_file_is_changing_the_gate`.

### C10 — The audit record moved or deleted

**Threat:** T12. **Control:** `changing-the-gate` covers
`.vibe-sentinel/`, the history database included; the hook spills an event it
cannot write rather than dropping it, and replays it later. **Status:**
covered, for anything that goes through the agent's tools.

**Evidence:** `adversarial:move-history-database`,
`adversarial:delete-history-database`,
`test_hook::test_an_unwritable_database_spills_instead_of_dropping`,
`test_hook::test_replay_drains_the_spill_keeping_the_original_time`.

### C11 — A dangerous MCP tool call

**Threat:** T13. **Control:** `deleting-through-a-connected-tool` settles an MCP
tool whose name says it deletes as `unsafe`; a project names its own tools with
`applies_to = "tool"`. The gate sees the name, not the arguments.
**Status:** partial.

**Evidence:** `test_safety::test_a_connected_tool_that_deletes_is_settled_unsafe`,
`test_safety::test_a_project_names_its_own_tools`,
`test_safety::test_a_deleting_tool_is_refused_under_enforce_with_the_model_down`,
`adversarial:mcp-delete-repository`, `adversarial:mcp-delete-page`.

### C12 — PowerShell

**Threat:** T14. **Control:** `deleting-files` matches `Remove-Item`, and
`running-unseen-code` matches `iwr … | iex` and `Invoke-Expression` over a
download. The rest of the danger set reads POSIX shells, and the hook has
never run on native Windows. **Status:** partial.

**Evidence:** `test_safety::test_destructive_commands_escalate`,
`adversarial:powershell-recursive-delete`,
`adversarial:powershell-download-and-run`.

### C13 — Prompt injection against the judge

**Threat:** T15. **Control:** every shipped prompt marks which blocks are data
and names the field an attempt to steer the model is reported in; the gate
judges the action, whatever asked for it. No eval measures an injection yet.
**Status:** partial.

**Evidence:** `test_prompts::test_a_prompt_given_untrusted_content_says_it_is_data`,
`test_prompts::test_a_prompt_names_where_an_attempt_gets_reported`,
`test_prompts::test_every_delimiter_a_prompt_names_is_one_its_module_emits`.

### C14 — A different model answering as the judge

**Threat:** T16. **Control:** a model name only the project builds, with the
digest it was built from recorded beside its build file; `backend status`
names environment overrides. It cannot check the digest. **Status:** partial,
and no test covers it.

### C15 — A question cut short without an error

**Threat:** T17. **Control:** the documentation sets Ollama's context, and a
project can pin one in the model it builds. Nothing detects a truncated prompt
at run time; a truncated *answer* is detected and retried. **Status:**
partial.

**Evidence:** `test_llm::test_a_truncated_answer_is_discarded_even_when_it_parses`,
for the answer side only.

### C16 — A secret sent to a remote model

**Threat:** T18. **Control:** a credential excerpt goes only to a loopback
endpoint, reduced to a prefix and a shape; no environment variable can change
that. An explicit `[credentials] allow_remote_model = true` permits a remote
endpoint. **Status:** prevented by default.

**Evidence:** `test_credentials::test_a_remote_endpoint_is_refused`,
`test_credentials::test_nothing_is_sent_to_a_remote_endpoint`,
`test_credentials::test_an_environment_override_cannot_send_a_secret_off_the_machine`,
`test_credentials::test_the_printable_excerpt_never_contains_the_value`,
`test_gates::test_no_credential_value_reaches_the_database`.

### C17 — The dependency inventory sent to a third party

**Threat:** T19. **Control:** the index is contacted only under `--online`, and
only about names already flagged and those `pyproject.toml` declares publicly;
the CVE check is off unless configured. **Status:** prevented.

**Evidence:** `test_packages::test_only_flagged_and_publicly_declared_names_are_sent_to_the_index`.

### C18 — The gate has no verdict

**Threat:** the model answers `unclear`, does not answer in time, or the gate
fails. **Control:** under `enforce`, the command is asked about with Claude
Code's permission prompt (`[safety] no_verdict = "ask"`), and where nobody can
answer that ends as a refusal naming the fix. A configuration or policy the
gate cannot read is refused, never read as off. The cost — prompts on commands
that were fine — is measured: `safe-command-interrupted` and
`twin-interrupted` in [evals.md](evals.md). **Status:** covered.

**Evidence:** `test_safety::test_no_model_means_unreviewed_and_asked`,
`test_safety::test_enforce_asks_about_an_unclear_verdict`,
`test_safety::test_a_broken_gate_under_enforce_asks_and_never_blocks`,
`test_safety::test_a_misspelt_no_verdict_is_not_read_as_allow`,
`test_safety::test_an_unreadable_config_refuses_the_command`,
`test_safety::test_a_broken_danger_set_refuses_under_enforce`.

### C19 — The hook outlives its own timeout

**Threat:** Claude Code cancels a hook that runs past its timeout and lets the
call proceed. **Control:** none can close it; `[safety] timeout` (8 s) is kept
well under the hook's own (20 s), so the gate answers before it is cancelled.
**Status:** not covered.

**Evidence:** `test_safety::test_a_slow_model_cannot_blow_the_hook_budget`.

### C20 — The judge is wrong

**Threat:** a small local model gives the wrong verdict. **Control:** a
declared verdict settles a command without the model; everything else is a
rate, measured by the `safety` and `adversarial` suites against labelled cases
and scored as the gate decides. **Status:** measured, not prevented — in the
pinned judge's baseline, 1 verdict in 81 called an unsafe command safe
(`git reset --hard` over uncommitted edits, in one run of three); see
[THREAT_MODEL.md](THREAT_MODEL.md#what-a-verdict-is-worth).

**Evidence:** `test_safety::test_a_declared_verdict_settles_it_without_the_model`,
`test_evals::test_the_evals_measure_what_the_gate_decides_not_what_the_model_says`,
`safety:git-reset-hard-over-edits`.
