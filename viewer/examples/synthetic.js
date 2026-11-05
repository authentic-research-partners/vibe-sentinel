/* Invented evidence for demonstration. No real project data. */
(function(root) {
  "use strict";
  const example = {
  "result": {
    "schema_version": 3,
    "profile": "local-detail",
    "run_id": 204,
    "baseline_run_id": 200,
    "generated_at": "2026-10-20T14:32:00+00:00",
    "outcome": "findings",
    "exit_code": 1,
    "comparison": "compared",
    "provenance": {
      "captured_at": "2026-10-20T14:31:20+00:00",
      "availability": "captured",
      "source_kind": "git",
      "source_identity_policy": "git_head_and_path",
      "source_identity_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "source_revision": "1234567890abcdef1234567890abcdef12345678",
      "source_dirty": true,
      "consistency": "not_atomic",
      "config_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "rules_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "scope_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "fingerprint_status": "captured",
      "fingerprint_scope": "behavior_excluding_auth_and_opaque_request_fields",
      "tool_version": "0.1.0",
      "tool_build_identity": "unverified",
      "packs": [],
      "scanner_executable": "",
      "scanner_python": "3.13.0",
      "judge_configured_name": "synthetic-local-judge",
      "judge_endpoint": "",
      "judge_identity_sha256": "",
      "judge_weights_identity": "unverified",
      "build_freshness": "not_assessed"
    },
    "measured_environment": null,
    "measured_environment_sha256": "",
    "failed_stages": [],
    "probes": [
      {
        "id": "vite-build",
        "status": "complete"
      },
      {
        "id": "openapi",
        "status": "complete"
      },
      {
        "id": "python-relationships",
        "status": "complete"
      }
    ],
    "gates": [
      {
        "id": "licenses",
        "status": "unconfigured"
      },
      {
        "id": "packages",
        "status": "findings"
      },
      {
        "id": "credentials",
        "status": "complete"
      }
    ],
    "judges": [
      {
        "id": "delivery-shape",
        "status": "answered"
      },
      {
        "id": "surface-review",
        "status": "answered"
      },
      {
        "id": "gate:packages",
        "status": "answered"
      },
      {
        "id": "gate:credentials",
        "status": "not_asked"
      }
    ],
    "horizons": [
      {
        "id": "1w",
        "status": "complete"
      },
      {
        "id": "1m",
        "status": "incomplete"
      }
    ],
    "gates_status": "complete",
    "counts": {
      "probes": 3,
      "observations": 162,
      "changes": 3,
      "findings": 2,
      "failing_findings": 1,
      "baseline_unmeasured": 0
    },
    "evidence": {
      "availability": "captured",
      "failure": "",
      "lenses": [
        {
          "id": "delivery-shape",
          "title": "Delivery shape",
          "source": "synthetic-rules.toml",
          "question": "Has the browser delivery surface grown enough to need a deliberate decision?",
          "watch": [
            "vite-build"
          ],
          "status": "answered",
          "assessment": "The account entry carries more bytes. Compare the measured payload with the intended scope of the entry.",
          "ratings": [
            {
              "probe_id": "vite-build",
              "key": "entry:account:bytes",
              "severity": "medium",
              "note": "The account entry increased by 38 percent.",
              "applied": true
            }
          ],
          "dropped_ratings": 0
        },
        {
          "id": "surface-review",
          "title": "Application surface",
          "source": "synthetic-rules.toml",
          "question": "Do the recorded interfaces and dependencies still fit the declared component boundaries?",
          "watch": [],
          "status": "answered",
          "assessment": "The account entry grew alongside a new export route. The removed worker edge is a separate structural change.",
          "ratings": [
            {
              "probe_id": "vite-build",
              "key": "entry:account:bytes",
              "severity": "high",
              "note": "Confirm the new account functionality belongs in this browser entry.",
              "applied": true
            },
            {
              "probe_id": "openapi",
              "key": "operation:POST:/exports",
              "severity": "medium",
              "note": "A new export operation expands the API surface.",
              "applied": true
            }
          ],
          "dropped_ratings": 0
        }
      ],
      "changes": [
        {
          "label": "Account browser entry",
          "structural": {
            "identity": "account",
            "pointer": "",
            "file": "",
            "edge_schema": "",
            "ecosystem": "",
            "source": "frontend/src/account.ts",
            "target": "",
            "kind": "",
            "phase": "",
            "resolution": "",
            "metric": "bytes",
            "unit": "bytes"
          },
          "probe_id": "vite-build",
          "key": "entry:account:bytes",
          "kind": "grew",
          "before": 184200.0,
          "after": 254100.0,
          "before_state": "",
          "after_state": "",
          "severity": "high",
          "note": "Confirm the new account functionality belongs in this browser entry.",
          "lens_id": "surface-review"
        },
        {
          "label": "POST /exports",
          "structural": {
            "identity": "POST /exports",
            "pointer": "/paths/~1exports/post",
            "file": "",
            "edge_schema": "",
            "ecosystem": "",
            "source": "",
            "target": "",
            "kind": "",
            "phase": "",
            "resolution": "",
            "metric": "",
            "unit": ""
          },
          "probe_id": "openapi",
          "key": "operation:POST:/exports",
          "kind": "appeared",
          "before": null,
          "after": 1.0,
          "before_state": "",
          "after_state": "operation",
          "severity": "medium",
          "note": "A new export operation expands the API surface.",
          "lens_id": "surface-review"
        },
        {
          "label": "Worker dependency on billing",
          "structural": {
            "identity": "",
            "pointer": "",
            "file": "",
            "edge_schema": "1",
            "ecosystem": "python",
            "source": "service.worker",
            "target": "service.billing",
            "kind": "import",
            "phase": "runtime",
            "resolution": "resolved",
            "metric": "",
            "unit": ""
          },
          "probe_id": "python-relationships",
          "key": "edge:worker:billing",
          "kind": "disappeared",
          "before": 1.0,
          "after": null,
          "before_state": "runtime",
          "after_state": "",
          "severity": "medium",
          "note": "",
          "lens_id": ""
        }
      ],
      "gates": [
        {
          "gate": "packages",
          "key": "near-miss:requests/requsets",
          "kind": "near-miss",
          "subject": "requests/requsets",
          "risk": "",
          "verdict": "typosquat",
          "failing": true,
          "reason": "The second name appears to be a misspelling.",
          "decision": {
            "origin": "model",
            "status": "answered",
            "question": "'requests' and 'requsets' are both installed and are one edit apart. Are these two real packages, or is one of them a misspelling of the other?",
            "source": "built-in"
          },
          "matches": null,
          "spdx": "",
          "suspect": "requsets"
        },
        {
          "gate": "credentials",
          "key": "assigned-secret:fixtures/example.env",
          "kind": "assigned-secret",
          "subject": "fixtures/example.env",
          "risk": "tracked",
          "verdict": "pinned",
          "failing": false,
          "reason": "The fixture deliberately carries a nonfunctional example value.",
          "decision": {
            "origin": "pin",
            "status": "not_asked",
            "question": "Does this assignment contain a live credential?",
            "source": "synthetic-policy.toml"
          },
          "matches": 1,
          "spdx": "",
          "suspect": ""
        }
      ],
      "withheld_dependency_changes": 0
    }
  }
};
  if (typeof module !== "undefined" && module.exports) module.exports = example;
  else root.SentinelExample = example;
})(typeof globalThis !== "undefined" ? globalThis : this);
