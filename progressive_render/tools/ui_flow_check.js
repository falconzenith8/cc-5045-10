/* Headless harness: drives the real web/app.js against a live server.
 *
 * It stubs just enough DOM + fetch to let app.js run, then asserts the golden
 * path the task requires: switching preview priority and submitting makes a
 * preview frame appear, the final pane produces a render, and a failure shows
 * up in the preview/final error panes. No browser is available in CI, so this
 * exercises the same JS logic the page loads. It is an accompaniment to the
 * GUI entry point, not a replacement for it.
 *
 * Usage:
 *   python3 run_server.py --port 8033 --workers 4 &
 *   node tools/ui_flow_check.js            # BASE defaults to :8033
 *   BASE=http://127.0.0.1:8000 node tools/ui_flow_check.js
 */
const fs = require("fs");
const path = require("path");

const BASE = process.env.BASE || "http://127.0.0.1:8033";
const APP = path.join(__dirname, "..", "web", "app.js");

// ---- minimal DOM ----
class El {
  constructor(id) {
    this.id = id; this.textContent = ""; this.className = "";
    this.style = {}; this.value = ""; this.checked = true;
    this._src = ""; this._listeners = {}; this.innerHTML = "";
    this.dataset = {}; this.classList = { toggle: () => {}, add: () => {}, remove: () => {} };
  }
  set src(v) { this._src = v; } get src() { return this._src; }
  addEventListener(ev, fn) { (this._listeners[ev] ||= []).push(fn); }
  appendChild() {}
}
const els = {};
const ids = [
  "toast","scene","reviewer","pw","ph","azimuth","elevation","distance","fov",
  "albedo","priority","coalesce","btnPreview","btnFinal","btnFail","btnSim",
  "azVal","elVal","distVal","fovVal","previewBox","finalBox","pvSpp","previewImg",
  "pvHash","pvState","pvQueue","pvFirst","viewHash","pvProg","pvStale","pvErr",
  "fnSpp","finalImg","fnHash","fnState","fnQueue","fnLatency","fnEff","fnResume",
  "fnProg","fnErr","poolInfo","policyPill","queueDepth","queueLimit","inFlight",
  "kSubmitted","kDone","kSuper","kReject","kChunks","kChunkFail","kFailed",
  "kPvLat","kFnLat","jobsBody","timelineBody","polName","polChannel","polStatus",
  "polWorkers","polLane","slaHint","ageHint","cfgView","simCard","simOut",
];
for (const id of ids) els[id] = new El(id);
els.scene.value = "simple_sphere";
els.reviewer.value = "reviewer-alice";
els.pw.value = "120"; els.ph.value = "90";
els.azimuth.value = "40"; els.elevation.value = "18";
els.distance.value = "6.5"; els.fov.value = "45";
els.albedo.value = "0.8,0.25,0.2";
els.priority.value = "5";

global.document = {
  getElementById: (id) => els[id] || (els[id] = new El(id)),
  querySelectorAll: () => [],
};
global.window = {};
global.setTimeout = setTimeout;
global.clearTimeout = clearTimeout;
global.setInterval = () => 0;              // no infinite polling in the test
global.clearInterval = () => {};

// ---- fetch wrapper: real network, but record frames by job kind ----
const frames = { preview: 0, final: 0, byJob: {} };
let knownKinds = {};                       // job_id -> "preview"/"final"
const realFetch = global.fetch;
global.fetch = async (url, opts) => {
  const res = await realFetch(BASE + url, opts);
  const m = /\/api\/frame\/(\w+)/.exec(url);
  if (m) {
    const jid = m[1];
    const st = res.headers.get("X-Job-State");
    const spp = Number(res.headers.get("X-Effective-Spp") || 0);
    const kind = knownKinds[jid] || "preview";
    frames.byJob[jid] = (frames.byJob[jid] || 0) + 1;
    if (st && spp > 0) frames[kind]++;
  }
  return res;
};
global.Blob = class { constructor(parts){ this.size = parts?parts.length:0; } };
const RealURL = global.URL;
global.URL = Object.assign(function(u, b){ return new RealURL(u, b); }, RealURL);
global.URL.createObjectURL = () => "blob:x";
global.URL.revokeObjectURL = () => {};

