"""Credentials at rest: where they are, and whether they are real.

**Why this belongs in a tool that is not a linter.** It never asks
whether a line of code is good. It asks a question with a factual
answer: *is there a live credential sitting in this working tree, and
where.* That is an inventory fact about the repository, the same kind of
fact as "this dependency is AGPL" — measurable, reproducible, and not a
matter of anyone's taste. What it does with the answer is what every
other gate here does: name the file, name the rule, and make you record
a decision rather than a suppression.

**Two stages, because one would be wrong in both directions.** A regex
alone cannot tell ``AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG`` (the
example from Amazon's own documentation, pasted into ten thousand
READMEs) from the one next to it that opens an account. A model alone
cannot read forty thousand files. So the first stage is a stdlib pattern
match — over *paths*, for files whose whole purpose is holding
credentials, and over *content*, for the ones hardcoded into ordinary
source — and only what it flags reaches the model.

Triage is generous, as it is in :mod:`vibe_sentinel.safety`, and for the
same reason: a false positive costs one model call, a false negative is
the whole failure.

**Nothing leaves the machine.** This module reads secrets, so the rule
that governs :mod:`vibe_sentinel.packages` is stricter here: the excerpt
goes to the configured model endpoint and nowhere else, and it refuses to
go even there unless that endpoint is loopback. Point ``[llm] endpoint``
at another host and the gate stops rather than posting your keys to it;
``allow_remote_model = true`` is the deliberate override. A candidate's
value is revealed to that local model only as a short prefix — enough to
tell a shape from a placeholder, not enough to use — and never at all to
stdout, to the log, or to the history database. What gets recorded is
the path, the rule and the verdict.

**The gitignore question is deliberately yours.** A ``.env`` that git
ignores is, for a lot of teams, the sanctioned way to hold a local
development key, and a gate that calls it an incident gets switched off.
For other teams it is exactly the problem. So it is its own setting —
``gitignored = "allow" | "warn" | "deny"`` — and the default reports it
without failing on it.

Our own recommendation is the third one, and the reason is specific to
what this tool watches. ``.gitignore`` keeps a file out of a commit. It
does nothing about the file. An agent with a shell reads ``.env`` with
``cat`` whether or not git can see it, and it does not have to be
malicious to do so: it is looking for the database URL, the file is
right there, and the contents land in a transcript that goes somewhere
you do not control. The only version of this that an agent cannot read
by accident is the one where the secret is not on disk — the OS keychain
(Keychain Access, libsecret / gnome-keyring, Windows Credential
Manager), fetched at process start into an environment variable that
dies with the process.

Which is why ``--home`` exists. ``~/.aws/credentials`` was never in your
repository, so no ``.gitignore`` was ever protecting it, and it is one
``cat`` away from any agent you run.
"""

from __future__ import annotations

import fnmatch
import math
import re
import subprocess
import tomllib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from vibe_sentinel.paths import CONFIG_FILENAME
from vibe_sentinel.pins import check_pins

if TYPE_CHECKING:  # pragma: no cover - import cost is the point
    from vibe_sentinel.config import SentinelConfig


#: The project's own config file. A ``[credentials]`` table here is the normal
#: place for the policy — one config file for the whole tool.
PROJECT_CONFIG = Path(CONFIG_FILENAME)

#: Standalone policy file, read when the project config has no ``[credentials]``
#: table. For an organisation shipping one set of rules across many repos.
POLICY_PATH = Path("security") / "credential-policy.toml"


# --------------------------------------------------------------------------------------
# What counts as a secret
# --------------------------------------------------------------------------------------


