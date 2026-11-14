"use strict";
// Demo surface for the agent. No framework, no build step; every value from the
// network is inserted with textContent, never innerHTML.

const STATES = [
  ["PLANNED", "planned"], ["INTENT_RECORDED", "intent recorded"], ["SENT", "sent"],
  ["UNKNOWN", "outcome unknown"], ["VERIFYING", "checking"], ["VERIFIED", "verified"],
  ["FAILED_NOT_APPLIED", "not applied, retrying"], ["NEEDS_HUMAN", "needs a human"],
];
const COLORS = {
  PLANNED: "--planned", INTENT_RECORDED: "--intent", SENT: "--sent", UNKNOWN: "--unknown",
  VERIFYING: "--verifying", VERIFIED: "--verified", FAILED_NOT_APPLIED: "--retry", NEEDS_HUMAN: "--human",
};
const CHAOS = [
  ["Lose next refund response", "drop_response_after_commit", { system: "payments", count: 1 }],
  ["Fail next refund", "fail_before_commit", { system: "payments", count: 1 }],
  ["Slow payments (5 s)", "latency", { system: "payments", ms: 5000, count: 3 }],
  ["Payments API changes (v2)", "api_version", { system: "payments", version: 2 }],
  ["Edit attendee att_120", "mutate_record", { attendee_id: "att_120", fields: { email: "new@example.com" } }],
  ["Lose venue response", "drop_response_after_commit", { system: "venue", count: 1 }],
  ["Clear chaos", "clear", {}],
];

// Events that tell the self-healing story: [label, colour].
const HEAL = {
  DRIFT_DETECTED: ["API change detected", "amber"],
  MEMORY_RETRIEVED: ["remembered a similar fix", "blue"],
  ADAPTATION_PROPOSED: ["fix proposed", "blue"],
  CANARY_STARTED: ["testing fix (dry run)", "blue"],
  CANARY_FAILED: ["test failed, fix rejected", "red"],
  CANARY_PASSED: ["test passed", "green"],
  MEMORY_SAVED: ["lesson saved", "green"],
};

const $ = (id) => document.getElementById(id);
const app = { runId: null, es: null, seen: new Set(), busy: false };

function toast(msg) {
  $("toast").textContent = msg;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { $("toast").textContent = ""; }, 6000);
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, init);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
    throw new Error(detail);
  }
  return data;
}

async function act(label, fn) {
  try { await fn(); } catch (err) { toast(`${label}: ${err.message}`); }
  refresh();
}

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

const money = (minor) => `$${(minor / 100).toLocaleString("en-CA", { minimumFractionDigits: 2 })}`;

// --- grid ---

function buildLegend() {
  const legend = $("legend");
  for (const [state, label] of STATES) {
    const s = el("span", label);
    s.style.setProperty("--c", `var(${COLORS[state]})`);
    legend.append(s);
  }
}

function renderGrid(grid) {
  const host = $("grid");
  // A small demo event gets big cells so every state is easy to read.
  host.style.setProperty("--cols", grid.cells.length <= 40 ? 10 : 20);
  host.classList.toggle("big", grid.cells.length <= 40);
  $("grid-n").textContent = grid.cells.length;
  if (host.children.length !== grid.cells.length) {
    host.replaceChildren(...grid.cells.map(() => el("div", undefined, "cell")));
  }
  grid.cells.forEach((cell, i) => {
    const node = host.children[i];
    node.className = `cell ${cell.state}`;
    node.title = `${cell.attendee_id}: ${cell.state.replaceAll("_", " ").toLowerCase()}`;
  });
  $("venue-state").textContent = grid.venue ? grid.venue.replaceAll("_", " ").toLowerCase() : "-";
}

// --- belief vs reality ---

