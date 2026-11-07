"use strict";

// Everything from the server goes in through textContent. The one exception is
// the lesson preview, which the server renders with raw HTML disabled.
const app = document.getElementById("app");
const STEP_LABELS = {
  research: "Research", generate: "Lesson generation",
  qc_factual: "QC Layer 1: research and factual accuracy",
  qc_instructional: "QC Layer 2: instructional quality",
};
const TABS = [["workflow", "Workflow"], ["sources", "Sources"], ["edit", "Edit"],
  ["qc_factual", "QC 1: Facts"], ["qc_instructional", "QC 2: Teaching"],
  ["preview", "Preview"], ["history", "History"]];

let lessonId = null, state = null, tab = "workflow", pollTimer = null, dirty = false;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== false && value != null) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child != null && child !== false) node.append(child);
  }
  return node;
}

function toast(message, bad = false) {
  const box = document.getElementById("toast");
  box.textContent = message;
  box.className = bad ? "bad" : "";
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, bad ? 9000 : 3500);
}

async function api(path, options = {}) {
  const init = { ...options };
  if (init.body !== undefined) {
    init.body = JSON.stringify(init.body);
    init.headers = { "Content-Type": "application/json" };
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new Error("Could not reach the server. Your text is still here; try again.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

async function act(fn, success) {
  try {
    const result = await fn();
    if (success) toast(success);
    await refresh(true);
    return result;
  } catch (error) {
    toast(error.message, true);
  }
}

// ---- home ---------------------------------------------------------------
async function showHome() {
  lessonId = null; state = null; clearTimeout(pollTimer);
  location.hash = "";
  app.replaceChildren(el("p", { class: "muted" }, "Loading..."));
  const lessons = await api("/api/lessons").catch(() => []);

  const title = el("input", { id: "title", maxlength: 200, placeholder: "HTTP Requests and Responses" });
  const objective = el("textarea", {
    id: "objective", maxlength: 1000, style: "min-height:90px;font-family:inherit",
    placeholder: "Explain GET and POST methods and distinguish between 2xx, 4xx, and 5xx response status categories.",
  });
  const error = el("p", { class: "error", role: "alert" });
  const submit = el("button", { class: "primary", type: "submit" }, "Research and generate lesson");

  const form = el("form", {
    class: "panel", onsubmit: async (event) => {
      event.preventDefault();
      error.textContent = "";
      if (!title.value.trim() || !objective.value.trim()) {
        error.textContent = "Enter both a lesson title and a learning objective.";
        return;
      }
      submit.disabled = true;
      try {
        const created = await api("/api/lessons", { method: "POST", body: { title: title.value, objective: objective.value } });
        location.hash = `lesson/${created.id}`;
      } catch (failure) {
        error.textContent = failure.message;
        submit.disabled = false;
      }
    },
  },
    el("h1", {}, "New lesson"),
    el("p", { class: "muted" }, "Creates a researched 45-minute lesson and audits it with two independent QC layers."),
    el("label", { for: "title" }, "Lesson title"), title,
    el("label", { for: "objective" }, "Learning objective"), objective,
    error, submit);

  const list = lessons.length ? el("div", { class: "panel" }, el("h2", { style: "margin-top:0" }, "Lessons"),
    lessons.map((lesson) => el("div", { class: "step" },
      el("div", { class: "body" },
        el("a", { href: `#lesson/${lesson.id}` }, lesson.title),
        el("div", { class: "muted" }, lesson.objective)),
      el("span", { class: "muted" }, lesson.latest_version ? `v${lesson.latest_version}` : "no draft")))) : null;
  app.replaceChildren(form, list || "");
}

// ---- lesson workspace ----------------------------------------------------
async function refresh(force = false) {
  if (!lessonId) return;
  clearTimeout(pollTimer);
  try {
    const next = await api(`/api/lessons/${lessonId}`);
    const moved = !state || next.version !== state.version;
    const changed = JSON.stringify(next) !== JSON.stringify(state);
    state = next;
    // Never redraw the editor under someone who is typing.
    if ((force || changed) && !(tab === "edit" && dirty && !moved)) render();
  } catch (error) {
    if (error.status === 404) { toast("Lesson not found.", true); showHome(); return; }
    toast(error.message, true);
  }
  if (state && state.running) pollTimer = setTimeout(refresh, 1500);
}

function render() {
  const { lesson, version, time_budget: budget } = state;
  const header = el("div", { class: "row spread" },
    el("div", { style: "min-width:0;flex:1" },
      el("h1", {}, lesson.title),
      el("div", { class: "muted" }, lesson.objective)),
    el("div", { class: "row" },
      version ? el("span", { class: "muted" }, `Version ${version}`) : null,
      budget ? el("span", { class: `badge ${budget.within_limit ? "done" : "failed"}` }, `${budget.total_minutes} / ${budget.limit} min`) : null,
      version ? el("a", { class: "button primary", href: `/api/lessons/${lessonId}/export` }, "Export ZIP") : null));

  const tabs = el("div", { class: "tabs", role: "tablist" }, TABS.map(([id, label]) =>
    el("button", {
      class: id === tab ? "active" : "", role: "tab", "aria-selected": String(id === tab),
      onclick: () => {
        if (tab === "edit" && dirty && !confirm("Leave the editor? Your unsaved text stays as a browser draft.")) return;
        tab = id; render();
      },
    }, label)));

  const views = {
    workflow: viewWorkflow, sources: viewSources, edit: viewEdit, qc_factual: viewFactual,
    qc_instructional: viewInstructional, preview: viewPreview, history: viewHistory,
  };
  app.replaceChildren(header, tabs, views[tab]());
}

function viewWorkflow() {
  return el("div", { class: "panel" }, state.steps.map((step) => {
    const canRetry = !state.running && (step.status === "failed" || step.status === "done");
    return el("div", { class: "step" },
      el("span", { class: `badge ${step.status}` }, step.status),
      el("div", { class: "body" },
        el("h3", {}, STEP_LABELS[step.name]),
        step.detail ? el("div", { class: "muted" }, step.detail) : null,
        step.error ? el("div", { class: "error" }, step.error) : null,
        step.attempts > 1 ? el("div", { class: "muted" }, `Attempts: ${step.attempts}`) : null),
      canRetry ? el("button", { onclick: () => retry(step.name) }, step.status === "failed" ? "Retry" : "Run again") : null);
  }));
}

async function retry(name, confirmed = false) {
  try {
    await api(`/api/lessons/${lessonId}/steps/${name}/retry`, { method: "POST", body: { confirm: confirmed } });
    await refresh(true);
  } catch (error) {
    if (error.status === 409 && !confirmed && error.message.includes("new version")) {
      if (confirm(error.message + " Continue?")) retry(name, true);
    } else toast(error.message, true);
  }
}

function viewSources() {
  const { sources, research } = state;
  if (!sources.length) return el("div", { class: "panel muted" }, "No research saved yet.");
  const url = el("input", { placeholder: "https://www.youtube.com/watch?v=...", "aria-label": "YouTube link" });
  const groups = [["Websites", (s) => s.kind === "web"], ["Books", (s) => s.kind === "book"], ["YouTube videos", (s) => s.kind === "video"]];
  return el("div", {},
    (research.warnings || []).map((text) => el("div", { class: "banner" }, text)),
    groups.map(([label, match]) => {
      const rows = sources.filter(match);
      return el("div", { class: "panel" }, el("h2", { style: "margin-top:0" }, `${label} (${rows.length})`),
        rows.length ? el("div", { class: "scroll" }, el("table", {},
          el("thead", {}, el("tr", {}, ["ID", "Source", "Reliability", "Usable as evidence"].map((h) => el("th", {}, h)))),
          el("tbody", {}, rows.map((source) => el("tr", {},
            el("td", {}, source.id),
            el("td", {}, el("a", { href: safeUrl(source.url), target: "_blank", rel: "noopener noreferrer" }, source.title),
              el("div", { class: "muted" }, [source.author, source.channel, source.year].filter(Boolean).join(", "))),
            el("td", {}, `Tier ${source.tier}: ${source.tier_reason}`),
            el("td", {}, source.evidence ? `Yes (${source.text_chars.toLocaleString()} characters)` : "No, metadata only")))))) :
          el("p", { class: "muted" }, "None found."));
    }),
    el("div", { class: "panel" },
      el("h2", { style: "margin-top:0" }, "Add a YouTube video"),
      el("div", { class: "row" }, el("div", { style: "flex:1;min-width:220px" }, url),
        el("button", { onclick: () => act(() => api(`/api/lessons/${lessonId}/videos`, { method: "POST", body: { url: url.value } }), "Video added") }, "Add"))),
    el("details", { class: "panel" }, el("summary", {}, "Research log"),
      el("div", { class: "scroll" }, el("table", {},
        el("thead", {}, el("tr", {}, ["Connector", "Query", "Found", "Error"].map((h) => el("th", {}, h)))),
        el("tbody", {}, (research.diagnostics || []).map((row) => el("tr", {},
          el("td", {}, row.connector), el("td", {}, row.query), el("td", {}, String(row.found)),
          el("td", { class: "error" }, row.error || ""))))))));
}

function safeUrl(url) {
  return /^https?:\/\//i.test(url) ? url : "#";
}

// ---- editing --------------------------------------------------------------
const draftKey = () => `lessonforge-draft-${lessonId}`;

function readDraft() {
  try { return JSON.parse(localStorage.getItem(draftKey())); } catch { return null; }
}
function writeDraft(base, sections) {
  try { localStorage.setItem(draftKey(), JSON.stringify({ base, sections })); } catch { /* storage may be unavailable */ }
}
function clearDraft() {
  try { localStorage.removeItem(draftKey()); } catch { /* storage may be unavailable */ }
}

function viewEdit() {
  if (!state.content) return el("div", { class: "panel muted" }, "There is no lesson to edit yet.");
  const draft = readDraft();
  const sections = structuredClone(draft ? draft.sections : state.content.sections);
  dirty = Boolean(draft);
  // Fixed when editing starts. If the lesson moves on, saving must conflict
  // rather than silently overwrite the newer version.
  const base = draft ? draft.base : state.version;
  const note = el("input", { placeholder: "What changed? (optional)", "aria-label": "Change note" });
  const touch = () => { dirty = true; writeDraft(base, sections); };

  const save = async () => {
    try {
      const saved = await api(`/api/lessons/${lessonId}/content`, {
        method: "PUT", body: { base_version: base, sections, note: note.value || "Instructor edit" },
      });
      clearDraft(); dirty = false;
      toast(`Saved as version ${saved.version}`);
      await refresh(true);
    } catch (error) { toast(error.message, true); }
  };
  const discard = () => {
    if (!confirm("Discard your unsaved changes?")) return;
    clearDraft(); dirty = false; render();
  };

  return el("div", {},
    draft ? el("div", { class: "banner" }, `Restored an unsaved draft based on version ${draft.base}.`) : null,
    state.reports.qc_factual || state.reports.qc_instructional ? el("p", { class: "muted" }, "Saving creates a new version. Earlier versions stay in History.") : null,
    sections.map((section) => el("div", { class: "panel" },
      el("div", { class: "grid2" },
        el("div", {}, el("label", { style: "margin-top:0" }, `${section.kind} section title`),
          el("input", { value: section.title, oninput: (e) => { section.title = e.target.value; touch(); } })),
        el("div", {}, el("label", { style: "margin-top:0" }, "Minutes"),
          el("input", { type: "number", min: 1, value: section.minutes, oninput: (e) => { section.minutes = Math.max(1, Number(e.target.value) || 1); touch(); } }))),
      el("label", {}, "Content (Markdown)"),
      el("textarea", { oninput: (e) => { section.body = e.target.value; touch(); } }, section.body))),
    el("div", { class: "panel row" }, el("div", { style: "flex:1;min-width:220px" }, note),
      el("button", { class: "primary", onclick: save }, "Save version"),
      draft ? el("button", { onclick: discard }, "Discard draft") : null));
}

// ---- QC --------------------------------------------------------------------
function reportHeader(name, report) {
  const step = state.steps.find((s) => s.name === name);
  return el("div", {},
    step.status === "failed" ? el("div", { class: "banner bad" }, `This layer failed: ${step.error}`) : null,
    report.stale ? el("div", { class: "banner" }, `This report reviewed version ${report.lesson_version}, and the lesson is now version ${state.version}.`) : null,
    el("div", { class: "row spread" }, el("p", {}, report.summary),
      el("button", { disabled: state.running, onclick: () => retry(name) }, "Rerun on latest version")));
}

function missingReport(name) {
  const step = state.steps.find((s) => s.name === name);
  return el("div", { class: "panel" },
    step.status === "failed" ? el("p", { class: "error" }, step.error) : el("p", { class: "muted" }, `Status: ${step.status}`),
    step.status === "failed" ? el("button", { onclick: () => retry(name) }, "Retry") : null);
}

function issueList(name, report) {
  const order = { high: 0, medium: 1, low: 2 };
  const issues = [...report.issues].sort((a, b) => order[a.severity] - order[b.severity]);
  if (!issues.length) return el("p", { class: "muted" }, "No issues were found.");
  const base = `/api/lessons/${lessonId}/reports/${name}/issues`;
  return issues.map((issue) => {
    const open = issue.status === "open" || issue.status === "stale";
    return el("div", { class: `issue ${open ? "" : "closed"}` },
      el("div", { class: "row" },
        el("span", { class: `badge ${issue.severity}` }, issue.severity),
        el("b", {}, issue.category.replaceAll("_", " ")),
        issue.section_id ? el("span", { class: "muted" }, `Section ${issue.section_id}`) : null,
        el("span", { class: "muted" }, issue.origin),
        issue.status !== "open" ? el("span", { class: `badge ${issue.status}` }, issue.status) : null),
      el("p", { style: "margin:.4rem 0" }, issue.description),
      el("p", { class: "muted", style: "margin:.2rem 0" }, issue.suggestion),
      issue.find ? el("div", { class: "diff del" }, issue.find) : null,
      issue.replace ? el("div", { class: "diff add" }, issue.replace) : null,
      el("div", { class: "row", style: "margin-top:.5rem" },
        open && issue.applicable ? el("button", { class: "primary", onclick: () => act(() => api(`${base}/${issue.id}/apply`, { method: "POST" }), "Recommendation applied") }, "Apply fix") : null,
        open && !issue.applicable ? el("span", { class: "muted" }, "Needs a manual edit") : null,
        open ? el("button", { onclick: () => act(() => api(`${base}/${issue.id}/status`, { method: "POST", body: { status: "dismissed" } })) }, "Dismiss") : null,
        issue.status === "dismissed" ? el("button", { onclick: () => act(() => api(`${base}/${issue.id}/status`, { method: "POST", body: { status: "open" } })) }, "Reopen") : null));
  });
}

function stat(value, label) {
  return el("div", { class: "stat" }, el("b", {}, String(value)), el("span", { class: "muted" }, label));
}

function viewFactual() {
  const report = state.reports.qc_factual;
  if (!report) return missingReport("qc_factual");
  const counts = report.verdict_counts;
  return el("div", {},
    reportHeader("qc_factual", report),
    el("div", { class: "stats" },
      stat(report.support_rate == null ? "n/a" : `${report.support_rate}%`, `of ${report.claims_total} claims supported`),
      stat(counts.contradicted || 0, "contradicted"),
      stat((counts.unsupported || 0) + (counts.unverified || 0), "unsupported or unverified"),
      stat(report.issues.filter((i) => i.status === "open").length, "open issues")),
    (report.notes || []).map((text) => el("div", { class: "banner" }, text)),
    el("h2", {}, "Issues and recommendations"), issueList("qc_factual", report),
    el("h2", {}, "Claim verification"),
    el("div", { class: "panel scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Claim", "Verdict", "Evidence"].map((h) => el("th", {}, h)))),
      el("tbody", {}, report.claims.map((claim) => el("tr", {},
        el("td", {}, claim.claim, el("div", { class: "muted" }, `Section ${claim.section_id}`)),
        el("td", {}, el("span", { class: `badge ${claim.verdict}` }, claim.verdict.replaceAll("_", " ")),
          claim.found_by ? el("div", { class: "muted" }, claim.found_by) : null),
        el("td", {}, claim.evidence_quote ? `${claim.evidence_source}: "${claim.evidence_quote}"` : claim.explanation)))))),
    el("h2", {}, "Source audit"),
    el("div", { class: "panel scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ["ID", "Source", "Tier", "Citations", "Link"].map((h) => el("th", {}, h)))),
      el("tbody", {}, report.source_audit.map((row) => el("tr", {},
        el("td", {}, row.id), el("td", {}, row.title), el("td", {}, String(row.tier)),
        el("td", {}, String(row.citations)), el("td", { class: row.link }, row.link)))))));
}