class Secret(BaseModel):
    """One thing worth looking at, and what to ask about it.

    ``pattern`` decides whether something is worth a model call at all.
    ``question`` is what the model is then asked, in your words — literally
    the prompt, the same way a probe placeholder's description is.

    Declared rather than shipped, for the reason every rule set here is:
    a built-in list cannot know that ``fixtures/expired-key.pem`` was
    revoked in 2023, or that anything matching ``svc-prod-*`` is real by
    definition and not something an 8B model should get a vote on.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    question: str
    pattern: str
    applies_to: str = "content"
    """``path`` matches the file's path — the file exists to hold credentials,
    so its name is the signal. ``content`` matches each line of a file that is
    not supposed to hold any."""
    verdict: str = ""
    """Settle it here instead of asking. Empty means ask the model.

    Same lesson as the safety gate's: a question informs a judgement, only
    this decides one. Use it where you already know — a path under
    ``secrets/`` that is real by construction, or a fixtures directory whose
    keys are all expired."""


#: The base layer. Every one can be overridden by id, switched off, or
#: replaced wholesale — see :func:`load_secrets`. A starting point, not a
#: policy.
#:
#: Files whose *purpose* is to hold credentials. Here the name is the whole
#: signal, so the pattern is over the path and the model is asked the only
#: question left: is this one populated, or is it the template.
BUILTIN_SECRETS: tuple[Secret, ...] = (
    Secret(
        id="dotenv-file",
        title="An environment file",
        applies_to="path",
        pattern=r"(^|/)\.env(\.[^/]*)?$|(^|/)env\.(sh|list)$",
        question=(
            "Does this file hold real credentials, or is it the template — the "
            "committed .env.example whose values are all placeholders? A single "
            "real value pasted into a template file is the finding; the rest of "
            "the placeholders around it are not."
        ),
    ),
    Secret(
        id="private-key-file",
        title="A private key file",
        applies_to="path",
        pattern=(
            r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$"
            r"|\.(pem|key|p8|p12|pfx|jks|keystore|ppk|asc|gpg)$"
        ),
        question=(
            "Is this a private key, and does it still open anything? A public "
            "certificate, a CA bundle, or a key generated for a test that was "
            "never deployed is not a leak — say which of those it is."
        ),
    ),
    Secret(
        id="cloud-credentials",
        title="A cloud provider's credential file",
        applies_to="path",
        pattern=(
            r"(^|/)\.aws/(credentials|config)$"
            r"|(^|/)\.azure/(credentials|accessTokens\.json)$"
            r"|(^|/)(application_default_)?credentials\.json$"
            r"|(^|/)[^/]*service[_-]?account[^/]*\.json$"
            r"|(^|/)\.s3cfg$"
        ),
        question=(
            "Does this hold usable cloud credentials — a long-lived access key "
            "or a service-account private key — or only a profile name, a "
            "region, and a reference to credentials held elsewhere?"
        ),
    ),
    Secret(
        id="registry-auth",
        title="A package or container registry login",
        applies_to="path",
        pattern=(
            r"(^|/)\.npmrc$|(^|/)\.pypirc$|(^|/)\.netrc$|(^|/)_netrc$"
            r"|(^|/)\.gem/credentials$|(^|/)\.cargo/credentials(\.toml)?$"
            r"|(^|/)\.docker/config\.json$|(^|/)\.composer/auth\.json$"
        ),
        question=(
            "Does this contain an auth token or password, or only registry URLs "
            "and settings? A token here publishes packages under your name."
        ),
    ),
    Secret(
        id="vcs-credentials",
        title="A stored version-control login",
        applies_to="path",
        pattern=r"(^|/)\.git-credentials$|(^|/)gh/hosts\.ya?ml$",
        question=(
            "Does this hold a personal access token? One here reaches every "
            "repository the account can see, not only this one."
        ),
    ),
    Secret(
        id="database-credentials",
        title="A stored database password",
        applies_to="path",
        pattern=(
            r"(^|/)\.pgpass$|(^|/)\.my\.cnf$|(^|/)\.mylogin\.cnf$"
            r"|(^|/)\.dbeaver-data-sources[^/]*\.json$"
        ),
        question=(
            "Does this hold a password, and does the host it names look like "
            "something with real data behind it?"
        ),
    ),
    Secret(
        id="cluster-config",
        title="A cluster or orchestration config",
        applies_to="path",
        pattern=r"(^|/)\.kube/config$|(^|/)kubeconfig(\.[^/]*)?$",
        question=(
            "Does this embed a client certificate, token, or password, or does "
            "it delegate to an external credential helper? A kubeconfig with an "
            "embedded token is a key to the cluster."
        ),
    ),
    Secret(
        id="infrastructure-state",
        title="Infrastructure variables or state",
        applies_to="path",
        pattern=(
            r"\.auto\.tfvars(\.json)?$|(^|/)[^/]*\.tfvars(\.json)?$"
            r"|(^|/)terraform\.tfstate(\.backup)?$|\.tfstate$"
        ),
        question=(
            "Does this contain credentials? Terraform state stores every "
            "attribute it manages in clear text, including the passwords and "
            "keys resources were created with, so a state file is usually the "
            "richest single secret in a repository."
        ),
    ),
    Secret(
        id="secret-store-file",
        title="A file that says it holds secrets",
        applies_to="path",
        pattern=(
            r"(^|/)(secrets?|credentials?|passwords?|vault)"
            r"\.(ya?ml|json|toml|ini|cfg|conf|properties|txt|enc)$"
        ),
        question=(
            "Does this hold live values, or is it a schema, a sealed/encrypted "
            "blob, or a list of key names with the values fetched elsewhere?"
        ),
    ),
    Secret(
        id="shell-history",
        title="A shell history file",
        applies_to="path",
        pattern=(
            r"(^|/)\.(bash|zsh|ksh|sh|node_repl|python|psql|mysql)_history$"
            r"|(^|/)\.local/share/fish/fish_history$"
        ),
        question=(
            "Does any line here carry a credential — an `export TOKEN=...`, a "
            "`psql` or `curl` invocation with a password in it? Agents export "
            "keys to run one command and the shell keeps the line forever."
        ),
    ),
    # ---- Credentials hardcoded into files that are not supposed to hold any ----
    Secret(
        id="private-key-block",
        title="A private key pasted into a file",
        pattern=(
            r"-----BEGIN\s+(?:RSA |DSA |EC |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"
        ),
        question=(
            "This is a private key block inside a file that is not a key file. "
            "Is it a real key? A fixture generated for a test suite is not a "
            "leak — but say so explicitly, because anything shaped like this "
            "will be treated as a key by whatever reads it next."
        ),
    ),
    Secret(
        id="cloud-access-key",
        title="A cloud access key id",
        pattern=r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_\-]{35}\b",
        question=(
            "Is this a real access key id, or the one from the provider's own "
            "documentation? AKIAIOSFODNN7EXAMPLE and AKIAIOSFODNN7EXAMPLE-style "
            "ids appear in every AWS tutorial ever written and are not "
            "credentials. A key id with a matching secret beside it is."
        ),
    ),
    Secret(
        id="provider-token",
        title="A token with a recognisable vendor prefix",
        pattern=(
            r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"
            r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
            r"|\bglpat-[A-Za-z0-9_\-]{16,}\b"
            r"|\bxox[abposr]-[A-Za-z0-9-]{10,}\b"
            r"|\bsk-ant-[A-Za-z0-9_\-]{20,}\b"
            r"|\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b"
            r"|\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"
            r"|\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b"
            r"|\bnpm_[A-Za-z0-9]{30,}\b"
            r"|\bdop_v1_[a-f0-9]{40,}\b"
            r"|\bhf_[A-Za-z0-9]{30,}\b"
            r"|\bAC[0-9a-f]{32}\b"
        ),
        question=(
            "These prefixes are issued by one vendor and mean the string was "
            "minted by them. Is this one live, or has it been revoked or "
            "redacted? Note that a `_test_` key is still a key, to a sandbox."
        ),
    ),
    Secret(
        id="assigned-secret",
        title="A name that means secret, assigned a literal",
        # The name is anchored with a lookbehind rather than `\b`, because
        # `_` is a word character and SCREAMING_SNAKE_CASE is how secrets
        # are actually named. `\b` put no boundary between the `_` and the
        # `P` of `DB_PASSWORD`, so this rule missed `DB_PASSWORD`,
        # `GITHUB_TOKEN`, `STRIPE_SECRET_KEY` and `AWS_SECRET_ACCESS_KEY`
        # — the last of which is the example this module's own docstring
        # opens with. Anything in a `.env` was still caught by the path
        # rule; the same line in settings.py or a compose file was not.
        # It still will not match inside a word: `mypassword` has an
        # alphanumeric before the name and fails the lookbehind, which is
        # the anti-substring discipline the rest of this file keeps.
        pattern=(
            r"(?i)(?<![A-Za-z0-9])(?:pass(?:wd|word|phrase)?|secret|token"
            r"|api[_\-]?key|access[_\-]?key|private[_\-]?key|client[_\-]?secret"
            r"|auth[_\-]?key|credential)s?(?:[_\-][A-Za-z0-9]+)*"
            r"\b\s*[:=]{1,2}\s*[\"']([^\"'\n]{6,})[\"']"
        ),
        question=(
            "Is the assigned value a real credential? Most are not: test "
            "fixtures, sample data, a documented default, the string 'changeme'. "
            "Judge the value, not the variable name — and if it reads as a "
            "credential for something that exists, say so."
        ),
    ),
    Secret(
        id="connection-string",
        title="A password inside a connection URL",
        pattern=r"\b[a-z][a-z0-9+.\-]{2,}://[^\s:@/\"']{1,64}:([^\s:@/\"']{3,})@",
        question=(
            "This URL carries a password in it. Does the host it points at hold "
            "anything real — is it localhost, a docker-compose service, a test "
            "container, or something reachable?"
        ),
    ),
    Secret(
        id="authorization-header",
        title="An authorization header with a value in it",
        pattern=(
            r"(?i)authorization[\"']?\s*[:=]\s*[\"']?\s*"
            r"(?:bearer|basic|token)\s+([A-Za-z0-9._\-+/=]{16,})"
        ),
        question=(
            "Is this a real token, or a documentation example / a value "
            "substituted at run time? A Basic header is base64 of "
            "user:password — decode it before deciding."
        ),
    ),
    Secret(
        id="json-web-token",
        title="A JSON Web Token",
        pattern=r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b",
        question=(
            "A JWT's payload is base64, not encryption — decode it. What does it "
            "grant, who issued it, and has it expired? An expired token from a "
            "test run is not a leak; a service token with no expiry is."
        ),
    ),
)


def load_secrets(root: Path | None = None) -> tuple[Secret, ...]:
    """The active rule set: built-ins, layered with the project's own.

    Same rules as probes and dangers, for the same reason:

      - a ``[[secret]]`` with a NEW id **adds** one,
      - one reusing a built-in id **overrides** that built-in,
      - ``[credentials] use = ["id", ...]`` keeps **only** those,
      - ``[credentials] disable = ["id", ...]`` **removes** one,
      - ``[credentials] use_builtins = false`` starts from nothing.

    Adding rather than replacing is the important half: declaring the one
    rule you care about must not silently drop the seventeen you had.

    Raises ``ValueError`` naming the file and the entry when a rule is
    malformed — never falls back to a quieter rule set, because a gate
    that silently checks for less than you asked is worse than one that
    stops.
    """
    merged: dict[str, Secret] = {s.id: s for s in BUILTIN_SECRETS}
    if root is None:
        return tuple(merged.values())

    path = root / CONFIG_FILENAME
    if not path.is_file():
        return tuple(merged.values())
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ValueError(f"{path} is not readable as TOML: {e}") from e

    raw_settings = data.get("credentials")
    settings: dict[str, Any] = raw_settings if isinstance(raw_settings, dict) else {}
    if not settings.get("use_builtins", True):
        merged = {}

    # Rule files first, the project config last, so a team can keep a shared
    # set under version control and still override one entry locally. Globs
    # are sorted, so which of two files wins is not the filesystem's choice.
    for pattern in settings.get("rule_files", []):
        matched = sorted(root.glob(str(pattern)))
        if not matched:
            raise ValueError(
                f"{path}: [credentials] rule_files entry {pattern!r} matches no "
                f"file under {root}. A rule file that is not there checks for "
                f"nothing, so it is an error rather than a no-op."
            )
        for rule_path in matched:
            for secret in _secrets_in_file(rule_path):
                merged[secret.id] = secret

    for index, raw in enumerate(data.get("secret", []), start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: [[secret]] #{index} is not a table.")
        secret = _secret_from_toml(raw, path, index)
        merged[secret.id] = secret

    keep = settings.get("use")
    if keep is not None:
        unknown = [s for s in keep if s not in merged]
        if unknown:
            known = ", ".join(sorted(merged)) or "(none)"
            raise ValueError(
                f"{path}: [credentials] use names rule(s) that do not exist: "
                f"{', '.join(unknown)}. Available: {known}."
            )
        merged = {k: v for k, v in merged.items() if k in keep}

    disable = settings.get("disable", [])
    unknown = [s for s in disable if s not in merged]
    if unknown:
        known = ", ".join(sorted(merged)) or "(none)"
        raise ValueError(
            f"{path}: [credentials] disable names rule(s) that do not exist: "
            f"{', '.join(unknown)}. Available: {known}. A stale entry here "
            f"silently checks for nothing, so it is an error, not a no-op."
        )
    for secret_id in disable:
        del merged[secret_id]

    return tuple(merged.values())


def _secrets_in_file(path: Path) -> list[Secret]:
    """Every ``[[secret]]`` in one rule file."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ValueError(f"{path} is not readable as TOML: {e}") from e
    tables = data.get("secret", [])
    if not tables:
        raise ValueError(
            f"{path} declares no [[secret]] tables. A rule file with no rules "
            f"in it is more likely a mistake than an intention."
        )
    return [_secret_from_toml(raw, path, i) for i, raw in enumerate(tables, 1)]


