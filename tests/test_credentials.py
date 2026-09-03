"""The credential gate: what it flags, what it redacts, and what it refuses.

Three failure modes matter here and they pull against each other. Missing
a live key is the obvious one. Flagging every `password = ""` in a test
suite is the one that gets the gate switched off. And the third is
particular to this check: it reads secrets, so a bug that *prints* one is
worse than a bug that misses one. The redaction tests below are not
cosmetic — they are the ones that must not regress.
"""

from __future__ import annotations

import asyncio

from pathlib import Path

import pytest

from vibe_sentinel import credentials as creds
from vibe_sentinel.config import SentinelConfig


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".vibe-sentinel.toml").write_text("", encoding="utf-8")
    return tmp_path


def scan(root: Path, **policy_kwargs: object) -> creds.Scan:
    policy = creds.Policy(**policy_kwargs)  # type: ignore[arg-type]
    return creds.collect(root, creds.load_secrets(root), policy)


def rules_fired(root: Path) -> set[str]:
    return {c.rule for c in scan(root).candidates}


# --- the pattern stage: what is worth a model call -------------------------


@pytest.mark.parametrize(
    ("name", "rule"),
    [
        (".env", "dotenv-file"),
        (".env.local", "dotenv-file"),
        (".env.example", "dotenv-file"),
        ("id_ed25519", "private-key-file"),
        ("server.pem", "private-key-file"),
        ("certs/client.p12", "private-key-file"),
        (".aws/credentials", "cloud-credentials"),
        ("gcp-service-account.json", "cloud-credentials"),
        (".npmrc", "registry-auth"),
        (".netrc", "registry-auth"),
        (".git-credentials", "vcs-credentials"),
        (".pgpass", "database-credentials"),
        ("kubeconfig", "cluster-config"),
        ("terraform.tfstate", "infrastructure-state"),
        ("prod.auto.tfvars", "infrastructure-state"),
        ("config/secrets.yaml", "secret-store-file"),
        (".bash_history", "shell-history"),
    ],
)
def test_files_that_exist_to_hold_credentials(
    project: Path, name: str, rule: str
) -> None:
    target = project / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("TOKEN=abcdef0123456789abcdef\n", encoding="utf-8")
    assert rule in rules_fired(project)


@pytest.mark.parametrize(
    ("body", "rule"),
    [
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n", "private-key-block"),
        ("aws_key = 'AKIAIOSFODNN7EXAMPLE'\n", "cloud-access-key"),
        ("token = 'ghp_aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5'\n", "provider-token"),
        ("KEY = 'sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaaaaa'\n", "provider-token"),
        ('password = "s3cr3t-but-real"\n', "assigned-secret"),
        ("url = 'postgres://app:hunter2@db.internal:5432/x'\n", "connection-string"),
        (
            "headers = {'Authorization': 'Bearer abcdefghij0123456789'}\n",
            "authorization-header",
        ),
        (
            "t = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP'\n",
            "json-web-token",
        ),
    ],
)
def test_credentials_hardcoded_into_ordinary_source(
    project: Path, body: str, rule: str
) -> None:
    (project / "app.py").write_text(body, encoding="utf-8")
    assert rule in rules_fired(project)


@pytest.mark.parametrize(
    "body",
    [
        'password = os.environ["DB_PASSWORD"]\n',
        'password = "${DB_PASSWORD}"\n',
        'password = "{{ vault_db_password }}"\n',
        'api_key = "<your-api-key-here>"\n',
        'secret = "****"\n',
        "token = None\n",
        "password = get_password()\n",
        "# the api_key is read from the environment\n",
    ],
)
def test_things_that_cannot_be_credentials_are_not_asked_about(
    project: Path, body: str
) -> None:
    """Structurally not a literal — dropped before any model call.

    Deciding that ``changeme`` is a placeholder is the model's job. Deciding
    that ``os.environ["X"]`` is not a string at all is not.
    """
    (project / "app.py").write_text(body, encoding="utf-8")
    assert "assigned-secret" not in rules_fired(project)


