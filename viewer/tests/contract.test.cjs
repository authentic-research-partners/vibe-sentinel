"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const C = require("../contract.js");
const example = require("../examples/synthetic.js");
const copy = value => JSON.parse(JSON.stringify(value));
const parse = result => C.parseResult(JSON.stringify({result}));

for (const version of [1, 2, 3]) test(`frozen version ${version} imports without invented evidence`, () => {
  const raw = JSON.parse(fs.readFileSync(path.join(__dirname, `fixtures/result-v${version}.json`), "utf8"));
  const result = parse(raw.result || raw);
  assert.equal(result.schema_version, version);
  if (version < 3) assert.equal(result.evidence, undefined);
});

test("synthetic example is identical to validated v3 fixture", () => {
  assert.deepEqual(example, JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/result-v3.json"), "utf8")));
  assert.equal(parse(example.result).evidence.changes[0].lens_id, "surface-review");
});

for (const mutate of [
  r => { r.schema_version = 99; },
  r => { delete r.provenance; },
  r => { r.counts.failing_findings = "0"; },
  r => { r.exit_code = 0; },
  r => { r.outcome = "complete"; r.exit_code = 0; },
  r => { r.comparison = "no_baseline"; },
  r => { r.probes[0].status = "answered"; },
  r => { r.gates[0].status = "error"; },
  r => { r.evidence.changes[0].key = "missing"; },
  r => { r.evidence.changes[0].lens_id = "delivery-shape"; },
  r => { r.evidence.lenses[0].status = "not_asked"; },
  r => { r.evidence.lenses[0].ratings[0].applied = false; },
  r => { r.evidence.lenses = []; },
  r => { delete r.evidence.lenses[0].question; },
  r => { r.evidence.gates[0].decision.origin = "mechanical"; },
  r => { r.evidence.gates[0].decision.status = "failed"; },
  r => { r.evidence.gates[0].extra = "unversioned"; },
  r => { r.profile = "shareable"; },
]) test(`reject malformed or contradictory contract: ${mutate.toString()}`, () => {
  const result = copy(example.result); mutate(result); assert.throws(() => parse(result));
});

test("aggregate evidence is explicitly withheld, never synthesized", () => {
  const result = copy(example.result); result.profile = "shareable";
  result.evidence = {availability: "withheld", failure: "", lenses: [], changes: [], gates: [], withheld_dependency_changes: 0};
  const accepted = parse(result);
  assert.equal(accepted.counts.changes, 3); assert.equal(accepted.evidence.changes.length, 0);
});

test("bounded evidence failure remains incomplete", () => {
  const result = copy(example.result); result.profile = "local"; result.outcome = "incomplete"; result.exit_code = 2;
  result.evidence = {availability: "unavailable", failure: "limit_exceeded", lenses: [], changes: [], gates: [], withheld_dependency_changes: 0};
  assert.equal(parse(result).evidence.failure, "limit_exceeded");
  result.outcome = "complete"; result.exit_code = 0;
  assert.throws(() => parse(result));
});

test("a failed judge is neither unanswered-clean nor an invented answer", () => {
  const result = copy(example.result); result.outcome = "error"; result.exit_code = 2; result.failed_stages = ["judgement"];
  const lens = result.evidence.lenses[0]; lens.status = "failed"; lens.assessment = ""; lens.ratings = [];
  result.judges[0].status = "incomplete";
  assert.equal(parse(result).evidence.lenses[0].status, "failed");
});

test("untrusted text is preserved as data and never treated as markup", () => {
  const result = copy(example.result), hostile = '<img src=x onerror="globalThis.pwned=true">';
  result.evidence.changes[0].label = hostile;
  result.evidence.lenses[0].question = hostile;
  const accepted = parse(result);
  assert.equal(accepted.evidence.changes[0].label, hostile);
  assert.equal(globalThis.pwned, undefined);
});

test("malformed JSON diagnostics never echo source text", () => {
  assert.throws(() => C.parseResult('{"private-secret":"DO-NOT-REPEAT"'), error => !error.message.includes("DO-NOT-REPEAT"));
  assert.throws(() => C.parseResult(" ".repeat(C.FILE_LIMIT + 1)), /16 MiB/);
  assert.throws(() => C.parseResult(JSON.stringify({result: {unexpected: "x".repeat(32769)}})), /text field/);
});

test("deep input fails before recursive rendering", () => {
  let deep = "x"; for (let i = 0; i < 55; i++) deep = [deep];
  assert.throws(() => C.parseResult(JSON.stringify({result: deep})), /complex/);
});

test("filter and pagination do not mutate recorded evidence", () => {
  const rows = copy(example.result.evidence.changes), original = copy(rows);
  assert.equal(C.filterRows(rows, "ACCOUNT", "high").length, 1);
  assert.equal(C.filterRows(rows, "/paths/~1exports", "all").length, 1);
  assert.equal(C.filterRows(rows, "no match", "all").length, 0);
  assert.equal(C.filterRows(example.result.evidence.gates, "requsets", "failing").length, 1);
  const many = Array.from({length: 97}, (_, i) => ({key: String(i)}));
  assert.equal(C.pageRows(many, 2).rows.length, C.PAGE_SIZE);
  assert.equal(C.pageRows(many, 99).rows.length, 7);
  assert.deepEqual(rows, original);
});

test("CSP and runtime code have no network, storage or HTML injection surface", () => {
  const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");
  assert.match(html, /connect-src 'none'/); assert.match(html, /base-uri 'none'/);
  assert.match(html, /object-src 'none'/); assert.match(html, /form-action 'none'/);
  for (const name of ["viewer.js", "contract.js", "examples/synthetic.js"]) {
    const script = fs.readFileSync(path.join(__dirname, "..", name), "utf8");
    assert.doesNotMatch(script, /\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|localStorage|sessionStorage|indexedDB|eval)\b|innerHTML|outerHTML|insertAdjacentHTML|document\.write|sendBeacon/);
  }
  assert.doesNotMatch(html, /(?:src|href)="https?:|<script[^>]*>[^\s<]/);
});

for (const make of [
  r => '{"result":{"schema_version":999},"result":' + JSON.stringify(r) + '}',
  r => JSON.stringify({result: r}).replace('"outcome":"findings"', '"outcome":"error","outcome":"findings"'),
  r => '{"r\\u0065sult":null,"result":' + JSON.stringify(r) + '}',
]) test("duplicate and escaped-equivalent object keys are refused", () => {
  assert.throws(() => C.parseResult(make(example.result)), /Duplicate JSON keys/);
});

test("fatal UTF-8 decoding protects recorded string identities", () => {
  assert.throws(() => C.decodeFile(new Uint8Array([0xff]).buffer), /UTF-8/);
  assert.equal(C.decodeFile(new TextEncoder().encode(JSON.stringify(example)).buffer).schema_version, 3);
});