def _secret_from_toml(raw: dict[str, object], path: Path, index: int) -> Secret:
    """One ``[[secret]]`` table, validated with its remediation named."""
    where = f"{path}: [[secret]] #{index}"
    secret_id = str(raw.get("id", "")).strip()
    if not secret_id:
        raise ValueError(
            f"{where} has no id. Every rule needs one to be overridden or "
            f"disabled by name."
        )
    question = str(raw.get("question", "")).strip()
    if not question:
        raise ValueError(
            f"{where} ({secret_id}) has no question. The question is what the "
            f"model is actually asked — a pattern with nothing to ask about "
            f"flags a file and then says nothing useful about it."
        )
    verdict = str(raw.get("verdict", "")).strip()
    if verdict and verdict not in ("real", "placeholder", "unclear"):
        raise ValueError(
            f"{where} ({secret_id}) has verdict={verdict!r}. Use 'real', "
            f"'placeholder', 'unclear', or leave it out to ask the model."
        )
    applies_to = str(raw.get("applies_to", "content"))
    if applies_to not in ("path", "content"):
        raise ValueError(
            f"{where} ({secret_id}) has applies_to={applies_to!r}. Use 'path' "
            f"to match the file's name, or 'content' to match its lines."
        )
    pattern = str(raw.get("pattern", ""))
    if not pattern:
        raise ValueError(
            f"{where} ({secret_id}) has no pattern, so nothing would ever match it."
        )
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(
            f"{where} ({secret_id}) has a pattern that is not a valid regular "
            f"expression: {e}"
        ) from e
    return Secret(
        id=secret_id,
        title=str(raw.get("title", secret_id)),
        question=question,
        pattern=pattern,
        applies_to=applies_to,
        verdict=verdict,
    )


# --------------------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------------------
#
# A missing ``[credentials]`` table is not an error, unlike the licence policy.
# There is no allow-list to get wrong: every rule still runs and nothing is
# pinned, which is the safe reading of "no policy".


#: Directories whose contents are not this project's own text, and which would
#: otherwise dominate the walk. Every one is either regenerated, vendored, or
#: ours. Add to it with ``[credentials] exclude``.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".vibe-sentinel",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".nox",
        "node_modules",
        "bower_components",
        "vendor",
        "site-packages",
        ".venv",
        "venv",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".gradle",
        ".terraform",
    }
)

#: Credential stores that live in the home directory rather than any
#: repository — checked only under ``--home``. These are the ones the
#: ``.gitignore`` argument has never covered: they were never in a repository,
#: so nothing was ever keeping them out of one, and an agent with a shell
#: reads them as easily as it reads your source.
HOME_LOCATIONS: tuple[str, ...] = (
    ".aws/credentials",
    ".aws/config",
    ".azure/credentials",
    ".config/gcloud/application_default_credentials.json",
    ".config/gh/hosts.yml",
    ".docker/config.json",
    ".kube/config",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".my.cnf",
    ".gem/credentials",
    ".cargo/credentials.toml",
    ".git-credentials",
    ".ssh/id_rsa",
    ".ssh/id_dsa",
    ".ssh/id_ecdsa",
    ".ssh/id_ed25519",
    ".bash_history",
    ".zsh_history",
)

#: How a finding relates to git, which is the whole gitignore question.
#: ``tracked`` is committed or staged; ``untracked`` is one ``git add -A``
#: from being; ``ignored`` is the case the policy setting exists for;
#: ``outside`` is a home-directory file no repository ever covered.
EXPOSURES = ("tracked", "untracked", "ignored", "outside", "unknown")


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True)

    pins: tuple[dict[str, Any], ...] = ()
    #: What to do about a credential in a file git is ignoring. ``warn``
    #: reports it and does not fail the gate; ``deny`` treats it like any
    #: other; ``allow`` drops it before the model is ever asked.
    gitignored: str = "warn"
    exclude: tuple[str, ...] = ()
    max_file_kb: int = 512
    max_files: int = 20000
    #: Leading characters of a candidate value shown to the local model, so a
    #: vendor prefix (``sk-ant-``, ``AKIA``) can be told from a placeholder.
    #: Never more than half the value, never anything at all to stdout, the
    #: log, or the database. 0 sends only the shape.
    reveal_chars: int = 8
    concurrency: int = 4
    #: Refuse to send excerpts anywhere but loopback. The one setting here
    #: that is about where bytes go rather than what counts as a finding.
    allow_remote_model: bool = False
    source: str = "defaults"

    def pin_for(self, path: str) -> dict[str, Any] | None:
        for pin in self.pins:
            for pattern in pin.get("paths", ()):
                if fnmatch.fnmatch(path, str(pattern)):
                    return pin
        return None

    def accepts(self, path: str, rule: str) -> bool:
        """Whether a recorded pin covers this rule for this path.

        Scoped to the rules it lists, the same way a package pin is: accepting
        ``assigned-secret`` for a fixtures file does not accept
        ``private-key-block`` in it later. That is the whole difference
        between a pin and an ignore.
        """
        pin = self.pin_for(path)
        if pin is None:
            return False
        accepted = {str(a).lower() for a in pin.get("accept", ())}
        return rule.lower() in accepted or "*" in accepted

    def excluded(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.exclude)