def test_ordinary_source_is_not_flagged(project: Path) -> None:
    (project / "app.py").write_text(
        "def load(path):\n"
        "    with open(path) as handle:\n"
        "        return json.load(handle)\n"
        "\n"
        "MAX_RETRIES = 3\n"
        "TIMEOUT_SECONDS = 30.0\n",
        encoding="utf-8",
    )
    assert rules_fired(project) == set()


def test_one_candidate_per_rule_not_per_match(project: Path) -> None:
    (project / "app.py").write_text(
        'a = "AKIAIOSFODNN7EXAMPLE"\nb = "AKIAJONESJONESJONES1"\n', encoding="utf-8"
    )
    hits = [c for c in scan(project).candidates if c.rule == "cloud-access-key"]
    assert len(hits) == 1
    assert hits[0].lines == (1, 2)


# --- redaction: the tests that must not regress ----------------------------


def test_the_printable_excerpt_never_contains_the_value(project: Path) -> None:
    (project / "app.py").write_text(
        'STRIPE = "sk_live_51H8xQ2LkdIwHu7ixaBcDeFgH"\n', encoding="utf-8"
    )
    candidate = next(c for c in scan(project).candidates if c.rule == "provider-token")
    assert "sk_live_51H8xQ2LkdIwHu7ixaBcDeFgH" not in candidate.excerpt
    assert "STRIPE" in candidate.excerpt
    assert "redacted" in candidate.excerpt


def test_a_second_secret_on_the_same_line_is_redacted_too(project: Path) -> None:
    """The leak that reducing only the reported span would leave.

    A key id and its secret land on one line often enough that reporting one
    while printing the other beside it is a real failure, not a corner case.
    """
    (project / "app.py").write_text(
        'creds = ("AKIAIOSFODNN7EXAMPLE", "ghp_aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z")\n',
        encoding="utf-8",
    )
    for candidate in scan(project).candidates:
        assert "AKIAIOSFODNN7EXAMPLE" not in candidate.excerpt
        assert "ghp_aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z" not in candidate.excerpt


def test_a_credential_file_keeps_its_keys_and_loses_its_values(
    project: Path,
) -> None:
    (project / ".env").write_text(
        "DEBUG=true\n"
        "DB_HOST=localhost\n"
        "DJANGO_SECRET_KEY=8f2b1c9e4a7d6035bb914ee2c7a1d0f35e6c9b8a4d2f1e07\n",
        encoding="utf-8",
    )
    candidate = next(c for c in scan(project).candidates if c.rule == "dotenv-file")
    assert "DJANGO_SECRET_KEY" in candidate.excerpt
    assert "8f2b1c9e4a7d6035bb914ee2c7a1d0f35e6c9b8a4d2f1e07" not in candidate.excerpt
    # Short, ordinary values survive: they are the context that tells a
    # populated file from its template.
    # Key names and short, ordinary values survive: they are what tells a
    # populated file from its template.
    assert "DEBUG=true" in candidate.excerpt
    assert "DB_HOST=" in candidate.excerpt


def test_the_local_model_sees_a_prefix_and_never_more_than_half(
    project: Path,
) -> None:
    value = "sk-ant-api03-" + "z" * 40
    (project / "app.py").write_text(f'KEY = "{value}"\n', encoding="utf-8")
    policy = creds.Policy(reveal_chars=8)
    candidate = next(
        c
        for c in creds.collect(project, creds.load_secrets(project), policy).candidates
        if c.rule == "provider-token"
    )
    assert "sk-ant-a" in candidate.local_excerpt
    assert value not in candidate.local_excerpt


def test_reveal_chars_zero_sends_only_the_shape(project: Path) -> None:
    (project / "app.py").write_text(
        'KEY = "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n', encoding="utf-8"
    )
    policy = creds.Policy(reveal_chars=0)
    candidate = next(
        c
        for c in creds.collect(project, creds.load_secrets(project), policy).candidates
        if c.rule == "provider-token"
    )
    assert "sk-ant" not in candidate.local_excerpt
    assert "entropy" in candidate.local_excerpt


def test_entropy_separates_a_placeholder_from_a_generated_key() -> None:
    assert creds.entropy("your-secret-key-here") < creds.entropy(
        "8f2b1c9e4a7d6035bb914ee2c7a1d0f3"
    )


# --- where a finding sits relative to git ----------------------------------


