# Use of the local model

The model reads narrow questions prepared by deterministic mechanisms. It does
not walk the repository, choose probe parameters, or discover changes itself.

## What reaches the model

| Check | Mechanical preparation | Model's role |
|---|---|---|
| Drift | Probes, baseline comparison, horizons and fitted series | Rate existing changes, once per active lens |
| Credentials | Rules nominate candidates; pins and declared verdicts settle some | Judge remaining candidates from redacted context |
| Packages | Audit the installed and declared sets; pair similar names | Distinguish two packages from a possible misspelling |
| Licences | Resolve SPDX expressions and evaluate policy | Draft a pin note on request; never change the gate's verdict |
| Safety | Match dangers against the proposed call and tree | Review each matched question with that agent's recent command history |

A lens cannot add or remove a change. Credentials and package reviews can clear
*candidates*: that is their purpose. A credential verdict is an inference from
context, not verification against the service that issued the key.

Each matched question is its own request. Related requests share a system
message so a backend can reuse its prefix. Requests run one at a time by default;
raise `concurrency` only after measuring a backend that batches. See
[Benchmarks](../benchmarks/README.md).

## Questions and limits

Projects phrase questions in `[[lens]]`, `[[danger]]` and `[[secret]]`.
The drift, licence and package passes also accept `guidance`. A lens's
`watch` determines when it has a subject worth asking about.

Untrusted excerpts, commands and package metadata are marked as data in the
prompts. Answers are validated against schemas; text is clipped to declared
bounds. Truncated answers are retried with more room, rather than accepted as
complete. These mechanisms constrain the answer's shape, not its truth:
[Evals](evals.md) measures the judgements.

## When the backend cannot answer

There is no `--no-model` mode. Behavior depends on which decision needs an
answer:

| Command or path | Without a usable model answer |
|---|---|
| `scan` | Fails with exit 2 when required model work cannot complete; measurements remain recorded |
| `credentials` | Exits 2 if candidates remain that require model judgement; pins and declared verdicts need no call |
| `packages` | Exits 2 on a connection failure when near-miss review is needed; an unusable individual verdict leaves that pair as a finding |
| `licenses` | The mechanical gate still works; a requested model-assisted pin draft is unavailable |
| Safety hook | In `enforce`, asks by default on unclear or missing verdicts; `observe` records without intervening |

Safety mode, unreadable-policy handling, declared verdicts and the hook timeout
are specified in [Command safety](gates.md#command-safety). In particular,
`[safety] no_verdict = "allow"` is an explicit choice, not the default.

The test suite stubs `llm_query` in `tests/conftest.py`; it does not use a
separate model-free implementation.

## Endpoint and privacy

The client uses an OpenAI-compatible `/v1/chat/completions` endpoint.
Backend-specific request fields belong in `[llm] extra_body`. Model setup,
context sizing, thinking settings and machine-local overrides are documented
once in [Getting Started](getting-started.md#2-start-the-model-backend).

Credential values are reduced to a prefix and shape for the model and redacted
for reports. Excerpts require a loopback endpoint unless
`[credentials] allow_remote_model = true` explicitly permits a remote one.
Other model calls use the configured endpoint, which may be remote.

A model name does not prove which weights answered. Nor does a valid JSON
response prove that the server read the whole prompt. The
[threat model](THREAT_MODEL.md) distinguishes those limits from answer validation.