def policy_from_data(data: dict[str, Any], where: str) -> Policy:
    gitignored = str(data.get("gitignored", "warn"))
    if gitignored not in ("allow", "warn", "deny"):
        raise ValueError(
            f"{where}: gitignored={gitignored!r}. Use 'allow' (a secret in an "
            f"ignored file is not a finding), 'warn' (report it, do not fail "
            f"on it — the default), or 'deny' (treat it like any other)."
        )
    reveal = int(data.get("reveal_chars", 8))
    if reveal < 0:
        raise ValueError(f"{where}: reveal_chars must not be negative")
    # Same rule as the licence gate's, and now the same code. A credential
    # pin selects on `paths` rather than `packages`; everything else about
    # what makes it a decision rather than an ignore is identical.
    check_pins(data.get("pin", ()) or (), subject="paths", where=where)
    return Policy(
        pins=tuple(data.get("pin", ())),
        gitignored=gitignored,
        exclude=tuple(str(p) for p in data.get("exclude", ())),
        max_file_kb=int(data.get("max_file_kb", 512)),
        max_files=int(data.get("max_files", 20000)),
        reveal_chars=reveal,
        concurrency=max(1, int(data.get("concurrency", 4))),
        allow_remote_model=bool(data.get("allow_remote_model", False)),
        source=where,
    )


def load_policy(path: Path | None = None, root: Path | None = None) -> Policy:
    """Resolve the credential policy for ``root``.

    ``path``, then a ``[credentials]`` table in the project's
    ``.vibe-sentinel.toml``, then ``security/credential-policy.toml``. Falling
    through all three yields the defaults rather than an error: with no
    allow-list to state, "no policy" means "check everything and pin nothing".
    """
    root = root or Path.cwd()

    if path is not None:
        if not path.exists():
            raise FileNotFoundError(
                f"No credential policy at {path}. Remove --policy to use the "
                f"[credentials] table in {root / PROJECT_CONFIG}, or the defaults."
            )
        return policy_from_data(tomllib.loads(path.read_text()), str(path))

    project = root / PROJECT_CONFIG
    if project.is_file():
        data = tomllib.loads(project.read_text())
        if "credentials" in data:
            return policy_from_data(data["credentials"], f"{project} [credentials]")

    standalone = root / POLICY_PATH
    if standalone.is_file():
        return policy_from_data(tomllib.loads(standalone.read_text()), str(standalone))

    return Policy()


# --------------------------------------------------------------------------------------
# Reducing a value to its shape
# --------------------------------------------------------------------------------------
#
# Everything below exists so that the bytes leaving this module are the
# smallest set that can still answer the question. Two different reductions,
# for two different destinations:
#
#   blind    -> stdout, the log, the history database. Reveals nothing.
#   partial  -> the local model, and only it. A short prefix plus the shape.
#
# A value's *shape* is often the answer on its own. `SECRET_KEY=your-key-here`
# is twenty low-entropy characters of English; `SECRET_KEY=` followed by
# forty-four characters at 5.1 bits each is not something anyone typed as an
# example. The model reads both correctly without ever seeing the second one.


def entropy(value: str) -> float:
    """Shannon entropy in bits per character.

    Not a detector — a random-looking string is not necessarily a secret and a
    weak password is not random. It is context, passed to the model alongside
    the length, because together they separate a generated credential from a
    placeholder more reliably than either does alone.
    """
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def shape(value: str) -> str:
    """``"44 chars, entropy 5.1"`` — what a value looks like, not what it is."""
    return f"{len(value)} chars, entropy {entropy(value):.1f}"


#: Values that cannot be a credential whatever the file around them says: a
#: number, a boolean, a loopback address. These are the context that makes the
#: rest of an excerpt readable — `port = 5432` is what tells a reader the file
#: is configuration — and the set is enumerable, which is the point.
#:
#: It used to be "short and low-entropy" instead, and that is not the same
#: test. `hunter22` is eight characters at 2.75 bits per character and it is
#: also somebody's password; so are `Passw0rd`, `s3cret!` and `admin`, and
#: `assigned-secret` matches from six characters up, so the whole class was in
#: scope by construction. It did not even buy what it was for: `localhost` is
#: nine characters and was redacted anyway. Length is not a statement about
#: what a string opens.
_NEVER_A_SECRET = re.compile(
    r"""^(?:
          \d+(?:\.\d+)*                            # 5432, 8.0, 127.0.0.1
        | true | false | yes | no | on | off       # flags
        | none | null | nil | undefined            # absence
        | localhost | ::1                          # this machine
    )$""",
    re.IGNORECASE | re.VERBOSE,
)


def is_context(value: str) -> bool:
    """Whether a value is structurally incapable of being a credential.

    An empty value counts: ``API_KEY=`` with nothing after it is the single
    most useful thing an excerpt can show, because it is what tells a template
    from a populated file.
    """
    return not value or _NEVER_A_SECRET.match(value) is not None


def blind(value: str) -> str:
    """The value, revealing nothing. Safe for stdout, logs and the database.

    This reduction is what the "never to stdout, the log, or the history
    database" guarantee in this module's docstring actually rests on, so the
    only values that pass through whole are the ones :func:`is_context`
    can prove are not credentials.
    """
    return value if is_context(value) else f"<redacted: {shape(value)}>"