def git_repo(root: Path) -> None:
    import subprocess

    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_exposure_tells_tracked_from_ignored_from_untracked(project: Path) -> None:
    import subprocess

    git_repo(project)
    (project / ".gitignore").write_text(".env\n", encoding="utf-8")
    (project / ".env").write_text("SECRET_KEY=abcdef0123456789abcdef\n", "utf-8")
    (project / "committed.py").write_text('password = "abcdef0123"\n', "utf-8")
    (project / "new.py").write_text('password = "0123456789abc"\n', "utf-8")
    subprocess.run(
        ["git", "-C", str(project), "add", "committed.py", ".gitignore"],
        check=True,
        capture_output=True,
    )

    by_path = {c.path: c.exposure for c in scan(project).candidates}
    assert by_path[".env"] == "ignored"
    assert by_path["committed.py"] == "tracked"
    assert by_path["new.py"] == "untracked"


def test_without_git_nothing_is_called_ignored(project: Path) -> None:
    """The quiet direction is the dangerous one.

    Guessing ``ignored`` for a repository git cannot answer for would let the
    gitignored setting drop a real finding on a guess.
    """
    (project / ".env").write_text("SECRET_KEY=abcdef0123456789abcdef\n", "utf-8")
    result = scan(project)
    assert {c.exposure for c in result.candidates} == {"unknown"}
    assert result.git_note


# --- the gitignore rule, which is the optional one -------------------------


def judge(root: Path, policy: creds.Policy) -> creds.Findings:
    secrets = creds.load_secrets(root)
    return asyncio.run(
        creds.adjudicate(
            creds.collect(root, secrets, policy), secrets, policy, use_model=False
        )
    )


def ignored_env(project: Path) -> None:
    git_repo(project)
    (project / ".gitignore").write_text(".env\n", encoding="utf-8")
    (project / ".env").write_text("SECRET_KEY=abcdef0123456789abcdef\n", "utf-8")


def test_gitignored_allow_drops_it_before_anything_is_asked(project: Path) -> None:
    ignored_env(project)
    findings = judge(project, creds.Policy(gitignored="allow"))
    assert findings.judgements == ()


def test_gitignored_warn_reports_it_without_failing(project: Path) -> None:
    ignored_env(project)
    policy = creds.Policy(gitignored="warn")
    findings = judge(project, policy)
    assert findings.gitignored()
    assert findings.failing(policy) == ()


def test_gitignored_deny_fails_like_any_other(project: Path) -> None:
    ignored_env(project)
    policy = creds.Policy(gitignored="deny")
    findings = judge(project, policy)
    assert len(findings.failing(policy)) == 1


# --- pins, declared verdicts, and never claiming a review ------------------


def test_a_pin_is_scoped_to_the_rules_it_names(project: Path) -> None:
    (project / "fixture.pem").write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n", encoding="utf-8"
    )
    policy = creds.Policy(
        pins=({"paths": ["fixture.pem"], "accept": ["private-key-file"]},)
    )
    verdicts = {j.candidate.rule: j.verdict for j in judge(project, policy).judgements}
    assert verdicts["private-key-file"] == "pinned"
    # The block rule fired in the same file and was NOT accepted by that pin.
    assert verdicts["private-key-block"] != "pinned"


def test_a_declared_verdict_settles_it_without_a_model(project: Path) -> None:
    (project / ".vibe-sentinel.toml").write_text(
        "[[secret]]\n"
        'id = "our-service-keys"\n'
        'title = "Our own key format"\n'
        "pattern = 'acme_live_[A-Za-z0-9]{16,}'\n"
        'verdict = "real"\n'
        'question = "Is this ours?"\n',
        encoding="utf-8",
    )
    (project / "app.py").write_text(
        'KEY = "acme_live_aB3dE5fG7hJ9kL1m"\n', encoding="utf-8"
    )
    findings = judge(project, creds.Policy())
    declared = next(
        j for j in findings.judgements if j.candidate.rule == "our-service-keys"
    )
    assert declared.verdict == "real"
    assert declared.reviewed is False
    assert not findings.reviewed


