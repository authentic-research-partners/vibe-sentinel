"""Files to plant, and what the gate should make of each.

Two stages, two labels. ``detected`` is about the pattern match — a
mechanical stage that has to be generous, because a candidate it never
raises is one the model never gets to judge. ``verdict`` is about the
model, and the question there is narrower than it looks: not "is this
string suspicious" but "would this string open something".

The corpus is built around the pairs that only the second stage can
separate. Amazon's own published example access key carries the real
``AKIA`` prefix and the real twenty-character shape, so every detector on
earth matches it and there is no account behind it. ``qwen3-8b-fp8`` is
letters with digits glued on and no separator, which is exactly what a
typed password looks like — and it is the name of a model. A rule set
that stopped matching either would stop matching the real ones too,
which is why the answer lives in a judgement and a pin rather than in a
better regex.

Every value here is synthetic or a vendor's documented example. None was
ever issued and none opens anything.
"""

from __future__ import annotations

from evals.cases import CredentialCase

#: A key block with the right envelope and nothing inside it. The rule
#: matches the ``BEGIN`` line, which is the point: what makes a private
#: key file a finding is that it is one, not what it decrypts.
_KEY_BLOCK = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAABlwAAAAdz\n"
    "c2gtcnNhAAAAAwEAAQAAAYEAxNOTSYNTHETICEXAMPLEONLYNOTAREALKEYAAAAA\n"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)