def partial(value: str, reveal: int) -> str:
    """The value reduced for the local model: a prefix, then the shape.

    Never more than half of it, whatever ``reveal_chars`` says, so the answer
    to "what does this open" is not in the prompt even when the prefix is.
    Context values pass through whole — they reach loopback only, and a port
    number is what tells the model the rest of the file is configuration.
    """
    if is_context(value):
        return value
    keep = min(reveal, len(value) // 2)
    prefix = value[:keep]
    return f"{prefix}…<{shape(value)}>" if prefix else f"<{shape(value)}>"


#: An assignment in almost any config or source syntax: an optionally quoted
#: name, a `=` or `:`, then the value. Used to reduce a whole file to its
#: keys and its value shapes.
_ASSIGNMENT = re.compile(
    r"""(?P<lead>["']?[\w.\-\[\]]{1,64}["']?\s*[:=]{1,2}\s*)(?P<val>"[^"\n]*"|'[^'\n]*'|[^\s,;}\]]+)"""
)

#: A bare token long and dense enough to be a credential even with no `=`
#: in front of it — .pgpass fields, .netrc words, a key on its own line.
_BARE_TOKEN = re.compile(r"(?<![\w/.\-])[A-Za-z0-9+/=_\-]{16,}(?![\w])")


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Overlapping spans folded into their union, in order.

    Two rules can claim overlapping text — ``db_password = "postgres://a:b@c"``
    fires ``assigned-secret`` on the whole value and ``connection-string`` on
    the password inside it. Replacing nested spans one at a time shifts the
    offsets out from under each other; replacing their union does not, and
    reducing slightly more than one rule asked for is the harmless direction.
    """
    out: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _replace_spans(line: str, spans: list[tuple[int, int]], render: Any) -> str:
    """``line`` with each span replaced by ``render`` of what was there."""
    out = line
    for span_start, span_end in reversed(_merged(spans)):
        out = out[:span_start] + render(line[span_start:span_end]) + out[span_end:]
    return out


def _value_spans(line: str) -> list[tuple[int, int]]:
    """Where the values are on this line — never where the names are.

    The names have to survive. A populated ``.env`` and its committed template
    have the same keys and nothing else in common, so a reduction that eats
    ``DJANGO_SECRET_KEY`` along with what follows it has destroyed the half
    that answers the question.

    So the assignment pass runs first and claims its whole match, and the
    bare-token pass only picks up what it did not claim — the formats with no
    ``=`` in them at all, like ``.pgpass`` and ``.netrc``.
    """
    spans: list[tuple[int, int]] = []
    claimed: list[tuple[int, int]] = []
    for match in _ASSIGNMENT.finditer(line):
        claimed.append(match.span())
        start, end = match.span("val")
        raw = match.group("val")
        if len(raw) > 1 and raw[0] in ("'", '"') and raw.endswith(raw[0]):
            start, end = start + 1, end - 1
        if end > start:
            spans.append((start, end))
    for match in _BARE_TOKEN.finditer(line):
        start, end = match.span()
        if not any(
            start < claimed_end and claimed_start < end
            for claimed_start, claimed_end in claimed
        ):
            spans.append((start, end))
    return spans


def _reduce_line(line: str, render: Any) -> str:
    """One line with every value it carries reduced by ``render``.

    Structure survives — the key names, the operators, the comments — because
    that is what tells a template from a populated file. Only the values go.
    """
    return _replace_spans(line, _value_spans(line), render)


#: Characters of a matched line kept either side of the match. A minified
#: bundle is one line; the token in it is still worth reporting.
_CONTEXT_CHARS = 90


def _reduce_spans(line: str, spans: list[tuple[int, int]], render: Any) -> str:
    """A line with **every** given span reduced, trimmed around them.

    Every one, not only the span being reported. A line can carry two
    credentials — a key id and its secret, a user and a password — and
    reducing one of them while printing the other beside it is a leak with
    the word "redacted" next to it.
    """
    if not spans:
        return line.strip()
    ordered = _merged(spans)
    out = _replace_spans(line, spans, render)

    # Trim around the outermost span, adjusting for what the reductions did
    # to the offsets on the way past.
    first, last = ordered[0][0], ordered[-1][1]
    grown = len(out) - len(line)
    left = max(0, first - _CONTEXT_CHARS)
    right = min(len(out), last + grown + _CONTEXT_CHARS)
    head = "…" if left else ""
    tail = "…" if right < len(out) else ""
    return f"{head}{out[left:right]}{tail}".strip()


# --------------------------------------------------------------------------------------
# The first stage: finding candidates, mechanically
# --------------------------------------------------------------------------------------


class Candidate(BaseModel):
    """One (file, rule) pair the pattern match flagged.

    Grouped by rule rather than by match so that a file with nine tokens of
    the same kind is one question, not nine. The model's answer is about the
    pair, which is also the unit a pin accepts.
    """

    model_config = ConfigDict(frozen=True)

    rule: str
    title: str
    path: str
    """Repo-relative and POSIX-separated, or absolute for a home-directory
    file. This is the string a ``[[credentials.pin]]`` glob matches."""
    applies_to: str = "content"
    exposure: str = "unknown"
    lines: tuple[int, ...] = ()
    excerpt: str = ""
    """Reveals nothing. This is the only one of the two that may be printed,
    logged, or written to the history database."""
    local_excerpt: str = ""
    """A short prefix of each value plus its shape. Goes to the local model
    and nowhere else — see this module's docstring and
    :func:`endpoint_is_local`."""


class Scan(BaseModel):
    """Everything the mechanical stage looked at, and what it could not."""

    model_config = ConfigDict(frozen=True)

    candidates: tuple[Candidate, ...] = ()
    files_read: int = 0
    files_skipped: int = 0
    unreadable: tuple[str, ...] = ()
    """Paths that exist and were not read. Reported rather than swallowed: a
    file this could not open is not a file it found nothing in."""
    truncated: bool = False
    """True when ``max_files`` stopped the walk. The result is then a floor,
    not an inventory, and the report has to say so."""
    git_note: str = ""


#: A value that is not a value: an environment lookup, a template
#: interpolation, a placeholder in angle brackets, a run of one character.
#: Dropped before the model is asked, because they are not judgements — the
#: string is structurally incapable of being a credential.
_NOT_A_LITERAL = re.compile(
    r"""^\s*(?:
          \$\{?[\w.:\-]+\}?             # $VAR  ${VAR}
        | %\([\w.]+\)[sdr]              # %(name)s
        | \{\{[^}]*\}\}                 # {{ template }}
        | \{[\w.]*\}                    # {placeholder}
        | <[^>]{0,64}>                  # <your-key-here>
        | os\.environ.*                 # os.environ["..."]
        | process\.env.*                # process.env.FOO
        | \*+ | x+ | X+ | \.+ | -+ | _+ # ****  xxxx  ....
    )\s*$""",
    re.VERBOSE,
)


def _is_a_literal(value: str) -> bool:
    """Whether a captured value is a literal at all.

    Deliberately narrow. It drops only strings that cannot be a credential
    whatever they say — never ones that merely look harmless. Deciding that
    ``changeme`` is a placeholder is the model's job, and doing it here with
    a word list is how a real password named ``test_password`` gets missed.
    """
    return not _NOT_A_LITERAL.match(value)


def _is_binary(head: bytes) -> bool:
    return b"\x00" in head


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


def _line_of(starts: list[int], offset: int) -> int:
    import bisect

    return bisect.bisect_right(starts, offset)


def _secret_span(match: re.Match[str]) -> tuple[int, int]:
    """The part of a match that is the value: group 1 if there is one."""
    if match.re.groups and match.group(1) is not None:
        return match.span(1)
    return match.span()


#: Lines of a credential-holding file shown to the model. Enough to see the
#: key names and whether values are populated; not the whole file.
_HEAD_LINES = 60

#: Matches of one rule in one file carried into the prompt. Past this the
#: answer does not change and the prompt only gets longer.
_MAX_MATCHES = 12


def _content_candidates(
    text: str, path: str, secrets: Iterable[Secret], policy: Policy
) -> list[Candidate]:
    """Every content rule that fires in one file, one Candidate per rule.

    Two passes. The first finds every match of every rule and remembers where
    on its line it sat; the second renders the lines. They are separate
    because a line's reduction has to account for *all* the matches on it,
    not only the rule currently being reported — see :func:`_reduce_spans`.
    """
    starts = _line_starts(text)
    lines = text.splitlines()
    #: rule id -> line number -> spans of that rule's values on that line
    hits: dict[str, dict[int, list[tuple[int, int]]]] = {}
    #: line number -> spans of every rule's values on it
    all_spans: dict[int, list[tuple[int, int]]] = {}

    for secret in secrets:
        rx = re.compile(secret.pattern)
        for match in rx.finditer(text):
            span = _secret_span(match)
            value = text[span[0] : span[1]]
            if secret.id == "assigned-secret" and not _is_a_literal(value):
                continue
            number = _line_of(starts, match.start())
            offset = starts[number - 1]
            local = (span[0] - offset, span[1] - offset)
            per_line = hits.setdefault(secret.id, {})
            if len(per_line) >= _MAX_MATCHES and number not in per_line:
                continue
            per_line.setdefault(number, []).append(local)
            all_spans.setdefault(number, []).append(local)

    found: list[Candidate] = []
    for secret in secrets:
        per_line = hits.get(secret.id, {})
        if not per_line:
            continue
        numbers = sorted(per_line)
        safe: list[str] = []
        shown: list[str] = []
        for number in numbers:
            line = lines[number - 1] if 0 < number <= len(lines) else ""
            spans = all_spans.get(number, [])
            safe.append(f"{number}: {_reduce_spans(line, spans, blind)}")
            shown.append(
                f"{number}: "
                f"{_reduce_spans(line, spans, lambda v: partial(v, policy.reveal_chars))}"
            )
        found.append(
            Candidate(
                rule=secret.id,
                title=secret.title,
                path=path,
                applies_to="content",
                lines=tuple(numbers),
                excerpt="\n".join(safe),
                local_excerpt="\n".join(shown),
            )
        )
    return found


def _path_candidate(
    secret: Secret, path: str, text: str | None, policy: Policy
) -> Candidate:
    """One credential-holding file, reduced to its keys and its value shapes.

    The keys survive and the values do not, because the keys are what answer
    the question. A populated ``.env`` and its committed template have the
    same key names and nothing else in common: one has forty-four dense
    characters after the ``=``, the other has ``your-secret-key-here``.
    """
    if text is None:
        note = "(binary or unreadable — judged on its name and location alone)"
        return Candidate(
            rule=secret.id,
            title=secret.title,
            path=path,
            applies_to="path",
            excerpt=note,
            local_excerpt=note,
        )
    head = text.splitlines()[:_HEAD_LINES]
    more = text.count("\n") + 1 - len(head)
    tail = [f"… {more} more line(s)"] if more > 0 else []
    return Candidate(
        rule=secret.id,
        title=secret.title,
        path=path,
        applies_to="path",
        lines=tuple(range(1, len(head) + 1)),
        excerpt="\n".join([_reduce_line(line, blind) for line in head] + tail),
        local_excerpt="\n".join(
            [
                _reduce_line(line, lambda v: partial(v, policy.reveal_chars))
                for line in head
            ]
            + tail
        ),
    )


def _iter_files(
    root: Path, policy: Policy, problems: list[str]
) -> Iterator[tuple[Path, str]]:
    """Every candidate file under ``root``, as ``(absolute, relative)``.

    Directory symlinks are not followed: a link back up the tree turns this
    into an infinite walk, and a link out of it turns a project scan into a
    filesystem scan.

    A directory that cannot be listed is recorded in ``problems`` and the walk
    continues. Ending the scan on one root-owned subdirectory would report
    fewer findings than there are, which is the failure this whole check
    exists to avoid.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError as e:
            problems.append(f"{current.relative_to(root).as_posix() or '.'}/: {e}")
            continue
        for entry in entries:
            relative = entry.relative_to(root).as_posix()
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in SKIP_DIRS and not policy.excluded(relative):
                    stack.append(entry)
                continue
            if entry.is_file() and not policy.excluded(relative):
                yield entry, relative