const src = fs.readFileSync(APP, "utf8") +
  "\nglobalThis.__app = { state, submitPreview, submitFinal, pollFrame, " +
  "refreshKernel, postJSON, currentForm };\n";
(0, eval)(src);   // app.js top-level consts are lexical; export them explicitly
const { state, submitPreview, submitFinal, pollFrame, refreshKernel } =
  globalThis.__app;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  // Initial paint submits a preview.
  await sleep(1500);
  let snap = await (await realFetch(BASE + "/api/kernel")).json();
  const firstPreview = snap.jobs.find((j) => j.kind === "preview");
  console.log("initial preview submitted:", !!firstPreview);

  // Golden path: switch priority to urgent and submit.
  els.priority.value = "8";
  await submitPreview({ silent: false });
  knownKinds[state.previewJob] = "preview";
  const urgentJobId = state.previewJob;
  await sleep(1500);

  // Submit final.
  await submitFinal(false);
  knownKinds[state.finalJob] = "final";
  await sleep(1500);

  // Drive frame polling the way the page's interval would, until the final
  // job reaches done (or times out).
  let finalDone = false;
  for (let i = 0; i < 120; i++) {
    await pollFrame(state.previewJob, "preview");
    await pollFrame(state.finalJob, "final");
    await sleep(400);
    const s = await (await realFetch(BASE + "/api/kernel")).json();
    const f = s.jobs.find((j) => j.job_id === state.finalJob);
    if (f && f.state === "done") { finalDone = true; break; }
  }

  snap = await (await realFetch(BASE + "/api/kernel")).json();
  const prev = snap.jobs.find((j) => j.job_id === urgentJobId);
  const fin = snap.jobs.find((j) => j.job_id === state.finalJob);

  // Failure path on a fresh reviewer via the UI's failure button.
  els.reviewer.value = "reviewer-carol";
  await submitFinal(true);
  await sleep(1500);
  await refreshKernel();
  await pollFrame(state.finalJob, "final");
  await sleep(800);
  await refreshKernel();

  const results = {
    preview_job_ok: !!prev,
    preview_effective_spp: prev && prev.effective_spp,
    preview_first_frame_ms: prev && prev.first_frame_ms,
    preview_img_set: !!els.previewImg.src,
    preview_queue_shown: els.pvQueue.textContent,
    preview_priority: prev && prev.priority,
    final_job_ok: !!fin,
    final_state: fin && fin.state,
    final_done: finalDone,
    final_spp: fin && fin.effective_spp,
    final_target: fin && fin.target_spp,
    final_img_set: !!els.finalImg.src,
    frames_preview: frames.preview,
    frames_final: frames.final,
    policy_pill: els.policyPill.textContent,
    lane_text: els.polLane.textContent,
    sla_hint: els.slaHint.textContent,
    timeline_rows: (els.timelineBody.innerHTML.match(/<tr>/g) || []).length,
    failure_pane: els.fnErr.style.display,
    failure_text: els.fnErr.textContent,
  };
  console.log(JSON.stringify(results, null, 2));

  const problems = [];
  if (!results.preview_job_ok) problems.push("no preview job");
  if (!results.preview_img_set) problems.push("preview image never painted");
  if (results.preview_priority !== 8) problems.push("priority switch not applied");
  if (!results.final_job_ok) problems.push("no final job");
  if (!results.final_done) problems.push("final never reached done");
  if (results.final_spp < results.final_target)
    problems.push("final spp below target");
  if (!results.final_img_set) problems.push("final image never painted");
  if (results.frames_preview < 1) problems.push("no preview frames served");
  if (results.frames_final < 1) problems.push("no final frames served");
  if (results.failure_pane !== "block") problems.push("failure not shown in pane");
  if (results.timeline_rows < 1) problems.push("timeline not rendered");
  console.log(problems.length ? "HARNESS FAIL: " + problems.join("; ")
                              : "HARNESS OK");
  process.exit(problems.length ? 1 : 0);
})();