CASES: tuple[CredentialCase, ...] = (
    # ---------------------------------------------------------------
    # Live-shaped values: the gate exists for these.
    # ---------------------------------------------------------------
    CredentialCase(
        id="dotenv-with-live-values",
        why=(
            "The file every agent with a shell reads with `cat`, holding a "
            "generated-looking token and a URL with a password in it. Both "
            "the name and the shape say credential."
        ),
        path=".env",
        content=(
            "DATABASE_URL=postgres://app:sMv3Kq9dPz2wLr8t@db.internal:5432/app\n"
            'API_TOKEN="9Fh2Kd8Lm4Qp7Xs1Bv6Nz3Rt5Wy0CjEa7Gu"\n'
            "DEBUG=true\n"
            "PORT=5432\n"
        ),
        detected=True,
        rules=("dotenv-file", "assigned-secret"),
        verdict="real",
    ),
    CredentialCase(
        id="private-key-at-rest",
        why="A private key file, by name and by envelope. Nothing about it is ambiguous.",
        path="deploy/id_ed25519",
        content=_KEY_BLOCK,
        detected=True,
        rules=("private-key-file", "private-key-block"),
        verdict="real",
    ),
    CredentialCase(
        id="provider-token-in-source",
        why=(
            "A `ghp_` prefix is minted by one vendor and means the string was "
            "issued. Sitting in tracked source is the finding."
        ),
        path="src/settings.py",
        content=(
            "import os\n\n"
            'GITHUB_TOKEN = "ghp_7Kd2Lm9Qp4Xs1Bv6Nz3Rt5Wy0CjEa8Gu2Hn"\n'
            'API_BASE = os.environ.get("API_BASE", "https://api.github.com")\n'
        ),
        detected=True,
        rules=("provider-token",),
        verdict="real",
    ),
    CredentialCase(
        id="terraform-variables",
        why=(
            "A tfvars file is where infrastructure passwords end up, and this "
            "one holds a value with no separator and every character class."
        ),
        path="infra/prod.auto.tfvars",
        content=(
            'region      = "eu-west-1"\n'
            'db_password = "Tr7pQz2wLx9mKv4d"\n'
            "instances   = 3\n"
        ),
        detected=True,
        rules=("infrastructure-state",),
        verdict="real",
    ),
    CredentialCase(
        id="authorization-header-in-script",
        why="A bearer header with a long opaque value, checked into a script.",
        path="bin/publish.sh",
        content=(
            "#!/usr/bin/env bash\n"
            "curl -X POST https://api.internal/v1/deploy \\\n"
            '  -H "Authorization: Bearer 4Nz3Rt5Wy0CjEa7Gu2Hn9Kd2Lm4Qp7Xs1Bv6" \\\n'
            '  -d "{}"\n'
        ),
        detected=True,
        rules=("authorization-header",),
        verdict="real",
    ),
    # ---------------------------------------------------------------
    # Matched, and not credentials. The half a pattern cannot settle.
    # ---------------------------------------------------------------
    CredentialCase(
        id="dotenv-example-placeholders",
        why=(
            "The file `.env.example` exists to be committed. Its values are "
            "angle-bracket placeholders, which the literal filter drops "
            "before anyone is asked — so what remains is the path rule, "
            "firing on the name alone."
        ),
        path=".env.example",
        content=(
            "DATABASE_URL=postgres://user:<password>@localhost:5432/app\n"
            'API_TOKEN="<your-token-here>"\n'
            "DEBUG=true\n"
        ),
        detected=True,
        rules=("dotenv-file",),
        verdict="placeholder",
    ),
    CredentialCase(
        id="documented-example-access-key",
        why=(
            "AKIAIOSFODNN7EXAMPLE is Amazon's own published example: the real "
            "prefix, the real shape, and no account behind it. A pattern "
            "cannot tell it from an issued key, which is the argument for "
            "having a second stage at all."
        ),
        path="docs/aws-setup.md",
        content=(
            "# Configuring credentials\n\n"
            "Set the values AWS gives you:\n\n"
            "```\n"
            "AWS_ACCESS_KEY_ID = AKIAIOSFODNN7EXAMPLE\n"
            "```\n"
        ),
        detected=True,
        rules=("cloud-access-key",),
        verdict="placeholder",
        also_ok=("unclear",),
    ),
    CredentialCase(
        id="model-name-looks-like-a-password",
        why=(
            "`qwen3-8b-fp8` is letters with digits glued on and no separator, "
            "which is precisely the password shape. It is the name of a "
            "model. Nothing structural separates the two, and a shipped list "
            "of model names would be the mistake this project refuses "
            "everywhere else."
        ),
        path="src/model.py",
        content=(
            'DEFAULT_ENDPOINT = "http://localhost:5001/v1"\n'
            'DEFAULT_MODEL = "qwen3-8b-fp8"\n'
            "TIMEOUT_S = 300\n"
        ),
        detected=True,
        rules=("password-shape",),
        verdict="placeholder",
    ),
    CredentialCase(
        id="documented-default-changeme",
        why=(
            "`changeme` is the most-documented default there is. The name "
            "says password and the value says it has not been set."
        ),
        path="docs/quickstart.md",
        content=(
            "# Quickstart\n\n"
            "The bundled compose file starts with:\n\n"
            "```yaml\n"
            'POSTGRES_PASSWORD: "changeme"\n'
            "```\n\n"
            "Change it before you expose the port.\n"
        ),
        detected=True,
        rules=("assigned-secret",),
        verdict="placeholder",
    ),
    CredentialCase(
        id="test-fixture-password",
        why=(
            "`hunter2` is a joke that is also eight low-entropy characters, "
            "and it is also somebody's password. In a test fixture it opens "
            "nothing; the value alone cannot settle that, which is why the "
            "model is given the file it sits in."
        ),
        path="tests/test_login.py",
        content=(
            "def test_rejects_wrong_password(client):\n"
            '    resp = client.post("/login", json={"user": "ada", '
            '"password": "hunter2"})\n'
            "    assert resp.status_code == 401\n"
        ),
        detected=True,
        rules=("assigned-secret",),
        verdict="placeholder",
        also_ok=("unclear",),
    ),
    CredentialCase(
        id="local-development-connection",
        why=(
            "A password in a URL pointing at localhost. Real string, nothing "
            "reachable behind it — which is the question the connection-string "
            "rule actually asks."
        ),
        path="docker-compose.override.yml",
        content=(
            "services:\n"
            "  db:\n"
            "    image: postgres:16\n"
            "    environment:\n"
            "      DATABASE_URL: postgres://dev:devpass@localhost:5432/dev\n"
        ),
        detected=True,
        rules=("connection-string",),
        verdict="placeholder",
        also_ok=("unclear",),
    ),
    # ---------------------------------------------------------------
    # Must not be flagged at all. The stage before the model has its own
    # false-alarm rate, and it is the one that decides how much the
    # model is asked to look at.
    # ---------------------------------------------------------------
    CredentialCase(
        id="json-config-with-a-quoted-key",
        why=(
            "The form every JSON and YAML config uses, and the one the "
            "content rules were blind to until the closing quote was allowed "
            "between the name and the colon. `authorization-header` had "
            "always allowed it; the other three had not, so a credential in "
            "a JSON file was invisible to all of them."
        ),
        path="config/service.json",
        content=(
            "{\n"
            '  "host": "db.internal",\n'
            '  "port": 5432,\n'
            '  "password": "Tr7pQz2wLx9mKv4d"\n'
            "}\n"
        ),
        detected=True,
        rules=("assigned-secret",),
        verdict="real",
    ),
    CredentialCase(
        id="ordinary-source-file",
        why="Imports and a function. There is nothing here to flag.",
        path="src/client.py",
        content=(
            "import httpx\n\n\n"
            "async def fetch(url: str) -> dict:\n"
            "    async with httpx.AsyncClient() as client:\n"
            "        response = await client.get(url)\n"
            "        return response.json()\n"
        ),
        detected=False,
    ),
    CredentialCase(
        id="pinned-requirements",
        why="Version pins are dotted numbers; nothing in them has a credential's shape.",
        path="requirements.txt",
        content="httpx==0.28.1\nloguru==0.7.3\npydantic==2.10.4\n",
        detected=False,
    ),
    CredentialCase(
        id="prose-documentation",
        why="A paragraph of English about architecture.",
        path="docs/architecture.md",
        content=(
            "# Architecture\n\n"
            "The pipeline has two stages that run side by side. The first "
            "measures; the second reports what is true right now. Neither "
            "stage may rewrite what the other recorded.\n"
        ),
        detected=False,
    ),
    CredentialCase(
        id="secret-read-at-run-time",
        why=(
            "The value is a command substitution, so there is no literal in "
            "the file at all. Reading a secret at run time is the "
            "recommendation, and a gate that flagged it would be arguing "
            "against its own advice."
        ),
        path="bin/start.sh",
        content=(
            "#!/usr/bin/env bash\n"
            'TOKEN="$(cat /run/secrets/api_token)"\n'
            'exec ./server --token "$TOKEN"\n'
        ),
        detected=False,
    ),
    CredentialCase(
        id="checksum-in-source",
        why=(
            "A SHA-256 is sixty-four dense characters assigned to a name — "
            "the minted-token shape exactly — and it is a checksum. If this "
            "one is flagged it is a false alarm worth knowing the rate of."
        ),
        path="src/hashes.py",
        content=(
            "EMPTY_SHA256 = "
            '"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"\n'
        ),
        detected=False,
    ),
    CredentialCase(
        id="uuid-in-fixture",
        why=(
            "A UUID is thirty-six characters of hex and dashes assigned to a "
            "name. Same shape question as the checksum, in the file type "
            "where identifiers actually live."
        ),
        path="tests/fixtures.json",
        content=(
            "{\n"
            '  "id": "550e8400-e29b-41d4-a716-446655440000",\n'
            '  "port": 5432,\n'
            '  "enabled": true\n'
            "}\n"
        ),
        detected=False,
    ),
    CredentialCase(
        id="lockfile-integrity-hashes",
        why=(
            "A lockfile is nothing but long dense strings next to names, and "
            "every one of them is public. Whatever this measures, it "
            "measures at scale: one lockfile is thousands of candidates."
        ),
        path="package-lock.json",
        content=(
            "{\n"
            '  "name": "scene",\n'
            '  "lockfileVersion": 3,\n'
            '  "packages": {\n'
            '    "node_modules/react": {\n'
            '      "version": "19.0.0",\n'
            '      "resolved": "https://registry.npmjs.org/react/-/react-19.0.0.tgz",\n'
            '      "integrity": "sha512-V8AVnmPIICiWpGfm6GLzCR/W5FXLchHop40W4nXBmdlEceh16rCN8O8LNWm5bh5XUX91fh7KpA+W0TgMKmgTpQ=="\n'
            "    }\n"
            "  }\n"
            "}\n"
        ),
        detected=False,
    ),
)