def collect(
    root: Path,
    secrets: tuple[Secret, ...],
    policy: Policy,
    *,
    home: bool = False,
) -> Scan:
    """Run the mechanical stage over ``root``. No model, no network.

    This is the whole check under ``--no-model``, and it is also what the
    probe records: candidates, unadjudicated, with the values already gone.
    """
    path_rules = [s for s in secrets if s.applies_to == "path"]
    content_rules = [s for s in secrets if s.applies_to == "content"]
    path_matchers = [(s, re.compile(s.pattern)) for s in path_rules]
    limit = policy.max_file_kb * 1024

    candidates: list[Candidate] = []
    unreadable: list[str] = []
    read = skipped = 0
    truncated = False

    for absolute, relative in _iter_files(root, policy, unreadable):
        if read + skipped >= policy.max_files:
            truncated = True
            break
        matched_path = [s for s, rx in path_matchers if rx.search(relative)]
        try:
            size = absolute.stat().st_size
        except OSError as e:
            unreadable.append(f"{relative}: {e}")
            continue
        if size > limit:
            skipped += 1
            if matched_path:
                unreadable.append(
                    f"{relative}: {size // 1024} KB exceeds max_file_kb "
                    f"({policy.max_file_kb}) — flagged on its name, not read"
                )
                candidates += [
                    _path_candidate(s, relative, None, policy) for s in matched_path
                ]
            continue
        try:
            raw = absolute.read_bytes()
        except OSError as e:
            unreadable.append(f"{relative}: {e}")
            continue
        read += 1
        if _is_binary(raw[:8192]):
            candidates += [
                _path_candidate(s, relative, None, policy) for s in matched_path
            ]
            continue
        text = raw.decode("utf-8", errors="replace")
        candidates += [_path_candidate(s, relative, text, policy) for s in matched_path]
        candidates += _content_candidates(text, relative, content_rules, policy)

    if home:
        candidates += _collect_home(path_matchers, policy)

    mapped, note = exposure_map(root, [c.path for c in candidates])
    resolved = tuple(
        c.model_copy(update={"exposure": mapped.get(c.path, c.exposure)})
        for c in candidates
    )
    return Scan(
        candidates=resolved,
        files_read=read,
        files_skipped=skipped,
        unreadable=tuple(unreadable),
        truncated=truncated,
        git_note=note,
    )


def _collect_home(
    path_matchers: list[tuple[Secret, re.Pattern[str]]], policy: Policy
) -> list[Candidate]:
    """The home-directory credential stores that exist, reduced the same way.

    Path rules only. Nothing here walks the home directory or reads anything
    it was not asked for: the list is fixed, absolute, and short.
    """
    home = Path.home()
    found: list[Candidate] = []
    for name in HOME_LOCATIONS:
        target = home / name
        if not target.is_file():
            continue
        shown = f"~/{name}"
        matched = [s for s, rx in path_matchers if rx.search(shown)]
        if not matched:
            continue
        try:
            raw = target.read_bytes()[: policy.max_file_kb * 1024]
            text = None if _is_binary(raw[:8192]) else raw.decode("utf-8", "replace")
        except OSError:
            text = None
        found += [
            _path_candidate(s, shown, text, policy).model_copy(
                update={"exposure": "outside"}
            )
            for s in matched
        ]
    return found


# --------------------------------------------------------------------------------------
# Where a finding sits relative to git
# --------------------------------------------------------------------------------------