function viewInstructional() {
  const report = state.reports.qc_instructional;
  if (!report) return missingReport("qc_instructional");
  const budget = report.time_budget;
  return el("div", {},
    reportHeader("qc_instructional", report),
    el("div", { class: "stats" },
      stat(`${report.average_score} / 5`, "average rubric score"),
      stat(`${budget.total_minutes} min`, `of ${budget.limit} allowed`),
      stat(report.objective_coverage.filter((c) => c.coverage === "strong").length + ` / ${report.objective_coverage.length}`, "sub-objectives strongly covered"),
      stat(report.issues.filter((i) => i.status === "open").length, "open issues")),
    el("h2", {}, "Issues and recommendations"), issueList("qc_instructional", report),
    el("h2", {}, "Objective coverage"),
    el("div", { class: "panel scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Sub-objective", "Coverage", "Where", "Assessed", "Note"].map((h) => el("th", {}, h)))),
      el("tbody", {}, report.objective_coverage.map((row) => el("tr", {},
        el("td", {}, row.subobjective), el("td", {}, el("span", { class: `badge ${row.coverage}` }, row.coverage)),
        el("td", {}, row.where), el("td", {}, row.assessed_by_check ? "Yes" : "No"), el("td", {}, row.note)))))),
    el("h2", {}, "Rubric"),
    el("div", { class: "panel scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Criterion", "Score", "Rationale", "Weakness"].map((h) => el("th", {}, h)))),
      el("tbody", {}, report.rubric.map((row) => el("tr", {},
        el("td", {}, row.criterion.replaceAll("_", " ")), el("td", {}, `${row.score} / 5`), el("td", {}, row.rationale), el("td", {}, row.weakness)))))),
    el("h2", {}, "Time budget"),
    el("div", { class: "panel scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Section", "Planned", "Reading", "Counted"].map((h) => el("th", {}, h)))),
      el("tbody", {}, budget.sections.map((row) => el("tr", {},
        el("td", {}, row.title), el("td", {}, `${row.declared} min`), el("td", {}, `${row.reading} min`), el("td", {}, `${row.effective} min`)))))),
    el("h2", {}, "Structure checks"),
    el("div", { class: "panel" }, report.structure_checks.map((row) =>
      el("div", {}, el("span", { class: row.passed ? "done" : "failed" }, row.passed ? "Pass" : "Fail"), `: ${row.check}`))));
}

