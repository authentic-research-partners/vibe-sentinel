# Security

## Reporting a vulnerability

Email **oss@arpconnect.com**. Please do not open a public issue for a
security problem: an issue is readable by everyone before there is a fix.

Include what you ran, what you expected, what happened, and the version
(`vibe-sentinel --version`). A command or file that reproduces it is the
most useful thing you can send.

Ordinary bugs, including a verdict you disagree with, belong in the public
issue tracker.

## Supported versions

vibe-sentinel is pre-1.0. Fixes go into the next release, not into older
ones — use the latest.

## What counts as a vulnerability

The boundaries the tool states and does not keep:

- a credential excerpt sent to a non-loopback model endpoint without the
  explicit `allow_remote_model` override, or an unredacted credential value
  written to stdout, the log or the history database;
- the dependency inventory sent to a third party without `--online`;
- a command the documentation says the safety gate flags passing it
  unflagged, or a way to make the gate record a verdict it did not reach;
- the hook blocking or crashing an agent's tool call when the gate is off
  or in `observe`.

## What does not

The gaps in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) are known and
listed there — reads outside the watched home-directory paths, uploads to
expected hosts, policy writes hidden inside interpreter code, and others. A report that one of them
is exploitable is welcome, but it is a known gap rather than a new finding.
So is:

- a wrong verdict from the local model — that is a rate, measured in
  [docs/evals.md](docs/evals.md); a case that reproduces it is best filed
  as an eval case;
- anything that needs an attacker already running code as the user. The
  safety gate is a guardrail inside the agent's trust domain, not a
  sandbox. `hook --install --managed` and the system policy move its
  configuration out of the agent's reach; the agent still runs as the user.

## Assumptions

- The user and the machine are trusted; the coding agent acts for the user
  and can be wrong or steered.
- The model endpoint is local. Nothing is sent over the network unless a
  command says so: `packages --online`, or a remote `[llm] endpoint` the
  user configured — and a credential excerpt refuses even that, unless the
  project's config sets `allow_remote_model`.
- Only Claude Code calls the hook today.
