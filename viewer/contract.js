/* The offline import boundary. Keep it aligned with supported frozen result versions. */
(function (root) {
  "use strict";
  const FILE_LIMIT = 16 * 1024 * 1024;
  const NODE_LIMIT = 250000;
  const PAGE_SIZE = 30;
  const RESULT_KEYS = "schema_version profile run_id baseline_run_id generated_at outcome exit_code comparison provenance measured_environment measured_environment_sha256 failed_stages probes gates judges horizons gates_status counts".split(" ");
  const PROVENANCE_KEYS = "captured_at availability source_kind source_identity_policy source_identity_sha256 source_revision source_dirty consistency config_sha256 rules_sha256 scope_sha256 fingerprint_status fingerprint_scope tool_version tool_build_identity packs scanner_executable scanner_python judge_configured_name judge_endpoint judge_identity_sha256 judge_weights_identity build_freshness".split(" ");
  const COUNT_KEYS = "probes observations changes findings failing_findings baseline_unmeasured".split(" ");
  const STRUCTURAL_KEYS = "identity pointer file edge_schema ecosystem source target kind phase resolution metric unit".split(" ");
  const RANK = ["info", "low", "medium", "high"];
  const STAGES = {
    probes: ["complete", "error"],
    gates: ["complete", "findings", "error", "unconfigured"],
    judges: ["answered", "not_asked", "incomplete", "not_run", "error"],
    horizons: ["complete", "incomplete"],
  };
  function fail(message) { throw new Error(message || "This result does not match a supported evidence contract. Export it again with Sentinel."); }
  function object(value) { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
  function shape(value, keys) {
    if (!object(value) || Object.keys(value).length !== keys.length || !keys.every(key => Object.hasOwn(value, key))) fail();
  }
  function text(value, max = 8192) { if (typeof value !== "string" || Array.from(value).length > max) fail(); }
  function one(value, allowed) { if (!allowed.includes(value)) fail(); }
  function integer(value, nullable = false) { if (nullable && value === null) return; if (!Number.isSafeInteger(value) || value < 0) fail(); }
  function bool(value, nullable = false) { if (nullable && value === null) return; if (typeof value !== "boolean") fail(); }
  function array(value, limit = 10000) { if (!Array.isArray(value) || value.length > limit) fail("This result contains too many records for the offline viewer."); }
  function strings(value, limit = 10000) { array(value, limit); value.forEach(item => text(item)); }
  function number(value) { if (value !== null && (typeof value !== "number" || !Number.isFinite(value))) fail(); }
  function budget(value) {
    let count = 0;
    const pending = [[value, 0]];
    while (pending.length) {
      const [item, depth] = pending.pop();
      if (++count > NODE_LIMIT || depth > 48) fail("This file is too complex for the offline viewer. Use a bounded result export.");
      if (typeof item === "string" && item.length > 32768) fail("A text field exceeds the viewer's import limit.");
      if (item && typeof item === "object") {
        const entries = Array.isArray(item) ? item : Object.values(item);
        if (entries.length > NODE_LIMIT - count) fail("This file contains too many records for the offline viewer.");
        for (const entry of entries) pending.push([entry, depth + 1]);
      }
    }
  }
  function provenance(value, version) {
    shape(value, PROVENANCE_KEYS);
    for (const key of PROVENANCE_KEYS.filter(key => !["packs", "source_dirty"].includes(key))) text(value[key]);
    bool(value.source_dirty, true);
    one(value.availability, ["captured", "unavailable"]);
    one(value.source_kind, ["git", "non_git", "unavailable"]);
    one(value.source_identity_policy, ["git_head_and_path", "canonical_path_only", "unavailable"]);
    one(value.consistency, ["not_atomic"]);
    one(value.fingerprint_status, ["captured", "unavailable"]);
    one(value.fingerprint_scope, ["behavior_excluding_auth_and_opaque_request_fields"]);
    one(value.tool_build_identity, ["unverified"]);
    one(value.judge_weights_identity, ["unverified"]);
    one(value.build_freshness, ["not_assessed"]);
    if (version === 1) { one(value.packs, ["not_implemented"]); return; }
    array(value.packs, 256);
    for (const pack of value.packs) {
      shape(pack, "id version protocol manifest_sha256 executables ecosystems supported_inputs selected_probes inventory_readers inventory_status history_coverage".split(" "));
      for (const key of ["id", "version", "manifest_sha256"]) text(pack[key]);
      one(pack.protocol, [1]);
      one(pack.inventory_status, ["not_declared", "declared_not_assessed"]);
      one(pack.history_coverage, ["baseline_only"]);
      for (const key of ["ecosystems", "supported_inputs", "selected_probes", "inventory_readers"]) strings(pack[key]);
      array(pack.executables, 256);
      for (const executable of pack.executables) {
        shape(executable, ["id", "command", "path", "identity"]);
        for (const key of ["id", "command", "path"]) text(executable[key]);
        one(executable.identity, ["path_only_contents_unverified"]);
      }
    }
  }
  function evidence(result) {
    const value = result.evidence;
    shape(value, ["availability", "failure", "lenses", "changes", "gates", "withheld_dependency_changes"]);
    one(value.availability, ["captured", "unavailable", "withheld"]);
    one(value.failure, ["", "limit_exceeded"]);
    array(value.lenses, 256); array(value.changes); array(value.gates); integer(value.withheld_dependency_changes);
    if (["mcp", "shareable"].includes(result.profile) && value.availability !== "withheld") fail("This export profile must withhold detailed evidence.");
    if (result.profile === "local-detail" && value.availability !== "captured") fail("This local-detail export has no captured evidence.");
    if (value.failure || value.availability === "withheld") {
      if (value.lenses.length || value.changes.length || value.gates.length || value.withheld_dependency_changes) fail();
      if (value.failure && (!["incomplete", "error"].includes(result.outcome) || !["unavailable", "withheld"].includes(value.availability))) fail();
      return;
    }
    if (new TextEncoder().encode(JSON.stringify(value)).length > 8 * 1024 * 1024) fail("The evidence exceeds its captured size limit.");
    if (value.changes.length + value.withheld_dependency_changes !== result.counts.changes) fail("The evidence and recorded change counts disagree.");
    const probeIds = new Set(result.probes.map(stage => stage.id));
    const changes = new Map();
    for (const change of value.changes) {
      shape(change, "label structural probe_id key kind before after before_state after_state severity note lens_id".split(" "));
      for (const key of ["label", "probe_id", "key", "before_state", "after_state", "note", "lens_id"]) text(change[key]);
      one(change.kind, ["appeared", "disappeared", "grew", "shrank", "changed"]); one(change.severity, RANK);
      number(change.before); number(change.after);
      if (change.structural !== null) { shape(change.structural, STRUCTURAL_KEYS); STRUCTURAL_KEYS.forEach(key => text(change.structural[key])); }
      const pair = JSON.stringify([change.probe_id, change.key]);
      if (changes.has(pair) || !probeIds.has(change.probe_id) || change.probe_id === "dependency-versions") fail("A change has an inconsistent measurement identity.");
      changes.set(pair, change);
    }
    const gateJudgeCount = result.gates.filter(stage => ["packages", "credentials"].includes(stage.id)).length;
    if (value.availability === "captured" && value.lenses.length !== result.judges.length - gateJudgeCount) fail("Captured questions do not match the recorded judge stages.");
    const lensIds = new Set(), selected = new Map();
    value.lenses.forEach((lens, index) => {
      shape(lens, "id title source question watch status assessment ratings dropped_ratings".split(" "));
      for (const key of ["id", "title", "source"]) text(lens[key]);
      text(lens.question, 32768); text(lens.assessment, 700); strings(lens.watch);
      one(lens.status, ["not_asked", "answered", "failed"]); array(lens.ratings, 40); integer(lens.dropped_ratings);
      const stage = result.judges[index];
      if (lensIds.has(lens.id) || !stage || stage.id !== lens.id || stage.status !== ({answered: "answered", not_asked: "not_asked", failed: "incomplete"})[lens.status]) fail("A question's recorded status is inconsistent.");
      lensIds.add(lens.id);
      if (lens.status !== "answered" && (lens.assessment || lens.ratings.length || lens.dropped_ratings)) fail("An unanswered question cannot carry an answer.");
      for (const rating of lens.ratings) {
        shape(rating, ["probe_id", "key", "severity", "note", "applied"]);
        text(rating.probe_id); text(rating.key); text(rating.note, 240); one(rating.severity, RANK); bool(rating.applied);
        const pair = JSON.stringify([rating.probe_id, rating.key]);
        if (!changes.has(pair)) fail("An answer references an unrecorded measurement.");
        const previous = selected.get(pair);
        const applied = !previous || RANK.indexOf(rating.severity) > RANK.indexOf(previous.rating.severity);
        if (rating.applied !== applied) fail("An answer's application does not match the recorded order.");
        if (applied) selected.set(pair, {id: lens.id, rating});
      }
    });
    for (const [pair, change] of changes) {
      const winner = selected.get(pair);
      if (change.lens_id ? !winner || winner.id !== change.lens_id || winner.rating.severity !== change.severity || winner.rating.note !== change.note : !!winner) fail("The winning answer does not match the recorded change.");
    }
    const gateIds = new Set(result.gates.map(stage => stage.id)), identities = new Set();
    if (value.gates.filter(finding => finding.failing !== null).length !== result.counts.findings || value.gates.filter(finding => finding.failing === true).length !== result.counts.failing_findings) fail("Gate evidence and recorded finding counts disagree.");
    for (const finding of value.gates) {
      shape(finding, "gate key kind subject risk verdict failing reason decision matches spdx suspect".split(" "));
      for (const key of ["gate", "key", "kind", "subject", "risk", "verdict", "reason", "spdx", "suspect"]) text(finding[key]);
      bool(finding.failing, true); integer(finding.matches, true);
      const pair = JSON.stringify([finding.gate, finding.key]);
      if (!gateIds.has(finding.gate) || identities.has(pair)) fail("A finding has an inconsistent gate identity.");
      identities.add(pair);
      const decision = finding.decision;
      shape(decision, ["origin", "status", "question", "source"]);
      text(decision.question, 32768); text(decision.source);
      one(decision.origin, ["mechanical", "model", "declared_verdict", "pin", "unknown"]);
      one(decision.status, ["not_asked", "answered", "failed", "unavailable"]);
      if (["mechanical", "declared_verdict", "pin"].includes(decision.origin) && decision.status !== "not_asked") fail();
      if (decision.origin === "model" && !["answered", "failed"].includes(decision.status)) fail();
      if (decision.origin === "unknown" && decision.status === "answered") fail();
      if (decision.status === "failed" && (finding.verdict || finding.reason)) fail("A failed decision cannot carry an answer.");
    }
  }
  function validateResult(result) {
    budget(result);
    if (!object(result) || ![1, 2, 3].includes(result.schema_version)) fail("Unsupported result version. This viewer reads Sentinel result versions 1, 2 and 3.");
    const version = result.schema_version;
    shape(result, version === 3 ? [...RESULT_KEYS, "evidence"] : RESULT_KEYS);
    one(result.profile, version === 3 ? ["local", "local-detail", "mcp", "shareable"] : ["local", "mcp", "shareable"]);
    integer(result.run_id, true); integer(result.baseline_run_id, true); text(result.generated_at);
    one(result.outcome, ["complete", "findings", "incomplete", "error"]);
    one(result.comparison, ["compared", "no_baseline", "incomplete", "not_run"]);
    one(result.gates_status, ["complete", "error", "not_run"]);
    if (result.exit_code !== ({complete: 0, findings: 1, incomplete: 2, error: 2})[result.outcome]) fail("The recorded outcome and exit code disagree.");
    provenance(result.provenance, version);
    if (result.measured_environment !== null) {
      if (!object(result.measured_environment)) fail();
      for (const [key, value] of Object.entries(result.measured_environment)) { text(key); text(value); }
    }
    text(result.measured_environment_sha256);
    strings(result.failed_stages, 16); result.failed_stages.forEach(stage => one(stage, ["configuration", "measurement", "judgement", "gates", "storage"]));
    for (const [name, statuses] of Object.entries(STAGES)) {
      array(result[name]);
      for (const stage of result[name]) { shape(stage, ["id", "status"]); text(stage.id); one(stage.status, statuses); }
    }
    shape(result.counts, COUNT_KEYS); COUNT_KEYS.forEach(key => integer(result.counts[key]));
    if (result.counts.failing_findings > result.counts.findings || result.counts.probes !== result.probes.length) fail("The recorded counts are inconsistent.");
    const failed = [...result.probes, ...result.gates, ...result.judges].some(stage => stage.status === "error") || result.gates_status === "error";
    const incomplete = result.failed_stages.length > 0 || [...result.probes, ...result.judges].some(stage => ["incomplete", "not_run"].includes(stage.status)) || result.gates_status === "not_run" || ["incomplete", "not_run"].includes(result.comparison) || result.provenance.availability !== "captured" || result.provenance.fingerprint_status !== "captured" || result.counts.baseline_unmeasured > 0;
    if (failed && result.outcome !== "error" || incomplete && ["complete", "findings"].includes(result.outcome)) fail("Incomplete or failed work cannot be displayed as a complete result.");
    if (result.comparison === "compared" && result.baseline_run_id === null || result.comparison === "no_baseline" && result.baseline_run_id !== null) fail("The comparison and baseline identity disagree.");
    if (result.outcome === "complete" && (result.counts.failing_findings > 0 || result.gates.some(stage => stage.status === "findings") || version === 3 && result.evidence && Array.isArray(result.evidence.changes) && result.evidence.changes.some(change => change.severity !== "info"))) fail("A result with known findings cannot be displayed as complete without findings.");
    if (version === 3) evidence(result);
    return result;
  }
  function hasDuplicateKeys(input) {
    // JSON.parse discards earlier duplicate properties. Scan object scopes first
    // so a later success field cannot conceal an earlier failure or identity.
    const scopes = [];
    let position = 0, tokens = 0;
    while (position < input.length) {
      const char = input[position];
      if (/\s/.test(char) || char === ":") { position++; continue; }
      if (char === ",") { if (scopes.length && scopes[scopes.length - 1].object) scopes[scopes.length - 1].key = true; position++; continue; }
      if (char === "}" || char === "]") { scopes.pop(); position++; continue; }
      if (++tokens > NODE_LIMIT) fail("This file is too complex for the offline viewer.");
      if (char === "{" || char === "[") {
        scopes.push({object: char === "{", key: char === "{", keys: new Set()});
        if (scopes.length > 48) fail("This file is too complex for the offline viewer.");
        position++; continue;
      }
      if (char === '"') {
        const start = position++;
        while (position < input.length) {
          if (input[position] === "\\") { position += 2; continue; }
          if (input[position++] === '"') break;
        }
        const scope = scopes[scopes.length - 1];
        if (scope && scope.object && scope.key) {
          let key;
          try { key = JSON.parse(input.slice(start, position)); }
          catch (_) { return false; } // The full JSON parse rejects malformed syntax.
          if (scope.keys.has(key)) return true;
          scope.keys.add(key); scope.key = false;
        }
        continue;
      }
      while (position < input.length && !/[\s,}:\]\[]/.test(input[position])) position++;
      if (position < input.length && input[position] === "[") continue;
    }
    return false;
  }
  function decodeFile(buffer) {
    let decoded;
    try { decoded = new TextDecoder("utf-8", {fatal: true}).decode(buffer); }
    catch (_) { fail("This file is not valid UTF-8 JSON. Export it again with Sentinel."); }
    return parseResult(decoded);
  }
  function parseResult(input) {
    if (typeof input !== "string" || new TextEncoder().encode(input).length > FILE_LIMIT) fail("Choose a JSON export smaller than 16 MiB.");
    if (hasDuplicateKeys(input)) fail("Duplicate JSON keys are not valid evidence. Export the result again with Sentinel.");
    let payload;
    try { payload = JSON.parse(input); } catch (_) { fail("This file is not valid JSON. Choose an export written by Sentinel."); }
    budget(payload);
    if (!object(payload) || !Object.hasOwn(payload, "result") || Object.keys(payload).some(key => !["result", "snapshot", "drift", "gates"].includes(key))) fail("Choose a Sentinel JSON export containing a versioned result envelope.");
    return validateResult(payload.result);
  }
  function filterRows(rows, query, severity = "all") {
    const needle = query.trim().toLocaleLowerCase();
    return rows.filter(row => (severity === "all" || row.severity === severity || severity === "failing" && row.failing === true || severity === "partial" && row.failing === null) && (!needle || [row.label, row.subject, row.key, row.probe_id, row.gate, row.kind, row.note, row.reason, row.suspect, ...Object.values(row.structural || {})].filter(value => typeof value === "string").some(value => value.toLocaleLowerCase().includes(needle))));
  }
  function pageRows(rows, page = 0) {
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    const selected = Math.min(Math.max(0, page), pages - 1);
    return {rows: rows.slice(selected * PAGE_SIZE, (selected + 1) * PAGE_SIZE), page: selected, pages, total: rows.length};
  }
  const api = {FILE_LIMIT, PAGE_SIZE, parseResult, decodeFile, validateResult, filterRows, pageRows, STRUCTURAL_KEYS};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.SentinelContract = Object.freeze(api);
})(typeof globalThis !== "undefined" ? globalThis : this);