function truthRow(label, believed, real, fmt, opts = {}) {
  const tr = el("tr");
  tr.append(el("td", label));
  tr.append(el("td", believed === null ? "-" : fmt(believed)));
  tr.append(el("td", real === null || real === undefined ? "?" : fmt(real)));
  const verdict = el("td", "", "verdict");
  if (opts.alarmIfPositive) {
    if (real > 0) { verdict.textContent = "DUPLICATE MONEY MOVED"; verdict.classList.add("bad"); tr.className = "mismatch"; }
    else { verdict.textContent = "none"; verdict.classList.add("ok"); }
  } else if (believed !== null && real !== null && real !== undefined) {
    if (believed === real) { verdict.textContent = "matches"; verdict.classList.add("ok"); }
    else if (real > believed) { verdict.textContent = "done, not yet confirmed"; verdict.classList.add("warn"); }
    else { verdict.textContent = "BELIEF IS WRONG"; verdict.classList.add("bad"); tr.className = "mismatch"; }
  }
  tr.append(verdict);
  return tr;
}

function renderTruth(data) {
  const b = data.belief, r = data.reality;
  const count = (n) => String(n);
  const rows = [
    truthRow("Refunds issued", b.verified_refunds, r && r.payments.refund_count, count),
    truthRow("Money refunded", b.verified_minor, r && r.payments.total_minor, money),
    truthRow("Tickets cancelled", b.verified_cancellations, r && r.ticketing.cancelled, count),
    truthRow("Emails sent", b.verified_notifications, r && r.notifications.sent, count),
    truthRow("Venue released", b.venue_released ? 1 : 0, r && (r.venue.status === "cancelled" ? 1 : 0), (n) => (n ? "yes" : "no")),
    truthRow("Attendees refunded twice", null, r && r.payments.duplicate_refunds, count, { alarmIfPositive: true }),
  ];
  $("truth").tBodies[0].replaceChildren(...rows);
  if (r) {
    const note = el("tr");
    note.append(el("td", "Payments API version"), el("td", "-"), el("td", `v${r.payments_api_version}`), el("td", ""));
    $("truth").tBodies[0].append(note);
  }
  $("truth-error").textContent = data.reality_error ? `Cannot read the systems: ${data.reality_error}` : "";
}

// --- feed (SSE) ---

function addEvent(ev) {
  if (app.seen.has(ev._id)) return;
  app.seen.add(ev._id);
  if (HEAL[ev.type]) addChip(ev);
  const li = el("li", undefined, `${ev.level} t-${ev.type}`);
  const time = el("time", new Date(ev.ts).toLocaleTimeString("en-GB"));
  const body = el("div");
  body.append(el("span", ev.type, "type"), document.createTextNode(ev.message));
  li.append(time, body);
  const feed = $("feed");
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
  feed.append(li);
  while (feed.children.length > 300) feed.firstChild.remove();
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}

function addChip(ev) {
  const [label, colour] = HEAL[ev.type];
  const chips = $("heal-chips");
  const chip = el("span", label, `chip ${colour}`);
  chip.title = ev.message;
  chips.append(chip);
  while (chips.children.length > 10) chips.firstChild.remove();
  $("heal").hidden = false;
}

function clearHeal() {
  $("heal-chips").replaceChildren();
  $("heal").hidden = true;
}

function openStream(runId) {
  if (app.es) app.es.close();
  app.seen = new Set();
  $("feed").replaceChildren();
  clearHeal();
  app.es = new EventSource(`/events?run_id=${encodeURIComponent(runId)}`);
  app.es.onmessage = (m) => addEvent(JSON.parse(m.data));
}

// --- polling ---

function setPill(node, text, kind) {
  node.textContent = text;
  node.className = `pill ${kind || ""}`;
}

async function refresh() {
  if (app.busy) return;
  app.busy = true;
  try {
    if (!app.runId) {
      const { runs } = await api("/runs");
      if (runs.length) { app.runId = runs[0]._id; openStream(app.runId); }
    }
    const [agent, truth] = await Promise.all([api("/agent/status"), api(`/reality${app.runId ? `?run_id=${app.runId}` : ""}`)]);
    renderTruth(truth);
    if (agent.alive) setPill($("agent-pill"), `agent running (pid ${agent.pid})`, "ok");
    else setPill($("agent-pill"), "agent not running", "bad");
    $("btn-kill").disabled = !agent.alive;
    $("btn-start").disabled = agent.alive;
    if (app.runId) {
      const [run, grid] = await Promise.all([api(`/runs/${app.runId}`), api(`/runs/${app.runId}/grid`)]);
      renderGrid(grid);
      const done = run.state_counts.VERIFIED, total = Object.values(run.state_counts).reduce((a, b) => a + b, 0);
      setPill($("run-pill"), `${run.status.toLowerCase().replace("_", " ")} · ${done}/${total} verified`,
        run.status === "COMPLETED" ? "ok" : run.status === "NEEDS_HUMAN" ? "bad" : "warn");
      $("grid-sub").textContent = run._id;
    }
  } catch (err) {
    toast(`refresh: ${err.message}`);
  } finally {
    app.busy = false;
  }
}

