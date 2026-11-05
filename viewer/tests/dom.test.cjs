"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
class Element {
  constructor(tag = "div") { this.tagName = tag; this.children = []; this.events = {}; this.attributes = {}; this.dataset = {}; this.hidden = false; this._text = ""; this.value = ""; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_) { throw new Error("Untrusted HTML sink used"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, handler) { (this.events[name] ||= []).push(handler); }
  fire(name, event = {}) { for (const fn of this.events[name] || []) fn({target: this, preventDefault() {}, ...event}); }
  focus() { this.focused = true; }
  scrollIntoView() {}
}
function boot() {
  const ids = "workspace evidence-file status-message empty-state loaded-state file-context clear-file load-example result-heading result-notice panel-drift panel-gates panel-questions panel-coverage drop-overlay".split(" ");
  const nodes = Object.fromEntries(ids.map(id => [id, new Element()]));
  const tabs = ["drift", "gates", "questions", "coverage"].map(name => { const node = new Element("button"); node.dataset.tab = name; return node; });
  const doc = new Element("document");
  doc.getElementById = id => nodes[id]; doc.createElement = tag => new Element(tag); doc.createTextNode = text => {const node = new Element("text"); node.textContent = text; return node;}; doc.querySelectorAll = () => tabs;
  class Reader {
    constructor() { this.handlers = {}; this.readyState = 0; }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    readAsArrayBuffer(file) { this.readyState = 2; this.result = typeof file.contents === "string" ? new TextEncoder().encode(file.contents).buffer : file.contents; this.handlers.load(); }
    abort() { this.readyState = 2; }
  }
  const context = vm.createContext({document: doc, FileReader: Reader, TextEncoder, TextDecoder, setTimeout, clearTimeout});
  vm.runInContext("window = globalThis", context);
  for (const script of ["contract.js", "examples/synthetic.js", "viewer.js"]) vm.runInContext(fs.readFileSync(path.join(__dirname, "..", script), "utf8"), context, {filename: script});
  function load(contents, size = contents.length) { nodes["evidence-file"].files = [{name: "local.json", contents, size}]; nodes["evidence-file"].fire("change"); }
  return {nodes, tabs, context, load};
}
function descendants(node, tag) { return node.children.flatMap(child => [...(child.tagName === tag ? [child] : []), ...descendants(child, tag)]); }

test("synthetic example renders separate sections and expandable answers", () => {
  const {nodes, tabs} = boot(); nodes["load-example"].fire("click");
  assert.equal(nodes["loaded-state"].hidden, false);
  assert.match(nodes["result-notice"].textContent, /Synthetic demonstration/);
  assert.match(nodes["panel-drift"].textContent, /Account browser entry/);
  const finding = descendants(nodes["panel-drift"], "details")[0]; finding.open = true; finding.fire("toggle");
  assert.match(finding.textContent, /184200/); assert.match(finding.textContent, /254100/);
  assert.match(finding.textContent, /Final attribution/); assert.match(finding.textContent, /Configured question/);
  assert.match(nodes["panel-gates"].textContent, /Unconfigured/);
  assert.match(nodes["panel-coverage"].textContent, /Not atomic/);
  assert.match(nodes["panel-coverage"].textContent, /Action reviews are outside/);
  tabs[0].fire("keydown", {key: "ArrowRight"});
  assert.equal(nodes["panel-gates"].hidden, false); assert.equal(tabs[1].focused, true);
});

test("XSS strings render only as text through the actual DOM renderer", () => {
  const {nodes, context, load} = boot();
  const payload = JSON.parse(vm.runInContext("JSON.stringify(SentinelExample)", context));
  const hostile = '<script>alert("private")</script><img src=x onerror="pwned()">';
  payload.result.evidence.changes[0].label = hostile;
  payload.result.evidence.lenses[0].question = hostile;
  load(JSON.stringify(payload));
  const finding = descendants(nodes["panel-drift"], "details")[0]; finding.open = true; finding.fire("toggle");
  assert.ok(finding.textContent.includes(hostile));
  assert.equal(descendants(finding, "script").length, 0); assert.equal(descendants(finding, "img").length, 0);
});

test("malformed file clears the previous result and reports no private input", () => {
  const {nodes, load} = boot(); nodes["load-example"].fire("click");
  load('{"private":"CANARY"');
  assert.equal(nodes["loaded-state"].hidden, true); assert.equal(nodes["empty-state"].hidden, false);
  assert.equal(nodes["result-heading"].textContent, "");
  assert.match(nodes["status-message"].textContent, /not valid JSON/);
  assert.doesNotMatch(nodes["status-message"].textContent, /CANARY/);
});

test("old and aggregate files explain unavailable details instead of an empty clean list", () => {
  const {nodes, context, load} = boot();
  const old = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/result-v2.json"), "utf8"));
  load(JSON.stringify({result: old}));
  assert.match(nodes["panel-drift"].textContent, /Detailed evidence was not captured/);
  const aggregate = JSON.parse(vm.runInContext("JSON.stringify(SentinelExample)", context));
  aggregate.result.profile = "shareable";
  aggregate.result.evidence = {availability: "withheld", failure: "", lenses: [], changes: [], gates: [], withheld_dependency_changes: 0};
  load(JSON.stringify(aggregate));
  assert.match(nodes["panel-drift"].textContent, /Detailed evidence is withheld/);
  assert.match(nodes["result-heading"].textContent, /3changes/);
});

test("oversize selection is rejected before reading", () => {
  const {nodes, load} = boot(); load("DO NOT READ", 17 * 1024 * 1024);
  assert.match(nodes["status-message"].textContent, /16 MiB/);
  assert.equal(nodes["loaded-state"].hidden, true);
});

for (const comparison of ["incomplete", "not_run"]) test(`subtitle preserves ${comparison} comparison status`, () => {
  const {nodes, context, load} = boot();
  const payload = JSON.parse(vm.runInContext("JSON.stringify(SentinelExample)", context));
  payload.result.comparison = comparison; payload.result.outcome = "incomplete"; payload.result.exit_code = 2;
  load(JSON.stringify(payload));
  assert.doesNotMatch(nodes["result-heading"].textContent, /Compared with baseline/);
  assert.match(nodes["result-heading"].textContent, comparison === "incomplete" ? /Incomplete comparison/ : /Comparison not run/);
});

test("malformed UTF-8 is rejected without replacing a source identity", () => {
  const {nodes, load} = boot(); load(new Uint8Array([0xff, 0xfe, 0x22]).buffer, 3);
  assert.match(nodes["status-message"].textContent, /not valid UTF-8/);
  assert.equal(nodes["loaded-state"].hidden, true);
});