def _git(root: Path, args: list[str], stdin: bytes | None = None) -> tuple[int, bytes]:
    """One git invocation. Fixed argv, no shell, failures are answers."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, shell=False
            ["git", "-C", str(root), *args],
            input=stdin,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 127, b""
    return done.returncode, done.stdout


def exposure_map(root: Path, paths: list[str]) -> tuple[dict[str, str], str]:
    """How each path relates to git: tracked, ignored, untracked, or outside.

    This is the input to the one setting in this module that is a matter of
    taste rather than fact. Getting it wrong in the quiet direction would be
    the worst failure here, so a repository git cannot answer for reports
    ``unknown`` and says why — never ``ignored``, which is the reading that
    would let a finding be dropped.
    """
    inside = [p for p in paths if not p.startswith("~/")]
    outside = {p: "outside" for p in paths if p.startswith("~/")}
    if not inside:
        return outside, ""

    code, _ = _git(root, ["rev-parse", "--is-inside-work-tree"])
    if code == 127:
        return {**outside, **dict.fromkeys(inside, "unknown")}, (
            "git is not on PATH, so no finding could be placed as tracked, "
            "ignored or untracked"
        )
    if code != 0:
        return {**outside, **dict.fromkeys(inside, "unknown")}, (
            f"{root} is not a git work tree, so nothing here is ignored by "
            f"git — every file is simply on disk"
        )

    code, out = _git(root, ["ls-files", "-z", "--"])
    tracked = set(out.decode("utf-8", "replace").split("\0")) if code == 0 else set()
    tracked.discard("")

    rest = sorted({p for p in inside if p not in tracked})
    ignored: set[str] = set()
    if rest:
        payload = ("\0".join(rest) + "\0").encode("utf-8")
        code, out = _git(root, ["check-ignore", "-z", "--stdin"], stdin=payload)
        # 0 = some were ignored, 1 = none were, anything else = it did not run.
        if code in (0, 1):
            ignored = {p for p in out.decode("utf-8", "replace").split("\0") if p}

    resolved = dict(outside)
    for path in inside:
        if path in tracked:
            resolved[path] = "tracked"
        elif path in ignored:
            resolved[path] = "ignored"
        else:
            resolved[path] = "untracked"
    return resolved, ""


# --------------------------------------------------------------------------------------
# The second stage: asking the local model, and only a local one
# --------------------------------------------------------------------------------------


def endpoint_is_local(endpoint: str) -> tuple[bool, str]:
    """Whether the configured model endpoint is on this machine.

    Every other check here can run against whatever ``[llm] endpoint`` names,
    because what it sends is a list of package names or a count of modules.
    This one sends the inside of your ``.env``. So it asks first, and the
    answer is loopback or nothing — ``allow_remote_model = true`` is how you
    say you meant it, and it is deliberately not a command-line flag: sending
    credentials somewhere is not a decision to make while typing.
    """
    import ipaddress
    from urllib.parse import urlparse

    host = (urlparse(endpoint).hostname or "").strip("[]")
    if not host:
        return False, f"{endpoint!r} names no host"
    if host == "localhost" or host.endswith(".localhost"):
        return True, ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False, (
            f"{host} is a hostname this cannot resolve to a loopback address"
        )
    if address.is_loopback or address.is_unspecified:
        return True, ""
    if address.is_private:
        return False, (
            f"{host} is on your network but is not this machine — another host, or a VM"
        )
    return False, f"{host} is a public address"


CREDENTIAL_SYSTEM_PROMPT = """\
You judge whether a string found in a codebase is a working credential.

You are not reviewing code, style, or whether secrets are being handled
well. One question: would this open something.

**Values are shown to you reduced, and the reduction is the evidence.**
A value you can read in full is a value short or ordinary enough that
reading it costs nothing. Anything else appears as a short prefix and its
shape:

    API_KEY = "sk-ant-a…<48 chars, entropy 4.9>"
    API_KEY = "your-api-key-here"

The first is 48 characters at 4.9 bits each behind a vendor's prefix.
Nobody types that as an example. The second is English. That difference
is usually the whole answer, and where it is not, say so rather than
guessing.

Judge the value, never the variable name. `test_password = "hunter2"` in
a fixture is not a credential. `EXAMPLE_KEY` holding forty random
characters is.

Verdicts:
- real:        it would open something. A key that exists, a password for
               a reachable host, a token a vendor issued and nobody
               revoked.
- placeholder: it would not. A template value, an example from the
               provider's own documentation, a fixture generated for a
               test, a key you can see is expired or revoked, an empty or
               obviously fake string.
- unclear:     you cannot tell from what you were given. A real answer,
               not a hedge — it is what sends this to a person.

Things that are NOT reasons to answer placeholder:
- The file is small, or looks like config, or is in a tests directory.
- The value is labelled `test`, `dev`, `staging` or `sandbox`. Those are
  real systems with real keys.
- Git is ignoring the file. That is somebody else's question and it has
  already been answered; it says nothing about whether the value works.