def test_no_model_never_reads_as_a_review(project: Path) -> None:
    (project / "app.py").write_text('password = "abcdef0123"\n', encoding="utf-8")
    findings = judge(project, creds.Policy())
    assert findings.reviewed is False
    assert all(not j.reviewed for j in findings.judgements)
    assert "adjudicated by nobody" in findings.note


# --- the network boundary --------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:5001/v1",
        "http://127.0.0.1:5001/v1",
        "http://[::1]:5001/v1",
        "http://0.0.0.0:8000/v1",
        "http://vllm.localhost/v1",
    ],
)
def test_a_local_endpoint_is_allowed(endpoint: str) -> None:
    local, _ = creds.endpoint_is_local(endpoint)
    assert local


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.openai.com/v1",
        "http://192.168.1.40:5001/v1",
        "http://gpu-box.lan:5001/v1",
        "http://10.0.0.5/v1",
    ],
)
def test_a_remote_endpoint_is_refused(endpoint: str) -> None:
    local, why = creds.endpoint_is_local(endpoint)
    assert not local
    assert why


def test_nothing_is_sent_to_a_remote_endpoint(project: Path) -> None:
    """The gate stops rather than posting the inside of a .env anywhere."""
    (project / "app.py").write_text(
        'KEY = "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n', encoding="utf-8"
    )
    policy = creds.Policy()
    secrets = creds.load_secrets(project)
    findings = asyncio.run(
        creds.adjudicate(
            creds.collect(project, secrets, policy),
            secrets,
            policy,
            SentinelConfig(llm_endpoint="https://api.example.com/v1"),
        )
    )
    assert findings.reviewed is False
    assert "allow_remote_model" in findings.note
    assert all(j.verdict == "unclear" for j in findings.judgements)


# --- the rule set layers like every other one ------------------------------


def test_a_new_secret_adds_without_dropping_the_builtins(project: Path) -> None:
    (project / ".vibe-sentinel.toml").write_text(
        "[[secret]]\n"
        'id = "internal-hostname"\n'
        'title = "An internal hostname"\n'
        "pattern = 'db-prod-[0-9]+'\n"
        'question = "Is this production?"\n',
        encoding="utf-8",
    )
    active = {s.id for s in creds.load_secrets(project)}
    assert "internal-hostname" in active
    assert "dotenv-file" in active


def test_reusing_a_builtin_id_overrides_it(project: Path) -> None:
    (project / ".vibe-sentinel.toml").write_text(
        "[[secret]]\n"
        'id = "dotenv-file"\n'
        'title = "Ours"\n'
        'applies_to = "path"\n'
        "pattern = 'settings.local'\n"
        'question = "?"\n',
        encoding="utf-8",
    )
    rule = next(s for s in creds.load_secrets(project) if s.id == "dotenv-file")
    assert rule.pattern == "settings.local"


def test_disable_removes_one_and_use_keeps_only_those(project: Path) -> None:
    (project / ".vibe-sentinel.toml").write_text(
        '[credentials]\ndisable = ["shell-history"]\n', encoding="utf-8"
    )
    assert "shell-history" not in {s.id for s in creds.load_secrets(project)}

    (project / ".vibe-sentinel.toml").write_text(
        '[credentials]\nuse = ["dotenv-file"]\n', encoding="utf-8"
    )
    assert {s.id for s in creds.load_secrets(project)} == {"dotenv-file"}


def test_use_builtins_false_starts_from_nothing(project: Path) -> None:
    (project / ".vibe-sentinel.toml").write_text(
        "[credentials]\nuse_builtins = false\n", encoding="utf-8"
    )
    assert creds.load_secrets(project) == ()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("[[secret]]\ntitle = 'x'\nquestion = 'q'\npattern = 'p'\n", "no id"),
        ("[[secret]]\nid = 'a'\npattern = 'p'\n", "no question"),
        ("[[secret]]\nid = 'a'\nquestion = 'q'\n", "no pattern"),
        ("[[secret]]\nid = 'a'\nquestion = 'q'\npattern = '('\n", "not a valid"),
        (
            "[[secret]]\nid = 'a'\nquestion = 'q'\npattern = 'p'\nverdict = 'maybe'\n",
            "verdict=",
        ),
        (
            "[[secret]]\nid = 'a'\nquestion = 'q'\npattern = 'p'\napplies_to = 'x'\n",
            "applies_to=",
        ),
        ('[credentials]\ndisable = ["nope"]\n', "do not exist"),
        ('[credentials]\nuse = ["nope"]\n', "do not exist"),
    ],
)
def test_a_malformed_rule_set_stops_rather_than_checking_less(
    project: Path, body: str, expected: str
) -> None:
    (project / ".vibe-sentinel.toml").write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=expected):
        creds.load_secrets(project)


