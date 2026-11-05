(function () {
  "use strict";
  const C = window.SentinelContract;
  const byId = id => document.getElementById(id);
  const state = {result: null, reader: null, synthetic: false, tab: "drift"};
  const statusWords = {complete: "Complete", findings: "Findings", error: "Failed", incomplete: "Incomplete", unconfigured: "Unconfigured", not_run: "Not run", not_asked: "Not asked", answered: "Answered", failed: "Failed", unavailable: "Unavailable", high: "High", medium: "Medium", low: "Low", info: "Info"};
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function badge(status) {
    const tone = ["error", "failed", "high"].includes(status) ? "bad" : ["findings", "incomplete", "medium", "unconfigured", "not_run", "unavailable"].includes(status) ? "warning" : ["complete", "answered"].includes(status) ? "good" : "neutral";
    return el("span", statusWords[status] || status, "badge " + tone);
  }
  function facts(entries, className = "facts") {
    const list = el("dl", null, className);
    for (const [label, value, mono] of entries) {
      list.append(el("dt", label), el("dd", value === "" || value === null || value === undefined ? "Not recorded" : value, mono ? "mono" : ""));
    }
    return list;
  }
  function notice(title, message, className = "") {
    const node = el("div", null, "notice " + className);
    node.append(el("strong", title), el("span", message));
    return node;
  }
  function empty(title, message) {
    const node = el("div", null, "empty-note"); node.append(el("strong", title), el("span", message)); return node;
  }
  function intro(title, message) {
    const node = el("div", null, "section-intro"), copy = el("div");
    copy.append(el("h2", title), el("p", message)); node.append(copy); return node;
  }
  function shownTime(value) {
    const date = new Date(value);
    return value && !Number.isNaN(date.getTime()) ? date.toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"}) : "Timestamp unavailable";
  }
  function message(text, mode = "") {
    const node = byId("status-message"); node.textContent = text; node.className = "message " + mode;
    node.hidden = !text; node.setAttribute("role", mode === "error" ? "alert" : "status");
  }
  function evidenceAvailable() { return state.result.schema_version === 3 && state.result.evidence.availability === "captured"; }
  function evidenceMissing() {
    const result = state.result;
    if (result.schema_version < 3) return empty("Detailed evidence was not captured", "This historical result predates captured questions and answers. Its counts and coverage are still available; today's configuration cannot fill the gap.");
    if (result.evidence.failure) return empty("Evidence capture exceeded its limit", "The scan retained an explicit capture failure. No shortened question or partial identity is presented as a complete record.");
    if (result.evidence.availability === "withheld") return empty("Detailed evidence is withheld", "This export contains aggregate statuses and counts. Open an explicitly selected local-detail export to inspect individual measurements and answers.");
    return empty("Evidence is unavailable", "This result has no complete captured evidence. Review its recorded coverage; an empty list does not mean that no findings exist.");
  }
  function paginate(container, rows, renderRow, emptyTitle, emptyMessage) {
    let currentPage = 0;
    function render(focusDirection = "") {
      container.replaceChildren();
      if (!rows.length) { container.append(empty(emptyTitle, emptyMessage)); return; }
      const page = C.pageRows(rows, currentPage); currentPage = page.page;
      container.append(el("p", `${page.page * C.PAGE_SIZE + 1}–${page.page * C.PAGE_SIZE + page.rows.length} of ${page.total}`, "list-count"));
      const list = el("div", null, "evidence-list"); page.rows.forEach((row, index) => list.append(renderRow(row, index))); container.append(list);
      if (page.pages > 1) {
        const pager = el("div", null, "pager"), previous = el("button", "Previous"), next = el("button", "Next");
        previous.type = next.type = "button"; previous.disabled = page.page === 0; next.disabled = page.page + 1 === page.pages;
        previous.addEventListener("click", () => { currentPage--; render("previous"); container.scrollIntoView({block: "nearest"}); });
        next.addEventListener("click", () => { currentPage++; render("next"); container.scrollIntoView({block: "nearest"}); });
        pager.append(previous, el("span", `Page ${page.page + 1} of ${page.pages}`), next); container.append(pager);
        if (focusDirection) { const target = focusDirection === "next" ? next : previous; (target.disabled ? (target === next ? previous : next) : target).focus(); }
      }
    }
    render();
  }
  function record(title, identity, status, description, renderBody) {
    const details = el("details", null, "finding"), summary = el("summary"), text = el("div");
    text.append(el("span", title, "finding-title"));
    if (identity) text.append(el("span", identity, "finding-id mono"));
    if (description) text.append(el("span", description, "finding-meta"));
    summary.append(text, badge(status)); details.append(summary);
    let built = false;
    details.addEventListener("toggle", () => {
      if (details.open && !built) { built = true; const body = el("div", null, "finding-body"); renderBody(body); details.append(body); }
    });
    return details;
  }
  function measurement(label, numeric, text, present) {
    const box = el("div", null, "measurement"); box.append(el("span", label));
    if (!present) box.append(el("code", "Not present"));
    else {
      if (numeric !== null) box.append(el("code", numeric));
      if (text) box.append(el("code", text));
      if (numeric === null && !text) box.append(el("code", "Presence recorded; no value"));
    }
    return box;
  }
  function answer(lens, rating, winner) {
    const section = el("div", null, "answer" + (winner ? " winner" : ""));
    const heading = el("h3", lens.title || lens.id); heading.append(badge(rating.severity));
    if (winner) heading.append(el("span", "Final attribution", "badge neutral"));
    section.append(heading, el("p", "Configured question", "question-label"), el("p", lens.question));
    section.append(el("p", "Recorded rating", "question-label"), el("p", rating.note || "No note was returned."));
    section.append(facts([["Lens", lens.id, true], ["Declaration", lens.source, true], ["Application", rating.applied ? "Applied when processed" : "Did not replace the existing rating"]]));
    return section;
  }
  function changeRecord(change) {
    const labels = {appeared: "Appeared", disappeared: "Disappeared", grew: "Grew", shrank: "Shrank", changed: "Changed"};
    const title = change.label || (change.structural && change.structural.identity) || change.key;
    return record(title, change.probe_id + " / " + change.key, change.severity, labels[change.kind], body => {
      const pair = el("div", null, "measurements");
      pair.append(measurement(`Before · baseline ${state.result.baseline_run_id ?? "unavailable"}`, change.before, change.before_state, change.kind !== "appeared"), measurement(`After · run ${state.result.run_id ?? "unavailable"}`, change.after, change.after_state, change.kind !== "disappeared"));
      body.append(pair);
      if (change.structural) {
        const entries = C.STRUCTURAL_KEYS.filter(key => change.structural[key]).map(key => [key.replaceAll("_", " "), change.structural[key], true]);
        if (entries.length) body.append(facts(entries));
      }
      if (change.note) body.append(el("p", change.note));
      const rows = state.result.evidence.lenses.flatMap(lens => lens.ratings.filter(rating => rating.probe_id === change.probe_id && rating.key === change.key).map(rating => ({lens, rating})));
      if (!rows.length) { body.append(empty("Mechanical severity", "No recorded model rating was applied to this measurement. Check Questions & answers for not-asked or failed lenses.")); return; }
      body.append(el("h3", "Answers attached to this measurement"));
      const answers = el("div"); body.append(answers);
      paginate(answers, rows, row => answer(row.lens, row.rating, row.rating.applied && row.lens.id === change.lens_id && row.rating.severity === change.severity && row.rating.note === change.note), "No answers", "No attached answer was captured.");
    });
  }
  function gateRecord(finding) {
    const label = finding.failing === null ? "Partial decision; gate did not finalize this finding" : finding.failing ? "Fails the gate" : "Does not fail the gate";
    return record(finding.subject || finding.key, finding.gate + " / " + finding.key, finding.decision.status === "failed" ? "failed" : finding.failing === true ? "findings" : finding.failing === null ? "incomplete" : "info", label, body => {
      const entries = [["Kind", finding.kind], ["Verdict", finding.verdict || "No recorded verdict"], ["Decision origin", finding.decision.origin.replaceAll("_", " ")], ["Question status", statusWords[finding.decision.status]], ["Declaration", finding.decision.source, true]];
      if (finding.risk) entries.push(["Exposure / risk", finding.risk]);
      if (finding.matches !== null) entries.push(["Candidate matches", finding.matches]);
      if (finding.spdx) entries.push(["Licence identifier", finding.spdx, true]);
      if (finding.suspect) entries.push(["Selected suspect", finding.suspect, true]);
      body.append(facts(entries));
      if (finding.decision.question) { body.append(el("h3", "Configured question"), el("p", finding.decision.question)); }
      if (finding.reason) body.append(el("h3", finding.decision.origin === "model" ? "Recorded answer" : "Recorded decision"), el("p", finding.reason));
      if (finding.decision.status === "failed") body.append(empty("No usable answer recorded", "This decision failed. It must not be read as a clean finding or a model verdict."));
      if (finding.decision.origin === "unknown") body.append(el("p", "The decision origin was not captured. Do not infer a model review from the verdict text."));
    });
  }
  function searchable(panel, rows, kind, renderRow) {
    const toolbar = el("div", null, "toolbar"), searchLabel = el("label", "Search evidence"), input = el("input"), filterLabel = el("label", kind === "drift" ? "Severity" : "Finding status"), select = el("select"), output = el("div");
    input.type = "search"; input.placeholder = kind === "drift" ? "Probe, path, identity or recorded note" : "Gate, subject or recorded reason";
    const choices = kind === "drift" ? [["all", "All severities"], ...["high", "medium", "low", "info"].map(value => [value, statusWords[value]])] : [["all", "All findings"], ["failing", "Failing findings"], ["partial", "Partial decisions"]];
    choices.forEach(([value, label]) => { const option = el("option", label); option.value = value; select.append(option); });
    select.value = "all";
    searchLabel.append(input); filterLabel.append(select); toolbar.append(searchLabel, filterLabel); panel.append(toolbar, output);
    let timer;
    const refresh = () => paginate(output, C.filterRows(rows, input.value, select.value), renderRow, "No matching evidence", "Try a different search or filter. This does not change the recorded result.");
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(refresh, 120); }); select.addEventListener("change", refresh); refresh();
  }
  function renderDrift() {
    const panel = byId("panel-drift"), result = state.result; panel.replaceChildren(intro("What moved since the baseline", "Measured changes stay separate from standing gate findings. Expand a record to trace its evidence."));
    if (result.comparison !== "compared") panel.append(notice("Comparison: " + result.comparison.replaceAll("_", " "), result.comparison === "no_baseline" ? "This run had no earlier baseline to compare against. It cannot establish that the project was unchanged." : "The recorded comparison is unavailable or incomplete. Any retained measurements do not make it complete."));
    if (!evidenceAvailable()) { panel.append(evidenceMissing()); return; }
    if (result.evidence.withheld_dependency_changes) panel.append(notice("Dependency details withheld", `${result.evidence.withheld_dependency_changes} dependency-version changes are omitted from this local-detail evidence contract.`));
    if (!result.evidence.changes.length) { panel.append(empty(result.comparison === "compared" ? "No baseline changes recorded" : "No comparison findings to inspect", "Read the comparison and coverage statuses before interpreting this empty list.")); return; }
    searchable(panel, result.evidence.changes, "drift", changeRecord);
  }
  function renderGates() {
    const panel = byId("panel-gates"), result = state.result; panel.replaceChildren(intro("What was true at this scan", "Gate findings describe state. Accepting a baseline does not clear them."));
    const stages = el("div", null, "status-list"); panel.append(stages, el("br")); paginate(stages, result.gates, stage => { const row = el("div", null, "status-row"); row.append(el("span", stage.id), badge(stage.status)); return row; }, "No gate stages recorded", "No state-gate coverage is represented here.");
    if (!evidenceAvailable()) { panel.append(evidenceMissing()); return; }
    if (!result.evidence.gates.length) { panel.append(empty("No individual gate findings recorded", "Unconfigured, failed and not-run gates are not evidence of a clean tree. Their statuses are shown above.")); return; }
    searchable(panel, result.evidence.gates, "gates", gateRecord);
  }
  function renderQuestions() {
    const panel = byId("panel-questions"), result = state.result; panel.replaceChildren(intro("Questions in force for this run", "Captured declarations and validated answers. These are historical records, not today's configuration."));
    if (!evidenceAvailable()) { panel.append(evidenceMissing()); return; }
    const list = el("div"); panel.append(list);
    paginate(list, result.evidence.lenses, lens => record(lens.title || lens.id, lens.id, lens.status, lens.watch.length ? "Watches " + lens.watch.slice(0, 4).join(", ") + (lens.watch.length > 4 ? " and additional probes" : "") : "Watches all probes", body => {
      body.append(el("h3", "Configured question"), el("p", lens.question), facts([["Declared in", lens.source, true], ["Resolved ratings", lens.ratings.length], ["Dropped references", lens.dropped_ratings]]));
      if (lens.status === "answered") body.append(el("h3", "Recorded assessment"), el("p", lens.assessment || "The judge returned no overall assessment text."));
      else body.append(empty(lens.status === "failed" ? "The question did not receive a usable answer" : "This question was not asked", lens.status === "failed" ? "A failure is not a judgement that nothing matters." : "No answer was needed for this lens on the recorded scan. No assessment is invented."));
      for (const rating of lens.ratings) {
        const row = el("div", null, "answer"); row.append(el("h3", rating.probe_id + " / " + rating.key, "mono"), badge(rating.severity), el("p", rating.note || "No note returned."), el("p", rating.applied ? "Applied when processed; see the measurement for final attribution." : "Did not replace the stronger or equal rating already applied.", "question-label")); body.append(row);
      }
    }), "No lens questions recorded", "The scan did not capture any declared drift lenses. Gate questions, if present, are attached to their state findings.");
  }
  function stageSection(title, rows, fallback) {
    const section = el("section", null, "coverage-section"); section.append(el("h3", title));
    const output = el("div"); section.append(output);
    paginate(output, rows, stage => { const row = el("div", null, "status-row"); row.append(el("span", stage.id), badge(stage.status)); return row; }, "No stages recorded", fallback);
    return section;
  }
  function renderCoverage() {
    const panel = byId("panel-coverage"), result = state.result, p = result.provenance;
    panel.replaceChildren(intro("Coverage, source and known limits", "Completion describes the recorded work. It does not certify the application or its deployed state."));
    if (result.failed_stages.length) panel.append(notice("Failed stages", result.failed_stages.join(", ")));
    const grid = el("div", null, "coverage-grid");
    grid.append(stageSection("Measurements", result.probes, "No probe measurements are represented here."), stageSection("Judge requests", result.judges, "No judge stages were recorded."), stageSection("State gates", result.gates, "No gate stages were recorded."), stageSection("Longer-term horizons", result.horizons, "No horizons were recorded. This is not a completed long-term comparison.")); panel.append(grid);
    const details = el("details", null, "source-details"); details.open = true; details.append(el("summary", "Recorded source & environment"));
    details.append(facts([
      ["Captured", shownTime(p.captured_at)], ["Source kind", p.source_kind.replaceAll("_", " ")], ["Source revision", p.source_revision, true], ["Working tree", p.source_dirty === null ? "Unknown" : p.source_dirty ? "Dirty at capture" : "Clean at capture"],
      ["Source identity policy", p.source_identity_policy.replaceAll("_", " ")], ["Source identity hash", p.source_identity_sha256, true], ["Consistency", "Not atomic: files could change during measurement"],
      ["Scanner version", p.tool_version], ["Scanner Python", p.scanner_python], ["Scanner executable", p.scanner_executable, true], ["Judge configured name", p.judge_configured_name], ["Judge weights", "Unverified"], ["Build freshness", "Not assessed by run provenance; inspect artifact measurements for their stated scope"],
      ["Measured environment", result.measured_environment ? [result.measured_environment.python, result.measured_environment.executable, result.measured_environment.selection].filter(Boolean).join(" / ") || "Recorded, without these display fields" : "Not recorded in this profile"],
      ["Configuration fingerprint", p.config_sha256, true], ["Rules fingerprint", p.rules_sha256, true], ["Scope fingerprint", p.scope_sha256, true], ["Fingerprint boundary", "Behavior excluding authentication and opaque request fields"],
    ])); panel.append(details);
    panel.append(empty("Action reviews are outside this scan export", "No command journal or safety review history is loaded by this viewer. Absence here does not establish that actions were reviewed."));
  }
  function chooseTab(name, focus = false) {
    state.tab = name;
    document.querySelectorAll("[data-tab]").forEach(button => { const active = button.dataset.tab === name; button.setAttribute("aria-selected", String(active)); button.tabIndex = active ? 0 : -1; if (active && focus) button.focus(); byId("panel-" + button.dataset.tab).hidden = !active; });
  }
  function display(result, fileName, synthetic = false) {
    state.result = result; state.synthetic = synthetic;
    message(""); byId("empty-state").hidden = true; byId("loaded-state").hidden = false; byId("file-context").hidden = false; byId("clear-file").hidden = false;
    const context = byId("file-context"); context.replaceChildren(el("div", fileName, "file-name"), el("span", synthetic ? "Synthetic example" : `${result.profile} export · v${result.schema_version}`, "file-profile"), facts([["Recorded run", result.run_id ?? "Unavailable"], ["Baseline used", result.baseline_run_id ?? "None"], ["Captured", shownTime(result.generated_at)], ["Revision", result.provenance.source_revision ? result.provenance.source_revision.slice(0, 12) : "Not recorded", true]], "sidebar-data"));
    const heading = byId("result-heading"), top = el("div", null, "result-topline"), title = el("div");
    const baseline = result.baseline_run_id === null ? "unavailable" : result.baseline_run_id;
    const comparison = result.comparison === "compared" ? `Compared with baseline ${baseline}` : result.comparison === "incomplete" ? `Incomplete comparison · baseline ${baseline}` : result.comparison === "not_run" ? `Comparison not run · baseline ${baseline}` : "No baseline comparison";
    title.append(el("p", synthetic ? "Synthetic example / no real project data" : `Recorded run ${result.run_id ?? "unavailable"}`, "context-label"), el("h1", ({complete: "Scan complete", findings: "Findings recorded", incomplete: "Evidence is incomplete", error: "Scan did not complete"})[result.outcome]), el("p", shownTime(result.generated_at) + " · " + comparison, "result-subtitle"));
    top.append(title, badge(result.outcome));
    const metrics = el("div", null, "result-metrics");
    for (const [count, label] of [[result.counts.observations, "observations"], [result.counts.changes, "changes"], [result.counts.findings, "gate findings"], [result.counts.failing_findings, "failing gate findings"]]) { const metric = el("span", null, "metric"); metric.append(el("strong", count.toLocaleString()), document.createTextNode(label)); metrics.append(metric); }
    heading.replaceChildren(top, metrics);
    const notices = byId("result-notice"); notices.replaceChildren();
    if (synthetic) notices.append(notice("Synthetic demonstration", "These findings are invented to illustrate the viewer. They are not a scan of your project.", "synthetic"));
    if (["local", "local-detail"].includes(result.profile)) notices.append(notice("Sensitive local evidence", "Questions, answers and source identifiers can contain private text. This tab reads the file locally and sends nothing.", "sensitive"));
    else notices.append(notice("Detailed evidence is withheld", "This profile retains aggregate status and identity. A count without detail is not an empty finding list."));
    renderDrift(); renderGates(); renderQuestions(); renderCoverage(); chooseTab("drift");
  }
  function clear() {
    if (state.reader && state.reader.readyState === 1) state.reader.abort();
    state.reader = null; state.result = null; byId("evidence-file").value = "";
    byId("loaded-state").hidden = true; byId("empty-state").hidden = false; byId("file-context").hidden = true; byId("clear-file").hidden = true;
    for (const id of ["file-context", "result-heading", "result-notice", "panel-drift", "panel-gates", "panel-questions", "panel-coverage"]) byId(id).replaceChildren();
    message("");
  }
  function load(file) {
    clear();
    if (!file || file.size > C.FILE_LIMIT) { message("Choose one JSON export smaller than 16 MiB.", "error"); return; }
    message("Reading and validating local evidence…", "loading");
    const reader = new FileReader(); state.reader = reader;
    reader.addEventListener("load", () => {
      if (state.reader !== reader) return;
      try { display(C.decodeFile(reader.result), file.name); }
      catch (error) { message(error.message, "error"); }
    });
    reader.addEventListener("error", () => { if (state.reader === reader) message("The file could not be read. Choose the export again.", "error"); });
    reader.readAsArrayBuffer(file);
  }
  byId("evidence-file").addEventListener("change", event => load(event.target.files[0]));
  byId("clear-file").addEventListener("click", clear);
  byId("load-example").addEventListener("click", () => { clear(); display(C.validateResult(window.SentinelExample.result), "Synthetic service example", true); });
  const tabs = [...document.querySelectorAll("[data-tab]")];
  tabs.forEach((button, index) => {
    button.addEventListener("click", () => chooseTab(button.dataset.tab));
    button.addEventListener("keydown", event => { let next = null; if (event.key === "ArrowRight") next = (index + 1) % tabs.length; else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length; else if (event.key === "Home") next = 0; else if (event.key === "End") next = tabs.length - 1; if (next !== null) { event.preventDefault(); chooseTab(tabs[next].dataset.tab, true); } });
  });
  let dragDepth = 0;
  document.addEventListener("dragenter", event => { if (event.dataTransfer && [...event.dataTransfer.types].includes("Files")) { event.preventDefault(); dragDepth++; byId("drop-overlay").hidden = false; } });
  document.addEventListener("dragover", event => { event.preventDefault(); });
  document.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) byId("drop-overlay").hidden = true; });
  document.addEventListener("drop", event => { event.preventDefault(); dragDepth = 0; byId("drop-overlay").hidden = true; const files = event.dataTransfer ? event.dataTransfer.files : []; if (files.length === 1) load(files[0]); else { clear(); message("Drop one JSON export at a time.", "error"); } });
})();