async function refreshLearning() {
  try {
    const [stats, mem] = await Promise.all([api("/learning/stats"), api("/memories")]);
    $("learn-runs").tBodies[0].replaceChildren(...stats.runs.map((r) => {
      const tr = el("tr");
      [r.run_id, r.diagnosis_calls, r.memory_hits, r.duration_s].forEach((v) => tr.append(el("td", String(v))));
      return tr;
    }));
    $("memories").replaceChildren(...mem.memories.map((m) =>
      el("li", `${m.polarity === "positive" ? "worked" : "did NOT work"} (x${m.success_count + m.failure_count}): ${m.description}`)));
  } catch (err) {
    toast(`learning: ${err.message}`);
  }
}

function renderExplain(ex) {
  $("ex-supposed").textContent = `${ex.supposed.plan_summary}\n${ex.supposed.total_tasks} tasks planned`;
  const a = ex.actual;
  const lines = [`${a.verified} verified externally`, `${a.unknown} still unconfirmed`, `${a.needs_human} need a human`];
  for (const e of a.evidence) lines.push(`${e.task_id}: ${JSON.stringify(e.observed)}`);
  $("ex-actual").textContent = lines.join("\n");
  $("ex-next").textContent = ex.next.length
    ? ex.next.map((n) => `${n.action}: ${n.task_id}\n  ${n.reason}`).join("\n")
    : "Nothing needs a human. It is safe to let the agent continue.";
}

// --- wiring ---

function wire() {
  buildLegend();
  $("chaos-buttons").replaceChildren(...CHAOS.map(([label, name, body]) => {
    const b = el("button", label);
    b.addEventListener("click", () => act(label, () => api(`/chaos/${name}`, { method: "POST", body })));
    return b;
  }));
  $("btn-run").addEventListener("click", () => act("start run", async () => {
    const { run_id } = await api("/runs", { method: "POST", body: {} });
    app.runId = run_id;
    openStream(run_id);
    await startAgent();
  }));
  $("btn-start").addEventListener("click", () => act("start agent", startAgent));
  $("btn-kill").addEventListener("click", () => act("kill", () => api("/agent/kill", { method: "POST" })));
  $("btn-reset").addEventListener("click", () => act("reset", async () => {
    const n = Math.min(200, Math.max(1, parseInt($("opt-n").value, 10) || 20));
    $("opt-n").value = n;
    if (!confirm(`Reset the mock world to ${n} attendees and clear runs? Learned memories are kept.`)) return;
    await api(`/reset?attendees=${n}`, { method: "POST" });
    app.runId = null;
    $("grid").replaceChildren();
    $("feed").replaceChildren();
    clearHeal();
    $("run-pill").textContent = "no run";
  }));
  $("btn-wipe").addEventListener("click", () => act("wipe", async () => {
    if (!confirm("Wipe the agent's records for this run? The systems keep their real state.")) return;
    await api(`/runs/${app.runId}/wipe_state`, { method: "POST" });
  }));
  $("btn-reconcile").addEventListener("click", () => act("reconcile", () => api(`/runs/${app.runId}/reconcile`, { method: "POST" })));
  $("btn-explain").addEventListener("click", () => act("explain", async () => renderExplain(await api(`/runs/${app.runId}/explain`))));
}

function startAgent() {
  return api("/agent/start", {
    method: "POST",
    body: { crash_at: $("opt-crash").value.trim() || null, demo_wrong_first: $("opt-wrong").checked },
  });
}

wire();
refresh();
refreshLearning();
setInterval(refresh, 1000);
// Keep the Explain panel current without a click (quietly: no error toasts).
setInterval(() => { if (app.runId) api(`/runs/${app.runId}/explain`).then(renderExplain).catch(() => {}); }, 3000);
setInterval(refreshLearning, 4000);