def test_an_invalid_gitignored_setting_names_the_alternatives(project: Path) -> None:
    (project / ".vibe-sentinel.toml").write_text(
        '[credentials]\ngitignored = "maybe"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="allow"):
        creds.load_policy(root=project)


# --- the walk --------------------------------------------------------------


def test_excluded_paths_and_skip_dirs_are_not_read(project: Path) -> None:
    (project / "node_modules").mkdir()
    (project / "node_modules" / ".env").write_text("K=abcdef0123456789ab\n", "utf-8")
    (project / "docs").mkdir()
    (project / "docs" / ".env").write_text("K=abcdef0123456789ab\n", "utf-8")
    policy = creds.Policy(exclude=("docs/*",))
    result = creds.collect(project, creds.load_secrets(project), policy)
    assert result.candidates == ()


def test_a_binary_credential_file_is_flagged_on_its_name(project: Path) -> None:
    (project / "keystore.jks").write_bytes(b"\x00\x01\x02binary\x00")
    candidate = next(
        c for c in scan(project).candidates if c.rule == "private-key-file"
    )
    assert "binary" in candidate.excerpt


def test_an_oversized_file_is_flagged_without_being_read(project: Path) -> None:
    (project / ".env").write_text("K=" + "a" * 5000 + "\n", encoding="utf-8")
    result = creds.collect(
        project, creds.load_secrets(project), creds.Policy(max_file_kb=1)
    )
    assert any(c.rule == "dotenv-file" for c in result.candidates)
    assert any("exceeds max_file_kb" in u for u in result.unreadable)


def test_a_truncated_walk_says_so(project: Path) -> None:
    for index in range(10):
        (project / f"f{index}.py").write_text("x = 1\n", encoding="utf-8")
    result = creds.collect(
        project, creds.load_secrets(project), creds.Policy(max_files=3)
    )
    assert result.truncated


def test_overlapping_matches_do_not_corrupt_the_line(project: Path) -> None:
    """Two rules claiming nested text must not shift each other's offsets.

    ``db_password = "postgres://a:secret@h"`` fires assigned-secret on the
    whole value and connection-string on the password inside it.
    """
    (project / "app.py").write_text(
        'db_password = "postgres://app:hunter2pass@db.internal/x"\n', encoding="utf-8"
    )
    for candidate in scan(project).candidates:
        assert "hunter2pass" not in candidate.excerpt
        assert "db_password" in candidate.excerpt
        assert candidate.excerpt.count("db_password") == 1


def test_a_directory_that_cannot_be_listed_does_not_end_the_walk(
    project: Path,
) -> None:
    """One unlistable subdirectory must not make the rest report clean."""
    blocked = project / "blocked"
    blocked.mkdir()
    (blocked / "x.py").write_text("x = 1\n", encoding="utf-8")
    (project / ".env").write_text("SECRET_KEY=abcdef0123456789abcdef\n", "utf-8")
    blocked.chmod(0o000)
    try:
        result = scan(project)
    finally:
        blocked.chmod(0o755)
    assert any(c.rule == "dotenv-file" for c in result.candidates)
    assert any("blocked" in problem for problem in result.unreadable)


def test_the_prompt_carries_the_instructions_and_is_shared_per_file(
    project: Path,
) -> None:
    """Same regression as the safety gate's, plus the caching claim.

    The system message must define the verdicts, and must be byte-identical
    across the rules that fired in one file — that identity is the whole
    reason the fan-out is affordable.
    """
    (project / ".env").write_text(
        "STRIPE=sk_live_51H8xQ2LkdIwHu7ixaBcDeFgHiJkLmNoP\n", encoding="utf-8"
    )
    candidates = list(scan(project).candidates)
    assert len(candidates) > 1

    context = creds.build_context(".env", "untracked", candidates)
    assert context.startswith(creds.CREDENTIAL_SYSTEM_PROMPT)
    for word in ("real:", "placeholder:", "unclear:"):
        assert word in context
    # One context for the file, not one per rule.
    assert context == creds.build_context(".env", "untracked", candidates)
    # And the value itself is not in it, instructions or no instructions.
    assert "sk_live_51H8xQ2LkdIwHu7ixaBcDeFgHiJkLmNoP" not in context


# --- what a reduction may reveal -------------------------------------------
#
# `blind` is the reduction the "never to stdout, the log, or the history
# database" guarantee rests on, so what it lets through is the whole of that
# promise. It used to let through anything short and low-entropy, which is
# not a statement about what a string opens: `hunter22` is eight characters
# at 2.75 bits and it is also somebody's password.


@pytest.mark.parametrize(
    "password", ["hunter2", "hunter22", "Passw0rd", "s3cret!", "admin", "postgres"]
)
def test_a_short_password_is_still_redacted(password: str) -> None:
    assert creds.blind(password) == f"<redacted: {creds.shape(password)}>"
    assert password not in creds.partial(password, reveal=8)


@pytest.mark.parametrize(
    "value", ["5432", "8.0", "127.0.0.1", "0.0.0.0", "true", "False", "none", "::1", ""]
)
def test_a_value_that_cannot_be_a_credential_is_shown(value: str) -> None:
    """`port = 5432` is what tells a reader the rest of the excerpt is
    configuration, so the set is enumerated rather than guessed at."""
    assert creds.is_context(value) is True
    assert creds.blind(value) == value


def test_localhost_is_context_however_long_it_is() -> None:
    """The old length test claimed this case and did not even cover it:
    `localhost` is nine characters and was redacted anyway."""
    assert creds.blind("localhost") == "localhost"


def test_a_prefix_is_never_more_than_half_the_value() -> None:
    """So the answer to "what does this open" is not in the prompt even
    when the prefix is."""
    value = "sk-ant-" + "a1B2c3D4" * 6
    shown = creds.partial(value, reveal=8)
    assert shown.startswith("sk-ant-a")
    assert value[len(value) // 2 :] not in shown


def test_a_short_password_in_a_file_never_reaches_the_excerpt(project: Path) -> None:
    """The end-to-end version: this is the string that used to be printed
    beside the word `redacted`."""
    (project / "settings.py").write_text('DB_PASSWORD = "hunter22"\n', encoding="utf-8")
    candidates = scan(project).candidates
    assert candidates, "the rule did not fire, so this proves nothing"
    for candidate in candidates:
        assert "hunter22" not in candidate.excerpt
        assert "DB_PASSWORD" in candidate.excerpt, "the key name has to survive"


@pytest.mark.parametrize(
    "line",
    [
        'DB_PASSWORD = "hunter22"',
        'MYSQL_PASSWORD="hunter22"',
        'GITHUB_TOKEN = "ghp_realish_looking_value"',
        'STRIPE_SECRET_KEY = "sk_live_abcdefghijklmnop"',
        'AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENG"',
    ],
)
def test_a_screaming_snake_case_name_is_matched(project: Path, line: str) -> None:
    """`_` is a word character, so the `\\b` this rule used to start with
    put no boundary between the `_` and the `P` of `DB_PASSWORD`. The whole
    convention secrets are actually named in was missed — including
    AWS_SECRET_ACCESS_KEY, the example this module's docstring opens with.
    A `.env` was still caught by the path rule; the same line in
    settings.py or a compose file was not.
    """
    (project / "settings.py").write_text(line + "\n", encoding="utf-8")
    assert "assigned-secret" in rules_fired(project)


@pytest.mark.parametrize(
    "line",
    [
        'mypassword = "hunter22"',
        'tokenizer_config = "bert-base-uncased"',
        'unrelated = "hunter22"',
    ],
)
def test_the_name_still_does_not_match_inside_a_word(project: Path, line: str) -> None:
    """Widening the front of the rule must not turn it into the substring
    match this whole file exists to avoid."""
    (project / "settings.py").write_text(line + "\n", encoding="utf-8")
    assert "assigned-secret" not in rules_fired(project)
