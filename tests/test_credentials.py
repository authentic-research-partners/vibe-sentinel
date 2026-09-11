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
from vibe_sentinel.config import SentinelConfig, load_config
from vibe_sentinel.exceptions import LLMConnectionError


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".vibe-sentinel.toml").write_text("", encoding="utf-8")
    return tmp_path


def scan(root: Path, **policy_kwargs: object) -> creds.Scan:
    policy = creds.Policy(**policy_kwargs)  # type: ignore[arg-type]
    return creds.search(root, creds.load_secrets(root), policy)


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
        for c in creds.search(project, creds.load_secrets(project), policy).candidates
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
        for c in creds.search(project, creds.load_secrets(project), policy).candidates
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


def judge(root: Path, policy: creds.Policy) -> creds.Adjudication:
    secrets = creds.load_secrets(root)
    return asyncio.run(
        creds.adjudicate(
            creds.search(root, secrets, policy),
            secrets,
            policy,
            SentinelConfig(),
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


def test_a_candidate_nobody_judged_fails_the_run(project: Path) -> None:
    """There is no verdict meaning "nobody looked". Deciding which pattern
    hits are real is half of this gate, so a run that could not ask is a
    failed run rather than a shorter answer — the alternative is a listing
    that reads like a clean tree because the backend was down."""
    (project / "app.py").write_text('password = "abcdef0123"\n', encoding="utf-8")

    async def silent(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("vibe_sentinel.llm.llm_query", silent)
    try:
        with pytest.raises(LLMConnectionError, match="did not come back"):
            judge(project, creds.Policy())
    finally:
        monkeypatch.undo()


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
    with pytest.raises(LLMConnectionError, match="allow_remote_model") as exc:
        asyncio.run(
            creds.adjudicate(
                creds.search(project, secrets, policy),
                secrets,
                policy,
                SentinelConfig(llm_endpoint="https://api.example.com/v1"),
            )
        )
    assert "Not sending anything" in str(exc.value)


def test_an_environment_override_cannot_send_a_secret_off_the_machine(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``VIBE_SENTINEL_LLM_ENDPOINT`` names which backend this machine can
    reach. It is not a way around the one refusal that is not negotiable.

    ``allow_remote_model`` is deliberately absent from ``ENV_OVERRIDES``
    for the same reason it is not a command-line flag, and the gate reads
    the resolved endpoint — so an override is refused exactly as a file
    saying the same thing is.
    """
    monkeypatch.setenv("VIBE_SENTINEL_LLM_ENDPOINT", "https://api.example.com/v1")
    monkeypatch.setenv("VIBE_SENTINEL_ALLOW_REMOTE_MODEL", "true")
    (project / ".vibe-sentinel.toml").write_text("", encoding="utf-8")
    (project / "app.py").write_text(
        'KEY = "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n', encoding="utf-8"
    )

    config = load_config(project / ".vibe-sentinel.toml")
    assert config.llm_endpoint == "https://api.example.com/v1"

    policy = creds.Policy()
    secrets = creds.load_secrets(project)
    with pytest.raises(LLMConnectionError, match="allow_remote_model") as exc:
        asyncio.run(
            creds.adjudicate(
                creds.search(project, secrets, policy), secrets, policy, config
            )
        )
    assert "Not sending anything" in str(exc.value)


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
    result = creds.search(project, creds.load_secrets(project), policy)
    assert result.candidates == ()


def test_a_binary_credential_file_is_flagged_on_its_name(project: Path) -> None:
    (project / "keystore.jks").write_bytes(b"\x00\x01\x02binary\x00")
    candidate = next(
        c for c in scan(project).candidates if c.rule == "private-key-file"
    )
    assert "binary" in candidate.excerpt


def test_an_oversized_file_is_flagged_without_being_read(project: Path) -> None:
    (project / ".env").write_text("K=" + "a" * 5000 + "\n", encoding="utf-8")
    result = creds.search(
        project, creds.load_secrets(project), creds.Policy(max_file_kb=1)
    )
    assert any(c.rule == "dotenv-file" for c in result.candidates)
    assert any("exceeds max_file_kb" in u for u in result.unreadable)


def test_a_truncated_walk_says_so(project: Path) -> None:
    for index in range(10):
        (project / f"f{index}.py").write_text("x = 1\n", encoding="utf-8")
    result = creds.search(
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


# --- the shape rules: the value is the signal, not the name ----------------


@pytest.mark.parametrize(
    "line",
    [
        'unrelated = "hunter22"',
        'DB = "p4ssw0rd"',
        'value: "s3cret!"',
        'x = "admin123"',
        'thing = "Passw0rd"',
    ],
)
def test_a_typed_password_is_found_though_nothing_names_it(
    project: Path, line: str
) -> None:
    """The gap every other rule leaves. `unrelated = "hunter22"` has a name
    that means nothing, no vendor prefix, no URL and no PEM block, and is
    far too short and too low in entropy to look generated. What is left is
    the value's shape: code joins words with a separator and a person typing
    a password glues them.

    That first line is deliberately the one
    `test_the_name_still_does_not_match_inside_a_word` asserts is *not* an
    `assigned-secret`. Both are right — it is not a name that means secret,
    and it is a string somebody could authenticate with.
    """
    (project / "settings.py").write_text(line + "\n", encoding="utf-8")
    assert "password-shape" in rules_fired(project)


@pytest.mark.parametrize(
    "line",
    [
        'build = "setuptools.build_meta"',  # a dotted name
        'probe = "commentary-ratio"',  # kebab-case, no digits
        'model = "a_very_ordinary_identifier_name"',  # snake_case, no digits
        'pattern = "^(all|declared)$"',  # a regex, not a value
        'tag = "v${VERSION}"',  # interpolated at run time
        'host = "db.internal.example.com"',  # separators: a hostname
        'note = "bert-base-uncased"',  # words, no glue
    ],
)
def test_what_the_shape_rule_declines_to_ask_about(project: Path, line: str) -> None:
    """The rule is deliberately generous, which only works because the
    obvious non-values are gone before anything is asked. A regex and a
    template are structurally incapable of being a credential; a dotted or
    separated name is code."""
    (project / "settings.py").write_text(line + "\n", encoding="utf-8")
    assert "password-shape" not in rules_fired(project)


def test_a_generated_key_is_found_though_nothing_names_it(project: Path) -> None:
    """Long, dense, mixed case, assigned to a name that says nothing."""
    line = 'value = "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY2zA4bC6d"'
    (project / "settings.py").write_text(line + "\n", encoding="utf-8")
    assert "minted-token" in rules_fired(project)


@pytest.mark.parametrize(
    "value",
    [
        "a_very_long_but_entirely_ordinary_identifier_name",  # 49 chars of words
        "0123456789abcdef0123456789abcdef",  # a hex digest
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # one repeated char
    ],
)
def test_length_alone_does_not_make_a_value_look_minted(
    project: Path, value: str
) -> None:
    """Length and mixed classes without the entropy floor select long
    identifiers; the floor without the classes selects prose. Both are
    needed, and neither is a detector on its own."""
    (project / "settings.py").write_text(f'value = "{value}"\n', encoding="utf-8")
    assert "minted-token" not in rules_fired(project)


def test_entropy_is_what_separates_prose_from_a_key() -> None:
    """The measurement behind `_MINTED_ENTROPY`. English runs about 4.1 bits
    per character, which is why a 4.0 floor nominated the CITATION.cff
    abstract; a generated key runs near 5."""
    assert creds.entropy("Documentation growing faster than the code") < 4.3
    assert creds.entropy("aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY") >= 4.3


@pytest.mark.parametrize(
    "line",
    [
        'DB_PWD = "s3cr3t-real"',
        'PAT = "abcdefghijkl"',
        'BEARER = "abcdefghijkl"',
        'DSN = "abcdefghijkl"',
        'salt = "abcdefghijkl"',
        'SIGNING_KEY = "abcdefghijkl"',
        'SESSION_KEY = "abcdefghijkl"',
        'ENCRYPTION_KEY = "abcdefghijkl"',
        'MASTER_KEY = "abcdefghijkl"',
    ],
)
def test_the_widened_name_list_is_matched(project: Path, line: str) -> None:
    (project / "settings.py").write_text(line + "\n", encoding="utf-8")
    assert "assigned-secret" in rules_fired(project)


@pytest.mark.parametrize(
    "line",
    [
        'path = "/usr/local/bin/env"',  # `pat` must not match inside `path`
        'pattern = "^[a-z]+$"',  # nor inside `pattern`
        'salted_hash = "abcdefghijkl"',  # nor `salt` inside `salted`
    ],
)
def test_the_widened_names_still_do_not_match_inside_a_word(
    project: Path, line: str
) -> None:
    """`pat` and `salt` are short, so widening the list is exactly where the
    substring discipline is most likely to be lost."""
    (project / "settings.py").write_text(line + "\n", encoding="utf-8")
    assert "assigned-secret" not in rules_fired(project)


def test_a_shape_rule_still_redacts_what_it_found(project: Path) -> None:
    """The rules are new; the guarantee is not. A password-shaped value
    reaches the excerpt as its shape and never as itself."""
    (project / "settings.py").write_text('x = "hunter22"\n', encoding="utf-8")
    found = [c for c in scan(project).candidates if c.rule == "password-shape"]
    assert found and "hunter22" not in found[0].excerpt
    assert "8 chars" in found[0].excerpt


# --- the shapes three content rules were blind to ---------------------------


def _matches(rule_id: str, line: str) -> bool:
    """Whether one shipped rule's pattern fires on one line."""
    import re

    rule = next(s for s in creds.builtin_secrets() if s.id == rule_id)
    return bool(re.search(rule.pattern, line))


@pytest.mark.parametrize(
    "line",
    [
        'password = "hunter22"',  # python
        "password: 'hunter22'",  # yaml, bare key
        'password: "hunter22"',  # yaml, quoted value
        '"password": "hunter22"',  # json, quoted key
        '{"api_key": "abcdef123456"}',  # json, inline
        '  "client_secret" : "s3cr3tvalue"',  # json, spaced
    ],
)
def test_a_quoted_key_is_still_an_assignment(line: str) -> None:
    """JSON and YAML quote the name, which put a `"` before the colon.

    `authorization-header` had always allowed it and the other three had
    not, so every credential in a JSON config was invisible to them — the
    single most common config format there is.
    """
    assert _matches("assigned-secret", line)


@pytest.mark.parametrize(
    "value",
    [
        "$(cat /run/secrets/api_token)",
        "`cat /run/secrets/api_token`",
        "sha512-V8AVnmPIICiWpGfm6GLzCR/W5FXLchHop40W4nXBmdlEceh16rCN8O8LNW==",
        "sha256-abc123XYZ+/=",
    ],
)
def test_a_value_that_cannot_be_a_credential_is_dropped(value: str) -> None:
    """Read at run time, or a published digest of a public artifact.

    `${VAR}` was already here and `$(...)` was not, so the shape this tool
    *recommends* — fetch the secret at process start — was raised as a
    candidate. An integrity hash is the other half: a lockfile holds
    thousands, every one of them published, and none authenticates
    anything.
    """
    assert not creds._is_a_literal(value)


@pytest.mark.parametrize(
    "value",
    [
        "9Fh2Kd8Lm4Qp7Xs1Bv6Nz3Rt5Wy0CjEa7Gu",
        "ghp_7Kd2Lm9Qp4Xs1Bv6Nz3Rt5Wy0CjEa8Gu2Hn",
        "hunter22",
    ],
)
def test_a_value_that_could_open_something_is_kept(value: str) -> None:
    """The other side of the same filter, so it cannot quietly widen."""
    assert creds._is_a_literal(value)


def test_a_dead_backend_fails_the_credentials_gate(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one caller that always re-raised, now covered like the others.

    It carried the loop and the argument for it in a comment, and no test,
    so the behaviour survived on attention rather than on a gate. When the
    loop became `llm.raise_if_unreachable` and the other two fan-outs
    started calling it too, nothing would have noticed the call being
    dropped from this one.
    """
    (project / "app.py").write_text('password = "abcdef0123"\n', encoding="utf-8")

    async def dead(*_a: object, **_kw: object) -> object:
        raise LLMConnectionError("Model endpoint unreachable after 3 attempts")

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", dead)
    with pytest.raises(LLMConnectionError) as caught:
        judge(project, creds.Policy())

    # Asserting the *shape*, because both paths raise and only one is right.
    # Without the guard the gate still fails, but as "put N candidate(s) to
    # the model and N did not come back: <the same connection error, once
    # per candidate>" — the aggregate the guard exists to avoid, with the
    # remediation buried N deep. Matching "unreachable" alone passes either
    # way, which is a test that cannot fail.
    message = str(caught.value)
    assert "did not come back" not in message, (
        f"the outage was reported as a shortfall of answers: {message}"
    )
    assert "unreachable" in message