Name what you saw. "44 base64 characters after DJANGO_SECRET_KEY, no
placeholder wording" is worth more to whoever reads this than "looks like
a secret".
"""


def build_context(path: str, exposure: str, candidates: list[Candidate]) -> str:
    """The shared half of the prompt: one file, reduced.

    This is the **system** message and it is byte-identical for every
    question asked about the same file, so a server that caches prefixes —
    vLLM does — prefills the file once however many rules fired in it. The
    divergent tail is one rule's question, from :func:`build_question`.
    """
    where = {
        "tracked": "tracked by git — it is committed, or staged to be",
        "untracked": "not tracked by git, and not ignored either — one "
        "`git add -A` from being committed",
        "ignored": "ignored by git, so it will not be committed",
        "outside": "outside the repository entirely, in the user's home directory",
        "unknown": "of unknown status to git",
    }.get(exposure, exposure)

    lines = [
        CREDENTIAL_SYSTEM_PROMPT,
        "",
        f"File:   {path}",
        f"Status: {where}.",
        "",
    ]
    for candidate in candidates:
        if candidate.applies_to == "path":
            lines.append(
                "This file's purpose is to hold credentials. Its keys are "
                "shown with their values reduced:"
            )
        else:
            lines.append(f"Lines matching ({candidate.rule}):")
        lines.append("")
        lines += [f"    {line}" for line in candidate.local_excerpt.splitlines()]
        lines.append("")
    return "\n".join(lines)


def build_question(secret: Secret) -> str:
    """The divergent half: one rule's question, in the owner's words.

    One question per request rather than a numbered list in one, for the
    reason the safety gate learned the hard way: a small model asked to weigh
    several things at once weighs the obvious one and skims the rest.
    """
    from vibe_sentinel.schemas import BREVITY

    return (
        f"Answer this one question about the file above, and nothing else.\n\n"
        f"({secret.id}) {secret.title}\n\n{secret.question}\n\n{BREVITY}"
    )


class Judgement(BaseModel):
    """One candidate and what was decided about it."""

    model_config = ConfigDict(frozen=True)

    candidate: Candidate
    verdict: str
    """``real``, ``placeholder``, ``unclear``, ``pinned``, or ``unreviewed``.

    The last two are not the model's: ``pinned`` means the policy already
    accepted this pair, and ``unreviewed`` means nobody looked. Neither may
    ever be rendered as though a review happened."""
    reason: str = ""
    reviewed: bool = False
    """Whether the model actually answered about this candidate."""


class Findings(BaseModel):
    """The whole check: what was found, what was decided, and by whom."""

    model_config = ConfigDict(frozen=True)

    scan: Scan
    judgements: tuple[Judgement, ...] = ()
    model: str = ""
    reviewed: bool = False
    """True only when the model rated at least one candidate. False means
    every verdict below is mechanical and no rendering may claim otherwise."""
    note: str = ""
    """Why the model was not asked, when it was not."""

    def failing(self, policy: Policy) -> tuple[Judgement, ...]:
        """The judgements that fail the gate.

        ``real`` and ``unclear`` both fail. Unlike the safety gate, this one
        does not stand between an agent and its work — it runs when you ask
        it to — so an unresolved candidate credential costs a look, not a
        stalled session. ``unreviewed`` never fails: the model not answering
        is not a finding about your code.
        """
        return tuple(
            j
            for j in self.judgements
            if j.verdict in ("real", "unclear")
            and not (j.candidate.exposure == "ignored" and policy.gitignored != "deny")
        )

    def gitignored(self) -> tuple[Judgement, ...]:
        """Findings in files git is ignoring — the separate, optional rule."""
        return tuple(
            j
            for j in self.judgements
            if j.candidate.exposure == "ignored" and j.verdict in ("real", "unclear")
        )


async def review(
    candidates: tuple[Candidate, ...],
    secrets: tuple[Secret, ...],
    config: SentinelConfig,
    policy: Policy,
) -> list[Judgement]:
    """Ask the local model about each candidate. Never raises.

    A candidate the model did not answer about comes back ``unreviewed``,
    which is not ``placeholder`` and must never be recorded as one. The rule
    is the same one the safety gate keeps: a review that did not happen is
    not a clean result.

    Awaited, never ``asyncio.run``. This is reached from the scan, which is
    already inside a loop, and a function that opens its own would raise
    there — it did, and the gate that failed was this one.
    """
    import asyncio

    from loguru import logger

    from vibe_sentinel.exceptions import LLMConnectionError
    from vibe_sentinel.json_schema import clip_to_bounds
    from vibe_sentinel.llm import llm_query
    from vibe_sentinel.schemas import _SECRET_SCHEMA, SecretOpinion

    by_id = {s.id: s for s in secrets}
    # Deterministic, and short: this is a yes/no with a sentence of reason.
    tuned = config.model_copy(update={"temperature": 0.0, "max_tokens": 512})

    grouped: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.path, []).append(candidate)
    contexts = {
        path: build_context(path, group[0].exposure, group)
        for path, group in grouped.items()
    }

    async def ask_all() -> list[Any]:
        limit = asyncio.Semaphore(max(1, policy.concurrency))

        async def one(candidate: Candidate) -> tuple[Candidate, dict[str, Any] | None]:
            secret = by_id.get(candidate.rule)
            if secret is None:
                return candidate, None
            async with limit:
                return candidate, await llm_query(
                    contexts[candidate.path],
                    build_question(secret),
                    _SECRET_SCHEMA,
                    f"credential-{candidate.rule}",
                    config=tuned,
                )

        return await asyncio.gather(
            *(one(c) for c in candidates), return_exceptions=True
        )

    try:
        results = await ask_all()
    except LLMConnectionError as e:
        logger.error("credentials: model unreachable ({}) — nothing reviewed", e)
        return [
            Judgement(candidate=c, verdict="unreviewed", reason=str(e))
            for c in candidates
        ]

    judgements: list[Judgement] = []
    for item in results:
        if isinstance(item, BaseException) or not isinstance(item, tuple):
            logger.warning("credentials: a review failed ({})", item)
            continue
        candidate, raw = item
        if raw is None:
            judgements.append(
                Judgement(
                    candidate=candidate,
                    verdict="unreviewed",
                    reason="the model did not answer about this one",
                )
            )
            continue
        try:
            opinion = SecretOpinion.model_validate(clip_to_bounds(SecretOpinion, raw))
        except Exception as e:  # noqa: BLE001 - an unusable answer is not a verdict
            logger.debug("credentials: unusable answer for {} ({})", candidate.rule, e)
            judgements.append(
                Judgement(
                    candidate=candidate,
                    verdict="unreviewed",
                    reason=f"the model's answer could not be read: {e}",
                )
            )
            continue
        judgements.append(
            Judgement(
                candidate=candidate,
                verdict=opinion.verdict,
                reason=opinion.reason,
                reviewed=True,
            )
        )

    answered = {(j.candidate.path, j.candidate.rule) for j in judgements}
    judgements += [
        Judgement(
            candidate=c,
            verdict="unreviewed",
            reason="the review did not come back for this one",
        )
        for c in candidates
        if (c.path, c.rule) not in answered
    ]
    return judgements


async def adjudicate(
    scan: Scan,
    secrets: tuple[Secret, ...],
    policy: Policy,
    config: SentinelConfig | None = None,
    *,
    use_model: bool = True,
) -> Findings:
    """Settle every candidate: by pin, by declared verdict, or by the model.

    The order matters. A pin is a decision someone already recorded, and a
    declared ``verdict`` is a fact about this project that an 8B model does
    not get a vote on. Only what neither settles is worth a model call.
    """
    by_id = {s.id: s for s in secrets}
    settled: list[Judgement] = []
    to_ask: list[Candidate] = []

    for candidate in scan.candidates:
        if policy.accepts(candidate.path, candidate.rule):
            pin = policy.pin_for(candidate.path) or {}
            settled.append(
                Judgement(
                    candidate=candidate,
                    verdict="pinned",
                    reason=str(pin.get("reason", "")).strip(),
                )
            )
            continue
        if candidate.exposure == "ignored" and policy.gitignored == "allow":
            continue
        declared = by_id.get(candidate.rule)
        if declared is not None and declared.verdict:
            settled.append(
                Judgement(
                    candidate=candidate,
                    verdict=declared.verdict,
                    reason=(
                        f"declared {declared.verdict} by this project's rule "
                        f"set ({declared.id}), so it was not put to the model"
                    ),
                )
            )
            continue
        to_ask.append(candidate)

    if not to_ask:
        return Findings(scan=scan, judgements=tuple(settled), reviewed=False)

    if not use_model or config is None:
        return Findings(
            scan=scan,
            judgements=tuple(
                settled
                + [
                    Judgement(
                        candidate=c,
                        verdict="unclear",
                        reason="not reviewed — no model was asked",
                    )
                    for c in to_ask
                ]
            ),
            reviewed=False,
            note="--no-model: these are pattern matches, adjudicated by nobody",
        )

    local, why = endpoint_is_local(config.llm_endpoint)
    if not local and not policy.allow_remote_model:
        return Findings(
            scan=scan,
            judgements=tuple(
                settled
                + [
                    Judgement(
                        candidate=c,
                        verdict="unclear",
                        reason="not reviewed — the model endpoint is not local",
                    )
                    for c in to_ask
                ]
            ),
            reviewed=False,
            note=(
                f"Not sending anything: {why}. This check would post the "
                f"inside of these files to {config.llm_endpoint}. Point "
                f"[llm] endpoint at a model on this machine, or set "
                f"[credentials] allow_remote_model = true if you own that "
                f"endpoint and meant it."
            ),
        )

    judged = await review(tuple(to_ask), secrets, config, policy)
    return Findings(
        scan=scan,
        judgements=tuple(settled + judged),
        model=config.llm_model,
        reviewed=any(j.reviewed for j in judged),
    )


#: The recommendation, in one place so the gate and the docs both say the
#: same thing. It is a claim about agents rather than about git, and
#: that is the point of it.
KEYCHAIN_ADVICE = """\
Keep keys and secrets in the OS keychain, not on disk — Keychain Access on
macOS, libsecret/gnome-keyring on Linux, Credential Manager on Windows — and
fetch them into the environment at process start.

  .gitignore keeps a file out of a commit. It does nothing about the file.
  A coding agent with a shell reads .env with `cat` whether or not git can
  see it, and it does not have to be misbehaving to do so: it wants the
  database URL, the file is right there, and the contents land in a
  transcript you do not control. A secret that is not on disk is the only
  one an agent cannot read by accident."""