function viewPreview() {
  const box = el("div", { class: "panel lesson" }, "Loading preview...");
  if (!state.content) { box.textContent = "There is no lesson to preview yet."; return box; }
  api(`/api/lessons/${lessonId}/preview`)
    .then((data) => { box.innerHTML = data.html; })
    .catch((error) => { box.textContent = error.message; });
  return box;
}

function viewHistory() {
  if (!state.versions.length) return el("div", { class: "panel muted" }, "No versions yet.");
  return el("div", { class: "panel" }, state.versions.map((version) =>
    el("div", { class: "step" },
      el("span", { class: "badge pending" }, `v${version.version}`),
      el("div", { class: "body" }, el("b", {}, version.note),
        el("div", { class: "muted" }, `${version.author}, ${new Date(version.created_at).toLocaleString()}`)),
      version.version !== state.version ? el("button", {
        onclick: () => act(() => api(`/api/lessons/${lessonId}/versions/${version.version}/restore`, { method: "POST" }), "Version restored"),
      }, "Restore") : el("span", { class: "muted" }, "Current"))));
}

// ---- routing ---------------------------------------------------------------
async function route() {
  const match = location.hash.match(/^#lesson\/(\d+)$/);
  if (match) {
    lessonId = Number(match[1]); state = null; tab = "workflow"; dirty = false;
    app.replaceChildren(el("p", { class: "muted" }, "Loading..."));
    await refresh(true);
  } else {
    await showHome();
  }
}

async function showStatus() {
  const box = document.getElementById("status");
  try {
    const status = await api("/api/status");
    const parts = [status.llm_ready ? `LLM: ${status.provider}` : "LLM not configured",
      status.web_search ? "web search on" : "web search off",
      status.youtube_api ? "YouTube API on" : "YouTube API off"];
    box.textContent = parts.join(" · ");
    if (!status.llm_ready) box.className = "error";
  } catch { box.textContent = "Server unreachable"; }
}

window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", (event) => { if (dirty) event.preventDefault(); });
document.getElementById("home").addEventListener("click", (event) => { event.preventDefault(); location.hash = ""; showHome(); });
showStatus();
route();
